"""
pipeline/m0_inspect.py - Milestone 0 data inspection (SPEC 6.0).

Purpose: measure the real GPS schema and static GTFS, apply every decision rule of
SPEC 6.0, and record the results for all later steps.
Inputs: data/raw/sqlite/bus_movements_YYYY_MM_DD.db, data/raw/gtfs/*.txt.
Outputs: reports/m0_findings.json (machine-readable), reports/m0_report.md (human).
Run: python -m pipeline.m0_inspect
"""

import argparse
import datetime as dt_mod
import json
import re
import statistics
import sys
import time
from pathlib import Path

import duckdb
import numpy as np

import config
from pipeline import geo

# Candidate column names for each canonical name (SPEC 6.0 M0-B).
COLUMN_CANDIDATES = {
    "vehicle_id": ["vehicle_id", "vehicleid", "vehicle", "bus_id", "device_id"],
    "lat": ["lat", "latitude"],
    "lon": ["lng", "lon", "long", "longitude"],
    "ts": ["timestamp", "ts", "unix_timestamp", "time_stamp", "epoch"],
    "route_id": ["route_id", "routeid", "route"],
    "trip_id": ["trip_id", "tripid", "trip"],
    "speed": ["speed", "velocity"],
    "date_ist": ["date"],
    "time_ist": ["time"],
}
REQUIRED_COLUMNS = ["vehicle_id", "lat", "lon", "route_id"]
GTFS_REQUIRED = ["stops.txt", "routes.txt", "trips.txt", "stop_times.txt"]
GTFS_ALL = GTFS_REQUIRED + ["shapes.txt", "calendar.txt"]
LADDER = ["route_id", "route_short_name", "route_long_name", "via_trip_id"]
DB_NAME_PATTERN = re.compile(r"bus_movements_(\d{4})_(\d{2})_(\d{2})\.db$")


class StopAndReport(Exception):
    """Raised when a SPEC 6.0 rule says STOP AND REPORT."""


def log(message: str) -> None:
    """Print one progress line immediately.

    Args:
        message: text to print.
    Returns:
        None.
    """
    print(message, flush=True)


def connect() -> duckdb.DuckDBPyConnection:
    """Open an in-memory DuckDB session with the SPEC 3 settings and sqlite support.

    Returns:
        The connection.
    """
    config.TMP_DIR.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    for statement in config.duckdb_settings():
        con.execute(statement)
    # Read every SQLite column as text so odd types never break the scan (M0-B).
    con.execute("INSTALL sqlite")
    con.execute("LOAD sqlite")
    con.execute("SET sqlite_all_varchar=true")
    return con


def db_path_for(date_iso: str) -> Path:
    """Path of the daily database for a date.

    Args:
        date_iso: YYYY-MM-DD.
    Returns:
        The .db path.
    """
    name = "bus_movements_" + date_iso.replace("-", "_") + ".db"
    return config.SQLITE_DIR / name


def attach(con: duckdb.DuckDBPyConnection, date_iso: str, alias: str) -> None:
    """Attach one day's SQLite file read-only under an alias.

    Args:
        con: DuckDB connection.
        date_iso: date of the file.
        alias: catalog name to use.
    Returns:
        None.
    """
    path = db_path_for(date_iso).relative_to(config.ROOT).as_posix()
    con.execute(f"ATTACH '{path}' AS {alias} (TYPE sqlite, READ_ONLY)")


def detach(con: duckdb.DuckDBPyConnection, alias: str) -> None:
    """Detach an attached database.

    Args:
        con: DuckDB connection.
        alias: catalog name.
    Returns:
        None.
    """
    con.execute(f"DETACH {alias}")


def norm(column_sql: str) -> str:
    """SQL expression normalising a route key (see config.ROUTE_NORM_SQL).

    Args:
        column_sql: SQL expression of the raw value.
    Returns:
        SQL text.
    """
    return config.ROUTE_NORM_SQL.replace("{c}", column_sql)


def weekday_of(date_iso: str) -> int:
    """Python weekday number (Monday 0) of a date.

    Args:
        date_iso: YYYY-MM-DD.
    Returns:
        0..6.
    """
    return dt_mod.date.fromisoformat(date_iso).weekday()


def nearest_same_weekday(target: str, available: list[str]) -> str | None:
    """Pick the available date of the same weekday closest to target.

    Args:
        target: wanted date.
        available: dates that exist.
    Returns:
        The chosen date, or None when no date of that weekday exists.
    """
    if target in available:
        return target
    target_date = dt_mod.date.fromisoformat(target)
    best = None
    best_gap = 10**9
    for candidate in available:
        candidate_date = dt_mod.date.fromisoformat(candidate)
        if candidate_date.weekday() != target_date.weekday():
            continue
        gap = abs((candidate_date - target_date).days)
        if gap < best_gap:
            best_gap = gap
            best = candidate
    return best


# ---------------------------------------------------------------------------
# M0-A and M0-C: available dates, row counts, date sets
# ---------------------------------------------------------------------------

def list_db_dates() -> list[str]:
    """Parse dates from the daily database file names (M0-A).

    Returns:
        Sorted list of YYYY-MM-DD strings.
    """
    dates = []
    for path in sorted(config.SQLITE_DIR.glob("*.db")):
        match = DB_NAME_PATTERN.search(path.name)
        if match is None:
            continue
        dates.append(f"{match.group(1)}-{match.group(2)}-{match.group(3)}")
    return sorted(dates)


def choose_date_sets(available: list[str], warnings: list[str]) -> dict:
    """Apply the M0-A rules to pick train, test, replay, and dev dates.

    Args:
        available: usable dates after the M0-C filter.
        warnings: list that receives WARNING lines.
    Returns:
        Dict with train_dates, test_dates, replay_date, dev_date.
    """
    test_present = [d for d in config.TEST_DATES if d in available]
    # Rule: keep the default split unless fewer than 5 of 7 test dates exist.
    if len(test_present) >= 5:
        test_dates = test_present
        train_dates = [d for d in config.TRAIN_DATES if d in available]
    else:
        test_dates = available[-7:]
        train_dates = [d for d in available if d < test_dates[0]]
        warnings.append("WARNING: fewer than 5 default test dates; test = last 7 dates")

    # Replay date: default, else the latest Tuesday in test, else latest weekday-type.
    replay_date = config.REPLAY_DATE
    if replay_date not in available:
        replay_date = None
        for candidate in reversed(test_dates):
            if weekday_of(candidate) == 1:
                replay_date = candidate
                break
        if replay_date is None:
            for candidate in reversed(test_dates):
                if config.day_type_of(candidate) == 0:
                    replay_date = candidate
                    break

    # Dev date: default, else the earliest weekday-type date.
    dev_date = config.DEV_DATE
    if dev_date not in available:
        dev_date = None
        for candidate in available:
            if config.day_type_of(candidate) == 0:
                dev_date = candidate
                break

    if len(train_dates) < config.MIN_TRAIN_DATES:
        raise StopAndReport(f"only {len(train_dates)} train dates: {train_dates}")
    result = {
        "train_dates": train_dates,
        "test_dates": test_dates,
        "replay_date": replay_date,
        "dev_date": dev_date,
    }
    return result


# ---------------------------------------------------------------------------
# M0-B: schema
# ---------------------------------------------------------------------------

def inspect_schema(con: duckdb.DuckDBPyConnection, alias: str) -> tuple[str, dict, list]:
    """Find the data table and map canonical names to real columns (M0-B).

    Args:
        con: connection with the day attached under alias.
        alias: catalog name.
    Returns:
        Tuple (table_name, schema_map, column_list).
    """
    # List all tables of the attached file.
    tables = con.execute(
        "SELECT DISTINCT table_name FROM duckdb_columns() WHERE database_name = ?",
        [alias],
    ).fetchall()
    table_names = [row[0] for row in tables]
    if len(table_names) == 0:
        raise StopAndReport("the SQLite file has no tables")
    table_name = table_names[0]
    if len(table_names) > 1:
        # Several tables: use the one with the most rows.
        best_rows = -1
        for name in table_names:
            rows = con.execute(f'SELECT count(*) FROM {alias}."{name}"').fetchone()[0]
            if rows > best_rows:
                best_rows = rows
                table_name = name

    # Column names of the chosen table, in file order.
    column_rows = con.execute(
        "SELECT column_name FROM duckdb_columns() "
        "WHERE database_name = ? AND table_name = ? ORDER BY column_index",
        [alias, table_name],
    ).fetchall()
    columns = [row[0] for row in column_rows]
    lower_to_actual = {}
    for column in columns:
        lower_to_actual[column.lower()] = column

    # Case-insensitive lookup of each canonical name's candidates, first hit wins.
    schema_map = {}
    for canonical, candidates in COLUMN_CANDIDATES.items():
        schema_map[canonical] = None
        for candidate in candidates:
            if candidate in lower_to_actual:
                schema_map[canonical] = lower_to_actual[candidate]
                break
    missing = [name for name in REQUIRED_COLUMNS if schema_map[name] is None]
    if len(missing) > 0:
        raise StopAndReport(f"cannot map {missing}; columns are {columns}")
    return table_name, schema_map, columns


def col(schema_map: dict, canonical: str) -> str:
    """Quoted SQL identifier of a mapped column, or NULL when unmapped.

    Args:
        schema_map: canonical -> actual column.
        canonical: canonical name.
    Returns:
        SQL text.
    """
    actual = schema_map[canonical]
    if actual is None:
        return "NULL"
    return f'"{actual}"'


# ---------------------------------------------------------------------------
# GTFS helpers
# ---------------------------------------------------------------------------

def gtfs_path(name: str) -> str:
    """Relative POSIX path of a GTFS file for DuckDB.

    Args:
        name: file name, for example "stops.txt".
    Returns:
        Path string.
    """
    return (config.GTFS_DIR / name).relative_to(config.ROOT).as_posix()


def load_gtfs_tables(con: duckdb.DuckDBPyConnection) -> None:
    """Load routes and trips as text tables g_routes and g_trips.

    Args:
        con: connection.
    Returns:
        None.
    """
    # All columns as text; ids are trimmed at use.
    con.execute(
        "CREATE OR REPLACE TABLE g_routes AS SELECT * FROM "
        f"read_csv('{gtfs_path('routes.txt')}', all_varchar=true, header=true)"
    )
    con.execute(
        "CREATE OR REPLACE TABLE g_trips AS SELECT * FROM "
        f"read_csv('{gtfs_path('trips.txt')}', all_varchar=true, header=true)"
    )


# ---------------------------------------------------------------------------
# Per sample day checks (M0-D to M0-L, M0-O)
# ---------------------------------------------------------------------------

def load_sample_day(con: duckdb.DuckDBPyConnection, alias: str, table_name: str,
                    schema_map: dict) -> None:
    """Copy one day into a typed temp table s for repeated checks.

    Args:
        con: connection with the day attached.
        alias: catalog of the day.
        table_name: data table.
        schema_map: canonical -> actual column.
    Returns:
        None. Creates table s (vehicle_id, lat, lon, ts_raw, route_raw, trip_raw,
        speed, date_raw, time_raw).
    """
    # One scan of the SQLite file; numbers are TRY_CAST so bad text becomes NULL.
    con.execute(
        "CREATE OR REPLACE TEMP TABLE s AS SELECT "
        f"NULLIF(trim({col(schema_map, 'vehicle_id')}), '') AS vehicle_id, "
        f"TRY_CAST({col(schema_map, 'lat')} AS DOUBLE) AS lat, "
        f"TRY_CAST({col(schema_map, 'lon')} AS DOUBLE) AS lon, "
        f"TRY_CAST({col(schema_map, 'ts')} AS DOUBLE) AS ts_raw, "
        f"NULLIF(trim({col(schema_map, 'route_id')}), '') AS route_raw, "
        f"NULLIF(trim({col(schema_map, 'trip_id')}), '') AS trip_raw, "
        f"TRY_CAST({col(schema_map, 'speed')} AS DOUBLE) AS speed, "
        f"{col(schema_map, 'date_ist')} AS date_raw, "
        f"{col(schema_map, 'time_ist')} AS time_raw "
        f'FROM {alias}."{table_name}"'
    )


def null_rates(con: duckdb.DuckDBPyConnection, alias: str, table_name: str,
               schema_map: dict) -> dict:
    """Share of NULL or empty values per mapped column (M0-D).

    Args:
        con: connection with the day attached.
        alias: catalog of the day.
        table_name: data table.
        schema_map: canonical -> actual column.
    Returns:
        Dict canonical name -> rate.
    """
    parts = []
    names = []
    for canonical, actual in schema_map.items():
        if actual is None:
            continue
        names.append(canonical)
        # NULL and empty strings both count as missing.
        parts.append(f"avg(CASE WHEN \"{actual}\" IS NULL OR trim(\"{actual}\") = '' "
                     "THEN 1.0 ELSE 0.0 END)")
    row = con.execute(f'SELECT {", ".join(parts)} FROM {alias}."{table_name}"').fetchone()
    rates = {}
    for index, name in enumerate(names):
        rates[name] = float(row[index])
    return rates


def ts_expression(ts_unit: str, ts_source: str) -> str:
    """SQL expression for Unix seconds from table s, given the M0-E decisions.

    Args:
        ts_unit: "s" or "ms".
        ts_source: "ts" or "ist_columns".
    Returns:
        SQL text producing seconds as DOUBLE.
    """
    if ts_source == "ist_columns":
        # IST wall clock parsed as if UTC, then shifted back by 5:30.
        return ("epoch(TRY_STRPTIME(date_raw || ' ' || time_raw, '%Y-%m-%d %H:%M:%S')) "
                f"- {config.IST_OFFSET_S}")
    if ts_unit == "ms":
        return "ts_raw / 1000.0"
    return "ts_raw"


def check_day(con: duckdb.DuckDBPyConnection, date_iso: str, warnings: list[str],
              has_ist_columns: bool) -> dict:
    """Run M0-E, F, G, J, K, O and the per-day parts of H and I on table s.

    Args:
        con: connection with table s loaded and g_routes, g_trips present.
        date_iso: the file date.
        warnings: list that receives WARNING lines.
        has_ist_columns: True when date and time columns are mapped.
    Returns:
        Dict of measured values for this day.
    """
    out = {"date": date_iso}

    # M0-E: unit from the median, then the share of rows on the file date.
    median_ts = con.execute("SELECT median(ts_raw) FROM s").fetchone()[0]
    ts_unit = "s"
    if median_ts is not None and median_ts > config.M0_TS_MS_THRESHOLD:
        ts_unit = "ms"
    out["ts_unit"] = ts_unit
    seconds_sql = ts_expression(ts_unit, "ts")
    date_sql = f"CAST(to_timestamp(({seconds_sql}) + {config.IST_OFFSET_S}) AS DATE)"
    row = con.execute(
        f"SELECT min({seconds_sql}), max({seconds_sql}), "
        f"avg(CASE WHEN {date_sql} = DATE '{date_iso}' THEN 1.0 ELSE 0.0 END) "
        "FROM s WHERE ts_raw IS NOT NULL"
    ).fetchone()
    out["ts_min"] = row[0]
    out["ts_max"] = row[1]
    out["ts_date_match"] = float(row[2])
    out["ts_source"] = "ts"
    if out["ts_date_match"] < config.M0_DATE_MATCH_MIN:
        out["ts_source"] = None
        if has_ist_columns:
            ist_sql = ts_expression(ts_unit, "ist_columns")
            ist_date = f"CAST(to_timestamp(({ist_sql}) + {config.IST_OFFSET_S}) AS DATE)"
            share = con.execute(
                f"SELECT avg(CASE WHEN {ist_date} = DATE '{date_iso}' THEN 1.0 ELSE 0.0 END) "
                "FROM s"
            ).fetchone()[0]
            out["ist_date_match"] = float(share)
            if share >= config.M0_DATE_MATCH_MIN:
                out["ts_source"] = "ist_columns"
    if out["ts_source"] is None:
        raise StopAndReport(f"{date_iso}: timestamps do not match the file date "
                            f"(share {out['ts_date_match']:.3f})")
    seconds_sql = ts_expression(ts_unit, out["ts_source"])

    # Hourly IST histogram and the quietest 3-hour window (circular).
    hist_rows = con.execute(
        f"SELECT CAST(floor((({seconds_sql}) + {config.IST_OFFSET_S}) / 3600) AS BIGINT) % 24 "
        "AS h, count(*) FROM s WHERE ts_raw IS NOT NULL GROUP BY 1 ORDER BY 1"
    ).fetchall()
    hist = [0] * 24
    for hour, count in hist_rows:
        hist[int(hour)] = int(count)
    out["hourly_rows_ist"] = hist
    quiet_start = 0
    quiet_sum = None
    for start in range(24):
        window = hist[start] + hist[(start + 1) % 24] + hist[(start + 2) % 24]
        if quiet_sum is None or window < quiet_sum:
            quiet_sum = window
            quiet_start = start
    out["quietest_3h_start"] = quiet_start
    if quiet_start not in [23, 0, 1, 2, 3]:
        warnings.append(f"WARNING: {date_iso} quietest 3-hour window starts at "
                        f"{quiet_start:02d}:00 IST, not between 23:00 and 03:00")

    # Build a typed view used by the remaining checks.
    con.execute(
        "CREATE OR REPLACE TEMP VIEW sv AS SELECT vehicle_id, lat, lon, speed, route_raw, "
        f"trip_raw, CAST(round({seconds_sql}) AS BIGINT) AS ts FROM s"
    )

    # M0-F: duplicate share on (vehicle_id, ts).
    row = con.execute(
        "SELECT count(*), count(DISTINCT (vehicle_id, ts)) FROM sv "
        "WHERE vehicle_id IS NOT NULL AND ts IS NOT NULL"
    ).fetchone()
    out["rows_valid_key"] = int(row[0])
    out["dup_rate"] = 1.0 - row[1] / max(row[0], 1)

    # M0-K: bounding box share, and the swapped test.
    row = con.execute(
        "SELECT avg(CASE WHEN lat BETWEEN ? AND ? AND lon BETWEEN ? AND ? THEN 1.0 ELSE 0.0 END), "
        "avg(CASE WHEN lon BETWEEN ? AND ? AND lat BETWEEN ? AND ? THEN 1.0 ELSE 0.0 END) "
        "FROM sv WHERE lat IS NOT NULL AND lon IS NOT NULL",
        [config.BBOX_LAT_MIN, config.BBOX_LAT_MAX, config.BBOX_LON_MIN, config.BBOX_LON_MAX,
         config.BBOX_LAT_MIN, config.BBOX_LAT_MAX, config.BBOX_LON_MIN, config.BBOX_LON_MAX],
    ).fetchone()
    out["bbox_rate"] = float(row[0])
    out["bbox_rate_swapped"] = float(row[1])

    # M0-G: gaps between consecutive distinct timestamps of the same vehicle.
    # M0-J: reported speed / derived speed for short, moving pairs.
    k = geo.COS_LAT0 * config.METRES_PER_DEG
    m = config.METRES_PER_DEG
    con.execute(
        "CREATE OR REPLACE TEMP TABLE pairs AS "
        "WITH d AS (SELECT DISTINCT ON (vehicle_id, ts) vehicle_id, ts, lat, lon, speed "
        "           FROM sv WHERE vehicle_id IS NOT NULL AND ts IS NOT NULL "
        "           AND lat IS NOT NULL AND lon IS NOT NULL) "
        "SELECT ts - LAG(ts) OVER w AS gap_s, "
        f"sqrt(power((lon - LAG(lon) OVER w) * {k}, 2) + power((lat - LAG(lat) OVER w) * {m}, 2)) "
        "AS move_m, speed FROM d WINDOW w AS (PARTITION BY vehicle_id ORDER BY ts)"
    )
    row = con.execute(
        "SELECT median(gap_s), quantile_cont(gap_s, 0.9) FROM pairs WHERE gap_s > 0"
    ).fetchone()
    out["median_gap_s"] = float(row[0])
    out["p90_gap_s"] = float(row[1])
    row = con.execute(
        "SELECT median(speed / (move_m / gap_s)) FROM ("
        "  SELECT * FROM pairs WHERE gap_s > 0 AND gap_s <= ? AND move_m >= ? "
        "  AND speed IS NOT NULL"
        f") USING SAMPLE reservoir({config.M0_SPEED_PAIRS} ROWS) REPEATABLE ({config.M0_SEED})",
        [config.M0_SPEED_MAX_GAP_S, config.M0_PROX_MOVE_M],
    ).fetchone()
    out["speed_ratio"] = None
    if row[0] is not None:
        out["speed_ratio"] = float(row[0])
    con.execute("DROP TABLE pairs")

    # M0-H: share of rows whose trip_id is a trips.txt trip_id.
    row = con.execute(
        "SELECT count(*), count(*) FILTER (WHERE trip_raw IN (SELECT trim(trip_id) FROM g_trips)) "
        "FROM sv"
    ).fetchone()
    out["n_rows"] = int(row[0])
    out["trip_matches"] = int(row[1])

    # M0-I: per-rung match counts on the normalised route key.
    rung_counts = {}
    for rung in ["route_id", "route_short_name", "route_long_name"]:
        hits = con.execute(
            f"SELECT count(*) FROM sv WHERE {norm('route_raw')} IN "
            f"(SELECT {norm(rung)} FROM g_routes WHERE {rung} IS NOT NULL)"
        ).fetchone()[0]
        rung_counts[rung] = int(hits)
    hits = con.execute(
        "SELECT count(*) FROM sv WHERE trip_raw IN "
        "(SELECT trim(trip_id) FROM g_trips WHERE route_id IN (SELECT route_id FROM g_routes))"
    ).fetchone()[0]
    rung_counts["via_trip_id"] = int(hits)
    out["rung_counts"] = rung_counts

    # M0-O: distinct vehicles and routes.
    row = con.execute("SELECT count(DISTINCT vehicle_id), count(DISTINCT route_raw) FROM sv").fetchone()
    out["vehicles"] = int(row[0])
    out["routes"] = int(row[1])
    return out


# ---------------------------------------------------------------------------
# M0-M: static GTFS
# ---------------------------------------------------------------------------

def check_gtfs(con: duckdb.DuckDBPyConnection) -> dict:
    """Inspect the static GTFS files (M0-M).

    Args:
        con: connection with g_routes and g_trips loaded.
    Returns:
        Dict with gtfs_counts, stop_times_null_arrival_rate, shape_coverage, use_shapes,
        service_filter, calendar_present, distinct_service_ids, max_arrival_hour.
    """
    counts = {}
    for name in GTFS_ALL:
        path = config.GTFS_DIR / name
        if not path.exists():
            counts[name] = None
            continue
        counts[name] = int(con.execute(
            f"SELECT count(*) FROM read_csv('{gtfs_path(name)}', all_varchar=true, header=true)"
        ).fetchone()[0])
    out = {"gtfs_counts": counts}

    # Arrival time nulls and the latest hour, straight from the text column.
    row = con.execute(
        "SELECT avg(CASE WHEN arrival_time IS NULL OR trim(arrival_time) = '' THEN 1.0 ELSE 0.0 END), "
        "max(TRY_CAST(split_part(trim(arrival_time), ':', 1) AS INTEGER)) "
        f"FROM read_csv('{gtfs_path('stop_times.txt')}', all_varchar=true, header=true)"
    ).fetchone()
    out["stop_times_null_arrival_rate"] = float(row[0])
    out["max_arrival_hour"] = int(row[1])

    # Share of trips whose shape_id exists in shapes.txt.
    shape_coverage = 0.0
    if counts["shapes.txt"] is not None:
        shape_coverage = con.execute(
            "SELECT avg(CASE WHEN trim(t.shape_id) IN (SELECT DISTINCT trim(shape_id) FROM "
            f"read_csv('{gtfs_path('shapes.txt')}', all_varchar=true, header=true)) "
            "THEN 1.0 ELSE 0.0 END) FROM g_trips t"
        ).fetchone()[0]
        shape_coverage = float(shape_coverage)
    out["shape_coverage"] = shape_coverage
    out["use_shapes"] = True
    if counts["shapes.txt"] is None or shape_coverage < config.M0_SHAPE_COVERAGE_MIN:
        out["use_shapes"] = False

    # Service filter: only when calendar.txt exists and trips use several service_ids.
    rows = con.execute(
        "SELECT trim(service_id), count(*) AS n FROM g_trips GROUP BY 1 ORDER BY n DESC, 1"
    ).fetchall()
    out["calendar_present"] = counts["calendar.txt"] is not None
    out["distinct_service_ids"] = len(rows)
    out["service_filter"] = None
    if out["calendar_present"] and len(rows) > 1:
        out["service_filter"] = rows[0][0]
    return out


# ---------------------------------------------------------------------------
# M0-N: route proximity
# ---------------------------------------------------------------------------

def route_proximity(con: duckdb.DuckDBPyConnection, dev_date: str, table_name: str,
                    schema_map: dict, rung: str, ts_unit: str, ts_source: str) -> dict:
    """Share of moving GPS points within 150 m of a stop of their route (M0-N).

    Args:
        con: connection with g_routes and g_trips loaded.
        dev_date: date to sample.
        table_name: data table.
        schema_map: canonical -> actual column.
        rung: route_key under test.
        ts_unit, ts_source: timestamp decisions from M0-E.
    Returns:
        Dict with route_proximity_rate (median) and per-route rates.
    """
    attach(con, dev_date, "dv")
    load_sample_day(con, "dv", table_name, schema_map)
    detach(con, "dv")
    seconds_sql = ts_expression(ts_unit, ts_source)

    # GTFS route index per normalised key; via_trip_id resolves through trips.txt.
    if rung == "via_trip_id":
        key_sql = "trim(t.route_id)"
        con.execute(
            "CREATE OR REPLACE TEMP TABLE pts AS SELECT s.vehicle_id, s.lat, s.lon, "
            f"CAST(round({seconds_sql}) AS BIGINT) AS ts, {key_sql} AS rkey "
            "FROM s JOIN g_trips t ON s.trip_raw = trim(t.trip_id) "
            "WHERE s.lat IS NOT NULL AND s.lon IS NOT NULL"
        )
        con.execute(
            "CREATE OR REPLACE TEMP TABLE rmap AS SELECT DISTINCT trim(route_id) AS rkey, "
            "trim(route_id) AS gtfs_route FROM g_routes"
        )
    else:
        con.execute(
            "CREATE OR REPLACE TEMP TABLE pts AS SELECT vehicle_id, lat, lon, "
            f"CAST(round({seconds_sql}) AS BIGINT) AS ts, {norm('route_raw')} AS rkey "
            "FROM s WHERE lat IS NOT NULL AND lon IS NOT NULL"
        )
        con.execute(
            f"CREATE OR REPLACE TEMP TABLE rmap AS SELECT DISTINCT {norm(rung)} AS rkey, "
            f"trim(route_id) AS gtfs_route FROM g_routes WHERE {rung} IS NOT NULL"
        )
    con.execute("DROP TABLE s")

    # Routes (keys) with at least 5,000 rows that join to GTFS.
    keys = con.execute(
        "SELECT rkey FROM pts WHERE rkey IN (SELECT rkey FROM rmap) "
        "GROUP BY rkey HAVING count(*) >= ? ORDER BY rkey",
        [config.M0_PROX_MIN_ROWS],
    ).fetchall()
    keys = [row[0] for row in keys]
    rng = np.random.default_rng(config.M0_SEED)
    n_pick = min(config.M0_PROX_ROUTES, len(keys))
    picked = []
    if n_pick > 0:
        picked_index = rng.choice(len(keys), size=n_pick, replace=False)
        picked = [keys[int(i)] for i in sorted(picked_index)]

    # Stops of each GTFS route, from stop_times of its trips.
    con.execute(
        "CREATE OR REPLACE TEMP TABLE route_stops AS "
        "SELECT DISTINCT trim(t.route_id) AS gtfs_route, CAST(st.stop_lat AS DOUBLE) AS lat, "
        "CAST(st.stop_lon AS DOUBLE) AS lon FROM g_trips t JOIN ("
        "  SELECT DISTINCT trim(x.trip_id) AS trip_id, y.stop_lat, y.stop_lon FROM "
        f"  read_csv('{gtfs_path('stop_times.txt')}', all_varchar=true, header=true) x JOIN "
        f"  read_csv('{gtfs_path('stops.txt')}', all_varchar=true, header=true) y "
        "  ON trim(x.stop_id) = trim(y.stop_id)"
        ") st ON trim(t.trip_id) = st.trip_id"
    )

    k = geo.COS_LAT0 * config.METRES_PER_DEG
    m = config.METRES_PER_DEG
    per_route = {}
    for key in picked:
        # Moving points of this route: at least 20 m from the previous point of the bus.
        points = con.execute(
            "SELECT lat, lon FROM ("
            "  SELECT lat, lon, sqrt(power((lon - LAG(lon) OVER w) * ?, 2) + "
            "  power((lat - LAG(lat) OVER w) * ?, 2)) AS move_m FROM pts WHERE rkey = ? "
            "  WINDOW w AS (PARTITION BY vehicle_id ORDER BY ts)"
            ") WHERE move_m >= ? "
            f"USING SAMPLE reservoir({config.M0_PROX_POINTS} ROWS) REPEATABLE ({config.M0_SEED})",
            [k, m, key, config.M0_PROX_MOVE_M],
        ).fetchnumpy()
        stops = con.execute(
            "SELECT lat, lon FROM route_stops WHERE gtfs_route IN "
            "(SELECT gtfs_route FROM rmap WHERE rkey = ?)",
            [key],
        ).fetchnumpy()
        if len(points["lat"]) == 0 or len(stops["lat"]) == 0:
            per_route[key] = 0.0
            continue
        px, py = geo.to_xy(points["lat"], points["lon"])
        sx, sy = geo.to_xy(stops["lat"], stops["lon"])
        # Distance from every point to its nearest stop; sizes are at most 10k x a few
        # hundred, so one broadcast is cheap.
        dx = px[:, None] - sx[None, :]
        dy = py[:, None] - sy[None, :]
        nearest = np.sqrt(np.min(dx * dx + dy * dy, axis=1))
        per_route[key] = float(np.mean(nearest <= config.M0_PROX_RADIUS_M))
    con.execute("DROP TABLE pts")
    rate = 0.0
    if len(per_route) > 0:
        rate = float(statistics.median(per_route.values()))
    return {"route_proximity_rate": rate, "per_route": per_route}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run() -> dict:
    """Run every Milestone 0 check and apply its decision rule.

    Returns:
        The findings dict (also written to disk).
    """
    started = time.time()
    warnings = []
    decisions = []
    con = connect()

    # Expected inputs must exist (A5).
    missing = [n for n in GTFS_REQUIRED if not (config.GTFS_DIR / n).exists()]
    if len(list_db_dates()) == 0 or len(missing) > 0:
        print(f"Expected archive databases in: {config.SQLITE_DIR}")
        print(f"Expected GTFS files in: {config.GTFS_DIR} (missing: {missing})")
        raise StopAndReport("input data missing")

    # M0-A (part 1): dates in file names.
    all_dates = list_db_dates()
    log(f"M0-A: {len(all_dates)} daily files, {all_dates[0]} .. {all_dates[-1]}")

    # M0-B: schema from the dev date file (or the first file).
    schema_date = config.DEV_DATE
    if schema_date not in all_dates:
        schema_date = all_dates[0]
    attach(con, schema_date, "d0")
    table_name, schema_map, columns = inspect_schema(con, "d0")
    detach(con, "d0")
    log(f"M0-B: table {table_name}, columns {columns}")
    decisions.append(("M0-B", f"table {table_name}; map {schema_map}", "sqlite_reader = duckdb"))

    # M0-C: row count per day; days under 10% of the median are dropped.
    rows_per_day = {}
    for date_iso in all_dates:
        attach(con, date_iso, "dc")
        rows_per_day[date_iso] = int(con.execute(f'SELECT count(*) FROM dc."{table_name}"').fetchone()[0])
        detach(con, "dc")
        log(f"M0-C: {date_iso} rows {rows_per_day[date_iso]:,}")
    median_rows = statistics.median(rows_per_day.values())
    available = [d for d in all_dates if rows_per_day[d] >= config.M0_MIN_DAY_SHARE * median_rows]
    dropped = [d for d in all_dates if d not in available]
    decisions.append(("M0-C", f"median rows {median_rows:,.0f}", f"dropped days {dropped}"))

    # M0-A (part 2): train/test/replay/dev selection.
    date_sets = choose_date_sets(available, warnings)
    decisions.append(("M0-A", f"{len(available)} usable dates",
                      f"train {len(date_sets['train_dates'])}, test {len(date_sets['test_dates'])}, "
                      f"replay {date_sets['replay_date']}, dev {date_sets['dev_date']}"))

    # GTFS tables needed by H and I, and M0-M.
    load_gtfs_tables(con)
    gtfs = check_gtfs(con)
    decisions.append(("M0-M", f"counts {gtfs['gtfs_counts']}, shape coverage "
                      f"{gtfs['shape_coverage']:.3f}, service ids {gtfs['distinct_service_ids']}",
                      f"use_shapes {gtfs['use_shapes']}, service_filter {gtfs['service_filter']}"))

    # Sample days: defaults, or the nearest available date of the same weekday.
    sample_dates = []
    for target in config.M0_SAMPLE_DATES:
        chosen = nearest_same_weekday(target, available)
        if chosen is not None and chosen not in sample_dates:
            sample_dates.append(chosen)
    has_ist_columns = schema_map["date_ist"] is not None and schema_map["time_ist"] is not None

    day_results = []
    day_null_rates = {}
    for date_iso in sample_dates:
        day_started = time.time()
        attach(con, date_iso, "ds")
        day_null_rates[date_iso] = null_rates(con, "ds", table_name, schema_map)
        load_sample_day(con, "ds", table_name, schema_map)
        detach(con, "ds")
        result = check_day(con, date_iso, warnings, has_ist_columns)
        con.execute("DROP VIEW sv")
        con.execute("DROP TABLE s")
        day_results.append(result)
        log(f"sample day {date_iso} checked in {time.time() - day_started:.0f} s")

    # M0-D: mean null rate per column over sample days.
    null_summary = {}
    for column_name in day_null_rates[sample_dates[0]]:
        values = [day_null_rates[d][column_name] for d in sample_dates]
        null_summary[column_name] = float(np.mean(values))

    # M0-E: unit and source must agree across sample days; the first day decides.
    ts_unit = day_results[0]["ts_unit"]
    ts_source = day_results[0]["ts_source"]
    decisions.append(("M0-E", f"date match {[round(r['ts_date_match'], 4) for r in day_results]}",
                      f"ts_unit {ts_unit}, ts_source {ts_source}"))

    # M0-F, M0-G: pooled over sample days.
    dup_rate = float(np.mean([r["dup_rate"] for r in day_results]))
    median_gap_s = float(np.median([r["median_gap_s"] for r in day_results]))
    p90_gap_s = float(np.median([r["p90_gap_s"] for r in day_results]))
    run_gap_s = 600
    if median_gap_s <= config.M0_GAP_FAST_S:
        run_gap_s = 300
    decisions.append(("M0-G", f"median gap {median_gap_s:.1f} s, p90 {p90_gap_s:.1f} s",
                      f"run_gap_s {run_gap_s}"))

    # M0-H: trip join over all sample rows.
    total_rows = sum(r["n_rows"] for r in day_results)
    trip_join_rate = sum(r["trip_matches"] for r in day_results) / max(total_rows, 1)
    trip_id_usable = trip_join_rate >= config.M0_JOIN_GOOD
    decisions.append(("M0-H", f"trip join rate {trip_join_rate:.4f}",
                      f"trip_id_usable {trip_id_usable}"))

    # M0-D rule needs M0-H.
    route_null = null_summary.get("route_id", 0.0)
    backfill = route_null > config.M0_ROUTE_NULL_BACKFILL and trip_id_usable
    decisions.append(("M0-D", f"route_id null rate {route_null:.4f}",
                      f"backfill_route_from_trip {backfill}"))

    # M0-I: join ladder rates; via_trip_id only if trip ids are usable.
    rung_rates = {}
    for rung in LADDER:
        if rung == "via_trip_id" and not trip_id_usable:
            continue
        hits = sum(r["rung_counts"][rung] for r in day_results)
        rung_rates[rung] = hits / max(total_rows, 1)
    ordered_rungs = []
    for rung in LADDER:
        if rung in rung_rates:
            ordered_rungs.append(rung)

    def pick_rung(candidates: list[str]) -> str | None:
        """Apply the M0-I rule to a list of rungs in ladder order.

        Args:
            candidates: rungs still allowed.
        Returns:
            The chosen rung or None when all are below 0.50.
        """
        for rung_name in candidates:
            if rung_rates[rung_name] >= config.M0_JOIN_GOOD:
                return rung_name
        best_name = None
        best_rate = -1.0
        for rung_name in candidates:
            if rung_rates[rung_name] > best_rate:
                best_rate = rung_rates[rung_name]
                best_name = rung_name
        if best_rate >= config.M0_JOIN_MIN:
            warnings.append(f"WARNING: no route rung reached 0.80; using {best_name} ({best_rate:.3f})")
            return best_name
        return None

    route_key = pick_rung(ordered_rungs)
    if route_key is None:
        raise StopAndReport(f"route join rates all below 0.50: {rung_rates}")
    decisions.append(("M0-I", f"rung rates {json.dumps({k: round(v, 4) for k, v in rung_rates.items()})}",
                      f"route_key {route_key} (rate {rung_rates[route_key]:.4f}); "
                      "keys normalised with ROUTE_NORM_SQL (837.0 -> 837)"))

    # M0-N: proximity test, walking down the ladder if the mapping looks wrong.
    proximity = None
    remaining = list(ordered_rungs[ordered_rungs.index(route_key):])
    while True:
        proximity = route_proximity(con, date_sets["dev_date"], table_name, schema_map,
                                    route_key, ts_unit, ts_source)
        rate = proximity["route_proximity_rate"]
        if rate >= config.M0_PROX_MIN:
            break
        remaining = remaining[1:]
        warnings.append(f"WARNING: proximity {rate:.3f} with {route_key}; trying next rung")
        if len(remaining) == 0:
            raise StopAndReport(f"route proximity below 0.30 on every rung (last {rate:.3f})")
        route_key = remaining[0]
    max_offset_m = 100
    if proximity["route_proximity_rate"] < config.M0_PROX_GOOD:
        max_offset_m = 150
        warnings.append(f"WARNING: route proximity {proximity['route_proximity_rate']:.3f} "
                        "is below 0.60; max_offset_m = 150")
    decisions.append(("M0-N", f"median proximity {proximity['route_proximity_rate']:.3f} over "
                      f"{len(proximity['per_route'])} routes", f"max_offset_m {max_offset_m}"))

    # M0-J: speed units, report only.
    ratios = [r["speed_ratio"] for r in day_results if r["speed_ratio"] is not None]
    speed_unit = "unknown"
    if len(ratios) > 0:
        ratio = float(np.median(ratios))
        if 0.8 <= ratio <= 1.25:
            speed_unit = "m/s"
        elif 2.9 <= ratio <= 4.5:
            speed_unit = "km/h"
        elif 1.6 <= ratio <= 2.3:
            speed_unit = "knots"
        decisions.append(("M0-J", f"median reported/derived {ratio:.3f}",
                          f"speed_unit {speed_unit} (not used, D15)"))

    # M0-K: bounding box.
    bbox_rate = float(np.mean([r["bbox_rate"] for r in day_results]))
    swap_latlon = False
    if bbox_rate < config.M0_BBOX_MIN:
        swapped = float(np.mean([r["bbox_rate_swapped"] for r in day_results]))
        if swapped >= config.M0_BBOX_MIN:
            swap_latlon = True
        else:
            raise StopAndReport(f"bbox share {bbox_rate:.3f}, swapped {swapped:.3f}")
    decisions.append(("M0-K", f"bbox share {bbox_rate:.4f}", f"swap_latlon {swap_latlon}"))

    # M0-L: memory estimate for the busiest sample day.
    busiest_rows = max(rows_per_day[d] for d in sample_dates)
    est_day_bytes = busiest_rows * config.M0_BYTES_PER_ROW
    day_chunks = 1
    if est_day_bytes > config.M0_DAY_BYTES_MAX:
        day_chunks = 4
    decisions.append(("M0-L", f"est bytes {est_day_bytes:,}", f"day_chunks {day_chunks}"))

    findings = {
        "available_dates": available,
        "train_dates": date_sets["train_dates"],
        "test_dates": date_sets["test_dates"],
        "replay_date": date_sets["replay_date"],
        "dev_date": date_sets["dev_date"],
        "table_name": table_name,
        "schema_map": schema_map,
        "sqlite_reader": "duckdb",
        "rows_per_day": rows_per_day,
        "null_rates": null_summary,
        "backfill_route_from_trip": backfill,
        "ts_unit": ts_unit,
        "ts_source": ts_source,
        "dup_rate": dup_rate,
        "median_gap_s": median_gap_s,
        "p90_gap_s": p90_gap_s,
        "run_gap_s": run_gap_s,
        "trip_join_rate": trip_join_rate,
        "trip_id_usable": trip_id_usable,
        "route_key": route_key,
        "route_join_rate": rung_rates[route_key],
        "route_rung_rates": rung_rates,
        "route_key_normalisation": "whole numbers written as integers (837.0 -> 837)",
        "speed_unit": speed_unit,
        "bbox_rate": bbox_rate,
        "swap_latlon": swap_latlon,
        "est_day_bytes": est_day_bytes,
        "day_chunks": day_chunks,
        "gtfs_counts": gtfs["gtfs_counts"],
        "stop_times_null_arrival_rate": gtfs["stop_times_null_arrival_rate"],
        "shape_coverage": gtfs["shape_coverage"],
        "use_shapes": gtfs["use_shapes"],
        "service_filter": gtfs["service_filter"],
        "route_proximity_rate": proximity["route_proximity_rate"],
        "max_offset_m": max_offset_m,
        "vehicles_per_day": {r["date"]: r["vehicles"] for r in day_results},
        "routes_per_day": {r["date"]: r["routes"] for r in day_results},
        "sample_dates": sample_dates,
        "warnings": warnings,
    }
    write_outputs(findings, decisions, day_results, proximity, gtfs, time.time() - started)
    return findings


def write_outputs(findings: dict, decisions: list, day_results: list, proximity: dict,
                  gtfs: dict, elapsed_s: float) -> None:
    """Write m0_findings.json and m0_report.md through .tmp names.

    Args:
        findings: the findings dict.
        decisions: list of (check id, measured, decision) tuples.
        day_results: per sample day measurements.
        proximity: M0-N details.
        gtfs: M0-M details.
        elapsed_s: run time.
    Returns:
        None.
    """
    config.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    tmp_json = config.FINDINGS_PATH.with_suffix(".json.tmp")
    with open(tmp_json, "w", encoding="utf-8") as handle:
        json.dump(findings, handle, indent=2)
    tmp_json.replace(config.FINDINGS_PATH)

    lines = ["# Milestone 0 inspection report", ""]
    if len(findings["warnings"]) > 0:
        for warning in findings["warnings"]:
            lines.append(f"- {warning}")
    else:
        lines.append("No warnings.")
    lines.append("")
    lines.append(f"Run time: {elapsed_s:.0f} s. Sample days: {findings['sample_dates']}.")
    lines.append("")
    lines.append("## Decisions")
    lines.append("")
    lines.append("| Check | Measured | Decision |")
    lines.append("|---|---|---|")
    for check_id, measured, decision in decisions:
        lines.append(f"| {check_id} | {measured} | {decision} |")
    lines.append("")
    lines.append("## Deviation")
    lines.append("")
    lines.append("GPS `route_id` is float text (`837.0`); GTFS `route_id` is `837`. Plain trimmed, "
                 "upper-cased matching gives 0% on every rung, so every route join normalises "
                 "whole numbers to integers first (`config.ROUTE_NORM_SQL`).")
    lines.append("")
    lines.append("## Rows per day")
    lines.append("")
    for date_iso, rows in findings["rows_per_day"].items():
        lines.append(f"- {date_iso}: {rows:,}")
    lines.append("")
    lines.append("## Sample days")
    for result in day_results:
        lines.append("")
        lines.append(f"### {result['date']}")
        lines.append(f"- ts range {result['ts_min']:.0f} .. {result['ts_max']:.0f}, "
                     f"file-date match {result['ts_date_match']:.4f}")
        lines.append(f"- duplicates {result['dup_rate']:.4f}, median gap {result['median_gap_s']:.1f} s, "
                     f"p90 gap {result['p90_gap_s']:.1f} s")
        lines.append(f"- bbox share {result['bbox_rate']:.4f}, speed ratio {result['speed_ratio']}")
        lines.append(f"- vehicles {result['vehicles']:,}, routes {result['routes']:,}")
        lines.append(f"- route rung matches {result['rung_counts']} of {result['n_rows']:,} rows")
        lines.append(f"- quietest 3 h window starts {result['quietest_3h_start']:02d}:00 IST")
        lines.append(f"- hourly rows (IST 00..23): {result['hourly_rows_ist']}")
    lines.append("")
    lines.append("## Null rates (mean of sample days)")
    lines.append("")
    for name, rate in findings["null_rates"].items():
        lines.append(f"- {name}: {rate:.4f}")
    lines.append("")
    lines.append("## Route proximity (M0-N)")
    lines.append("")
    for key, rate in proximity["per_route"].items():
        lines.append(f"- route {key}: {rate:.3f}")
    lines.append("")
    lines.append("## GTFS (M0-M)")
    lines.append("")
    lines.append(f"- counts {gtfs['gtfs_counts']}")
    lines.append(f"- null arrival rate {gtfs['stop_times_null_arrival_rate']:.4f}, "
                 f"max arrival hour {gtfs['max_arrival_hour']}")
    lines.append(f"- calendar present {gtfs['calendar_present']}, distinct service ids "
                 f"{gtfs['distinct_service_ids']}")
    tmp_md = config.M0_REPORT_PATH.with_suffix(".md.tmp")
    with open(tmp_md, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    tmp_md.replace(config.M0_REPORT_PATH)


def main() -> None:
    """Command-line entry point.

    Returns:
        None. Exits 1 on STOP AND REPORT.
    """
    parser = argparse.ArgumentParser(description="Milestone 0 data inspection (SPEC 6.0).")
    parser.parse_args()
    try:
        findings = run()
    except StopAndReport as error:
        # SPEC defines STOP AND REPORT as the only handled failure here.
        print(f"STOP AND REPORT: {error}")
        sys.exit(1)
    log(f"M0 done: route_key {findings['route_key']} rate {findings['route_join_rate']:.4f}, "
        f"proximity {findings['route_proximity_rate']:.3f}, warnings {len(findings['warnings'])}")


if __name__ == "__main__":
    main()
