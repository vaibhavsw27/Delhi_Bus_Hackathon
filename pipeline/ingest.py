"""
pipeline/ingest.py - one day's SQLite -> cleaned positions Parquet (SPEC 6.3).

Purpose: typed read, null/box/date filters, route resolution to network routes,
de-duplication, spike filter, and stationary thinning, all in DuckDB.
Inputs: data/raw/sqlite/bus_movements_YYYY_MM_DD.db, data/model/network.npz, findings.
Outputs: data/clean/positions/YYYY-MM-DD.parquet (zstd), sorted by (route_idx, vehicle_id, ts);
one stats record appended to reports/pipeline_stats.json.
Run: python -m pipeline.ingest --date 2026-10-06
"""

import argparse
import time

import duckdb
import numpy as np
import pyarrow as pa

import config
from pipeline import geo
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


def positions_path(date_iso: str):
    """Output path of a day's cleaned positions.

    Args:
        date_iso: YYYY-MM-DD.
    Returns:
        Path.
    """
    return config.POSITIONS_DIR / f"{date_iso}.parquet"


def ingest_day(date_iso: str, net: Network, findings: dict) -> dict | None:
    """Clean one day of GPS points (SPEC 6.3 steps 1-8).

    Args:
        date_iso: the day.
        net: network (for route keys).
        findings: Milestone 0 findings.
    Returns:
        The stats record, or None when the output already exists.
    """
    out_path = positions_path(date_iso)
    if out_path.exists():
        log(f"ingest {date_iso}: output exists, skipping")
        return None
    started = time.time()
    config.POSITIONS_DIR.mkdir(parents=True, exist_ok=True)
    config.TMP_DIR.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    for statement in config.duckdb_settings():
        con.execute(statement)
    con.execute("LOAD sqlite")
    con.execute("SET sqlite_all_varchar=true")
    db_file = config.SQLITE_DIR / ("bus_movements_" + date_iso.replace("-", "_") + ".db")
    con.execute(f"ATTACH '{db_file.relative_to(config.ROOT).as_posix()}' AS src (TYPE sqlite, READ_ONLY)")

    schema = findings["schema_map"]
    table = findings["table_name"]
    lat_col = f'"{schema["lat"]}"'
    lon_col = f'"{schema["lon"]}"'
    # Step 1: swap_latlon exchanges the two coordinate columns.
    if findings["swap_latlon"]:
        lat_col, lon_col = lon_col, lat_col
    # Timestamp per the M0-E decisions.
    if findings["ts_source"] == "ist_columns":
        ts_sql = (f"epoch(TRY_STRPTIME(\"{schema['date_ist']}\" || ' ' || \"{schema['time_ist']}\", "
                  f"'%Y-%m-%d %H:%M:%S')) - {config.IST_OFFSET_S}")
    elif findings["ts_unit"] == "ms":
        ts_sql = f"TRY_CAST(\"{schema['ts']}\" AS DOUBLE) / 1000.0"
    else:
        ts_sql = f"TRY_CAST(\"{schema['ts']}\" AS DOUBLE)"
    trip_sql = "NULL"
    if schema["trip_id"] is not None:
        trip_sql = f"NULLIF(trim(\"{schema['trip_id']}\"), '')"

    # Step 1: typed read of the day, keeping SQLite rowid for de-duplication order.
    con.execute(
        "CREATE TEMP TABLE raw AS SELECT rowid AS rid, "
        f"NULLIF(trim(\"{schema['vehicle_id']}\"), '') AS vehicle_id, "
        f"TRY_CAST({lat_col} AS DOUBLE) AS lat, TRY_CAST({lon_col} AS DOUBLE) AS lon, "
        f"CAST(round({ts_sql}) AS BIGINT) AS ts, "
        f"NULLIF(trim(\"{schema['route_id']}\"), '') AS route_raw, {trip_sql} AS trip_raw "
        f'FROM src."{table}"'
    )
    con.execute("DETACH src")
    rows_read = con.execute("SELECT count(*) FROM raw").fetchone()[0]

    # Step 2: drop nulls, points outside the box, and points whose IST date differs.
    con.execute(
        "CREATE TEMP TABLE s2 AS SELECT * FROM raw WHERE vehicle_id IS NOT NULL "
        "AND lat IS NOT NULL AND lon IS NOT NULL AND ts IS NOT NULL "
        "AND lat BETWEEN ? AND ? AND lon BETWEEN ? AND ? "
        f"AND CAST(to_timestamp(ts + {config.IST_OFFSET_S}) AS DATE) = DATE '{date_iso}'",
        [config.BBOX_LAT_MIN, config.BBOX_LAT_MAX, config.BBOX_LON_MIN, config.BBOX_LON_MAX],
    )
    con.execute("DROP TABLE raw")
    rows_step2 = con.execute("SELECT count(*) FROM s2").fetchone()[0]

    # Step 3: route key -> route index. When several routes share a key, the route with
    # the most scheduled trips wins.
    route_trips = np.zeros(net.n_routes, dtype=np.int64)
    np.add.at(route_trips, net.pat_route, net.pat_trips)
    keys = pa.table({"route_key": [str(k) for k in net.route_key],
                     "route_idx": np.arange(net.n_routes, dtype=np.int32),
                     "trips": route_trips})
    con.register("route_keys", keys)
    con.execute(
        "CREATE TEMP TABLE key_map AS SELECT route_key, arg_max(route_idx, trips) AS route_idx "
        "FROM route_keys GROUP BY route_key"
    )
    norm_sql = config.ROUTE_NORM_SQL.replace("{c}", "route_raw")
    if findings["route_key"] == "via_trip_id" or findings["backfill_route_from_trip"]:
        # Route from trips.txt by trip_id (always for via_trip_id, else only for nulls).
        trips_path = (config.GTFS_DIR / "trips.txt").relative_to(config.ROOT).as_posix()
        con.execute(
            "CREATE TEMP TABLE trip_route AS SELECT trim(trip_id) AS trip_id, "
            f"trim(route_id) AS route_id FROM read_csv('{trips_path}', all_varchar=true, header=true)"
        )
        if findings["route_key"] == "via_trip_id":
            key_sql = "t.route_id"
        else:
            key_sql = f"COALESCE({norm_sql}, t.route_id)"
        con.execute(
            f"CREATE TEMP TABLE s3 AS SELECT s.rid, s.vehicle_id, s.lat, s.lon, s.ts, k.route_idx "
            f"FROM s2 s LEFT JOIN trip_route t ON s.trip_raw = t.trip_id "
            f"JOIN key_map k ON k.route_key = {key_sql}"
        )
    else:
        con.execute(
            "CREATE TEMP TABLE s3 AS SELECT s.rid, s.vehicle_id, s.lat, s.lon, s.ts, k.route_idx "
            f"FROM s2 s JOIN key_map k ON k.route_key = {norm_sql}"
        )
    con.execute("DROP TABLE s2")
    rows_step3 = con.execute("SELECT count(*) FROM s3").fetchone()[0]

    # Step 4: one row per (vehicle_id, ts), the one read first from SQLite.
    k_x = geo.COS_LAT0 * config.METRES_PER_DEG
    con.execute(
        "CREATE TEMP TABLE s4 AS SELECT vehicle_id, route_idx, ts, lat, lon, "
        f"CAST((lon - {config.LON0}) * {k_x} AS DOUBLE) AS x, "
        f"CAST((lat - {config.LAT0}) * {config.METRES_PER_DEG} AS DOUBLE) AS y FROM ("
        "  SELECT *, row_number() OVER (PARTITION BY vehicle_id, ts ORDER BY rid) AS rn FROM s3"
        ") WHERE rn = 1"
    )
    con.execute("DROP TABLE s3")
    rows_step4 = con.execute("SELECT count(*) FROM s4").fetchone()[0]

    # Step 5: drop a point when the speed both into it and out of it exceeds 30 m/s.
    # Rows with no neighbour keep themselves because NULL > 30 is not TRUE.
    con.execute(
        "CREATE TEMP TABLE s5 AS SELECT vehicle_id, route_idx, ts, lat, lon, x, y FROM ("
        "  SELECT *, sqrt(power(x - LAG(x) OVER w, 2) + power(y - LAG(y) OVER w, 2)) "
        "         / greatest(ts - LAG(ts) OVER w, 1) AS v_in, "
        "         sqrt(power(LEAD(x) OVER w - x, 2) + power(LEAD(y) OVER w - y, 2)) "
        "         / greatest(LEAD(ts) OVER w - ts, 1) AS v_out "
        "  FROM s4 WINDOW w AS (PARTITION BY vehicle_id ORDER BY ts)"
        f") WHERE NOT COALESCE(v_in > {config.SPIKE_SPEED_MPS} AND v_out > {config.SPIKE_SPEED_MPS}, FALSE)"
    )
    con.execute("DROP TABLE s4")
    rows_step5 = con.execute("SELECT count(*) FROM s5").fetchone()[0]

    # Step 6: stationary thinning keeps the first and last point of every standstill.
    con.execute(
        "CREATE TEMP TABLE s6 AS SELECT vehicle_id, route_idx, ts, lat, lon, x, y FROM ("
        "  SELECT *, sqrt(power(x - LAG(x) OVER w, 2) + power(y - LAG(y) OVER w, 2)) AS d_prev, "
        "         sqrt(power(LEAD(x) OVER w - x, 2) + power(LEAD(y) OVER w - y, 2)) AS d_next "
        "  FROM s5 WINDOW w AS (PARTITION BY vehicle_id ORDER BY ts)"
        f") WHERE NOT COALESCE(d_prev < {config.STATIONARY_M} AND d_next < {config.STATIONARY_M}, FALSE)"
    )
    con.execute("DROP TABLE s5")
    row = con.execute("SELECT count(*), count(DISTINCT vehicle_id) FROM s6").fetchone()
    rows_step6 = row[0]
    vehicles = row[1]

    # Step 7: write sorted Parquet through a .tmp name.
    tmp_path = out_path.with_suffix(".parquet.tmp")
    con.execute(
        "COPY (SELECT vehicle_id, CAST(route_idx AS INTEGER) AS route_idx, ts, "
        f"CAST((ts + {config.IST_OFFSET_S}) % {config.SECONDS_PER_DAY} AS INTEGER) AS sod, "
        "lat, lon, CAST(x AS FLOAT) AS x, CAST(y AS FLOAT) AS y FROM s6 "
        "ORDER BY route_idx, vehicle_id, ts) "
        f"TO '{tmp_path.relative_to(config.ROOT).as_posix()}' (FORMAT parquet, COMPRESSION zstd)"
    )
    con.close()
    tmp_path.replace(out_path)

    # Step 8: stats record.
    record = {
        "step": "ingest", "date": date_iso, "rows_read": int(rows_read),
        "rows_after_filters": int(rows_step2), "rows_after_route": int(rows_step3),
        "rows_after_dedup": int(rows_step4), "rows_after_spike": int(rows_step5),
        "rows_after_thinning": int(rows_step6), "vehicles": int(vehicles),
        "seconds": round(time.time() - started, 1),
    }
    append_stats(record)
    log(f"ingest {date_iso}: {rows_read:,} -> {rows_step6:,} rows in {record['seconds']} s")
    return record


def main() -> None:
    """Command-line entry point.

    Returns:
        None.
    """
    parser = argparse.ArgumentParser(description="Ingest one day of GPS (SPEC 6.3).")
    parser.add_argument("--date", required=True, help="YYYY-MM-DD")
    args = parser.parse_args()
    ingest_day(args.date, Network(), config.load_findings())


if __name__ == "__main__":
    main()
