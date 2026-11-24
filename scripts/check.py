"""
scripts/check.py - the "done when" checks of every milestone (SPEC 12).

Purpose: one assertion per printed line, then "MN PASS" (exit 0) or "MN FAIL" (exit 1).
Inputs: the artifacts each milestone produces (reports/, data/model/, data/clean/, ...).
Outputs: console lines only.
Run: python scripts/check.py m0   (m0 .. m5)
"""

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

# Make the repository root importable when the script is run as a file.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import numpy as np  # noqa: E402

import config  # noqa: E402

M0_KEYS = [
    "available_dates", "train_dates", "test_dates", "replay_date", "dev_date",
    "table_name", "schema_map", "sqlite_reader", "rows_per_day", "null_rates",
    "backfill_route_from_trip", "ts_unit", "ts_source", "dup_rate", "median_gap_s",
    "p90_gap_s", "run_gap_s", "trip_join_rate", "trip_id_usable", "route_key",
    "route_join_rate", "speed_unit", "bbox_rate", "swap_latlon", "est_day_bytes",
    "day_chunks", "gtfs_counts", "stop_times_null_arrival_rate", "shape_coverage",
    "use_shapes", "service_filter", "route_proximity_rate", "max_offset_m",
    "vehicles_per_day", "routes_per_day",
]


class Checker:
    """Collects assertion results and prints them one per line."""

    def __init__(self, name: str) -> None:
        """Start a checker.

        Args:
            name: milestone label, for example "M0".
        Returns:
            None.
        """
        self.name = name
        self.failed = 0

    def check(self, ok: bool, text: str) -> bool:
        """Record and print one assertion.

        Args:
            ok: whether it holds.
            text: what was checked, with the measured value.
        Returns:
            The same ok value.
        """
        if ok:
            print(f"  ok    {text}")
        else:
            print(f"  FAIL  {text}")
            self.failed += 1
        return ok

    def info(self, text: str) -> None:
        """Print an informational line that is not an assertion.

        Args:
            text: the line.
        Returns:
            None.
        """
        print(f"  info  {text}")

    def finish(self) -> None:
        """Print the final line and exit with the right code.

        Returns:
            Never returns.
        """
        if self.failed == 0:
            print(f"{self.name} PASS")
            sys.exit(0)
        print(f"{self.name} FAIL")
        sys.exit(1)


def check_m0() -> None:
    """Milestone 0 assertions (SPEC 12).

    Returns:
        None (exits).
    """
    c = Checker("M0")
    c.check(config.M0_REPORT_PATH.exists(), "reports/m0_report.md exists")
    if not c.check(config.FINDINGS_PATH.exists(), "reports/m0_findings.json exists"):
        c.finish()
    findings = config.load_findings()
    missing = [key for key in M0_KEYS if key not in findings]
    c.check(len(missing) == 0, f"findings contain every key of SPEC 6.0 (missing: {missing})")
    c.check(findings.get("route_join_rate", 0) >= 0.50,
            f"route_join_rate >= 0.50 ({findings.get('route_join_rate')})")
    c.check(findings.get("route_proximity_rate", 0) >= 0.30,
            f"route_proximity_rate >= 0.30 ({findings.get('route_proximity_rate')})")
    c.check(len(findings.get("train_dates", [])) >= 10,
            f"len(train_dates) >= 10 ({len(findings.get('train_dates', []))})")
    c.finish()


def resolve_demo_pair(planner) -> tuple[str, str, str]:
    """Demo origin and destination by the Q4 searches, else the Q4 coordinates.

    Args:
        planner: a Planner.
    Returns:
        Tuple (origin param, destination param, description of what was used).
    """
    origin = None
    for query in config.DEMO_ORIGIN_QUERIES:
        results = planner.search.search(query, 1)
        if len(results) > 0:
            origin = results[0]["name"]
            break
    destination = None
    results = planner.search.search(config.DEMO_DEST_QUERY, 1)
    if len(results) > 0:
        destination = results[0]["name"]
    if origin is not None and destination is not None:
        return origin, destination, f"searches: '{origin}' -> '{destination}'"
    lat_o, lon_o = config.DEMO_ORIGIN_LATLON
    lat_d, lon_d = config.DEMO_DEST_LATLON
    return f"{lat_o},{lon_o}", f"{lat_d},{lon_d}", "Q4 coordinates"


def load_planner():
    """Load the network, the model, and a warmed-up planner.

    Returns:
        Planner instance.
    """
    from planner.journeys import Planner
    from planner.model import Model
    from planner.network import Network
    planner = Planner(Network(), Model())
    planner.warmup()
    return planner


def check_m1() -> None:
    """Milestone 1 assertions (SPEC 12).

    Returns:
        None (exits).
    """
    from planner.network import Network
    c = Checker("M1")
    if not c.check(config.NETWORK_PATH.exists(), "data/model/network.npz exists"):
        c.finish()
    net = Network()
    c.check(net.n_stops > 5000, f"network has more than 5,000 stops ({net.n_stops})")
    c.check(net.n_patterns > 1000, f"network has more than 1,000 patterns ({net.n_patterns})")
    bad = 0
    for p in range(net.n_patterns):
        dist = net.pat_stop_dist[net.pat_ptr[p]:net.pat_ptr[p + 1]]
        if np.any(np.diff(dist) <= 0):
            bad += 1
    c.check(bad == 0, f"every pattern has strictly increasing pat_stop_dist ({bad} bad)")

    planner = load_planner()
    origin, destination, used = resolve_demo_pair(planner)
    c.info(f"demo pair resolved by {used}")
    result = planner.plan(origin, destination, config.DEMO_DEPART_AT, "typical")
    options = result["options"]
    c.check(len(options) >= 1, f"demo plan returns at least 1 option ({len(options)})")
    totals = [round(o["total_s"] / 60, 1) for o in options]
    c.check(len(options) > 0 and all(20 * 60 <= o["total_s"] <= 180 * 60 for o in options),
            f"option totals between 20 and 180 min ({totals})")
    worst_gap = 0
    short_hops = 0
    for option in options:
        leg_sum = sum(leg["duration_s"] for leg in option["legs"])
        worst_gap = max(worst_gap, abs(leg_sum - option["total_s"]))
        for leg in option["legs"]:
            if leg["type"] != "bus":
                continue
            for hop in leg["hops"]:
                if len(hop["coords"]) < 2:
                    short_hops += 1
    c.check(worst_gap <= 2, f"leg durations sum to the total within 2 s (worst {worst_gap} s)")
    c.check(short_hops == 0, f"every bus hop has at least 2 coordinates ({short_hops} short)")

    # Median compute time over 20 calls after the warm-up call above.
    times_ms = []
    for _ in range(20):
        started = time.perf_counter()
        planner.plan(origin, destination, config.DEMO_DEPART_AT, "typical")
        times_ms.append((time.perf_counter() - started) * 1000)
    median_ms = statistics.median(times_ms)
    c.check(median_ms < 300, f"median compute time under 300 ms ({median_ms:.0f} ms)")
    c.finish()


POSITIONS_SCHEMA = {"vehicle_id": "VARCHAR", "route_idx": "INTEGER", "ts": "BIGINT",
                    "sod": "INTEGER", "lat": "DOUBLE", "lon": "DOUBLE", "x": "FLOAT", "y": "FLOAT"}
SEG_OBS_SCHEMA = {"vehicle_id": "VARCHAR", "pattern_idx": "INTEGER", "run_id": "INTEGER",
                  "seg_pos": "SMALLINT", "segment_idx": "INTEGER", "t_enter": "BIGINT",
                  "sod": "INTEGER", "bucket": "SMALLINT", "day_type": "TINYINT",
                  "tt_s": "FLOAT", "valid": "BOOLEAN"}


def parquet_schema(path: Path) -> dict:
    """Column name -> DuckDB type of a Parquet file.

    Args:
        path: the file.
    Returns:
        Dict.
    """
    import duckdb
    rows = duckdb.sql(f"DESCRIBE SELECT * FROM read_parquet('{path.as_posix()}')").fetchall()
    schema = {}
    for row in rows:
        schema[row[0]] = row[1]
    return schema


def latest_stats(step: str, date_iso: str) -> dict | None:
    """Most recent pipeline_stats.json record of a step and date.

    Args:
        step: "ingest" or "match".
        date_iso: date.
    Returns:
        Record or None.
    """
    if not config.PIPELINE_STATS_PATH.exists():
        return None
    with open(config.PIPELINE_STATS_PATH, "r", encoding="utf-8") as handle:
        records = json.load(handle)
    found = None
    for record in records:
        if record.get("step") == step and record.get("date") == date_iso:
            found = record
    return found


def check_m2() -> None:
    """Milestone 2 assertions on the dev date (SPEC 12).

    Returns:
        None (exits).
    """
    c = Checker("M2")
    findings = config.load_findings()
    dev = findings["dev_date"]
    pos_path = config.POSITIONS_DIR / f"{dev}.parquet"
    obs_path = config.SEG_OBS_DIR / f"{dev}.parquet"
    if not c.check(pos_path.exists() and obs_path.exists(), f"both Parquet files exist for {dev}"):
        c.finish()
    c.check(parquet_schema(pos_path) == POSITIONS_SCHEMA, "positions schema matches SPEC 6.3")
    c.check(parquet_schema(obs_path) == SEG_OBS_SCHEMA, "seg_obs schema matches SPEC 6.4")
    stats = latest_stats("match", dev)
    if not c.check(stats is not None, "match stats record exists"):
        c.finish()
    share = stats["active_groups_with_run"] / max(stats["active_groups"], 1)
    c.check(share >= 0.50, f"at least 50% of active groups produced a run ({share:.3f} of "
            f"{stats['active_groups']:,})")
    c.check(stats["observations"] >= 50000, f"at least 50,000 observations ({stats['observations']:,})")
    c.check(stats["valid_share"] >= 0.85, f"valid share at least 0.85 ({stats['valid_share']})")
    speed = stats["median_valid_speed_kmh"]
    c.check(8 <= speed <= 35, f"median valid speed between 8 and 35 km/h ({speed})")
    done = 0
    for date_iso in findings["available_dates"]:
        if (config.SEG_OBS_DIR / f"{date_iso}.parquet").exists():
            done += 1
    c.info(f"days complete: {done}/{len(findings['available_dates'])}")
    c.finish()


def pass_or_missed(flag: bool) -> str:
    """PASS or MISSED for success criteria (they never fail a milestone).

    Args:
        flag: criterion met.
    Returns:
        Text.
    """
    if flag:
        return "PASS"
    return "MISSED"


def check_m3() -> None:
    """Milestone 3 assertions (SPEC 12).

    Returns:
        None (exits).
    """
    from planner.network import Network
    c = Checker("M3")
    net = Network()
    seg_time = np.load(config.SEG_TIME_PATH)
    seg_level = np.load(config.SEG_LEVEL_PATH)
    with open(config.MODEL_META_PATH, "r", encoding="utf-8") as handle:
        meta = json.load(handle)
    expected_shape = (3, 48, net.n_segments)
    c.check(seg_time.shape == expected_shape, f"seg_time shape {seg_time.shape} == {expected_shape}")
    c.check(not np.isnan(seg_time).any(), "seg_time has no NaN")
    speed = net.seg_len_m[None, None, :] / seg_time * 3.6
    c.check(bool(np.all((speed >= 3 - 1e-3) & (speed <= 60 + 1e-3))),
            f"implied speeds within 3..60 km/h ({speed.min():.2f}..{speed.max():.2f})")
    good = float(np.mean(seg_level[0, 14:44, :] <= 2))
    c.check(good >= 0.10, f"weekday buckets 14-43 at L1 or L2: share {good:.3f} (>= 0.10)")
    profile = np.array(meta["profile_F"])
    peak = int(np.argmax(profile[0]))
    peak_ok = (16 <= peak <= 21) or (34 <= peak <= 40)
    c.check(peak_ok, f"weekday peak F bucket {peak} ({peak // 2:02d}:{(peak % 2) * 30:02d}) in "
            "08:00-11:00 or 17:00-20:30")
    results_path = config.VALIDATION_DIR / "results.json"
    if not c.check(results_path.exists(), "validation/results.json exists"):
        c.finish()
    with open(results_path, "r", encoding="utf-8") as handle:
        results = json.load(handle)
    head = results["headline"]
    c.check(head["mae_model_min"] < head["mae_timetable_min"],
            f"model MAE {head['mae_model_min']} < timetable MAE {head['mae_timetable_min']} min "
            f"on {head['n_trips']:,} trips")
    factors = np.load(config.LIVE_FACTORS_PATH)
    c.check(factors.shape == (288, net.n_segments), f"live_factors shape {factors.shape}")
    c.check(bool(np.all((factors >= 0.33 - 1e-6) & (factors <= 3.0 + 1e-6))),
            f"live factors within 0.33..3.0 ({factors.min():.3f}..{factors.max():.3f})")
    planner = load_planner()
    origin, destination, used = resolve_demo_pair(planner)
    live = planner.plan(origin, destination, config.DEMO_DEPART_AT, "live")
    has_delta = len(live["options"]) > 0 and live["options"][0]["live"] is not None and \
        "delta_s" in live["options"][0]["live"]
    delta_text = None
    if has_delta:
        delta_text = live["options"][0]["live"]["delta_s"]
    c.check(has_delta, f"demo plan in live mode returns live.delta_s ({delta_text})")
    c.check((config.VALIDATION_DIR / "accuracy_slide.png").exists(), "accuracy_slide.png exists")
    c.check((config.VALIDATION_DIR / "mae_by_hour.png").exists(), "mae_by_hour.png exists")
    reduction = 1 - head["mae_model_min"] / head["mae_timetable_min"]
    c.info(f"S1 {pass_or_missed(reduction >= 0.25)}: MAE {reduction * 100:.1f}% lower than timetable")
    c.info(f"S2 {pass_or_missed(head['mape_model_pct'] <= 20)}: MAPE {head['mape_model_pct']} %")
    typical = planner.plan(origin, destination, config.DEMO_DEPART_AT, "typical")
    s5 = False
    s5_text = "no option"
    if len(typical["options"]) > 0:
        first = typical["options"][0]
        s5 = first["in_bus_s"] > first["quiet_hour"]["in_bus_s"]
        s5_text = f"in-bus {first['in_bus_s']} s at 08:30 vs {first['quiet_hour']['in_bus_s']} s at quiet {first['quiet_hour']['label']}"
    c.info(f"S5 {pass_or_missed(s5)}: {s5_text}")
    c.finish()


def check_m4() -> None:
    """Milestone 4 assertions (SPEC 12).

    Returns:
        None (exits).
    """
    c = Checker("M4")
    index_path = config.WEB_DIR / "index.html"
    style_path = config.WEB_DIR / "style.css"
    app_path = config.WEB_DIR / "app.js"
    c.check(index_path.exists() and style_path.exists() and app_path.exists(), "the three web files exist")
    index_text = index_path.read_text(encoding="utf-8")
    app_text = app_path.read_text(encoding="utf-8")
    c.check("leaflet@1.9.4" in index_text, "index.html references Leaflet 1.9.4")
    c.check("family=Inter" in index_text, "index.html references Inter")
    missing = [color for color in config.SPEED_COLORS if color not in app_text]
    c.check(len(missing) == 0, f"app.js contains the five speed colors (missing {missing})")
    planner = load_planner()
    origin, destination, used = resolve_demo_pair(planner)
    morning = planner.plan(origin, destination, config.DEMO_DEPART_AT, "typical")
    night = planner.plan(origin, destination, config.DEMO_DEPART_AT[:11] + "22:00", "typical")
    in_bus_morning = [o["in_bus_s"] for o in morning["options"]]
    in_bus_night = [o["in_bus_s"] for o in night["options"]]
    c.check(len(in_bus_morning) > 0 and in_bus_morning != in_bus_night,
            f"in_bus_s differs at 08:30 {in_bus_morning} and 22:00 {in_bus_night}")
    fields_ok = True
    for option in morning["options"] + night["options"]:
        for key in ["quiet_hour", "confidence", "signature"]:
            if key not in option or option[key] is None:
                fields_ok = False
    c.check(fields_ok, "every option has quiet_hour, confidence, signature")
    c.finish()


def check_m5() -> None:
    """Milestone 5 assertions (SPEC 12).

    Returns:
        None (exits).
    """
    c = Checker("M5")
    readme = REPO_ROOT / "README.md"
    if not c.check(readme.exists(), "README.md exists"):
        c.finish()
    lines = readme.read_text(encoding="utf-8").splitlines()
    c.check(len(lines) < 150, f"README.md is under 150 lines ({len(lines)})")
    for heading in ["How to run", "How the model works", "Validation", "Limits"]:
        found = any(line.lstrip("#").strip() == heading for line in lines if line.startswith("#"))
        c.check(found, f"README.md has the heading '{heading}'")
    c.finish()


def main() -> None:
    """Dispatch to the milestone check named on the command line.

    Returns:
        None (exits).
    """
    parser = argparse.ArgumentParser(description="Milestone checks (SPEC 12).")
    parser.add_argument("milestone", choices=["m0", "m1", "m2", "m3", "m4", "m5"])
    args = parser.parse_args()
    handlers = {
        "m0": check_m0,
        "m1": check_m1,
        "m2": check_m2,
        "m3": check_m3,
        "m4": check_m4,
        "m5": check_m5,
    }
    handlers[args.milestone]()


if __name__ == "__main__":
    main()
