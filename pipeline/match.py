"""
pipeline/match.py - cleaned positions -> segment observations for one day (SPEC 6.4).

Purpose: project each bus's points onto every pattern polyline of its route, split into
forward-moving runs, interpolate stop-crossing times, and emit one observation per
consecutive pair of crossed stops; then drop duplicate rows from overlapping patterns.
Inputs: data/clean/positions/YYYY-MM-DD.parquet, data/model/network.npz, findings.
Outputs: data/clean/seg_obs/YYYY-MM-DD.parquet (zstd); one stats record appended to
reports/pipeline_stats.json.
Run: python -m pipeline.match --date 2026-10-06
"""

import argparse
import time

import duckdb
import numpy as np
import pyarrow as pa
from numba import njit, prange

import config
from pipeline.geo import project_one
from pipeline.gtfs_network import append_stats
from planner.network import Network


def log(message: str) -> None:
    """Print one progress line immediately.

    Args:
        message: text.
    Returns:
        None.
    """
    print(message, flush=True)


@njit(cache=True, parallel=True)
def project_work(px: np.ndarray, py: np.ndarray, work_start: np.ndarray, work_end: np.ndarray,
                 work_pat: np.ndarray, work_off: np.ndarray, poly_ptr: np.ndarray,
                 poly_x: np.ndarray, poly_y: np.ndarray, poly_cum: np.ndarray,
                 d_out: np.ndarray, off_out: np.ndarray) -> None:
    """Project the points of every (group, pattern) work item onto that pattern (step 3).

    Args:
        px, py: all points of the day in metres.
        work_start, work_end: point range of each work item's group.
        work_pat: pattern of each work item.
        work_off: offset of each work item in d_out / off_out.
        poly_ptr, poly_x, poly_y, poly_cum: pattern polylines.
        d_out, off_out: receive along-distance and offset per (work item, point).
    Returns:
        None.
    """
    for w in prange(work_start.shape[0]):
        p = work_pat[w]
        v0 = poly_ptr[p]
        v1 = poly_ptr[p + 1]
        vx = poly_x[v0:v1]
        vy = poly_y[v0:v1]
        vcum = poly_cum[v0:v1]
        base = work_off[w]
        start = work_start[w]
        # Every point is projected onto every edge of the polyline (nearest point).
        for k in range(start, work_end[w]):
            result = project_one(px[k], py[k], vx, vy, vcum, 0, v1 - v0 - 1)
            d_out[base + k - start] = result[0]
            off_out[base + k - start] = result[1]


@njit(cache=True)
def grow_float(arr: np.ndarray, size: int) -> np.ndarray:
    """Return a copy of arr with capacity doubled (at least size).

    Args:
        arr: array to grow.
        size: required capacity.
    Returns:
        Larger array with the old values copied.
    """
    new_cap = max(arr.shape[0] * 2, size)
    out = np.empty(new_cap, dtype=arr.dtype)
    out[:arr.shape[0]] = arr
    return out


@njit(cache=True)
def grow_int(arr: np.ndarray, size: int) -> np.ndarray:
    """Return a copy of an integer array with capacity doubled (at least size).

    Args:
        arr: array to grow.
        size: required capacity.
    Returns:
        Larger array with the old values copied.
    """
    new_cap = max(arr.shape[0] * 2, size)
    out = np.empty(new_cap, dtype=arr.dtype)
    out[:arr.shape[0]] = arr
    return out


@njit(cache=True)
def runs_and_obs(ts: np.ndarray, vcode: np.ndarray, work_start: np.ndarray,
                 work_end: np.ndarray, work_pat: np.ndarray, work_off: np.ndarray,
                 d_all: np.ndarray, off_all: np.ndarray, pat_ptr: np.ndarray,
                 pat_seg: np.ndarray, pat_stop_dist: np.ndarray, max_offset_m: float,
                 run_gap_s: float, backtrack_m: float, min_run_segs: int,
                 min_run_m: float) -> tuple:
    """Runs, monotone distance, stop crossings, and observations (steps 4-6).

    Args:
        ts: point times (Unix seconds).
        vcode: vehicle code per point.
        work_*: work items as in project_work.
        d_all, off_all: projections from project_work.
        pat_ptr, pat_seg, pat_stop_dist: pattern arrays.
        max_offset_m, run_gap_s, backtrack_m, min_run_segs, min_run_m: SPEC 6.4 constants.
    Returns:
        Tuple of observation arrays (vehicle code, pattern, run id, seg_pos, segment,
        t_enter float, tt float) and runs kept per work item.
    """
    cap = 1 << 20
    o_veh = np.empty(cap, dtype=np.int64)
    o_pat = np.empty(cap, dtype=np.int64)
    o_run = np.empty(cap, dtype=np.int64)
    o_pos = np.empty(cap, dtype=np.int64)
    o_seg = np.empty(cap, dtype=np.int64)
    o_tenter = np.empty(cap, dtype=np.float64)
    o_tt = np.empty(cap, dtype=np.float64)
    n_obs = 0
    run_counter = 0
    runs_per_work = np.zeros(work_start.shape[0], dtype=np.int64)
    max_n = 0
    for w in range(work_start.shape[0]):
        if work_end[w] - work_start[w] > max_n:
            max_n = work_end[w] - work_start[w]
    md = np.empty(max_n, dtype=np.float64)
    run_of = np.empty(max_n, dtype=np.int64)
    idx = np.empty(max_n, dtype=np.int64)
    max_stops = 0
    for p in range(pat_ptr.shape[0] - 1):
        if pat_ptr[p + 1] - pat_ptr[p] > max_stops:
            max_stops = pat_ptr[p + 1] - pat_ptr[p]
    cross = np.empty(max_stops, dtype=np.float64)

    for w in range(work_start.shape[0]):
        start = work_start[w]
        n = work_end[w] - start
        base = work_off[w]
        p = work_pat[w]
        sp0 = pat_ptr[p]
        n_stops = pat_ptr[p + 1] - sp0

        # Step 4: split into runs and keep the running maximum of the distance.
        run = -1
        run_max = 0.0
        last_ts = 0
        for i in range(n):
            run_of[i] = -1
            if off_all[base + i] > max_offset_m:
                continue
            d = d_all[base + i]
            t = ts[start + i]
            start_new = False
            if run == -1:
                start_new = True
            elif t - last_ts > run_gap_s:
                start_new = True
            elif d < run_max - backtrack_m:
                start_new = True
            if start_new:
                run += 1
                run_max = d
            elif d > run_max:
                run_max = d
            md[i] = run_max
            run_of[i] = run
            last_ts = t

        # Steps 5 and 6: process each run's points in order.
        i = 0
        while i < n:
            if run_of[i] < 0:
                i += 1
                continue
            this_run = run_of[i]
            m = 0
            j = i
            while j < n:
                if run_of[j] == this_run:
                    idx[m] = j
                    m += 1
                elif run_of[j] > this_run:
                    break
                j += 1
            i = j
            first = idx[0]
            last = idx[m - 1]
            if md[last] - md[first] < min_run_m:
                continue
            # Crossing time of every stop with md[first] <= S < md[last].
            k0 = -1
            k1 = -1
            jj = 0
            for k in range(n_stops):
                s_dist = pat_stop_dist[sp0 + k]
                if s_dist < md[first]:
                    continue
                if s_dist >= md[last]:
                    break
                while jj + 1 < m and md[idx[jj + 1]] <= s_dist:
                    jj += 1
                a = idx[jj]
                b = idx[jj + 1]
                frac = (s_dist - md[a]) / (md[b] - md[a])
                cross[k] = ts[start + a] + frac * (ts[start + b] - ts[start + a])
                if k0 < 0:
                    k0 = k
                k1 = k
            if k0 < 0 or k1 - k0 + 1 < min_run_segs + 1:
                continue
            # Emit one observation per consecutive pair of crossed stops.
            needed = n_obs + (k1 - k0)
            if needed > o_veh.shape[0]:
                o_veh = grow_int(o_veh, needed)
                o_pat = grow_int(o_pat, needed)
                o_run = grow_int(o_run, needed)
                o_pos = grow_int(o_pos, needed)
                o_seg = grow_int(o_seg, needed)
                o_tenter = grow_float(o_tenter, needed)
                o_tt = grow_float(o_tt, needed)
            for k in range(k0, k1):
                o_veh[n_obs] = vcode[start]
                o_pat[n_obs] = p
                o_run[n_obs] = run_counter
                o_pos[n_obs] = k
                o_seg[n_obs] = pat_seg[sp0 + k]
                o_tenter[n_obs] = cross[k]
                o_tt[n_obs] = cross[k + 1] - cross[k]
                n_obs += 1
            run_counter += 1
            runs_per_work[w] += 1
    return (o_veh[:n_obs], o_pat[:n_obs], o_run[:n_obs], o_pos[:n_obs], o_seg[:n_obs],
            o_tenter[:n_obs], o_tt[:n_obs], runs_per_work)


def seg_obs_path(date_iso: str):
    """Output path of a day's segment observations.

    Args:
        date_iso: YYYY-MM-DD.
    Returns:
        Path.
    """
    return config.SEG_OBS_DIR / f"{date_iso}.parquet"


def match_day(date_iso: str, net: Network, findings: dict) -> dict | None:
    """Match one day of cleaned positions (SPEC 6.4 steps 1-9).

    Args:
        date_iso: the day.
        net: network.
        findings: Milestone 0 findings.
    Returns:
        Stats record, or None when the output already exists.
    """
    out_path = seg_obs_path(date_iso)
    if out_path.exists():
        log(f"match {date_iso}: output exists, skipping")
        return None
    started = time.time()
    config.SEG_OBS_DIR.mkdir(parents=True, exist_ok=True)
    max_offset_m = float(findings.get("max_offset_m", config.MAX_OFFSET_M_DEFAULT))
    run_gap_s = float(findings.get("run_gap_s", config.RUN_GAP_S_DEFAULT))
    day_type = config.day_type_of(date_iso)
    con = duckdb.connect()
    for statement in config.duckdb_settings():
        con.execute(statement)
    in_path = (config.POSITIONS_DIR / f"{date_iso}.parquet").relative_to(config.ROOT).as_posix()

    # Patterns of each route, as CSR.
    route_order = np.argsort(net.pat_route, kind="stable")
    route_pat_ptr = np.zeros(net.n_routes + 1, dtype=np.int64)
    route_pat_ptr[1:] = np.cumsum(np.bincount(net.pat_route, minlength=net.n_routes))
    poly_x = net.poly_x.astype(np.float64)
    poly_y = net.poly_y.astype(np.float64)
    poly_cum = net.poly_cum.astype(np.float64)
    pat_stop_dist = net.pat_stop_dist.astype(np.float64)

    # Vehicle codes: index into the sorted list of distinct vehicle ids.
    vehicles = con.execute(
        f"SELECT DISTINCT vehicle_id FROM read_parquet('{in_path}') ORDER BY vehicle_id"
    ).fetchnumpy()["vehicle_id"]

    obs_tables = []
    stats = {"groups": 0, "groups_with_run": 0, "active_groups": 0, "active_groups_with_run": 0,
             "runs": 0, "points": 0}
    run_offset = 0
    n_chunks = int(findings.get("day_chunks", 1))
    for chunk in range(n_chunks):
        # Step 1: the day's (or chunk's) points in (route, vehicle, time) order.
        chunk_filter = ""
        if n_chunks > 1:
            chunk_filter = f"WHERE hash(vehicle_id) % {n_chunks} = {chunk}"
        data = con.execute(
            "SELECT route_idx, dense_rank() OVER (ORDER BY vehicle_id) - 1 AS vrank, vehicle_id, "
            f"ts, x, y FROM read_parquet('{in_path}') {chunk_filter} "
            "ORDER BY route_idx, vehicle_id, ts"
        ).fetchnumpy()
        route_idx = data["route_idx"].astype(np.int64)
        vcode = np.searchsorted(vehicles, data["vehicle_id"]).astype(np.int64)
        ts = data["ts"].astype(np.int64)
        px = data["x"].astype(np.float64)
        py = data["y"].astype(np.float64)
        n_points = ts.shape[0]
        stats["points"] += int(n_points)
        if n_points == 0:
            continue

        # Step 2: group boundaries of (route, vehicle).
        change = np.ones(n_points, dtype=bool)
        change[1:] = (route_idx[1:] != route_idx[:-1]) | (vcode[1:] != vcode[:-1])
        g_start = np.flatnonzero(change)
        g_end = np.append(g_start[1:], n_points)
        g_route = route_idx[g_start]
        n_groups = g_start.shape[0]

        # Step 3: one work item per (group, pattern of the group's route).
        pats_per_group = route_pat_ptr[g_route + 1] - route_pat_ptr[g_route]
        work_group = np.repeat(np.arange(n_groups), pats_per_group)
        first_work = np.repeat(np.cumsum(pats_per_group) - pats_per_group, pats_per_group)
        within = np.arange(work_group.shape[0]) - first_work
        work_pat = route_order[route_pat_ptr[g_route[work_group]] + within].astype(np.int64)
        work_start = g_start[work_group].astype(np.int64)
        work_end = g_end[work_group].astype(np.int64)
        sizes = work_end - work_start
        work_off = np.zeros(sizes.shape[0], dtype=np.int64)
        work_off[1:] = np.cumsum(sizes)[:-1]
        total = int(sizes.sum())
        d_all = np.empty(total, dtype=np.float64)
        off_all = np.empty(total, dtype=np.float64)
        project_work(px, py, work_start, work_end, work_pat, work_off, net.poly_ptr.astype(np.int64),
                     poly_x, poly_y, poly_cum, d_all, off_all)
        log(f"match {date_iso}: projected {total:,} point-pattern pairs "
            f"({n_groups:,} groups) at {time.time() - started:.0f} s")

        # Steps 4-6.
        result = runs_and_obs(ts, vcode, work_start, work_end, work_pat, work_off, d_all, off_all,
                              net.pat_ptr.astype(np.int64), net.pat_seg.astype(np.int64),
                              pat_stop_dist, max_offset_m, run_gap_s, float(config.BACKTRACK_M),
                              config.MIN_RUN_SEGS, float(config.MIN_RUN_M))
        o_veh, o_pat, o_run, o_pos, o_seg, o_tenter, o_tt, runs_per_work = result
        del d_all
        del off_all

        # Group statistics for the M2 acceptance check.
        runs_per_group = np.bincount(work_group, weights=runs_per_work, minlength=n_groups)
        active = (g_end - g_start) >= 200
        stats["groups"] += int(n_groups)
        stats["groups_with_run"] += int(np.count_nonzero(runs_per_group > 0))
        stats["active_groups"] += int(np.count_nonzero(active))
        stats["active_groups_with_run"] += int(np.count_nonzero(active & (runs_per_group > 0)))
        stats["runs"] += int(runs_per_work.sum())

        # Step 7: plausibility filter.
        seg_len = net.seg_len_m[o_seg].astype(np.float64)
        speed_kmh = np.zeros(o_tt.shape[0])
        positive = o_tt > 0
        speed_kmh[positive] = seg_len[positive] / o_tt[positive] * 3.6
        valid = positive & (o_tt <= config.OBS_MAX_S) & (speed_kmh >= config.OBS_MIN_KMH) & \
            (speed_kmh <= config.OBS_MAX_KMH)
        t_enter = np.round(o_tenter).astype(np.int64)
        sod = ((t_enter + config.IST_OFFSET_S) % config.SECONDS_PER_DAY).astype(np.int32)
        obs_tables.append(pa.table({
            "vcode": o_veh,
            "pattern_idx": o_pat.astype(np.int32),
            "run_id": (o_run + run_offset).astype(np.int32),
            "seg_pos": o_pos.astype(np.int16),
            "segment_idx": o_seg.astype(np.int32),
            "t_enter": t_enter,
            "sod": sod,
            "bucket": (sod // config.BUCKET_S % config.N_BUCKETS).astype(np.int16),
            "day_type": np.full(o_tt.shape[0], day_type, dtype=np.int8),
            "tt_s": o_tt.astype(np.float32),
            "valid": valid,
        }))
        if o_run.shape[0] > 0:
            run_offset += int(o_run.max()) + 1

    # Step 8: drop rows duplicated by a lower-numbered pattern of the same route.
    obs = pa.concat_tables(obs_tables)
    con.register("obs_raw", obs)
    con.register("veh_tab", pa.table({"vcode": np.arange(vehicles.shape[0], dtype=np.int64),
                                      "vehicle_id": vehicles}))
    log(f"match {date_iso}: runs done at {time.time() - started:.0f} s, de-duplicating")
    # Number the rows so duplicates can be removed by id.
    con.execute("CREATE TEMP TABLE obs_n AS SELECT row_number() OVER () AS oid, * FROM obs_raw")
    n_before = con.execute("SELECT count(*) FROM obs_n").fetchone()[0]
    # Rows that have a twin from a lower-numbered pattern of the same vehicle on the same
    # segment within the window. The equality keys make this a hash join.
    con.execute(
        "CREATE TEMP TABLE drop_ids AS SELECT DISTINCT o.oid FROM obs_n o JOIN obs_n o2 "
        "ON o2.vcode = o.vcode AND o2.segment_idx = o.segment_idx "
        "WHERE o2.pattern_idx < o.pattern_idx "
        f"AND abs(o2.t_enter - o.t_enter) <= {config.DEDUP_WINDOW_S}"
    )
    con.execute(
        "CREATE TEMP TABLE obs_dedup AS SELECT * EXCLUDE (oid) FROM obs_n "
        "WHERE oid NOT IN (SELECT oid FROM drop_ids)"
    )
    row = con.execute(
        "SELECT count(*), avg(CASE WHEN valid THEN 1.0 ELSE 0.0 END) FROM obs_dedup"
    ).fetchone()
    n_obs = int(row[0])
    valid_share = float(row[1] or 0.0)
    # Median speed of valid observations, for the stats record.
    seg_len_table = pa.table({"segment_idx": np.arange(net.n_segments, dtype=np.int32),
                              "seg_len": net.seg_len_m.astype(np.float64)})
    con.register("seg_len_tab", seg_len_table)
    median_speed = con.execute(
        "SELECT median(l.seg_len / o.tt_s * 3.6) FROM obs_dedup o JOIN seg_len_tab l "
        "USING (segment_idx) WHERE o.valid"
    ).fetchone()[0]

    # Step 9: write Parquet through a .tmp name.
    tmp_path = out_path.with_suffix(".parquet.tmp")
    con.execute(
        "COPY (SELECT v.vehicle_id, o.pattern_idx, o.run_id, o.seg_pos, o.segment_idx, o.t_enter, "
        "o.sod, o.bucket, o.day_type, o.tt_s, o.valid FROM obs_dedup o JOIN veh_tab v USING (vcode) "
        "ORDER BY o.run_id, o.seg_pos) "
        f"TO '{tmp_path.relative_to(config.ROOT).as_posix()}' (FORMAT parquet, COMPRESSION zstd)"
    )
    con.close()
    tmp_path.replace(out_path)
    record = {
        "step": "match", "date": date_iso, "points": stats["points"], "groups": stats["groups"],
        "groups_with_run": stats["groups_with_run"], "active_groups": stats["active_groups"],
        "active_groups_with_run": stats["active_groups_with_run"], "runs": stats["runs"],
        "observations_before_dedup": int(n_before), "observations": n_obs,
        "valid_share": round(valid_share, 4), "median_valid_speed_kmh": round(float(median_speed or 0), 2),
        "max_offset_m": max_offset_m, "run_gap_s": run_gap_s,
        "seconds": round(time.time() - started, 1),
    }
    append_stats(record)
    log(f"match {date_iso}: {n_obs:,} observations, valid {valid_share:.3f}, median speed "
        f"{record['median_valid_speed_kmh']} km/h, {record['seconds']} s")
    return record


def main() -> None:
    """Command-line entry point.

    Returns:
        None.
    """
    parser = argparse.ArgumentParser(description="Match one day of positions (SPEC 6.4).")
    parser.add_argument("--date", required=True, help="YYYY-MM-DD")
    args = parser.parse_args()
    match_day(args.date, Network(), config.load_findings())


if __name__ == "__main__":
    main()
