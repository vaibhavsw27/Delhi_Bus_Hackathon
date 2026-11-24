"""
config.py - every constant of the project in one place, plus load_findings().

Purpose: single source for paths, thresholds, and model constants. Data-dependent
values that Milestone 0 determines are read from reports/m0_findings.json through
load_findings(); they are never hard-coded elsewhere.
Inputs: reports/m0_findings.json (optional, written by pipeline/m0_inspect.py).
Outputs: module-level constants and the load_findings() / duckdb_settings() helpers.
"""

import datetime as dt_mod
import json
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths (all relative to the repository root, which is this file's folder)
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
GTFS_DIR = RAW_DIR / "gtfs"
SQLITE_DIR = RAW_DIR / "sqlite"
CLEAN_DIR = DATA_DIR / "clean"
POSITIONS_DIR = CLEAN_DIR / "positions"
SEG_OBS_DIR = CLEAN_DIR / "seg_obs"
MODEL_DIR = DATA_DIR / "model"
TMP_DIR = DATA_DIR / "tmp"
REPORTS_DIR = ROOT / "reports"
LOGS_DIR = ROOT / "logs"
WEB_DIR = ROOT / "web"
VALIDATION_DIR = ROOT / "validation"

FINDINGS_PATH = REPORTS_DIR / "m0_findings.json"
M0_REPORT_PATH = REPORTS_DIR / "m0_report.md"
PIPELINE_STATS_PATH = REPORTS_DIR / "pipeline_stats.json"
NETWORK_PATH = MODEL_DIR / "network.npz"
SEG_TIME_PATH = MODEL_DIR / "seg_time.npy"
SEG_LEVEL_PATH = MODEL_DIR / "seg_level.npy"
MODEL_META_PATH = MODEL_DIR / "model_meta.json"
LIVE_FACTORS_PATH = MODEL_DIR / "live_factors.npy"

# ---------------------------------------------------------------------------
# DuckDB session (section 3)
# ---------------------------------------------------------------------------
DUCKDB_MEMORY_LIMIT = "8GB"
DUCKDB_THREADS = 4

# ---------------------------------------------------------------------------
# Geometry and time conventions (section 6)
# ---------------------------------------------------------------------------
LAT0 = 28.6139
LON0 = 77.2090
METRES_PER_DEG = 111194.93
IST_OFFSET_S = 19800
SECONDS_PER_DAY = 86400
BUCKET_S = 1800
N_BUCKETS = 48
N_HOURS = 24
N_DAY_TYPES = 3
DAY_TYPE_NAMES = ["weekday", "saturday", "sunday"]
BBOX_LAT_MIN = 28.20
BBOX_LAT_MAX = 29.00
BBOX_LON_MIN = 76.70
BBOX_LON_MAX = 77.70

# Default date sets (Milestone 0 rule M0-A may override them through findings)
HOLIDAYS = ["2026-10-02", "2026-10-20"]
TRAIN_START = "2026-10-01"
TRAIN_END = "2026-10-24"
TEST_START = "2026-10-25"
TEST_END = "2026-10-31"
REPLAY_DATE = "2026-10-27"
DEV_DATE = "2026-10-06"
M0_SAMPLE_DATES = ["2026-10-06", "2026-10-11", "2026-10-27"]

# ---------------------------------------------------------------------------
# Milestone 0 thresholds (section 6.0)
# ---------------------------------------------------------------------------
M0_MIN_DAY_SHARE = 0.10
M0_ROUTE_NULL_BACKFILL = 0.20
M0_TS_MS_THRESHOLD = 1e11
M0_DATE_MATCH_MIN = 0.95
M0_GAP_FAST_S = 60
M0_JOIN_GOOD = 0.80
M0_JOIN_MIN = 0.50
M0_BBOX_MIN = 0.90
M0_BYTES_PER_ROW = 28
M0_DAY_BYTES_MAX = 3e9
M0_PROX_ROUTES = 20
M0_PROX_MIN_ROWS = 5000
M0_PROX_POINTS = 10000
M0_PROX_MOVE_M = 20
M0_PROX_RADIUS_M = 150
M0_PROX_GOOD = 0.60
M0_PROX_MIN = 0.30
M0_SPEED_PAIRS = 100000
M0_SPEED_MAX_GAP_S = 30
M0_SHAPE_COVERAGE_MIN = 0.5
MIN_TRAIN_DATES = 10
SQLITE_CHUNK_ROWS = 500000
M0_SEED = 42

# Route key normalisation (deviation from SPEC 6.0 M0-I, recorded in m0_report.md):
# the GPS stores route_id as float text ("837.0") while GTFS has "837". A value that
# parses as a whole number is written as that integer; anything else is upper-cased
# and trimmed exactly as the spec says. Used on both sides of every route join.
# "{c}" is replaced by the SQL column expression.
ROUTE_NORM_SQL = (
    "CASE WHEN TRY_CAST(trim({c}) AS DOUBLE) IS NOT NULL "
    "AND TRY_CAST(trim({c}) AS DOUBLE) = floor(TRY_CAST(trim({c}) AS DOUBLE)) "
    "THEN CAST(CAST(TRY_CAST(trim({c}) AS DOUBLE) AS BIGINT) AS VARCHAR) "
    "ELSE upper(trim({c})) END"
)

# ---------------------------------------------------------------------------
# Static network (section 6.1)
# ---------------------------------------------------------------------------
MAX_PATTERNS_PER_ROUTE = 4
PATTERN_MIN_STOPS = 3
PATTERN_MIN_TRIPS = 3
PATTERN_MIN_SHARE = 0.10
SHAPE_SIMPLIFY_M = 5
STOP_SNAP_MAX_M = 150
SNAP_FIRST_SHARE = 0.25
SNAP_CHORD_FACTOR = 3
SNAP_EXTRA_M = 300
SEG_MIN_LEN_M = 10
SCHED_MIN_VALUES = 3
DEFAULT_SPEED_KMH = 18
SPEED_MIN_KMH = 3
SPEED_MAX_KMH = 60

# ---------------------------------------------------------------------------
# Ingest (section 6.3)
# ---------------------------------------------------------------------------
SPIKE_SPEED_MPS = 30
STATIONARY_M = 15

# ---------------------------------------------------------------------------
# Match (section 6.4)
# ---------------------------------------------------------------------------
MAX_OFFSET_M_DEFAULT = 100
RUN_GAP_S_DEFAULT = 300
BACKTRACK_M = 150
MIN_RUN_SEGS = 3
MIN_RUN_M = 1000
OBS_MIN_KMH = 1.5
OBS_MAX_KMH = 80
OBS_MAX_S = 1200
DEDUP_WINDOW_S = 120

# ---------------------------------------------------------------------------
# Model (section 7)
# ---------------------------------------------------------------------------
WINSOR_LO = 0.10
WINSOR_HI = 0.90
N_MIN_L1 = 8
N_MIN_L2 = 8
N_MIN_L3 = 5
N_MIN_L4 = 30
PROFILE_MIN_SEGS = 50
PROFILE_SEG_MIN_N3 = 50
PROFILE_F_MIN = 0.6
PROFILE_F_MAX = 2.5
RATIO_G_MIN = 0.5
RATIO_G_MAX = 3.0
CALIB_MIN = 0.85
CALIB_MAX = 1.20
CALIB_MAX_RUNS = 200000
CALIB_MIN_SEGS = 10
QUIET_BUCKET_MIN = 12
QUIET_BUCKET_MAX = 45
QUIET_MIN_GOOD_SHARE = 0.30
QUIET_BUCKET_DEFAULT = 12
LEVEL_SCORES = [0.0, 1.0, 0.8, 0.6, 0.4, 0.2]
CONF_HIGH = 0.75
CONF_MEDIUM = 0.45

# Live replay (section 7.8)
LIVE_HORIZON_S = 1800
LIVE_TICK_S = 300
LIVE_N_TICKS = 288
LIVE_WINDOW_S = 2700
LIVE_DECAY_S = 900
LIVE_RATIO_MIN = 0.33
LIVE_RATIO_MAX = 3.0
CITY_PRIOR_K = 50
LIVE_PRIOR_K = 2.0

# ---------------------------------------------------------------------------
# Planner (section 8)
# ---------------------------------------------------------------------------
WALK_SPEED_MPS = 1.2
WALK_DETOUR = 1.3
MAX_ACCESS_M = 800
ACCESS_FALLBACK_M = 2000
ACCESS_FALLBACK_STOPS = 3
MAX_ACCESS_STOPS = 30
TRANSFER_WALK_MAX_M = 300
TRANSFER_CHANGE_S = 60
MAX_WAIT_S = 720
MAX_ROUNDS = 3
RANK_TRANSFER_PENALTY_S = 300
MAX_OPTIONS = 3
OPTION_SLACK_FACTOR = 1.5
OPTION_SLACK_S = 600
WALK_ONLY_MAX_M = 1500
ROUND_IMPROVE_MIN_S = 60
INF_TIME = 1e18
SEARCH_DEFAULT_LIMIT = 8
SEARCH_MAX_LIMIT = 20

# Demo pair fallbacks (section 15, Q4)
DEMO_ORIGIN_QUERIES = ["kashmere gate", "kashmiri gate"]
DEMO_DEST_QUERY = "nehru place"
DEMO_ORIGIN_LATLON = (28.6677, 77.2285)
DEMO_DEST_LATLON = (28.5491, 77.2526)
DEMO_DEPART_AT = "2026-10-27T08:30"

# Real-time feed: polling the OTD GTFS-Realtime feed (planner/live.py)
OTD_FEED_URL = "https://otd.delhi.gov.in/api/realtime/VehiclePositions.pb"
OTD_KEY_PATH = ROOT / "secrets" / "otd_api_key.txt"
LIVE_POLL_S = 10
LIVE_RECOMPUTE_S = 60
LIVE_BUFFER_S = 3600
LIVE_HTTP_TIMEOUT_S = 20
LIVE_VEHICLE_STALE_S = 300
LIVE_FRESH_S = 300

# Rider-facing reliability and suggestions (phase 2)
RELIABILITY_MIN_SPANS = 200
WAIT_P90_SHARE = 0.9
RELIABLE_LATE_S = 300
VARIABLE_LATE_S = 720
BEST_DEPART_OFFSETS_MIN = [-60, -30, 30, 60, 90, 120]
BEST_DEPART_MIN_SAVING_S = 300
ARRIVE_BY_ITERATIONS = 4
ARRIVE_BY_FIRST_GUESS_S = 3600
SLIDER_FIRST_S = 5 * 3600
SLIDER_LAST_S = 23 * 3600 + 30 * 60
PROFILE_FIRST_BUCKET = 10

# Page metadata (section 9.2)
APP_NAME = "BusPulse Delhi"
SLIDER_MIN = "05:00"
SLIDER_MAX = "23:30"
SLIDER_STEP_MIN = 30
SPEED_CLASSES_KMH = [8, 12, 18, 25]
SPEED_COLORS = ["#D7263D", "#F26B38", "#F5A623", "#9BC53D", "#22A559"]


def duckdb_settings() -> list[str]:
    """Return the DuckDB SET statements every script runs (section 3).

    Returns:
        A list of SQL statements as strings.
    """
    # The spill directory is given as a forward-slash relative path so DuckDB
    # accepts it on every OS; scripts always run from the repository root.
    tmp_path = TMP_DIR.relative_to(ROOT).as_posix()
    statements = [
        f"SET memory_limit='{DUCKDB_MEMORY_LIMIT}'",
        f"SET threads={DUCKDB_THREADS}",
        f"SET temp_directory='{tmp_path}'",
        "SET preserve_insertion_order=false",
        "SET TimeZone='UTC'",
    ]
    return statements


def date_range(start: str, end: str) -> list[str]:
    """List ISO dates from start to end inclusive.

    Args:
        start: first date, YYYY-MM-DD.
        end: last date, YYYY-MM-DD.
    Returns:
        List of YYYY-MM-DD strings.
    """
    first = dt_mod.date.fromisoformat(start)
    last = dt_mod.date.fromisoformat(end)
    dates = []
    current = first
    while current <= last:
        dates.append(current.isoformat())
        current = current + dt_mod.timedelta(days=1)
    return dates


TRAIN_DATES = date_range(TRAIN_START, TRAIN_END)
TEST_DATES = date_range(TEST_START, TEST_END)


def day_type_of(date_iso: str) -> int:
    """Day type of a date: 0 weekday, 1 Saturday, 2 Sunday or holiday (section 6).

    Args:
        date_iso: date as YYYY-MM-DD.
    Returns:
        0, 1, or 2.
    """
    # Holidays count as Sunday-type days whatever their weekday is (A6).
    if date_iso in HOLIDAYS:
        return 2
    weekday = dt_mod.date.fromisoformat(date_iso).weekday()
    if weekday == 6:
        return 2
    if weekday == 5:
        return 1
    return 0


def load_findings() -> dict:
    """Load reports/m0_findings.json written by Milestone 0.

    Returns:
        The findings as a dict. Raises FileNotFoundError when Milestone 0 has not run,
        because every later step depends on these values.
    """
    with open(FINDINGS_PATH, "r", encoding="utf-8") as handle:
        findings = json.load(handle)
    return findings
