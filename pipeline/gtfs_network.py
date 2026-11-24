"""
pipeline/gtfs_network.py - static GTFS -> data/model/network.npz (SPEC 6.1).

Purpose: build stops, route patterns, pattern polylines with snapped stops, shared
segments, timetable segment times, headways, footpaths, and the stop -> pattern index.
Inputs: data/raw/gtfs/{stops,routes,trips,stop_times,shapes}.txt, reports/m0_findings.json.
Outputs: data/model/network.npz (arrays listed in SPEC 6.1).
Run: python -m pipeline.gtfs_network
"""

import argparse
import json
import math
import time

import duckdb
import numpy as np
import pyarrow as pa
from numba import njit
from shapely.geometry import LineString

import config
from pipeline import geo


def log(message: str) -> None:
    """Print one progress line immediately.

    Args:
        message: text.
    Returns:
        None.
    """
    print(message, flush=True)


def gtfs_csv(name: str) -> str:
    """DuckDB read_csv call for a GTFS file with every column as text (SPEC 6.1 step 1).

    Args:
        name: file name.
    Returns:
        SQL text.
    """
    path = (config.GTFS_DIR / name).relative_to(config.ROOT).as_posix()
    return f"read_csv('{path}', all_varchar=true, header=true)"


def time_to_s_sql(column: str) -> str:
    """SQL turning H:MM:SS or HH:MM:SS text into seconds; hours >= 24 are kept.

    Args:
        column: SQL column expression.
    Returns:
        SQL text producing INTEGER or NULL.
    """
    c = f"trim({column})"
    return (f"(TRY_CAST(split_part({c}, ':', 1) AS INTEGER) * 3600 + "
            f"TRY_CAST(split_part({c}, ':', 2) AS INTEGER) * 60 + "
            f"TRY_CAST(split_part({c}, ':', 3) AS INTEGER))")


# ---------------------------------------------------------------------------
# numba helpers
# ---------------------------------------------------------------------------

@njit(cache=True)
def snap_stops(vx: np.ndarray, vy: np.ndarray, vcum: np.ndarray, sx: np.ndarray,
               sy: np.ndarray, first_share: float, chord_factor: float,
               extra_m: float) -> tuple:
    """Snap stops in order onto a polyline inside the SPEC 6.1 step 6b windows.

    Args:
        vx, vy: polyline vertices in metres.
        vcum: cumulative distance per vertex.
        sx, sy: stops in metres, in pattern order.
        first_share: stop 0 searches along-distance <= first_share * length.
        chord_factor, extra_m: stop i searches [d(i-1), d(i-1) + chord_factor*chord + extra_m].
    Returns:
        Tuple (dist, off, snap_x, snap_y) arrays per stop.
    """
    n = sx.shape[0]
    dist = np.empty(n, dtype=np.float64)
    off = np.empty(n, dtype=np.float64)
    snap_x = np.empty(n, dtype=np.float64)
    snap_y = np.empty(n, dtype=np.float64)
    total = vcum[vcum.shape[0] - 1]
    for i in range(n):
        # Search window along the polyline for this stop.
        if i == 0:
            lo = 0.0
            hi = first_share * total
        else:
            chord = math.sqrt((sx[i] - sx[i - 1]) ** 2 + (sy[i] - sy[i - 1]) ** 2)
            lo = dist[i - 1]
            hi = lo + chord_factor * chord + extra_m
        best2 = 1e30
        best_d = lo
        best_x = sx[i]
        best_y = sy[i]
        for e in range(vx.shape[0] - 1):
            a = vcum[e]
            b = vcum[e + 1]
            # Skip edges that lie completely outside the window.
            if b < lo or a > hi:
                continue
            ex = vx[e + 1] - vx[e]
            ey = vy[e + 1] - vy[e]
            len2 = ex * ex + ey * ey
            t = 0.0
            if len2 > 0.0:
                t = ((sx[i] - vx[e]) * ex + (sy[i] - vy[e]) * ey) / len2
            # Restrict the foot point to the part of the edge inside the window.
            t_lo = 0.0
            t_hi = 1.0
            if b > a:
                t_lo = max(0.0, (lo - a) / (b - a))
                t_hi = min(1.0, (hi - a) / (b - a))
            if t < t_lo:
                t = t_lo
            if t > t_hi:
                t = t_hi
            fx = vx[e] + t * ex
            fy = vy[e] + t * ey
            d2 = (sx[i] - fx) ** 2 + (sy[i] - fy) ** 2
            if d2 < best2:
                best2 = d2
                best_d = a + t * (b - a)
                best_x = fx
                best_y = fy
        dist[i] = best_d
        off[i] = math.sqrt(best2)
        snap_x[i] = best_x
        snap_y[i] = best_y
    return dist, off, snap_x, snap_y


@njit(cache=True)
def build_footpaths(x: np.ndarray, y: np.ndarray, max_m: float, detour: float,
                    speed_mps: float) -> tuple:
    """All ordered stop pairs within max_m straight-line metres, as CSR (SPEC 6.1 step 10).

    Args:
        x, y: stop coordinates in metres.
        max_m: distance limit.
        detour: walk detour factor.
        speed_mps: walking speed.
    Returns:
        Tuple (foot_ptr, foot_to, foot_s, foot_m).
    """
    n = x.shape[0]
    counts = np.zeros(n, dtype=np.int64)
    max2 = max_m * max_m
    # First pass counts neighbours so the CSR arrays can be sized exactly.
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            dx = x[i] - x[j]
            dy = y[i] - y[j]
            if dx * dx + dy * dy <= max2:
                counts[i] += 1
    ptr = np.zeros(n + 1, dtype=np.int32)
    for i in range(n):
        ptr[i + 1] = ptr[i] + counts[i]
    total = ptr[n]
    to = np.empty(total, dtype=np.int32)
    foot_s = np.empty(total, dtype=np.float32)
    foot_m = np.empty(total, dtype=np.float32)
    # Second pass fills the neighbours.
    for i in range(n):
        k = ptr[i]
        for j in range(n):
            if i == j:
                continue
            dx = x[i] - x[j]
            dy = y[i] - y[j]
            d2 = dx * dx + dy * dy
            if d2 <= max2:
                walk_m = detour * math.sqrt(d2)
                to[k] = j
                foot_m[k] = walk_m
                foot_s[k] = walk_m / speed_mps
                k += 1
    return ptr, to, foot_s, foot_m


# ---------------------------------------------------------------------------
# Main build
# ---------------------------------------------------------------------------

def load_base_tables(con: duckdb.DuckDBPyConnection, findings: dict) -> None:
    """Load trips and stop_times, drop consecutive repeated stops, key each trip.

    Args:
        con: DuckDB connection.
        findings: Milestone 0 findings.
    Returns:
        None. Creates tables trips, st, trip_key.
    """
    # Trips with trimmed ids; step 2 keeps only the service filter when one is set.
    con.execute(
        "CREATE TABLE trips AS SELECT trim(trip_id) AS trip_id, trim(route_id) AS route_id, "
        "trim(service_id) AS service_id, NULLIF(trim(shape_id), '') AS shape_id "
        f"FROM {gtfs_csv('trips.txt')}"
    )
    if findings["service_filter"] is not None:
        con.execute("DELETE FROM trips WHERE service_id <> ?", [findings["service_filter"]])

    # Stop times of kept trips, with integer sequence and arrival seconds.
    con.execute(
        "CREATE TABLE st_raw AS SELECT trim(trip_id) AS trip_id, trim(stop_id) AS stop_id, "
        f"CAST(trim(stop_sequence) AS INTEGER) AS seq, {time_to_s_sql('arrival_time')} AS arr "
        f"FROM {gtfs_csv('stop_times.txt')} WHERE trim(trip_id) IN (SELECT trip_id FROM trips)"
    )
    # Step 3: drop a stop equal to the previous stop of the same trip, then number positions.
    con.execute(
        "CREATE TABLE st AS SELECT trip_id, stop_id, seq, arr, "
        "row_number() OVER (PARTITION BY trip_id ORDER BY seq) - 1 AS pos FROM ("
        "  SELECT *, LAG(stop_id) OVER (PARTITION BY trip_id ORDER BY seq) AS prev FROM st_raw"
        ") WHERE prev IS NULL OR prev <> stop_id"
    )
    con.execute("DROP TABLE st_raw")
    # Pattern key of each trip: its stop ids joined by '>'.
    con.execute(
        "CREATE TABLE trip_key AS SELECT k.trip_id, t.route_id, t.shape_id, k.pkey, k.n_stops "
        "FROM (SELECT trip_id, string_agg(stop_id, '>' ORDER BY seq) AS pkey, count(*) AS n_stops "
        "      FROM st GROUP BY trip_id) k JOIN trips t ON k.trip_id = t.trip_id"
    )


def select_patterns(con: duckdb.DuckDBPyConnection) -> list[tuple]:
    """Apply SPEC 6.1 step 4 and number patterns as in step 5.

    Args:
        con: connection with trip_key.
    Returns:
        List of (route_id, pkey, trips) in pattern index order.
    """
    # Count trips per (route, pattern); keep the frequent ones, at most 4 per route,
    # and fall back to the single most frequent pattern for routes that keep none.
    rows = con.execute(
        "WITH rt AS (SELECT route_id, count(*) AS rtrips FROM trip_key GROUP BY route_id), "
        "pc AS (SELECT route_id, pkey, any_value(n_stops) AS n_stops, count(*) AS trips "
        "       FROM trip_key GROUP BY route_id, pkey), "
        "c AS (SELECT pc.*, rt.rtrips, row_number() OVER (PARTITION BY pc.route_id "
        "      ORDER BY pc.trips DESC, pc.pkey) AS rn_all FROM pc JOIN rt USING (route_id) "
        "      WHERE pc.n_stops >= ?), "
        "ok AS (SELECT *, row_number() OVER (PARTITION BY route_id ORDER BY trips DESC, pkey) AS rn "
        "       FROM c WHERE trips >= greatest(?, ? * rtrips)), "
        "kept AS (SELECT route_id, pkey, trips FROM ok WHERE rn <= ? "
        "         UNION ALL SELECT route_id, pkey, trips FROM c WHERE rn_all = 1 "
        "         AND route_id NOT IN (SELECT route_id FROM ok)) "
        "SELECT route_id, pkey, trips FROM kept ORDER BY route_id, trips DESC, pkey",
        [config.PATTERN_MIN_STOPS, config.PATTERN_MIN_TRIPS, config.PATTERN_MIN_SHARE,
         config.MAX_PATTERNS_PER_ROUTE],
    ).fetchall()
    return rows


def build_polyline(stop_x: np.ndarray, stop_y: np.ndarray, shape_x: np.ndarray | None,
                   shape_y: np.ndarray | None) -> tuple:
    """Polyline of one pattern with its stops inserted as vertices (SPEC 6.1 step 6).

    Args:
        stop_x, stop_y: the pattern's stops in metres, in order.
        shape_x, shape_y: shape points in metres, or None when no shape applies.
    Returns:
        Tuple (vx, vy, vcum, stop_dist, stop_vtx, used_shape).
    """
    n = stop_x.shape[0]
    used_shape = False
    stop_dist = None
    snap_x = None
    snap_y = None
    vx = None
    vy = None
    vcum = None
    if shape_x is not None and shape_x.shape[0] >= 2:
        # Step 6a: simplify the shape in metres.
        line = LineString(np.column_stack([shape_x, shape_y]))
        simple = line.simplify(config.SHAPE_SIMPLIFY_M, preserve_topology=False)
        coords = np.asarray(simple.coords, dtype=np.float64)
        if coords.shape[0] >= 2:
            vx = coords[:, 0].copy()
            vy = coords[:, 1].copy()
            seg_lengths = np.hypot(np.diff(vx), np.diff(vy))
            vcum = np.concatenate([[0.0], np.cumsum(seg_lengths)])
            # Step 6b: snap stops in order inside their windows.
            dist, off, snap_x, snap_y = snap_stops(
                vx, vy, vcum, stop_x, stop_y, config.SNAP_FIRST_SHARE,
                float(config.SNAP_CHORD_FACTOR), float(config.SNAP_EXTRA_M))
            # Step 6c: reject the shape when a stop is far away or order breaks.
            far = bool(np.any(off > config.STOP_SNAP_MAX_M))
            decreasing = bool(np.any(np.diff(dist) < 0))
            if not far and not decreasing:
                used_shape = True
                stop_dist = dist
    if not used_shape:
        # Straight chords through the stops.
        vx = stop_x.astype(np.float64).copy()
        vy = stop_y.astype(np.float64).copy()
        chord = np.hypot(np.diff(vx), np.diff(vy))
        vcum = np.concatenate([[0.0], np.cumsum(chord)])
        stop_dist = vcum.copy()
        snap_x = vx.copy()
        snap_y = vy.copy()

    # Step 6d: force strictly increasing stop distances.
    stop_dist = stop_dist.copy()
    for i in range(1, n):
        if stop_dist[i] <= stop_dist[i - 1]:
            stop_dist[i] = stop_dist[i - 1] + 1.0

    if not used_shape:
        # Chord polylines already have the stops as vertices.
        return vx, vy, stop_dist.copy(), stop_dist, np.arange(n, dtype=np.int32), False

    # Step 6e: merge the snapped stop points into the vertex list, ordered by
    # along-distance; at equal distance the shape vertex comes before the stop.
    all_x = np.concatenate([vx, snap_x])
    all_y = np.concatenate([vy, snap_y])
    all_cum = np.concatenate([vcum, stop_dist])
    kind = np.concatenate([np.zeros(vx.shape[0], dtype=np.int64), np.ones(n, dtype=np.int64)])
    order = np.lexsort((kind, all_cum))
    merged_x = all_x[order]
    merged_y = all_y[order]
    merged_cum = all_cum[order]
    position_of = np.empty(order.shape[0], dtype=np.int64)
    position_of[order] = np.arange(order.shape[0])
    stop_vtx = position_of[vx.shape[0]:].astype(np.int32)
    return merged_x, merged_y, merged_cum, stop_dist, stop_vtx, True


def build(findings: dict) -> dict:
    """Run every step of SPEC 6.1 and return the arrays for network.npz.

    Args:
        findings: Milestone 0 findings.
    Returns:
        Dict of numpy arrays.
    """
    started = time.time()
    con = duckdb.connect()
    for statement in config.duckdb_settings():
        con.execute(statement)

    load_base_tables(con, findings)
    patterns = select_patterns(con)
    log(f"patterns kept: {len(patterns)}")

    # Step 5: stops used by kept patterns, numbered by stop_id ascending.
    pattern_stop_ids = []
    used_ids = set()
    for route_id, pkey, trips in patterns:
        ids = pkey.split(">")
        pattern_stop_ids.append(ids)
        used_ids.update(ids)
    stops_rows = con.execute(
        f"SELECT trim(stop_id), trim(stop_name), CAST(stop_lat AS DOUBLE), CAST(stop_lon AS DOUBLE) "
        f"FROM {gtfs_csv('stops.txt')} ORDER BY trim(stop_id)"
    ).fetchall()
    stop_rows = [row for row in stops_rows if row[0] in used_ids]
    stop_id = np.array([row[0] for row in stop_rows])
    stop_name = np.array([row[1] for row in stop_rows])
    stop_lat = np.array([row[2] for row in stop_rows], dtype=np.float64)
    stop_lon = np.array([row[3] for row in stop_rows], dtype=np.float64)
    stop_index = {}
    for index, sid in enumerate(stop_id):
        stop_index[str(sid)] = index
    sx_all, sy_all = geo.to_xy(stop_lat, stop_lon)

    # Routes: every route that keeps a pattern, numbered by route_id ascending.
    route_key_column = findings["route_key"]
    if route_key_column == "via_trip_id":
        route_key_column = "route_id"
    route_rows = con.execute(
        "SELECT trim(route_id), trim(route_short_name), trim(route_long_name), "
        f"{config.ROUTE_NORM_SQL.replace('{c}', route_key_column)} "
        f"FROM {gtfs_csv('routes.txt')} ORDER BY trim(route_id)"
    ).fetchall()
    kept_routes = set()
    for route_id_value, pkey, trips in patterns:
        kept_routes.add(route_id_value)
    route_rows = [row for row in route_rows if row[0] in kept_routes]
    route_id = np.array([row[0] for row in route_rows])
    route_names = []
    for row in route_rows:
        # route_name: short name, else long name, else id.
        name = row[0]
        if row[2] is not None and row[2] != "":
            name = row[2]
        if row[1] is not None and row[1] != "":
            name = row[1]
        route_names.append(name)
    route_name = np.array(route_names)
    route_key = np.array([row[3] for row in route_rows])
    route_index = {}
    for index, rid in enumerate(route_id):
        route_index[str(rid)] = index

    # Most common shape per pattern (ties: shape_id ascending).
    shape_rows = con.execute(
        "SELECT route_id, pkey, shape_id FROM ("
        "  SELECT route_id, pkey, shape_id, row_number() OVER (PARTITION BY route_id, pkey "
        "  ORDER BY count(*) DESC, shape_id) AS rn FROM trip_key WHERE shape_id IS NOT NULL "
        "  GROUP BY route_id, pkey, shape_id) WHERE rn = 1"
    ).fetchall()
    pattern_shape = {}
    for row in shape_rows:
        pattern_shape[(row[0], row[1])] = row[2]

    # Shape points, grouped by shape id (only when shapes are used).
    shape_slices = {}
    shape_x_all = None
    shape_y_all = None
    if findings["use_shapes"]:
        shape_table = con.execute(
            "SELECT trim(shape_id) AS sid, CAST(shape_pt_lat AS DOUBLE) AS lat, "
            f"CAST(shape_pt_lon AS DOUBLE) AS lon FROM {gtfs_csv('shapes.txt')} "
            "ORDER BY sid, CAST(trim(shape_pt_sequence) AS INTEGER)"
        ).fetchnumpy()
        shape_x_all, shape_y_all = geo.to_xy(shape_table["lat"], shape_table["lon"])
        sids = shape_table["sid"]
        starts = np.flatnonzero(np.concatenate([[True], sids[1:] != sids[:-1]]))
        ends = np.concatenate([starts[1:], [sids.shape[0]]])
        for start, end in zip(starts, ends):
            shape_slices[str(sids[start])] = (int(start), int(end))

    # Step 6: polylines and snapped stops for every pattern.
    pat_route = np.empty(len(patterns), dtype=np.int32)
    pat_trips = np.empty(len(patterns), dtype=np.int32)
    pat_headsign = []
    pat_ptr = [0]
    pat_stops_list = []
    pat_dist_list = []
    pat_vtx_list = []
    poly_ptr = [0]
    poly_x_list = []
    poly_y_list = []
    poly_cum_list = []
    shapes_used = 0
    for p, (route_id_value, pkey, trips) in enumerate(patterns):
        ids = pattern_stop_ids[p]
        idx = np.array([stop_index[s] for s in ids], dtype=np.int32)
        pat_route[p] = route_index[route_id_value]
        pat_trips[p] = trips
        pat_headsign.append(str(stop_name[idx[-1]]))
        shape_x = None
        shape_y = None
        shape_id_value = pattern_shape.get((route_id_value, pkey))
        if findings["use_shapes"] and shape_id_value in shape_slices:
            start, end = shape_slices[shape_id_value]
            shape_x = shape_x_all[start:end]
            shape_y = shape_y_all[start:end]
        vx, vy, vcum, dist, vtx, used = build_polyline(sx_all[idx], sy_all[idx], shape_x, shape_y)
        if used:
            shapes_used += 1
        pat_stops_list.append(idx)
        pat_dist_list.append(dist)
        pat_vtx_list.append(vtx)
        pat_ptr.append(pat_ptr[-1] + idx.shape[0])
        poly_x_list.append(vx)
        poly_y_list.append(vy)
        poly_cum_list.append(vcum)
        poly_ptr.append(poly_ptr[-1] + vx.shape[0])
    log(f"polylines built: {shapes_used} of {len(patterns)} patterns use their shape")

    pat_stops = np.concatenate(pat_stops_list).astype(np.int32)
    pat_stop_dist = np.concatenate(pat_dist_list).astype(np.float32)
    pat_stop_vtx = np.concatenate(pat_vtx_list).astype(np.int32)
    poly_x = np.concatenate(poly_x_list)
    poly_y = np.concatenate(poly_y_list)
    poly_cum = np.concatenate(poly_cum_list).astype(np.float32)
    poly_lat = poly_y / config.METRES_PER_DEG + config.LAT0
    poly_lon = poly_x / (geo.COS_LAT0 * config.METRES_PER_DEG) + config.LON0
    pat_ptr_arr = np.array(pat_ptr, dtype=np.int32)

    # Step 7: segments = distinct consecutive stop pairs, numbered by (from, to).
    pair_lengths = {}
    for p in range(len(patterns)):
        a = pat_ptr_arr[p]
        b = pat_ptr_arr[p + 1]
        for k in range(a, b - 1):
            pair = (int(pat_stops[k]), int(pat_stops[k + 1]))
            if pair not in pair_lengths:
                pair_lengths[pair] = []
            pair_lengths[pair].append(float(pat_stop_dist[k + 1] - pat_stop_dist[k]))
    pairs_sorted = sorted(pair_lengths.keys())
    seg_index = {}
    for g, pair in enumerate(pairs_sorted):
        seg_index[pair] = g
    n_seg = len(pairs_sorted)
    seg_from = np.array([pair[0] for pair in pairs_sorted], dtype=np.int32)
    seg_to = np.array([pair[1] for pair in pairs_sorted], dtype=np.int32)
    seg_len_m = np.empty(n_seg, dtype=np.float32)
    for g, pair in enumerate(pairs_sorted):
        seg_len_m[g] = max(float(np.median(pair_lengths[pair])), float(config.SEG_MIN_LEN_M))
    pat_seg = np.full(pat_stops.shape[0], -1, dtype=np.int32)
    for p in range(len(patterns)):
        a = pat_ptr_arr[p]
        b = pat_ptr_arr[p + 1]
        for k in range(a, b - 1):
            pat_seg[k] = seg_index[(int(pat_stops[k]), int(pat_stops[k + 1]))]
    log(f"segments: {n_seg}")

    # Register patterns and segments so the timetable SQL can join to them.
    pat_table = pa.table({
        "pidx": np.arange(len(patterns), dtype=np.int32),
        "route_id": [row[0] for row in patterns],
        "pkey": [row[1] for row in patterns],
        "n_stops": np.diff(pat_ptr_arr).astype(np.int32),
    })
    con.register("pat_tab", pat_table)
    seg_table = pa.table({
        "from_id": [str(stop_id[pair[0]]) for pair in pairs_sorted],
        "to_id": [str(stop_id[pair[1]]) for pair in pairs_sorted],
        "seg": np.arange(n_seg, dtype=np.int32),
    })
    con.register("seg_tab", seg_table)
    # Trips of kept patterns with their pattern index.
    con.execute(
        "CREATE TABLE kept_trips AS SELECT k.trip_id, p.pidx, p.n_stops FROM trip_key k "
        "JOIN pat_tab p ON k.route_id = p.route_id AND k.pkey = p.pkey"
    )

    # Step 8 (first part): fill missing arrival times by interpolation over stop distance.
    if findings["stop_times_null_arrival_rate"] > 0:
        fill_missing_arrivals(con, pat_ptr_arr, pat_stop_dist)

    # Step 8: timetable seconds per (hour, segment) from consecutive arrivals.
    con.execute(
        "CREATE TABLE hops AS SELECT h.from_id, h.to_id, h.arr, h.dt FROM ("
        "  SELECT st.stop_id AS from_id, LEAD(st.stop_id) OVER w AS to_id, st.arr, "
        "  LEAD(st.arr) OVER w - st.arr AS dt FROM st "
        "  WHERE st.trip_id IN (SELECT trip_id FROM kept_trips) "
        "  WINDOW w AS (PARTITION BY st.trip_id ORDER BY st.seq)"
        ") h WHERE h.to_id IS NOT NULL AND h.dt IS NOT NULL AND h.arr IS NOT NULL"
    )
    cell_rows = con.execute(
        "SELECT CAST((h.arr // 3600) % 24 AS INTEGER) AS hour, s.seg, median(h.dt), count(*) "
        "FROM hops h JOIN seg_tab s ON h.from_id = s.from_id AND h.to_id = s.to_id GROUP BY 1, 2"
    ).fetchall()
    day_rows = con.execute(
        "SELECT s.seg, median(h.dt), count(*) FROM hops h JOIN seg_tab s "
        "ON h.from_id = s.from_id AND h.to_id = s.to_id GROUP BY 1"
    ).fetchall()
    default_mps = config.DEFAULT_SPEED_KMH / 3.6
    sched_time = np.empty((config.N_HOURS, n_seg), dtype=np.float32)
    day_default = seg_len_m / default_mps
    for seg_value, med, count in day_rows:
        if count >= config.SCHED_MIN_VALUES:
            day_default[seg_value] = med
    sched_time[:, :] = day_default[None, :]
    for hour, seg_value, med, count in cell_rows:
        if count >= config.SCHED_MIN_VALUES:
            sched_time[hour, seg_value] = med
    # Clamp so implied speed stays within [3, 60] km/h.
    t_min = seg_len_m / (config.SPEED_MAX_KMH / 3.6)
    t_max = seg_len_m / (config.SPEED_MIN_KMH / 3.6)
    sched_time = np.clip(sched_time, t_min[None, :], t_max[None, :]).astype(np.float32)

    # Step 9: headway per (pattern, hour) from arrivals at the middle stop.
    head_rows = con.execute(
        "SELECT k.pidx, CAST((st.arr // 3600) % 24 AS INTEGER) AS hour, count(*) "
        "FROM kept_trips k JOIN st ON st.trip_id = k.trip_id AND st.pos = k.n_stops // 2 "
        "WHERE st.arr IS NOT NULL GROUP BY 1, 2"
    ).fetchall()
    headway_s = np.full((len(patterns), config.N_HOURS), np.inf, dtype=np.float32)
    for pidx, hour, count in head_rows:
        headway_s[pidx, hour] = 3600.0 / count

    # Step 10: footpaths between stops within 300 m.
    foot_ptr, foot_to, foot_s, foot_m = build_footpaths(
        sx_all, sy_all, float(config.TRANSFER_WALK_MAX_M), config.WALK_DETOUR,
        config.WALK_SPEED_MPS)

    # Step 11: stop -> (pattern, position) CSR.
    n_stop = stop_id.shape[0]
    sp_counts = np.zeros(n_stop, dtype=np.int64)
    np.add.at(sp_counts, pat_stops, 1)
    sp_ptr = np.zeros(n_stop + 1, dtype=np.int32)
    sp_ptr[1:] = np.cumsum(sp_counts)
    pat_of_entry = np.repeat(np.arange(len(patterns), dtype=np.int32), np.diff(pat_ptr_arr))
    pos_of_entry = (np.arange(pat_stops.shape[0]) - pat_ptr_arr[pat_of_entry]).astype(np.int32)
    order = np.lexsort((pos_of_entry, pat_of_entry, pat_stops))
    sp_pat = pat_of_entry[order].astype(np.int32)
    sp_pos = pos_of_entry[order].astype(np.int32)

    log(f"network built in {time.time() - started:.0f} s: {n_stop} stops, {len(route_id)} routes, "
        f"{len(patterns)} patterns, {n_seg} segments, {foot_to.shape[0]} footpaths")
    arrays = {
        "stop_id": stop_id, "stop_name": stop_name, "stop_lat": stop_lat, "stop_lon": stop_lon,
        "stop_x": sx_all.astype(np.float32), "stop_y": sy_all.astype(np.float32),
        "route_id": route_id, "route_name": route_name, "route_key": route_key,
        "pat_route": pat_route, "pat_trips": pat_trips, "pat_headsign": np.array(pat_headsign),
        "pat_ptr": pat_ptr_arr, "pat_stops": pat_stops, "pat_seg": pat_seg,
        "pat_stop_dist": pat_stop_dist, "pat_stop_vtx": pat_stop_vtx,
        "poly_ptr": np.array(poly_ptr, dtype=np.int32),
        "poly_x": poly_x.astype(np.float32), "poly_y": poly_y.astype(np.float32),
        "poly_lat": poly_lat, "poly_lon": poly_lon, "poly_cum": poly_cum,
        "seg_from": seg_from, "seg_to": seg_to, "seg_len_m": seg_len_m,
        "sched_time": sched_time, "headway_s": headway_s,
        "sp_ptr": sp_ptr, "sp_pat": sp_pat, "sp_pos": sp_pos,
        "foot_ptr": foot_ptr, "foot_to": foot_to, "foot_s": foot_s, "foot_m": foot_m,
    }
    stats = {
        "step": "gtfs_network", "stops": int(n_stop), "routes": int(len(route_id)),
        "patterns": len(patterns), "segments": n_seg, "shapes_used": shapes_used,
        "footpaths": int(foot_to.shape[0]), "seconds": round(time.time() - started, 1),
    }
    return {"arrays": arrays, "stats": stats}


def fill_missing_arrivals(con: duckdb.DuckDBPyConnection, pat_ptr: np.ndarray,
                          pat_stop_dist: np.ndarray) -> None:
    """Interpolate missing arrival times inside each trip over pattern stop distance.

    Args:
        con: connection with st and kept_trips.
        pat_ptr: pattern CSR pointer.
        pat_stop_dist: stop distances per pattern position.
    Returns:
        None. Updates st.arr in place for trips of kept patterns.
    """
    # Rows of kept trips in trip and position order, with the pattern of each trip.
    table = con.execute(
        "SELECT st.trip_id, st.pos, st.arr, k.pidx FROM st JOIN kept_trips k "
        "ON st.trip_id = k.trip_id ORDER BY st.trip_id, st.pos"
    ).fetchnumpy()
    trip_ids = table["trip_id"]
    arr = table["arr"]
    pos = table["pos"].astype(np.int64)
    pidx = table["pidx"].astype(np.int64)
    arr_values = np.where(np.ma.getmaskarray(arr), np.nan, np.ma.getdata(arr)).astype(np.float64)
    dist = pat_stop_dist[pat_ptr[pidx] + pos].astype(np.float64)
    starts = np.flatnonzero(np.concatenate([[True], trip_ids[1:] != trip_ids[:-1]]))
    ends = np.concatenate([starts[1:], [trip_ids.shape[0]]])
    filled = arr_values.copy()
    for start, end in zip(starts, ends):
        part = arr_values[start:end]
        known = ~np.isnan(part)
        # Only gaps between two timed stops can be interpolated.
        if known.all() or known.sum() < 2:
            continue
        filled[start:end] = np.interp(dist[start:end], dist[start:end][known], part[known])
        inside = (dist[start:end] >= dist[start:end][known].min()) & \
                 (dist[start:end] <= dist[start:end][known].max())
        filled[start:end] = np.where(inside, filled[start:end], np.nan)
    update = pa.table({"trip_id": trip_ids, "pos": pos, "arr_new": filled})
    con.register("arr_fill", update)
    con.execute(
        "UPDATE st SET arr = CAST(round(f.arr_new) AS INTEGER) FROM arr_fill f "
        "WHERE st.trip_id = f.trip_id AND st.pos = f.pos AND st.arr IS NULL AND NOT isnan(f.arr_new)"
    )


def append_stats(record: dict) -> None:
    """Append one JSON record to reports/pipeline_stats.json (a JSON list).

    Args:
        record: the stats record.
    Returns:
        None.
    """
    records = []
    if config.PIPELINE_STATS_PATH.exists():
        with open(config.PIPELINE_STATS_PATH, "r", encoding="utf-8") as handle:
            records = json.load(handle)
    records.append(record)
    tmp_path = config.PIPELINE_STATS_PATH.with_suffix(".json.tmp")
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(records, handle, indent=1)
    tmp_path.replace(config.PIPELINE_STATS_PATH)


def main() -> None:
    """Command-line entry point.

    Returns:
        None.
    """
    parser = argparse.ArgumentParser(description="Build network.npz from static GTFS (SPEC 6.1).")
    parser.parse_args()
    findings = config.load_findings()
    result = build(findings)
    config.MODEL_DIR.mkdir(parents=True, exist_ok=True)
    # np.savez appends .npz, so the temporary name ends in .tmp.npz.
    tmp_path = config.MODEL_DIR / "network.tmp.npz"
    np.savez(tmp_path, **result["arrays"])
    tmp_path.replace(config.NETWORK_PATH)
    append_stats(result["stats"])
    log(f"wrote {config.NETWORK_PATH}")


if __name__ == "__main__":
    main()
