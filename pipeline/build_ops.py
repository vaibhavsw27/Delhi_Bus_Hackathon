"""
pipeline/build_ops.py - operator (government) insights from the model and training data.

Data for the operator dashboard (/ops). Everything here uses training days only (1-24 Oct) and the
built model, so nothing is tuned on the test week.
Outputs (data/model/ops.json):
  - city_speed: network-average bus speed per half-hour for each day type.
  - audit: routes whose timetable is furthest from observed reality, with the observed
    run time per hour (a ready-made corrected timetable).
  - chokepoints: per weekday hour, the stretches that cost buses the most minutes.
  - reliability: routes whose trip times are least predictable.
  - bunching: routes whose buses most often arrive in clumps or leave long gaps.
Inputs: data/model/*, data/clean/seg_obs/<train dates>.parquet, findings.
Run: python -m pipeline.build_ops
"""

import argparse
import datetime as dt_mod
import json
import time

import duckdb
import numpy as np
import pyarrow as pa

import config
from pipeline.build_model import connect, load_spans, obs_path
from planner.model import predict_runs, walk_segments, walk_timetable
from planner.network import Network

OPS_PATH = config.MODEL_DIR / "ops.json"
AUDIT_HOURS = list(range(5, 23))
PEAK_HOURS = [8, 9, 10, 17, 18, 19]
TOP_N = 40
CHOKE_MIN_LEN_M = 150
CHOKE_TOP_N = 25
ROUTE_MIN_SPANS = 60
BUNCH_SHARE = 0.25
GAP_FACTOR = 2.0
BUNCH_HOURS = (7, 21)
BUNCH_MIN_GAPS = 80
AUDIT_MIN_GOOD_SHARE = 0.5


def log(message: str) -> None:
    """Print one progress line immediately.

    Args:
        message: text.
    Returns:
        None.
    """
    print(message, flush=True)


def segment_coords(net: Network) -> list:
    """Polyline [[lat, lon], ...] of every segment, from the first pattern that uses it.

    Args:
        net: network.
    Returns:
        List indexed by segment.
    """
    coords = [None] * net.n_segments
    for p in range(net.n_patterns):
        base = int(net.pat_ptr[p])
        for pos in range(int(net.pat_ptr[p + 1]) - base - 1):
            g = int(net.pat_seg[base + pos])
            if coords[g] is not None:
                continue
            v0 = int(net.poly_ptr[p]) + int(net.pat_stop_vtx[base + pos])
            v1 = int(net.poly_ptr[p]) + int(net.pat_stop_vtx[base + pos + 1])
            points = []
            for v in range(v0, v1 + 1):
                points.append([round(float(net.poly_lat[v]), 5), round(float(net.poly_lon[v]), 5)])
            coords[g] = points
    return coords


def build(net: Network, findings: dict) -> dict:
    """Compute every operator view.

    Args:
        net: network.
        findings: Milestone 0 findings.
    Returns:
        The ops dict.
    """
    started = time.time()
    seg_time = np.load(config.SEG_TIME_PATH).astype(np.float32)
    seg_level = np.load(config.SEG_LEVEL_PATH)
    with open(config.MODEL_META_PATH, "r", encoding="utf-8") as handle:
        meta = json.load(handle)
    quiet = meta["quiet_bucket"]
    seg_len = net.seg_len_m.astype(np.float64)
    good = seg_level <= 2

    # City speed per half-hour: total length / total time over segments learned from GPS
    # (L1 or L2) in that cell.
    city_speed = []
    for dt in range(config.N_DAY_TYPES):
        row = []
        for b in range(config.N_BUCKETS):
            mask = good[dt, b]
            if np.count_nonzero(mask) == 0:
                row.append(None)
                continue
            row.append(round(float(seg_len[mask].sum() / seg_time[dt, b, mask].sum() * 3.6), 2))
        city_speed.append(row)

    # Timetable audit: whole-pattern run time per hour, model against timetable, on
    # weekdays. Only patterns mostly learned from GPS are audited.
    scratch = np.empty(4096, dtype=np.float64)
    ones = np.ones(net.n_segments, dtype=np.float32)
    audit = []
    for p in range(net.n_patterns):
        segs = net.pat_seg[net.pat_ptr[p]:net.pat_ptr[p + 1] - 1]
        if segs.shape[0] < 5:
            continue
        model_min = []
        sched_min = []
        good_share = []
        for hour in AUDIT_HOURS:
            start = hour * 3600.0
            model_min.append(round(walk_segments(seg_time[0], segs, start, ones, start, 1800.0, scratch) / 60.0, 1))
            sched_min.append(round(walk_timetable(net.sched_time, segs, start, scratch) / 60.0, 1))
            good_share.append(float(np.mean(good[0, hour * 2, segs])))
        if float(np.mean(good_share)) < AUDIT_MIN_GOOD_SHARE:
            continue
        runs_any = bool(np.any(np.isfinite(net.headway_s[p, AUDIT_HOURS[0]:AUDIT_HOURS[-1] + 1])))
        if not runs_any:
            continue
        peak_index = [AUDIT_HOURS.index(h) for h in PEAK_HOURS]
        peak_gap = float(np.mean([model_min[i] - sched_min[i] for i in peak_index]))
        peak_sched = float(np.mean([sched_min[i] for i in peak_index]))
        stops = net.pattern_stops(p)
        audit.append({
            "pattern_idx": p,
            "route": str(net.route_name[net.pat_route[p]]),
            "from_stop": str(net.stop_name[stops[0]]),
            "to_stop": str(net.stop_name[stops[-1]]),
            "trips_per_day": int(net.pat_trips[p]),
            "length_km": round(float(net.pat_stop_dist[net.pat_ptr[p + 1] - 1]) / 1000.0, 1),
            "hours": AUDIT_HOURS,
            "model_min": model_min,
            "timetable_min": sched_min,
            "peak_gap_min": round(peak_gap, 1),
            "peak_gap_pct": round(peak_gap / max(peak_sched, 1e-6) * 100.0, 1),
            "gps_share": round(float(np.mean(good_share)), 2),
        })
    audit.sort(key=audit_key)
    log(f"audit: {len(audit)} patterns scored")

    # Chokepoints per weekday hour: minutes lost vs the quiet hour, times buses per hour.
    buses_per_hour = np.zeros((config.N_HOURS, net.n_segments))
    freq = np.where(np.isfinite(net.headway_s), 3600.0 / net.headway_s, 0.0)
    for p in range(net.n_patterns):
        segs = net.pat_seg[net.pat_ptr[p]:net.pat_ptr[p + 1] - 1]
        buses_per_hour[:, segs] += freq[p][:, None]
    coords = segment_coords(net)
    chokepoints = {}
    quiet_time = seg_time[0, quiet[0]]
    for hour in AUDIT_HOURS:
        b = hour * 2
        delay = seg_time[0, b] - quiet_time
        lost = delay * buses_per_hour[hour] / 60.0
        mask = good[0, b] & (seg_len >= CHOKE_MIN_LEN_M) & (delay > 0)
        candidates = np.flatnonzero(mask)
        order = candidates[np.argsort(-lost[candidates], kind="stable")][:CHOKE_TOP_N]
        rows = []
        for g in order:
            rows.append({
                "segment_idx": int(g),
                "from_stop": str(net.stop_name[net.seg_from[g]]),
                "to_stop": str(net.stop_name[net.seg_to[g]]),
                "speed_kmh": round(float(seg_len[g] / seg_time[0, b, g] * 3.6), 1),
                "quiet_speed_kmh": round(float(seg_len[g] / quiet_time[g] * 3.6), 1),
                "delay_s": int(round(float(delay[g]))),
                "buses_per_hour": round(float(buses_per_hour[hour, g]), 1),
                "bus_minutes_lost_per_hour": round(float(lost[g]), 1),
                "coords": coords[g],
            })
        chokepoints[str(hour)] = rows

    # Route reliability from training spans: spread of actual / estimated time.
    con = connect()
    train = findings["train_dates"]
    spans = load_spans(con, train, config.CALIB_MIN_SEGS)
    dummy = np.ones((1, net.n_segments), dtype=np.float32)
    ticks = np.full(spans["t0"].shape[0], -1, dtype=np.int64)
    pred = predict_runs(seg_time, spans["seg_flat"], spans["ptr"], spans["t0"], spans["day_type"],
                        dummy, ticks, 1800.0)
    ratio = spans["actual"] / pred
    reliability = []
    order = np.argsort(spans["pattern"], kind="stable")
    pats = spans["pattern"][order]
    starts = np.flatnonzero(np.concatenate([[True], pats[1:] != pats[:-1]]))
    ends = np.concatenate([starts[1:], [pats.shape[0]]])
    for start, end in zip(starts, ends):
        if end - start < ROUTE_MIN_SPANS:
            continue
        values = ratio[order[start:end]]
        p = int(pats[start])
        q50, q90 = np.quantile(values, [0.5, 0.9])
        mean_trip_min = float(np.mean(spans["actual"][order[start:end]])) / 60.0
        reliability.append({
            "pattern_idx": p, "route": str(net.route_name[net.pat_route[p]]),
            "headsign": str(net.pat_headsign[p]), "trips": int(end - start),
            "spread_pct": round(float((q90 - q50) / q50 * 100.0), 1),
            "late_min_on_bad_day": round(float((q90 - q50) / q50) * mean_trip_min, 1),
            "mean_trip_min": round(mean_trip_min, 1),
        })
    reliability.sort(key=spread_key)
    log(f"reliability: {len(reliability)} patterns")

    # Bunching at each pattern's middle stop on weekdays between 07:00 and 21:00.
    files = ", ".join(f"'{obs_path(d)}'" for d in train if config.day_type_of(d) == 0)
    n_stops = np.diff(net.pat_ptr)
    con.register("mid_tab", pa.table({"pattern_idx": np.arange(net.n_patterns, dtype=np.int32),
                                      "mid": (n_stops // 2).astype(np.int16)}))
    rows = con.execute(
        "WITH c AS (SELECT o.pattern_idx, CAST(to_timestamp(o.t_enter + 19800) AS DATE) AS d, "
        "o.t_enter, o.sod FROM read_parquet([" + files + "]) o JOIN mid_tab m "
        "ON o.pattern_idx = m.pattern_idx AND o.seg_pos = m.mid WHERE o.valid "
        f"AND o.sod BETWEEN {BUNCH_HOURS[0] * 3600} AND {BUNCH_HOURS[1] * 3600}), "
        "g AS (SELECT pattern_idx, sod, t_enter - LAG(t_enter) OVER "
        "(PARTITION BY pattern_idx, d ORDER BY t_enter) AS gap FROM c) "
        "SELECT pattern_idx, CAST(sod // 3600 AS INTEGER) AS hour, gap FROM g WHERE gap IS NOT NULL"
    ).fetchnumpy()
    pat_g = rows["pattern_idx"].astype(np.int64)
    hour_g = rows["hour"].astype(np.int64)
    gap = rows["gap"].astype(np.float64)
    sched = net.headway_s[pat_g, hour_g].astype(np.float64)
    ok = np.isfinite(sched)
    bunched = ok & (gap < BUNCH_SHARE * sched)
    long_gap = ok & (gap > GAP_FACTOR * sched)
    n_gaps = np.bincount(pat_g[ok], minlength=net.n_patterns)
    n_bunched = np.bincount(pat_g[bunched], minlength=net.n_patterns)
    n_long = np.bincount(pat_g[long_gap], minlength=net.n_patterns)
    bunching = []
    for p in np.flatnonzero(n_gaps >= BUNCH_MIN_GAPS):
        bunching.append({
            "pattern_idx": int(p), "route": str(net.route_name[net.pat_route[p]]),
            "headsign": str(net.pat_headsign[p]), "gaps": int(n_gaps[p]),
            "bunched_pct": round(float(n_bunched[p] / n_gaps[p] * 100.0), 1),
            "long_gap_pct": round(float(n_long[p] / n_gaps[p] * 100.0), 1),
        })
    bunching.sort(key=bunch_key)
    log(f"bunching: {len(bunching)} patterns")

    return {
        "built_at": dt_mod.datetime.now().isoformat(timespec="seconds"),
        "train_dates": [train[0], train[-1]],
        "city_speed": city_speed,
        "audit": audit[:TOP_N],
        "audit_count": len(audit),
        "chokepoints": chokepoints,
        "reliability": reliability[:TOP_N],
        "bunching": bunching[:TOP_N],
        "build_seconds": round(time.time() - started, 1),
    }


def audit_key(row: dict) -> float:
    """Sort audit rows by the largest peak-hour gap first.

    Args:
        row: audit row.
    Returns:
        Sort value.
    """
    return -abs(row["peak_gap_min"])


def spread_key(row: dict) -> float:
    """Sort reliability rows by the largest lateness on a bad day first.

    Args:
        row: reliability row.
    Returns:
        Sort value.
    """
    return -row["late_min_on_bad_day"]


def bunch_key(row: dict) -> float:
    """Sort bunching rows by the largest bunched plus long-gap share first.

    Args:
        row: bunching row.
    Returns:
        Sort value.
    """
    return -(row["bunched_pct"] + row["long_gap_pct"])


def main() -> None:
    """Command-line entry point.

    Returns:
        None.
    """
    parser = argparse.ArgumentParser(description="Build operator insights (phase 2).")
    parser.parse_args()
    ops = build(Network(), config.load_findings())
    tmp_path = OPS_PATH.with_suffix(".json.tmp")
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(ops, handle)
    tmp_path.replace(OPS_PATH)
    log(f"wrote {OPS_PATH} in {ops['build_seconds']} s")


if __name__ == "__main__":
    main()
