"""
pipeline/build_model.py - segment observations -> travel-time tables (SPEC 7.1-7.5, 7.7).

Purpose: winsorized cell means at three granularities, citywide profile F and timetable
ratio G, the five-level fallback, one global calibration factor, quiet buckets, and
level shares. `--timetable-only` writes the Milestone 1 placeholder model.
Inputs: data/clean/seg_obs/<train dates>.parquet, data/model/network.npz, findings.
Outputs: data/model/seg_time.npy [3,48,G] float32, seg_level.npy [3,48,G] uint8,
model_meta.json.
Run: python -m pipeline.build_model [--timetable-only]
"""

import argparse
import datetime as dt_mod
import json
import time

import duckdb
import numpy as np
import pyarrow as pa

import config
from planner.model import predict_runs
from planner.network import Network


def log(message: str) -> None:
    """Print one progress line immediately.

    Args:
        message: text.
    Returns:
        None.
    """
    print(message, flush=True)


def connect() -> duckdb.DuckDBPyConnection:
    """DuckDB connection with the SPEC 3 settings.

    Returns:
        Connection.
    """
    config.TMP_DIR.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    for statement in config.duckdb_settings():
        con.execute(statement)
    return con


def obs_path(date_iso: str) -> str:
    """Relative POSIX path of a day's seg_obs file.

    Args:
        date_iso: YYYY-MM-DD.
    Returns:
        Path text.
    """
    return (config.SEG_OBS_DIR / f"{date_iso}.parquet").relative_to(config.ROOT).as_posix()


def constants_snapshot() -> dict:
    """Every model constant, stored in model_meta.json for traceability.

    Returns:
        Dict of name -> value.
    """
    names = ["WINSOR_LO", "WINSOR_HI", "N_MIN_L1", "N_MIN_L2", "N_MIN_L3", "N_MIN_L4",
             "PROFILE_MIN_SEGS", "PROFILE_SEG_MIN_N3", "PROFILE_F_MIN", "PROFILE_F_MAX",
             "RATIO_G_MIN", "RATIO_G_MAX", "CALIB_MIN", "CALIB_MAX", "CALIB_MAX_RUNS",
             "CALIB_MIN_SEGS", "SPEED_MIN_KMH", "SPEED_MAX_KMH", "LIVE_HORIZON_S",
             "LIVE_TICK_S", "LIVE_WINDOW_S", "LIVE_DECAY_S", "CITY_PRIOR_K", "LIVE_PRIOR_K"]
    snapshot = {}
    for name in names:
        snapshot[name] = getattr(config, name)
    return snapshot


def write_model(seg_time: np.ndarray, seg_level: np.ndarray, meta: dict) -> None:
    """Write the three model files through .tmp names.

    Args:
        seg_time: [3, 48, G] float32.
        seg_level: [3, 48, G] uint8.
        meta: metadata dict.
    Returns:
        None.
    """
    config.MODEL_DIR.mkdir(parents=True, exist_ok=True)
    # np.save appends .npy, so temporary names end in .tmp.npy.
    tmp_time = config.MODEL_DIR / "seg_time.tmp.npy"
    tmp_level = config.MODEL_DIR / "seg_level.tmp.npy"
    np.save(tmp_time, seg_time.astype(np.float32))
    np.save(tmp_level, seg_level.astype(np.uint8))
    tmp_time.replace(config.SEG_TIME_PATH)
    tmp_level.replace(config.SEG_LEVEL_PATH)
    tmp_meta = config.MODEL_META_PATH.with_suffix(".json.tmp")
    with open(tmp_meta, "w", encoding="utf-8") as handle:
        json.dump(meta, handle, indent=1)
    tmp_meta.replace(config.MODEL_META_PATH)


def clamp_speed(table: np.ndarray, seg_len_m: np.ndarray) -> np.ndarray:
    """Clamp every cell so the implied speed lies in [SPEED_MIN_KMH, SPEED_MAX_KMH].

    Args:
        table: [..., G] seconds.
        seg_len_m: [G] metres.
    Returns:
        Clamped float32 array.
    """
    t_min = seg_len_m / (config.SPEED_MAX_KMH / 3.6)
    t_max = seg_len_m / (config.SPEED_MIN_KMH / 3.6)
    return np.clip(table, t_min, t_max).astype(np.float32)


def level_shares(seg_level: np.ndarray) -> dict:
    """Share of cells at each level over all day types and buckets.

    Args:
        seg_level: [3, 48, G].
    Returns:
        {"L1": share, ...}.
    """
    shares = {}
    total = seg_level.size
    for level in range(1, 6):
        shares[f"L{level}"] = round(float(np.count_nonzero(seg_level == level) / total), 4)
    return shares


def timetable_only(net: Network, findings: dict) -> None:
    """Milestone 1 placeholder: timetable times at level 5 everywhere (SPEC 7.1).

    Args:
        net: network.
        findings: Milestone 0 findings.
    Returns:
        None.
    """
    n_seg = net.n_segments
    seg_time = np.empty((config.N_DAY_TYPES, config.N_BUCKETS, n_seg), dtype=np.float32)
    for b in range(config.N_BUCKETS):
        seg_time[:, b, :] = net.sched_time[b // 2][None, :]
    seg_level = np.full(seg_time.shape, 5, dtype=np.uint8)
    ones = np.ones((config.N_DAY_TYPES, config.N_BUCKETS)).tolist()
    meta = {
        "mode": "timetable_only",
        "profile_F": ones,
        "ratio_G": ones,
        "quiet_bucket": [config.QUIET_BUCKET_DEFAULT] * 3,
        "calibration_k": 1.0,
        "level_share": level_shares(seg_level),
        "train_dates": findings["train_dates"],
        "replay_date": findings["replay_date"],
        "live_horizon_s": config.LIVE_HORIZON_S,
        "built_at": dt_mod.datetime.now().isoformat(timespec="seconds"),
        "constants": constants_snapshot(),
    }
    write_model(seg_time, seg_level, meta)
    log("wrote timetable-only model")


# ---------------------------------------------------------------------------
# Cell statistics (SPEC 7.2)
# ---------------------------------------------------------------------------

def winsor_sql(source: str, keys: str, quantile_fn: str) -> str:
    """SQL for the winsorized mean and count of tt_s grouped by keys.

    Args:
        source: table or subquery name with columns keys and tt_s.
        keys: comma-separated grouping columns.
        quantile_fn: "quantile_cont" or "approx_quantile".
    Returns:
        SQL text.
    """
    # q holds the 10th and 90th percentile per cell; each value is clipped to that range
    # and averaged, which is the winsorized mean of SPEC 7.2.
    join_on = " AND ".join(f"o.{k.strip()} = q.{k.strip()}" for k in keys.split(","))
    select_keys = ", ".join(f"o.{k.strip()}" for k in keys.split(","))
    return (
        f"WITH q AS (SELECT {keys}, {quantile_fn}(tt_s, {config.WINSOR_LO}) AS lo, "
        f"{quantile_fn}(tt_s, {config.WINSOR_HI}) AS hi FROM {source} GROUP BY {keys}) "
        f"SELECT {select_keys}, avg(least(greatest(o.tt_s, q.lo), q.hi)) AS wmean, "
        f"count(*) AS n FROM {source} o JOIN q ON {join_on} GROUP BY {select_keys}"
    )


def cell_stats(con: duckdb.DuckDBPyConnection, n_seg: int, quantile_fn: str) -> dict:
    """Compute c1/n1, c2/n2, c3/n3 and pattern speeds from the obs table.

    Args:
        con: connection with table obs(day_type, bucket, segment_idx, pattern_idx, tt_s, seg_len).
        n_seg: number of segments G.
        quantile_fn: quantile function name.
    Returns:
        Dict of numpy arrays.
    """
    shape = (config.N_DAY_TYPES, config.N_BUCKETS, n_seg)
    c1 = np.full(shape, np.nan, dtype=np.float64)
    n1 = np.zeros(shape, dtype=np.int64)
    rows = con.execute(winsor_sql("obs", "day_type, bucket, segment_idx", quantile_fn)).fetchnumpy()
    c1[rows["day_type"], rows["bucket"], rows["segment_idx"]] = rows["wmean"]
    n1[rows["day_type"], rows["bucket"], rows["segment_idx"]] = rows["n"]
    log(f"c1 cells: {rows['n'].shape[0]:,}")

    # c2: each observation also counts for the neighbouring buckets (circular).
    con.execute(
        "CREATE OR REPLACE TEMP VIEW obs3 AS "
        "SELECT o.day_type, CAST((o.bucket + d.shift + 48) % 48 AS SMALLINT) AS bucket, "
        "o.segment_idx, o.tt_s FROM obs o CROSS JOIN (VALUES (-1), (0), (1)) d(shift)"
    )
    c2 = np.full(shape, np.nan, dtype=np.float64)
    n2 = np.zeros(shape, dtype=np.int64)
    rows = con.execute(winsor_sql("obs3", "day_type, bucket, segment_idx", quantile_fn)).fetchnumpy()
    c2[rows["day_type"], rows["bucket"], rows["segment_idx"]] = rows["wmean"]
    n2[rows["day_type"], rows["bucket"], rows["segment_idx"]] = rows["n"]
    log(f"c2 cells: {rows['n'].shape[0]:,}")

    # c3: all observations of a segment.
    c3 = np.full(n_seg, np.nan, dtype=np.float64)
    n3 = np.zeros(n_seg, dtype=np.int64)
    rows = con.execute(winsor_sql("obs", "segment_idx", quantile_fn)).fetchnumpy()
    c3[rows["segment_idx"]] = rows["wmean"]
    n3[rows["segment_idx"]] = rows["n"]

    # Pattern speed: total length over total time of all its observations.
    rows = con.execute(
        "SELECT pattern_idx, sum(seg_len) / sum(tt_s) AS speed, count(*) AS n "
        "FROM obs GROUP BY pattern_idx"
    ).fetchnumpy()
    return {"c1": c1, "n1": n1, "c2": c2, "n2": n2, "c3": c3, "n3": n3,
            "pat_idx": rows["pattern_idx"], "pat_speed": rows["speed"], "pat_n": rows["n"]}


def fill_profile(values: np.ndarray, qualifies: np.ndarray, lo: float, hi: float) -> np.ndarray:
    """Fill non-qualifying cells from the nearest qualifying bucket, then clamp (SPEC 7.3).

    Args:
        values: [3, 48] raw medians.
        qualifies: [3, 48] bool.
        lo, hi: clamp range.
    Returns:
        [3, 48] filled array.
    """
    out = np.ones_like(values)
    for dt in range(config.N_DAY_TYPES):
        good = np.flatnonzero(qualifies[dt])
        if good.shape[0] == 0:
            continue
        for b in range(config.N_BUCKETS):
            if qualifies[dt, b]:
                out[dt, b] = values[dt, b]
                continue
            # Circular distance to each qualifying bucket; ties go to the earlier bucket.
            best_b = -1
            best_gap = 999
            for cand in good:
                diff = abs(int(cand) - b)
                gap = min(diff, config.N_BUCKETS - diff)
                earlier = (b - int(cand)) % config.N_BUCKETS <= config.N_BUCKETS // 2
                if gap < best_gap or (gap == best_gap and earlier):
                    best_gap = gap
                    best_b = int(cand)
            out[dt, b] = values[dt, best_b]
    # A day type with no qualifying bucket copies the weekday row.
    for dt in range(1, config.N_DAY_TYPES):
        if not qualifies[dt].any():
            out[dt] = out[0]
    return np.clip(out, lo, hi)


def profiles(stats: dict, sched_time: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Citywide profile F and timetable ratio G (SPEC 7.3).

    Args:
        stats: from cell_stats.
        sched_time: [24, G].
    Returns:
        Tuple (F, G), each [3, 48].
    """
    c1 = stats["c1"]
    n1 = stats["n1"]
    c3 = stats["c3"]
    n3 = stats["n3"]
    raw_f = np.ones((config.N_DAY_TYPES, config.N_BUCKETS))
    raw_g = np.ones((config.N_DAY_TYPES, config.N_BUCKETS))
    qualifies = np.zeros((config.N_DAY_TYPES, config.N_BUCKETS), dtype=bool)
    seg_ok = n3 >= config.PROFILE_SEG_MIN_N3
    for dt in range(config.N_DAY_TYPES):
        for b in range(config.N_BUCKETS):
            mask = (n1[dt, b] >= config.N_MIN_L1) & seg_ok
            if np.count_nonzero(mask) < config.PROFILE_MIN_SEGS:
                continue
            qualifies[dt, b] = True
            raw_f[dt, b] = np.median(c1[dt, b, mask] / c3[mask])
            raw_g[dt, b] = np.median(c1[dt, b, mask] / sched_time[b // 2, mask])
    f = fill_profile(raw_f, qualifies, config.PROFILE_F_MIN, config.PROFILE_F_MAX)
    g = fill_profile(raw_g, qualifies, config.RATIO_G_MIN, config.RATIO_G_MAX)
    return f, g


def apply_fallback(stats: dict, f: np.ndarray, g_ratio: np.ndarray, net: Network) -> tuple:
    """Five-level fallback per cell, then the speed clamp (SPEC 7.4).

    Args:
        stats: from cell_stats.
        f: [3, 48] profile.
        g_ratio: [3, 48] timetable ratio.
        net: network.
    Returns:
        Tuple (seg_time float64 [3,48,G], seg_level uint8 [3,48,G]).
    """
    n_seg = net.n_segments
    shape = (config.N_DAY_TYPES, config.N_BUCKETS, n_seg)
    seg_time = np.zeros(shape, dtype=np.float64)
    seg_level = np.zeros(shape, dtype=np.uint8)
    done = np.zeros(shape, dtype=bool)

    # L1 and L2 straight from the cell statistics.
    l1 = stats["n1"] >= config.N_MIN_L1
    seg_time[l1] = stats["c1"][l1]
    seg_level[l1] = 1
    done |= l1
    l2 = (~done) & (stats["n2"] >= config.N_MIN_L2)
    seg_time[l2] = stats["c2"][l2]
    seg_level[l2] = 2
    done |= l2

    # L3: the segment's overall mean shaped by the citywide profile.
    seg_l3 = stats["n3"] >= config.N_MIN_L3
    l3 = (~done) & seg_l3[None, None, :]
    l3_values = stats["c3"][None, None, :] * f[:, :, None]
    seg_time[l3] = np.broadcast_to(l3_values, shape)[l3]
    seg_level[l3] = 3
    done |= l3

    # L4: the pattern with the most observations among patterns containing the segment.
    pat_n = np.zeros(net.n_patterns, dtype=np.int64)
    pat_speed = np.zeros(net.n_patterns, dtype=np.float64)
    pat_n[stats["pat_idx"]] = stats["pat_n"]
    pat_speed[stats["pat_idx"]] = stats["pat_speed"]
    seg_best_n = np.zeros(n_seg, dtype=np.int64)
    seg_best_speed = np.zeros(n_seg, dtype=np.float64)
    for p in range(net.n_patterns):
        if pat_n[p] < config.N_MIN_L4:
            continue
        segs = net.pat_seg[net.pat_ptr[p]:net.pat_ptr[p + 1] - 1]
        better = pat_n[p] > seg_best_n[segs]
        seg_best_n[segs[better]] = pat_n[p]
        seg_best_speed[segs[better]] = pat_speed[p]
    seg_l4 = seg_best_n >= config.N_MIN_L4
    l4 = (~done) & seg_l4[None, None, :]
    safe_speed = np.where(seg_best_speed > 0, seg_best_speed, 1.0)
    l4_values = (net.seg_len_m / safe_speed)[None, None, :] * f[:, :, None]
    seg_time[l4] = np.broadcast_to(l4_values, shape)[l4]
    seg_level[l4] = 4
    done |= l4

    # L5: timetable times scaled by the citywide timetable ratio.
    l5 = ~done
    hours = np.arange(config.N_BUCKETS) // 2
    l5_values = net.sched_time[hours][None, :, :] * g_ratio[:, :, None]
    seg_time[l5] = np.broadcast_to(l5_values, shape)[l5]
    seg_level[l5] = 5
    return clamp_speed(seg_time, net.seg_len_m), seg_level


# ---------------------------------------------------------------------------
# Run spans (shared with calibration, validation, and build_live)
# ---------------------------------------------------------------------------

def load_spans(con: duckdb.DuckDBPyConnection, dates: list[str], min_segs: int) -> dict:
    """Maximal stretches of consecutive valid observations within runs (SPEC 11.1).

    Args:
        con: connection.
        dates: dates whose seg_obs files are read, one at a time.
        min_segs: minimum consecutive valid segments per span.
    Returns:
        Dict of arrays: seg_flat, tt_flat and sod_flat (per segment), ptr, t0 (sod of
        first t_enter), day_type, actual (s),
        pattern, pos_a, pos_b, date_index, t_enter (unix of first segment).
    """
    parts = {"seg": [], "len": [], "t0": [], "dt": [], "actual": [], "pattern": [],
             "pos_a": [], "date_index": [], "t_enter": [], "tt": [], "sod": []}
    for date_index, date_iso in enumerate(dates):
        path = config.SEG_OBS_DIR / f"{date_iso}.parquet"
        if not path.exists():
            continue
        # One day of observations in run order.
        table = con.execute(
            "SELECT run_id, seg_pos, segment_idx, pattern_idx, sod, day_type, tt_s, valid, t_enter "
            f"FROM read_parquet('{obs_path(date_iso)}') ORDER BY run_id, seg_pos"
        ).fetchnumpy()
        run = table["run_id"].astype(np.int64)
        pos = table["seg_pos"].astype(np.int64)
        valid = table["valid"].astype(bool)
        n = run.shape[0]
        if n == 0:
            continue
        # A new stretch starts at a new run, a gap in positions, or after an invalid row.
        start = np.ones(n, dtype=bool)
        start[1:] = (run[1:] != run[:-1]) | (pos[1:] != pos[:-1] + 1) | (~valid[:-1])
        group = np.cumsum(start) - 1
        n_group = int(group[-1]) + 1
        length = np.bincount(group, minlength=n_group)
        all_valid = np.bincount(group, weights=valid.astype(np.float64), minlength=n_group)
        keep = (all_valid == length) & (length >= min_segs)
        row_keep = keep[group]
        first_rows = np.flatnonzero(start)[keep]
        actual = np.bincount(group, weights=table["tt_s"].astype(np.float64), minlength=n_group)[keep]
        parts["seg"].append(table["segment_idx"][row_keep].astype(np.int32))
        parts["tt"].append(table["tt_s"][row_keep].astype(np.float64))
        parts["sod"].append(table["sod"][row_keep].astype(np.float64))
        parts["len"].append(length[keep].astype(np.int64))
        parts["t0"].append(table["sod"][first_rows].astype(np.float64))
        parts["dt"].append(table["day_type"][first_rows].astype(np.int64))
        parts["actual"].append(actual)
        parts["pattern"].append(table["pattern_idx"][first_rows].astype(np.int64))
        parts["pos_a"].append(pos[first_rows])
        parts["date_index"].append(np.full(first_rows.shape[0], date_index, dtype=np.int64))
        parts["t_enter"].append(table["t_enter"][first_rows].astype(np.int64))
    if len(parts["len"]) == 0:
        empty = np.zeros(0, dtype=np.int64)
        return {"seg_flat": np.zeros(0, dtype=np.int32), "ptr": np.zeros(1, dtype=np.int64),
                "tt_flat": np.zeros(0), "sod_flat": np.zeros(0),
                "t0": np.zeros(0), "day_type": empty, "actual": np.zeros(0), "pattern": empty,
                "pos_a": empty, "pos_b": empty, "date_index": empty, "t_enter": empty}
    lengths = np.concatenate(parts["len"])
    ptr = np.zeros(lengths.shape[0] + 1, dtype=np.int64)
    ptr[1:] = np.cumsum(lengths)
    pos_a = np.concatenate(parts["pos_a"])
    return {
        "seg_flat": np.concatenate(parts["seg"]),
        "tt_flat": np.concatenate(parts["tt"]),
        "sod_flat": np.concatenate(parts["sod"]),
        "ptr": ptr,
        "t0": np.concatenate(parts["t0"]),
        "day_type": np.concatenate(parts["dt"]),
        "actual": np.concatenate(parts["actual"]),
        "pattern": np.concatenate(parts["pattern"]),
        "pos_a": pos_a,
        "pos_b": pos_a + lengths,
        "date_index": np.concatenate(parts["date_index"]),
        "t_enter": np.concatenate(parts["t_enter"]),
    }


def subset_spans(spans: dict, index: np.ndarray) -> dict:
    """Select some spans, rebuilding the CSR arrays.

    Args:
        spans: from load_spans.
        index: span indices to keep.
    Returns:
        Spans dict of the same form.
    """
    lengths = spans["ptr"][index + 1] - spans["ptr"][index]
    ptr = np.zeros(index.shape[0] + 1, dtype=np.int64)
    ptr[1:] = np.cumsum(lengths)
    # Row positions of the kept spans inside the flat arrays, built without a Python loop.
    starts = spans["ptr"][index]
    span_of_row = np.repeat(np.arange(index.shape[0]), lengths)
    rows = starts[span_of_row] + (np.arange(span_of_row.shape[0]) - ptr[span_of_row])
    out = {"seg_flat": spans["seg_flat"][rows], "tt_flat": spans["tt_flat"][rows],
           "sod_flat": spans["sod_flat"][rows], "ptr": ptr}
    for key in ["t0", "day_type", "actual", "pattern", "pos_a", "pos_b", "date_index", "t_enter"]:
        out[key] = spans[key][index]
    return out


def predict_typical(seg_time: np.ndarray, spans: dict, horizon_s: float) -> np.ndarray:
    """Model prediction for spans in typical mode (r = 1).

    Args:
        seg_time: [3, 48, G].
        spans: spans dict.
        horizon_s: LIVE_HORIZON_S (unused when r = 1, but the walker takes it).
    Returns:
        Seconds per span.
    """
    dummy_live = np.ones((1, seg_time.shape[2]), dtype=np.float32)
    ticks = np.full(spans["t0"].shape[0], -1, dtype=np.int64)
    return predict_runs(seg_time.astype(np.float32), spans["seg_flat"], spans["ptr"],
                        spans["t0"], spans["day_type"], dummy_live, ticks, horizon_s)


# ---------------------------------------------------------------------------
# Full build
# ---------------------------------------------------------------------------

def full_build(net: Network, findings: dict) -> None:
    """Build the model from training observations (SPEC 7.2-7.5, 7.7).

    Args:
        net: network.
        findings: Milestone 0 findings.
    Returns:
        None.
    """
    started = time.time()
    con = connect()
    train = [d for d in findings["train_dates"] if (config.SEG_OBS_DIR / f"{d}.parquet").exists()]
    missing = [d for d in findings["train_dates"] if d not in train]
    if len(missing) > 0:
        raise FileNotFoundError(f"seg_obs missing for train dates {missing}")
    files = ", ".join(f"'{obs_path(d)}'" for d in train)

    # Valid training observations with their segment length (for pattern speed).
    seg_len = net.seg_len_m.astype(np.float64)
    con.register("seg_len_tab", pa.table({"segment_idx": np.arange(net.n_segments, dtype=np.int32),
                                          "seg_len": seg_len}))
    con.execute(
        "CREATE TABLE obs AS SELECT o.day_type, o.bucket, o.segment_idx, o.pattern_idx, o.tt_s, "
        f"l.seg_len FROM read_parquet([{files}]) o JOIN seg_len_tab l USING (segment_idx) "
        "WHERE o.valid"
    )
    n_obs = con.execute("SELECT count(*) FROM obs").fetchone()[0]
    log(f"training observations: {n_obs:,} from {len(train)} days")

    # 7.2: cell statistics; DuckDB out-of-memory falls back to approx_quantile (SPEC 7.2).
    quantile_fn = "quantile_cont"
    try:
        stats = cell_stats(con, net.n_segments, quantile_fn)
    except duckdb.OutOfMemoryException:
        quantile_fn = "approx_quantile"
        log("RULE 7.2 fired: quantile_cont out of memory -> approx_quantile")
        stats = cell_stats(con, net.n_segments, quantile_fn)
    con.execute("DROP TABLE obs")

    # 7.3 and 7.4.
    f, g_ratio = profiles(stats, net.sched_time)
    seg_time, seg_level = apply_fallback(stats, f, g_ratio, net)

    # 7.5: calibration on sampled training spans with the uncalibrated table.
    spans = load_spans(con, train, config.CALIB_MIN_SEGS)
    rng = np.random.default_rng(config.M0_SEED)
    n_spans = spans["t0"].shape[0]
    if n_spans > config.CALIB_MAX_RUNS:
        index = np.sort(rng.choice(n_spans, size=config.CALIB_MAX_RUNS, replace=False))
        sample = subset_spans(spans, index)
    else:
        sample = spans
    predicted = predict_typical(seg_time, sample, float(config.LIVE_HORIZON_S))
    raw_k = float(np.median(sample["actual"] / predicted))
    k = min(max(raw_k, config.CALIB_MIN), config.CALIB_MAX)
    log(f"calibration: {sample['t0'].shape[0]:,} spans, raw k {raw_k:.4f}, k {k:.4f}")
    # Deviation from SPEC 7.5 (recorded in the README): the speed clamp of 7.4 is applied
    # again after calibration, because k > 1 pushes the slowest cells below 3 km/h and the
    # Milestone 3 check requires every cell within 3..60 km/h.
    seg_time = clamp_speed(seg_time * k, net.seg_len_m)

    # Reliability ("usually there by"): how much slower than the estimate a trip can
    # run. Ratio actual / calibrated prediction on the training spans; the 90th
    # percentile per (day type, hour) gives "9 in 10 trips arrive by" times. Hours with
    # few spans use the day type's overall percentile.
    calibrated = predict_typical(seg_time, sample, float(config.LIVE_HORIZON_S))
    ratio = sample["actual"] / calibrated
    hour_of = (sample["t0"] // 3600).astype(np.int64) % 24
    rel_p50 = np.ones((config.N_DAY_TYPES, config.N_HOURS))
    rel_p90 = np.ones((config.N_DAY_TYPES, config.N_HOURS))
    for dt in range(config.N_DAY_TYPES):
        in_type = sample["day_type"] == dt
        if np.count_nonzero(in_type) < config.RELIABILITY_MIN_SPANS:
            in_type = np.ones(ratio.shape[0], dtype=bool)
        overall = np.quantile(ratio[in_type], [0.5, 0.9])
        for hour in range(config.N_HOURS):
            mask = in_type & (hour_of == hour)
            values = overall
            if np.count_nonzero(mask) >= config.RELIABILITY_MIN_SPANS:
                values = np.quantile(ratio[mask], [0.5, 0.9])
            rel_p50[dt, hour] = values[0]
            rel_p90[dt, hour] = max(values[1], 1.0)

    # 7.7: quiet bucket per day type among buckets with enough L1/L2 cells.
    quiet = []
    good_share = np.mean(seg_level <= 2, axis=2)
    for dt in range(config.N_DAY_TYPES):
        best_b = config.QUIET_BUCKET_DEFAULT
        best_f = None
        for b in range(config.QUIET_BUCKET_MIN, config.QUIET_BUCKET_MAX + 1):
            if good_share[dt, b] < config.QUIET_MIN_GOOD_SHARE:
                continue
            if best_f is None or f[dt, b] < best_f:
                best_f = f[dt, b]
                best_b = b
        quiet.append(int(best_b))

    meta = {
        "mode": "full",
        "profile_F": np.round(f, 4).tolist(),
        "ratio_G": np.round(g_ratio, 4).tolist(),
        "quiet_bucket": quiet,
        "calibration_k": round(k, 4),
        "calibration_k_raw": round(raw_k, 4),
        "calibration_spans": int(sample["t0"].shape[0]),
        "level_share": level_shares(seg_level),
        "reliability_p50": np.round(rel_p50, 4).tolist(),
        "reliability_p90": np.round(rel_p90, 4).tolist(),
        "l1l2_share_by_bucket": np.round(good_share, 4).tolist(),
        "counts": {"observations": int(n_obs), "segments": int(net.n_segments),
                   "segments_with_obs": int(np.count_nonzero(stats["n3"])),
                   "train_spans": int(n_spans)},
        "quantile_fn": quantile_fn,
        "train_dates": train,
        "replay_date": findings["replay_date"],
        "live_horizon_s": config.LIVE_HORIZON_S,
        "built_at": dt_mod.datetime.now().isoformat(timespec="seconds"),
        "build_seconds": round(time.time() - started, 1),
        "constants": constants_snapshot(),
    }
    write_model(seg_time, seg_level, meta)
    log(f"model built in {time.time() - started:.0f} s; level share {meta['level_share']}; "
        f"quiet buckets {quiet}")


def main() -> None:
    """Command-line entry point.

    Returns:
        None.
    """
    parser = argparse.ArgumentParser(description="Build the travel-time model (SPEC 7).")
    parser.add_argument("--timetable-only", action="store_true",
                        help="write the Milestone 1 timetable placeholder model")
    args = parser.parse_args()
    findings = config.load_findings()
    net = Network()
    if args.timetable_only:
        timetable_only(net, findings)
    else:
        full_build(net, findings)


if __name__ == "__main__":
    main()
