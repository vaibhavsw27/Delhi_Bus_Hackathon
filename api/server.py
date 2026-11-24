"""
api/server.py - FastAPI app: JSON endpoints over the planner and the static page (SPEC 9).

Inputs: data/model/*, validation/results.json (optional), web/.
Outputs: HTTP responses. Start with:
    python -m uvicorn api.server:app --host 127.0.0.1 --port 8000
"""

import datetime as dt_mod
import json
import re

import duckdb
import numpy as np
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import config
from pipeline.build_ops import OPS_PATH, segment_coords
from planner.journeys import PlanError, Planner
from planner.live import LiveEngine, ist_now
from planner.model import Model
from planner.network import Network

HHMM_PATTERN = re.compile(r"^\d{2}:\d{2}$")
REPLAY_VEHICLE_WINDOW_S = 120

# Load the network and model once at start, and compile numba before the first request.
NETWORK = Network()
MODEL = Model()
LIVE = LiveEngine(NETWORK, MODEL)
PLANNER = Planner(NETWORK, MODEL, LIVE)
PLANNER.warmup()
LIVE.start()
SEGMENT_COORDS = segment_coords(NETWORK)
OPS = None
if OPS_PATH.exists():
    with open(OPS_PATH, "r", encoding="utf-8") as handle:
        OPS = json.load(handle)

app = FastAPI(title=config.APP_NAME)
app.mount("/static", StaticFiles(directory=str(config.WEB_DIR)), name="static")


def error_response(code: str, message: str, status: int) -> JSONResponse:
    """The SPEC 9 error body.

    Args:
        code: error code.
        message: message.
        status: HTTP status.
    Returns:
        JSONResponse.
    """
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


@app.exception_handler(PlanError)
async def plan_error_handler(request: Request, error: PlanError) -> JSONResponse:
    """Turn a PlanError into the SPEC 9 error format.

    Args:
        request: the request (unused).
        error: the error.
    Returns:
        JSONResponse.
    """
    return error_response(error.code, error.message, error.status)


def validation_headline() -> dict | None:
    """Headline validation numbers for /api/meta, or None before validation ran.

    Returns:
        Dict or None.
    """
    path = config.VALIDATION_DIR / "results.json"
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as handle:
        results = json.load(handle)
    return results.get("headline")


@app.get("/")
def index() -> FileResponse:
    """Serve the single page.

    Returns:
        web/index.html.
    """
    return FileResponse(str(config.WEB_DIR / "index.html"))


@app.get("/api/health")
def health() -> dict:
    """Liveness and sizes (SPEC 9.1).

    Returns:
        Status dict.
    """
    return {"status": "ok", "model_loaded": True, "stops": NETWORK.n_stops,
            "patterns": NETWORK.n_patterns, "segments": NETWORK.n_segments}


@app.get("/api/meta")
def meta() -> dict:
    """Page configuration and model summary (SPEC 9.2).

    Returns:
        Meta dict.
    """
    replay_date = MODEL.meta.get("replay_date", config.REPLAY_DATE)
    quiet_labels = {}
    for dt, name in enumerate(config.DAY_TYPE_NAMES):
        minutes = MODEL.quiet_bucket[dt] * 30
        quiet_labels[name] = f"{minutes // 60:02d}:{minutes % 60:02d}"
    return {
        "app": config.APP_NAME,
        "replay_date": replay_date,
        "default_depart_at": f"{replay_date}T08:30",
        "slider": {"min": config.SLIDER_MIN, "max": config.SLIDER_MAX,
                   "step_min": config.SLIDER_STEP_MIN},
        "quiet_bucket_label": quiet_labels,
        "speed_classes_kmh": config.SPEED_CLASSES_KMH,
        "speed_colors": config.SPEED_COLORS,
        "map_center": [config.LAT0, config.LON0],
        "calibration_k": MODEL.meta.get("calibration_k", 1.0),
        "level_share": MODEL.meta.get("level_share"),
        "validation": validation_headline(),
    }


@app.get("/api/stops/search")
def stops_search(q: str | None = None, limit: str | None = None) -> JSONResponse:
    """Stop-name autocomplete (SPEC 9.3).

    Args:
        q: query text, at least 2 characters.
        limit: optional result count, default 8, max 20.
    Returns:
        {"results": [...]} or an error.
    """
    if q is None or len(q.strip()) < 2:
        return error_response("BAD_PARAM", "q must have at least 2 characters", 400)
    count = config.SEARCH_DEFAULT_LIMIT
    if limit is not None:
        if not limit.isdigit():
            return error_response("BAD_PARAM", "limit must be a positive integer", 400)
        count = min(max(int(limit), 1), config.SEARCH_MAX_LIMIT)
    return JSONResponse(content={"results": PLANNER.search.search(q, count)})


@app.get("/api/plan")
def plan(origin: str | None = None, destination: str | None = None,
         depart_at: str | None = None, mode: str | None = None,
         time_mode: str | None = None) -> JSONResponse:
    """Journey planning (SPEC 9.4), plus arrive-by (phase 2).

    Args:
        origin: "lat,lon" or stop name.
        destination: same.
        depart_at: YYYY-MM-DDTHH:MM (IST); the target arrival when time_mode is "arrive".
        mode: "typical" (default) or "live".
        time_mode: "depart" (default) or "arrive".
    Returns:
        Plan response or an error.
    """
    if origin is None or destination is None or depart_at is None:
        return error_response("BAD_PARAM", "origin, destination and depart_at are required", 400)
    chosen_mode = "typical"
    if mode is not None:
        chosen_mode = mode
    if time_mode is not None and time_mode not in ["depart", "arrive"]:
        return error_response("BAD_PARAM", "time_mode must be depart or arrive", 400)
    if time_mode == "arrive":
        result = PLANNER.plan_arrive_by(origin, destination, depart_at, chosen_mode)
    else:
        result = PLANNER.plan(origin, destination, depart_at, chosen_mode)
    return JSONResponse(content=result)


# ---------------------------------------------------------------------------
# Phase 2: live feed, network speeds, operator dashboard, installable app
# ---------------------------------------------------------------------------

def unix_to_hhmm(value: float | None) -> str | None:
    """IST HH:MM of a Unix time.

    Args:
        value: Unix seconds or None.
    Returns:
        Text or None.
    """
    if value is None:
        return None
    moment = dt_mod.datetime.fromtimestamp(value, dt_mod.timezone.utc) + \
        dt_mod.timedelta(seconds=config.IST_OFFSET_S)
    return moment.strftime("%H:%M")


@app.get("/api/live/status")
def live_status() -> dict:
    """State of the real-time feed.

    Returns:
        Status dict.
    """
    state = LIVE.snapshot()
    return {
        "enabled": state["enabled"], "connected": state["connected"],
        "ready": LIVE.is_fresh(), "today": ist_now().date().isoformat(),
        "vehicles": state["vehicles"], "observations": state["observations"],
        "city_factor": round(state["city"], 3), "updated_at": unix_to_hhmm(state["updated_unix"]),
        "last_fetch_at": unix_to_hhmm(state["last_fetch_unix"]), "error": state["error"],
        "replay_date": MODEL.meta.get("replay_date", config.REPLAY_DATE),
    }


@app.get("/api/live/vehicles")
def live_vehicles(at: str | None = None) -> JSONResponse:
    """Bus positions: the real feed now, or the replay day at HH:MM (SPEC 9.5).

    Args:
        at: optional HH:MM on the replay day.
    Returns:
        {"source", "vehicles": [...]}.
    """
    if at is None:
        return JSONResponse(content={"source": "feed", "vehicles": LIVE.vehicles()})
    if not HHMM_PATTERN.match(at):
        return error_response("BAD_PARAM", "at must be HH:MM", 400)
    sod = int(at[:2]) * 3600 + int(at[3:]) * 60
    replay = MODEL.meta.get("replay_date", config.REPLAY_DATE)
    path = (config.POSITIONS_DIR / f"{replay}.parquet").relative_to(config.ROOT).as_posix()
    # Latest replay-day point of each bus in the 120 s before the requested time.
    rows = duckdb.execute(
        "SELECT vehicle_id, arg_max(lat, sod), arg_max(lon, sod), arg_max(route_idx, sod) "
        f"FROM read_parquet('{path}') WHERE sod > ? AND sod <= ? GROUP BY vehicle_id",
        [sod - REPLAY_VEHICLE_WINDOW_S, sod],
    ).fetchall()
    vehicles = []
    for vehicle, lat, lon, route in rows:
        vehicles.append({"id": vehicle, "lat": round(lat, 5), "lon": round(lon, 5),
                         "route_name": str(NETWORK.route_name[route])})
    return JSONResponse(content={"source": "replay", "vehicles": vehicles})


@app.get("/api/network/segments")
def network_segments() -> dict:
    """Geometry of every segment for the city speed map (SPEC 9.5).

    Returns:
        {"segments": [[[lat, lon], ...], ...]}.
    """
    return {"segments": SEGMENT_COORDS}


@app.get("/api/network/speeds")
def network_speeds(depart_at: str | None = None, mode: str | None = None) -> JSONResponse:
    """Estimated speed of every segment at a time (SPEC 9.5); live uses today's feed.

    Args:
        depart_at: YYYY-MM-DDTHH:MM.
        mode: "typical" (default) or "live".
    Returns:
        {"speeds_kmh": [...], "levels": [...]}.
    """
    if depart_at is None or not re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$", depart_at):
        return error_response("BAD_PARAM", "depart_at must be YYYY-MM-DDTHH:MM", 400)
    moment = dt_mod.datetime.strptime(depart_at, "%Y-%m-%dT%H:%M")
    day_type = config.day_type_of(moment.date().isoformat())
    bucket = (moment.hour * 60 + moment.minute) // 30
    times = MODEL.seg_time[day_type, bucket].astype(np.float64)
    if mode == "live":
        state = LIVE.snapshot()
        replay = MODEL.meta.get("replay_date", config.REPLAY_DATE)
        if moment.date().isoformat() == ist_now().date().isoformat() and state["factors"] is not None:
            times = times * state["factors"]
        elif moment.date().isoformat() == replay and MODEL.live_factors is not None:
            times = times * MODEL.live_row(moment.hour * 3600 + moment.minute * 60)[0]
    speeds = NETWORK.seg_len_m / times * 3.6
    return JSONResponse(content={"speeds_kmh": np.round(speeds, 1).tolist(),
                                 "levels": MODEL.seg_level[day_type, bucket].tolist()})


@app.get("/api/ops")
def ops_data() -> JSONResponse:
    """Operator insights precomputed by pipeline/build_ops.py.

    Returns:
        The ops dict or an error when it has not been built.
    """
    if OPS is None:
        return error_response("NOT_BUILT", "run python -m pipeline.build_ops first", 404)
    return JSONResponse(content=OPS)


@app.get("/ops")
def ops_page() -> FileResponse:
    """Serve the operator dashboard.

    Returns:
        web/ops.html.
    """
    return FileResponse(str(config.WEB_DIR / "ops.html"))


@app.get("/sw.js")
def service_worker() -> FileResponse:
    """Service worker at the site root so it can control the whole app.

    Returns:
        web/sw.js.
    """
    return FileResponse(str(config.WEB_DIR / "sw.js"), media_type="application/javascript")


@app.get("/manifest.webmanifest")
def manifest() -> FileResponse:
    """Web app manifest for installing the page as an app.

    Returns:
        web/manifest.webmanifest.
    """
    return FileResponse(str(config.WEB_DIR / "manifest.webmanifest"),
                        media_type="application/manifest+json")
