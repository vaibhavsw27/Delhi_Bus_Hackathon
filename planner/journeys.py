"""
planner/journeys.py - journey planning around the RAPTOR core (SPEC 8.1-8.6, 9.4).

Purpose: resolve origin and destination, find access and egress stops, run RAPTOR up to
three times with banned routes, reconstruct legs, rank options, and build the JSON of
SPEC 9.4 including quiet-hour, timetable, confidence, and live comparisons.
Inputs: a Network and a Model.
Outputs: Planner.plan(...) -> response dict; PlanError for API errors.
"""

import datetime as dt_mod
import math
import re
import time

import numpy as np

import config
from pipeline import geo
from planner.live import ist_now
from planner.model import Model, walk_segments, walk_timetable
from planner.network import Network
from planner.raptor import INF, raptor
from planner.search import StopSearch

COORD_PATTERN = re.compile(r"^-?\d+(\.\d+)?,-?\d+(\.\d+)?$")
DEPART_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")


class PlanError(Exception):
    """An API error with a code, message, and HTTP status (SPEC 9)."""

    def __init__(self, code: str, message: str, status: int) -> None:
        """Create the error.

        Args:
            code: error code, for example "BAD_PARAM".
            message: human-readable message.
            status: HTTP status code.
        Returns:
            None.
        """
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def walk_seconds(dist_m: float) -> float:
    """Walking seconds for a straight-line distance (SPEC 8.2).

    Args:
        dist_m: straight-line metres.
    Returns:
        Seconds.
    """
    return config.WALK_DETOUR * dist_m / config.WALK_SPEED_MPS


def clock_text(t: float) -> str:
    """HH:MM:SS of a clock time (wrapped to one day).

    Args:
        t: seconds since 00:00 IST of the departure date.
    Returns:
        Text.
    """
    total = int(round(t)) % config.SECONDS_PER_DAY
    hours = total // 3600
    minutes = (total % 3600) // 60
    seconds = total % 60
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def level_label(score: float) -> str:
    """Confidence label from a score (SPEC 7.7).

    Args:
        score: 0..1.
    Returns:
        "high", "medium", or "low".
    """
    if score >= config.CONF_HIGH:
        return "high"
    if score >= config.CONF_MEDIUM:
        return "medium"
    return "low"


def round5(value: float) -> float:
    """Round a coordinate to 5 decimals.

    Args:
        value: degrees.
    Returns:
        Rounded value.
    """
    return round(float(value), 5)


class Planner:
    """Journey planner over one Network and one Model."""

    def __init__(self, net: Network, model: Model, live_engine=None) -> None:
        """Prepare lookups.

        Args:
            net: static network.
            model: travel-time model.
            live_engine: optional planner.live.LiveEngine for the real-time feed.
        Returns:
            None.
        """
        self.net = net
        self.model = model
        self.live_engine = live_engine
        self.search = StopSearch(net)
        # Patterns of each route, for banning whole routes in runs B and C.
        self.route_patterns = []
        for route in range(net.n_routes):
            self.route_patterns.append(np.flatnonzero(net.pat_route == route))
        self.no_ban = np.zeros(net.n_patterns, dtype=np.bool_)

    # ------------------------------------------------------------------
    # Parsing and resolution
    # ------------------------------------------------------------------

    def resolve_point(self, text: str, which: str) -> dict:
        """Turn "lat,lon" or a stop name into a labelled point (SPEC 9.4).

        Args:
            text: the parameter value.
            which: "origin" or "destination" (for messages).
        Returns:
            {"label", "lat", "lon"}.
        """
        value = text.strip()
        if COORD_PATTERN.match(value):
            parts = value.split(",")
            lat = float(parts[0])
            lon = float(parts[1])
            inside_lat = config.BBOX_LAT_MIN <= lat <= config.BBOX_LAT_MAX
            inside_lon = config.BBOX_LON_MIN <= lon <= config.BBOX_LON_MAX
            if not (inside_lat and inside_lon):
                raise PlanError("BAD_PARAM", f"{which} is outside the Delhi bounding box", 400)
            return {"label": f"Pin {lat:.5f}, {lon:.5f}", "lat": lat, "lon": lon}
        if len(value) < 2:
            raise PlanError("BAD_PARAM", f"{which} must be lat,lon or a stop name", 400)
        results = self.search.search(value, 1)
        if len(results) == 0:
            raise PlanError("STOP_NOT_FOUND", f"no stop matches '{value}'", 404)
        first = results[0]
        return {"label": first["name"], "lat": first["lat"], "lon": first["lon"]}

    def nearby_stops(self, lat: float, lon: float, error_code: str) -> tuple:
        """Access or egress stops around a point (SPEC 8.3 step 2).

        Args:
            lat, lon: the point.
            error_code: NO_STOPS_NEAR_ORIGIN or NO_STOPS_NEAR_DESTINATION.
        Returns:
            Tuple (stop indices int64, walk seconds float64, straight metres float64).
        """
        dist = geo.haversine_m(lat, lon, self.net.stop_lat, self.net.stop_lon)
        within = np.flatnonzero(dist <= config.MAX_ACCESS_M)
        if within.shape[0] > 0:
            order = within[np.argsort(dist[within], kind="stable")]
            chosen = order[:config.MAX_ACCESS_STOPS]
        else:
            fallback = np.flatnonzero(dist <= config.ACCESS_FALLBACK_M)
            if fallback.shape[0] == 0:
                place = "origin"
                if error_code == "NO_STOPS_NEAR_DESTINATION":
                    place = "destination"
                raise PlanError(error_code, f"no bus stop within 2 km of the {place}", 404)
            order = fallback[np.argsort(dist[fallback], kind="stable")]
            chosen = order[:config.ACCESS_FALLBACK_STOPS]
        straight = dist[chosen].astype(np.float64)
        seconds = config.WALK_DETOUR * straight / config.WALK_SPEED_MPS
        return chosen.astype(np.int64), seconds, straight

    # ------------------------------------------------------------------
    # RAPTOR runs and candidates
    # ------------------------------------------------------------------

    def run_raptor(self, banned: np.ndarray, ctx: dict) -> tuple:
        """One RAPTOR query with the context's parameters.

        Args:
            banned: bool per pattern.
            ctx: query context (see plan()).
        Returns:
            RAPTOR output tuple.
        """
        net = self.net
        return raptor(
            net.pat_ptr, net.pat_stops, net.pat_seg, net.headway_s, net.sp_ptr, net.sp_pat,
            net.sp_pos, net.foot_ptr, net.foot_to, net.foot_s, ctx["seg_time_dt"], ctx["r"],
            ctx["t_now"], self.model.live_horizon_s, banned, ctx["acc_stops"], ctx["acc_s"],
            ctx["egr_stops"], ctx["egr_s"], ctx["depart"], config.MAX_ROUNDS,
            float(config.MAX_WAIT_S), float(config.TRANSFER_CHANGE_S))

    def extract_candidates(self, out: tuple, ctx: dict) -> list[dict]:
        """Best egress per round, kept only when 60 s better than lower rounds (step 4).

        Args:
            out: RAPTOR output.
            ctx: query context.
        Returns:
            List of skeleton dicts with "arrival" and "legs".
        """
        tau = out[0]
        candidates = []
        best_lower = INF
        for k in range(1, config.MAX_ROUNDS + 1):
            arrivals = tau[k, ctx["egr_stops"]] + ctx["egr_s"]
            i = int(np.argmin(arrivals))
            arrival = float(arrivals[i])
            if arrival >= INF / 2:
                continue
            if arrival <= best_lower - config.ROUND_IMPROVE_MIN_S:
                skeleton = self.reconstruct(out, k, int(ctx["egr_stops"][i]))
                if skeleton is not None:
                    candidates.append(skeleton)
            if arrival < best_lower:
                best_lower = arrival
        return candidates

    def reconstruct(self, out: tuple, k: int, stop: int) -> dict | None:
        """Follow parent pointers back from (k, egress stop) (SPEC 8.5).

        Args:
            out: RAPTOR output.
            k: round.
            stop: egress stop index.
        Returns:
            {"access": stop, "bus": [(pattern, board_pos, alight_pos)], "egress": stop},
            or None if the parents are inconsistent.
        """
        tau, par_pat, par_board, par_alight, par_btime, par_foot = out
        net = self.net
        bus_legs = []
        s = stop
        egress_stop = stop
        guard = 0
        while k > 0:
            guard += 1
            if guard > 50:
                return None
            if par_foot[k, s] >= 0:
                # Reached by a footpath in this round. A footpath straight before the
                # destination is folded into the final walk, so the egress walk starts
                # at the alighting stop.
                if len(bus_legs) == 0:
                    egress_stop = int(par_foot[k, s])
                s = int(par_foot[k, s])
                continue
            p = int(par_pat[k, s])
            if p < 0:
                return None
            board_pos = int(par_board[k, s])
            alight_pos = int(par_alight[k, s])
            bus_legs.append((p, board_pos, alight_pos))
            s = int(net.pat_stops[net.pat_ptr[p] + board_pos])
            k -= 1
        bus_legs.reverse()
        return {"access": s, "bus": bus_legs, "egress": egress_stop}

    # ------------------------------------------------------------------
    # Forward evaluation of a skeleton
    # ------------------------------------------------------------------

    def foot_between(self, a: int, b: int) -> tuple[float, float]:
        """Walk seconds and metres between two stops (footpath table, else formula).

        Args:
            a, b: stop indices.
        Returns:
            Tuple (seconds, metres).
        """
        net = self.net
        lo = net.foot_ptr[a]
        hi = net.foot_ptr[a + 1]
        hits = np.flatnonzero(net.foot_to[lo:hi] == b)
        if hits.shape[0] > 0:
            e = lo + int(hits[0])
            return float(net.foot_s[e]), float(net.foot_m[e])
        straight = float(geo.haversine_m(net.stop_lat[a], net.stop_lon[a],
                                         net.stop_lat[b], net.stop_lon[b]))
        return walk_seconds(straight), config.WALK_DETOUR * straight

    def evaluate(self, skeleton: dict, ctx: dict, r: np.ndarray) -> dict:
        """Clock times of every leg of a skeleton, recomputed forward (SPEC 8.5).

        Args:
            skeleton: from reconstruct().
            ctx: query context.
            r: live ratios to use (ones for typical).
        Returns:
            Dict with "legs" (raw leg dicts with float clocks) and "arrive".
        """
        net = self.net
        origin = ctx["origin"]
        dest = ctx["destination"]
        legs = []
        t = ctx["depart"]
        # 1) Walk from the origin to the first boarding stop.
        first = skeleton["access"]
        straight = float(geo.haversine_m(origin["lat"], origin["lon"],
                                         net.stop_lat[first], net.stop_lon[first]))
        duration = walk_seconds(straight)
        legs.append({"type": "walk", "from": None, "to": first, "start": t, "end": t + duration,
                     "distance_m": config.WALK_DETOUR * straight})
        t += duration
        current = first
        scratch = np.empty(4096, dtype=np.float64)
        for index, (p, board_pos, alight_pos) in enumerate(skeleton["bus"]):
            board_stop = int(net.pat_stops[net.pat_ptr[p] + board_pos])
            # 4) Footpath between the previous alighting stop and this boarding stop.
            if board_stop != current:
                foot_s, foot_m = self.foot_between(current, board_stop)
                legs.append({"type": "walk", "from": current, "to": board_stop, "start": t,
                             "end": t + foot_s, "distance_m": foot_m})
                t += foot_s
            # 2) Wait: half the headway of this pattern in this hour, capped, plus the
            # change time for every bus leg after the first.
            h = float(net.headway_s[p, int(t // 3600) % 24])
            wait = h / 2.0
            if wait > config.MAX_WAIT_S:
                wait = float(config.MAX_WAIT_S)
            change = 0.0
            if index > 0:
                change = float(config.TRANSFER_CHANGE_S)
            legs.append({"type": "wait", "at": board_stop, "start": t, "end": t + change + wait,
                         "headway_s": h, "change_s": change})
            t += change + wait
            # 3) Ride with the walker.
            segs = net.pat_seg[net.pat_ptr[p] + board_pos:net.pat_ptr[p] + alight_pos]
            if scratch.shape[0] < segs.shape[0]:
                scratch = np.empty(segs.shape[0], dtype=np.float64)
            ride = walk_segments(ctx["seg_time_dt"], segs, t, r, ctx["t_now"],
                                 self.model.live_horizon_s, scratch)
            hops = scratch[:segs.shape[0]].copy()
            legs.append({"type": "bus", "pattern": p, "board_pos": board_pos,
                         "alight_pos": alight_pos, "start": t, "end": t + ride, "hops": hops,
                         "segs": segs.copy()})
            t += ride
            current = int(net.pat_stops[net.pat_ptr[p] + alight_pos])
        # 6) Walk from the last alighting stop to the destination.
        straight = float(geo.haversine_m(net.stop_lat[current], net.stop_lon[current],
                                         dest["lat"], dest["lon"]))
        duration = walk_seconds(straight)
        legs.append({"type": "walk", "from": current, "to": None, "start": t, "end": t + duration,
                     "distance_m": config.WALK_DETOUR * straight})
        t += duration
        return {"legs": legs, "arrive": t}

    # ------------------------------------------------------------------
    # JSON building
    # ------------------------------------------------------------------

    def stop_ref(self, s: int, with_coords: bool) -> dict:
        """JSON reference to a stop.

        Args:
            s: stop index.
            with_coords: include lat and lon.
        Returns:
            Dict with label, stop_id and optionally lat, lon.
        """
        ref = {"label": str(self.net.stop_name[s])}
        if with_coords:
            ref["lat"] = round5(self.net.stop_lat[s])
            ref["lon"] = round5(self.net.stop_lon[s])
        ref["stop_id"] = str(self.net.stop_id[s])
        return ref

    def option_json(self, option_id: int, evaluated: dict, skeleton: dict, ctx: dict) -> dict:
        """Build one option of the SPEC 9.4 response.

        Args:
            option_id: 0-based rank.
            evaluated: from evaluate() in the request mode.
            skeleton: from reconstruct().
            ctx: query context.
        Returns:
            The option dict.
        """
        net = self.net
        model = self.model
        dt = ctx["day_type"]
        depart = ctx["depart"]
        origin = ctx["origin"]
        dest = ctx["destination"]
        legs_out = []
        walk_total = 0
        wait_total = 0
        in_bus_total = 0
        scheduled_total = 0.0
        quiet_total = 0.0
        weighted_score = 0.0
        weighted_time = 0.0
        level_time = np.zeros(6, dtype=np.float64)
        # Day profile: in-bus seconds of these legs at every half hour, and the hours
        # when every bus leg runs.
        profile_s = np.zeros(config.N_BUCKETS, dtype=np.float64)
        runs_hour = np.ones(config.N_HOURS, dtype=bool)
        route_ids = []
        route_names = []
        scratch = np.empty(4096, dtype=np.float64)
        qb = model.quiet_bucket[dt]
        for leg in evaluated["legs"]:
            start_r = int(round(leg["start"]))
            end_r = int(round(leg["end"]))
            duration = end_r - start_r
            if leg["type"] == "walk":
                if leg["from"] is None:
                    from_ref = {"label": "Origin", "lat": round5(origin["lat"]),
                                "lon": round5(origin["lon"])}
                else:
                    from_ref = self.stop_ref(leg["from"], True)
                if leg["to"] is None:
                    to_ref = {"label": "Destination", "lat": round5(dest["lat"]),
                              "lon": round5(dest["lon"])}
                else:
                    to_ref = self.stop_ref(leg["to"], True)
                legs_out.append({
                    "type": "walk", "from": from_ref, "to": to_ref,
                    "start": clock_text(leg["start"]), "end": clock_text(leg["end"]),
                    "duration_s": duration, "distance_m": int(round(leg["distance_m"])),
                    "coords": [[from_ref["lat"], from_ref["lon"]], [to_ref["lat"], to_ref["lon"]]],
                })
                walk_total += duration
            elif leg["type"] == "wait":
                headway_value = None
                if math.isfinite(leg["headway_s"]):
                    headway_value = int(round(leg["headway_s"]))
                legs_out.append({
                    "type": "wait", "at": self.stop_ref(leg["at"], False),
                    "start": clock_text(leg["start"]), "end": clock_text(leg["end"]),
                    "duration_s": duration, "headway_s": headway_value,
                    "change_s": int(leg["change_s"]),
                })
                wait_total += duration
            else:
                p = leg["pattern"]
                a = leg["board_pos"]
                b = leg["alight_pos"]
                base = int(net.pat_ptr[p])
                segs = leg["segs"]
                route = int(net.pat_route[p])
                route_ids.append(str(net.route_id[route]))
                route_names.append(str(net.route_name[route]))
                # Timetable and quiet-hour figures for the same hops (SPEC 8.6).
                if scratch.shape[0] < segs.shape[0]:
                    scratch = np.empty(segs.shape[0], dtype=np.float64)
                scheduled = walk_timetable(net.sched_time, segs, leg["start"], scratch)
                quiet = float(np.sum(model.seg_time[dt, qb, segs]))
                profile_s += model.seg_time[dt][:, segs].sum(axis=1)
                runs_hour &= np.isfinite(net.headway_s[p])
                scheduled_total += scheduled
                quiet_total += quiet
                hops_out = []
                t_hop = leg["start"]
                leg_score = 0.0
                leg_time = 0.0
                for i in range(segs.shape[0]):
                    g = int(segs[i])
                    hop_s = float(leg["hops"][i])
                    bucket = int(t_hop // config.BUCKET_S) % config.N_BUCKETS
                    level = int(model.seg_level[dt, bucket, g])
                    dist_m = float(net.pat_stop_dist[base + a + i + 1] - net.pat_stop_dist[base + a + i])
                    speed = 0.0
                    if hop_s > 0:
                        speed = dist_m / hop_s * 3.6
                    v0 = int(net.poly_ptr[p]) + int(net.pat_stop_vtx[base + a + i])
                    v1 = int(net.poly_ptr[p]) + int(net.pat_stop_vtx[base + a + i + 1])
                    coords = []
                    for v in range(v0, v1 + 1):
                        coords.append([round5(net.poly_lat[v]), round5(net.poly_lon[v])])
                    hops_out.append({
                        "from_stop_id": str(net.stop_id[net.pat_stops[base + a + i]]),
                        "to_stop_id": str(net.stop_id[net.pat_stops[base + a + i + 1]]),
                        "time_s": int(round(hop_s)), "distance_m": int(round(dist_m)),
                        "speed_kmh": round(speed, 1), "level": level, "coords": coords,
                    })
                    leg_score += hop_s * model.level_scores[level]
                    leg_time += hop_s
                    level_time[level] += hop_s
                    t_hop += hop_s
                weighted_score += leg_score
                weighted_time += leg_time
                score = 1.0
                if leg_time > 0:
                    score = leg_score / leg_time
                leg_dist = float(net.pat_stop_dist[base + b] - net.pat_stop_dist[base + a])
                avg_speed = 0.0
                if leg["end"] > leg["start"]:
                    avg_speed = leg_dist / (leg["end"] - leg["start"]) * 3.6
                stops_out = []
                for pos in range(a, b + 1):
                    stops_out.append(self.stop_ref(int(net.pat_stops[base + pos]), True))
                legs_out.append({
                    "type": "bus", "route_id": str(net.route_id[route]),
                    "route_name": str(net.route_name[route]), "pattern_idx": int(p),
                    "headsign": str(net.pat_headsign[p]),
                    "board_stop": self.stop_ref(int(net.pat_stops[base + a]), True),
                    "alight_stop": self.stop_ref(int(net.pat_stops[base + b]), True),
                    "start": clock_text(leg["start"]), "end": clock_text(leg["end"]),
                    "duration_s": duration, "distance_m": int(round(leg_dist)),
                    "n_stops": int(b - a), "avg_speed_kmh": round(avg_speed, 1),
                    "scheduled_s": int(round(scheduled)), "quiet_s": int(round(quiet)),
                    "confidence": {"score": round(score, 2), "label": level_label(score)},
                    "stops": stops_out, "hops": hops_out,
                })
                in_bus_total += duration

        total_s = int(round(evaluated["arrive"])) - int(round(depart))
        transfers = max(len(route_ids) - 1, 0)
        quiet_in_bus = int(round(quiet_total))
        if len(route_ids) == 0:
            # Walk-only option (SPEC 9.4 field rules).
            quiet_in_bus = 0
            confidence = {"score": 1.0, "label": "high"}
            summary = "Walk"
            signature = "WALK"
        else:
            score = weighted_score / max(weighted_time, 1e-9)
            share = {}
            for level in range(1, 6):
                share[f"L{level}"] = round(float(level_time[level] / max(weighted_time, 1e-9)), 2)
            confidence = {"score": round(score, 2), "label": level_label(score), "level_share": share}
            summary = " > ".join(route_names)
            signature = ">".join(route_ids)
        quiet_delta = in_bus_total - quiet_in_bus
        if len(route_ids) == 0:
            quiet_delta = 0
        option = {
            "id": option_id,
            "signature": signature,
            "summary": summary,
            "depart_at": self.iso_at(ctx, depart),
            "arrive_at": self.iso_at(ctx, evaluated["arrive"]),
            "total_s": total_s,
            "walk_s": walk_total,
            "wait_s": wait_total,
            "in_bus_s": in_bus_total,
            "transfers": transfers,
            "scheduled_in_bus_s": int(round(scheduled_total)),
            "quiet_hour": {
                "label": bucket_label(qb),
                "bucket": qb,
                "in_bus_s": quiet_in_bus,
                "total_s": total_s - in_bus_total + quiet_in_bus,
                "delta_s": quiet_delta,
            },
            "live": None,
            "confidence": confidence,
            "legs": legs_out,
        }
        option["reliability"] = self.reliability(evaluated, ctx, total_s)
        option["day_profile"] = None
        if len(route_ids) > 0:
            values = []
            for bucket in range(config.PROFILE_FIRST_BUCKET, config.N_BUCKETS):
                if runs_hour[bucket // 2]:
                    values.append(round(float(profile_s[bucket]) / 60.0, 1))
                else:
                    values.append(None)
            option["day_profile"] = {"first_bucket": config.PROFILE_FIRST_BUCKET,
                                     "in_bus_min": values}
        option["best_departure"] = None
        if len(route_ids) > 0:
            option["best_departure"] = self.best_departure(skeleton, ctx, total_s)
        if ctx["mode"] == "live" and len(route_ids) > 0:
            typical = self.evaluate(skeleton, ctx, self.model.ones)
            typical_total = int(round(typical["arrive"])) - int(round(depart))
            option["live"] = {"typical_total_s": typical_total,
                              "delta_s": total_s - typical_total,
                              "city_factor": round(ctx["city_factor"], 2),
                              "source": ctx["live_source"], "updated_at": ctx["live_updated"]}
        elif ctx["mode"] == "live":
            option["live"] = {"typical_total_s": total_s, "delta_s": 0,
                              "city_factor": round(ctx["city_factor"], 2),
                              "source": ctx["live_source"], "updated_at": ctx["live_updated"]}
        return option

    def reliability(self, evaluated: dict, ctx: dict, total_s: int) -> dict:
        """How late an option can run: the time 9 in 10 such trips arrive by (phase 2).

        In-bus lateness uses the training-day 90th-percentile ratio of actual to
        estimated time for the hour; waiting lateness uses a 90th-percentile wait of a
        random arrival at the stop. The two are combined as independent errors.

        Args:
            evaluated: from evaluate().
            ctx: query context.
            total_s: the option's total seconds.
        Returns:
            {"label", "late_s", "safe_total_s", "safe_arrive_at"}.
        """
        p90 = self.model.reliability_p90
        bus_late = 0.0
        wait_late_sq = 0.0
        for leg in evaluated["legs"]:
            if leg["type"] == "bus":
                hour = int(leg["start"] // 3600) % 24
                ratio = 1.0
                if p90 is not None:
                    ratio = float(p90[ctx["day_type"], hour])
                bus_late += (leg["end"] - leg["start"]) * (ratio - 1.0)
            elif leg["type"] == "wait" and math.isfinite(leg["headway_s"]):
                planned = leg["end"] - leg["start"] - leg["change_s"]
                worst = min(config.WAIT_P90_SHARE * leg["headway_s"], 2.0 * config.MAX_WAIT_S)
                extra = max(worst - planned, 0.0)
                wait_late_sq += extra * extra
        late_s = int(round(math.sqrt(bus_late * bus_late + wait_late_sq)))
        label = "unreliable"
        if late_s <= config.RELIABLE_LATE_S:
            label = "reliable"
        elif late_s <= config.VARIABLE_LATE_S:
            label = "variable"
        safe_total = total_s + late_s
        return {"label": label, "late_s": late_s, "safe_total_s": safe_total,
                "safe_arrive_at": self.iso_at(ctx, ctx["depart"] + safe_total)[11:16]}

    def best_departure(self, skeleton: dict, ctx: dict, total_s: int) -> dict | None:
        """A nearby departure time when the same journey is clearly faster (phase 2).

        Args:
            skeleton: the option's legs.
            ctx: query context.
            total_s: the option's total at the requested time.
        Returns:
            {"depart_at": "HH:MM", "total_s", "saves_s"} or None.
        """
        best = None
        for offset_min in config.BEST_DEPART_OFFSETS_MIN:
            depart = ctx["depart"] + offset_min * 60
            if depart < config.SLIDER_FIRST_S or depart > config.SLIDER_LAST_S:
                continue
            trial = dict(ctx)
            trial["depart"] = depart
            trial["t_now"] = depart
            result = self.evaluate(skeleton, trial, self.model.ones)
            # Skip times when one of the buses does not run.
            runs = True
            for leg in result["legs"]:
                if leg["type"] == "wait" and not math.isfinite(leg["headway_s"]):
                    runs = False
            if not runs:
                continue
            trial_total = result["arrive"] - depart
            if best is None or trial_total < best[1]:
                best = (depart, trial_total)
        if best is None or total_s - best[1] < config.BEST_DEPART_MIN_SAVING_S:
            return None
        return {"depart_at": clock_text(best[0])[:5], "total_s": int(round(best[1])),
                "saves_s": int(round(total_s - best[1]))}

    def plan_arrive_by(self, origin_text: str, destination_text: str, arrive_at: str,
                       mode: str = "typical") -> dict:
        """Plan for a target arrival time: find when to leave (phase 2).

        Repeats the departure search a few times, moving the departure so that the
        best option's safe arrival (9 in 10 trips) meets the target.

        Args:
            origin_text, destination_text: as in plan().
            arrive_at: target arrival YYYY-MM-DDTHH:MM (IST).
            mode: "typical" or "live".
        Returns:
            plan() response with query.arrive_by and a leave_by time per option.
        """
        if arrive_at is None or not DEPART_PATTERN.match(arrive_at):
            raise PlanError("BAD_PARAM", "arrive time must be YYYY-MM-DDTHH:MM", 400)
        target = dt_mod.datetime.strptime(arrive_at, "%Y-%m-%dT%H:%M")
        midnight = dt_mod.datetime.combine(target.date(), dt_mod.time(0, 0))
        guess = max(target - dt_mod.timedelta(seconds=config.ARRIVE_BY_FIRST_GUESS_S), midnight)
        result = None
        for _ in range(config.ARRIVE_BY_ITERATIONS):
            result = self.plan(origin_text, destination_text, guess.strftime("%Y-%m-%dT%H:%M"), mode)
            if len(result["options"]) == 0:
                break
            safe = min(o["reliability"]["safe_total_s"] for o in result["options"])
            next_guess = max(target - dt_mod.timedelta(seconds=safe), midnight)
            next_guess = next_guess.replace(second=0, microsecond=0)
            if next_guess == guess:
                break
            guess = next_guess
        for option in result["options"]:
            leave = max(target - dt_mod.timedelta(seconds=option["reliability"]["safe_total_s"]), midnight)
            option["leave_by"] = leave.strftime("%H:%M")
        result["query"]["arrive_by"] = arrive_at
        return result

    def iso_at(self, ctx: dict, t: float) -> str:
        """ISO datetime (IST, no zone) of a clock time on the departure date.

        Args:
            ctx: query context with "date".
            t: seconds since 00:00 of that date.
        Returns:
            YYYY-MM-DDTHH:MM:SS text.
        """
        midnight = dt_mod.datetime.combine(ctx["date"], dt_mod.time(0, 0))
        moment = midnight + dt_mod.timedelta(seconds=int(round(t)))
        return moment.strftime("%Y-%m-%dT%H:%M:%S")

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def plan(self, origin_text: str, destination_text: str, depart_at: str,
             mode: str = "typical") -> dict:
        """Plan a journey (SPEC 8.3) and return the SPEC 9.4 response.

        Args:
            origin_text: "lat,lon" or stop name.
            destination_text: same.
            depart_at: YYYY-MM-DDTHH:MM in IST.
            mode: "typical" or "live".
        Returns:
            Response dict.
        """
        started = time.perf_counter()
        # Validate parameters.
        if origin_text is None or destination_text is None or depart_at is None:
            raise PlanError("BAD_PARAM", "origin, destination and depart_at are required", 400)
        if not DEPART_PATTERN.match(depart_at):
            raise PlanError("BAD_PARAM", "depart_at must be YYYY-MM-DDTHH:MM", 400)
        try:
            moment = dt_mod.datetime.strptime(depart_at, "%Y-%m-%dT%H:%M")
        except ValueError:
            raise PlanError("BAD_PARAM", "depart_at must be YYYY-MM-DDTHH:MM", 400)
        if mode not in ["typical", "live"]:
            raise PlanError("BAD_PARAM", "mode must be typical or live", 400)
        date_iso = moment.date().isoformat()
        replay_date = self.model.meta.get("replay_date", config.REPLAY_DATE)
        # Live mode: today's date uses the real-time feed, the replay date uses the replay.
        live_source = None
        if mode == "live":
            today = ist_now().date().isoformat()
            if self.live_engine is not None and self.live_engine.key is not None and date_iso == today:
                if not self.live_engine.is_fresh():
                    raise PlanError("LIVE_UNAVAILABLE",
                                    "the real-time feed is warming up or unreachable; try again "
                                    "in a minute or use Typical", 503)
                live_source = "feed"
            elif date_iso == replay_date:
                if self.model.live_factors is None:
                    raise PlanError("BAD_PARAM", "live mode is not built yet", 400)
                live_source = "replay"
            else:
                raise PlanError("LIVE_DATE_MISMATCH",
                                f"live is available for today (real-time feed) or {replay_date} "
                                "(replay)", 400)
        origin = self.resolve_point(origin_text, "origin")
        destination = self.resolve_point(destination_text, "destination")

        # Query context shared by every run.
        day_type = config.day_type_of(date_iso)
        depart = float(moment.hour * 3600 + moment.minute * 60)
        r = self.model.ones
        city_factor = 1.0
        t_now = depart
        live_updated = None
        if live_source == "replay":
            r, city_factor = self.model.live_row(depart)
        elif live_source == "feed":
            state = self.live_engine.snapshot()
            r = state["factors"]
            city_factor = state["city"]
            # The live effect fades from the current time, not from the departure time.
            now = ist_now()
            t_now = float(now.hour * 3600 + now.minute * 60 + now.second)
            updated = dt_mod.datetime.fromtimestamp(state["updated_unix"], dt_mod.timezone.utc)
            live_updated = (updated + dt_mod.timedelta(seconds=config.IST_OFFSET_S)).strftime("%H:%M")
        acc_stops, acc_s, acc_m = self.nearby_stops(origin["lat"], origin["lon"],
                                                    "NO_STOPS_NEAR_ORIGIN")
        egr_stops, egr_s, egr_m = self.nearby_stops(destination["lat"], destination["lon"],
                                                    "NO_STOPS_NEAR_DESTINATION")
        ctx = {
            "origin": origin, "destination": destination, "date": moment.date(),
            "day_type": day_type, "depart": depart, "t_now": t_now, "mode": mode,
            "live_source": live_source, "live_updated": live_updated,
            "seg_time_dt": self.model.seg_time[day_type], "r": r, "city_factor": city_factor,
            "acc_stops": acc_stops, "acc_s": acc_s, "egr_stops": egr_stops, "egr_s": egr_s,
        }

        # Run A, then B and C with the routes of the earliest candidates banned.
        candidates = []
        runs = 0
        out_a = self.run_raptor(self.no_ban, ctx)
        runs += 1
        found_a = self.extract_candidates(out_a, ctx)
        candidates.extend(found_a)
        if len(found_a) > 0:
            routes_a = self.routes_of(self.earliest(found_a, ctx))
            banned_b = self.ban_routes(routes_a)
            found_b = self.extract_candidates(self.run_raptor(banned_b, ctx), ctx)
            runs += 1
            candidates.extend(found_b)
            routes_ab = set(routes_a)
            if len(found_b) > 0:
                routes_ab.update(self.routes_of(self.earliest(found_b, ctx)))
            banned_c = self.ban_routes(routes_ab)
            found_c = self.extract_candidates(self.run_raptor(banned_c, ctx), ctx)
            runs += 1
            candidates.extend(found_c)

        # Evaluate each candidate forward in the request mode.
        evaluated = []
        for skeleton in candidates:
            result = self.evaluate(skeleton, ctx, r)
            signature = ">".join(str(self.net.route_id[self.net.pat_route[leg[0]]])
                                 for leg in skeleton["bus"])
            evaluated.append({"skeleton": skeleton, "eval": result, "signature": signature,
                              "transfers": len(skeleton["bus"]) - 1})
        # Walk-only option for short trips.
        straight_od = float(geo.haversine_m(origin["lat"], origin["lon"],
                                            destination["lat"], destination["lon"]))
        if straight_od <= config.WALK_ONLY_MAX_M:
            duration = walk_seconds(straight_od)
            walk_leg = {"type": "walk", "from": None, "to": None, "start": depart,
                        "end": depart + duration, "distance_m": config.WALK_DETOUR * straight_od}
            evaluated.append({"skeleton": {"access": None, "bus": [], "egress": None},
                              "eval": {"legs": [walk_leg], "arrive": depart + duration},
                              "signature": "WALK", "transfers": 0})

        # Step 7: de-duplicate by signature, keeping the earliest arrival.
        by_signature = {}
        for item in evaluated:
            key = item["signature"]
            if key not in by_signature or item["eval"]["arrive"] < by_signature[key]["eval"]["arrive"]:
                by_signature[key] = item
        unique = list(by_signature.values())
        options = []
        if len(unique) > 0:
            # Step 8: drop weak options; step 9: rank with the transfer penalty.
            best_total = min(item["eval"]["arrive"] - depart for item in unique)
            limit = config.OPTION_SLACK_FACTOR * best_total + config.OPTION_SLACK_S
            kept = [item for item in unique if item["eval"]["arrive"] - depart <= limit]
            kept.sort(key=rank_key)
            for index, item in enumerate(kept[:config.MAX_OPTIONS]):
                options.append(self.option_json(index, item["eval"], item["skeleton"], ctx))
        reason = None
        if len(options) == 0:
            reason = "NO_JOURNEY"
        compute_ms = int(round((time.perf_counter() - started) * 1000))
        return {
            "query": {
                "origin": {"label": origin["label"], "lat": round5(origin["lat"]),
                           "lon": round5(origin["lon"])},
                "destination": {"label": destination["label"], "lat": round5(destination["lat"]),
                                "lon": round5(destination["lon"])},
                "depart_at": depart_at,
                "day_type": config.DAY_TYPE_NAMES[day_type],
                "mode": mode,
            },
            "reason": reason,
            "options": options,
            "meta": {"compute_ms": compute_ms, "replay_date": replay_date, "raptor_runs": runs},
        }

    def earliest(self, found: list[dict], ctx: dict) -> dict:
        """Candidate with the earliest arrival (typical evaluation in the request mode).

        Args:
            found: candidate skeletons of one run.
            ctx: query context.
        Returns:
            The earliest skeleton.
        """
        best = found[0]
        best_arrive = INF
        for skeleton in found:
            arrive = self.evaluate(skeleton, ctx, ctx["r"])["arrive"]
            if arrive < best_arrive:
                best_arrive = arrive
                best = skeleton
        return best

    def routes_of(self, skeleton: dict) -> list[int]:
        """Route indices used by a skeleton's bus legs.

        Args:
            skeleton: candidate.
        Returns:
            List of route indices.
        """
        return [int(self.net.pat_route[leg[0]]) for leg in skeleton["bus"]]

    def ban_routes(self, routes) -> np.ndarray:
        """Bool mask banning every pattern of the given routes.

        Args:
            routes: iterable of route indices.
        Returns:
            bool array per pattern.
        """
        banned = np.zeros(self.net.n_patterns, dtype=np.bool_)
        for route in routes:
            banned[self.route_patterns[route]] = True
        return banned

    def warmup(self) -> None:
        """Compile the numba functions once with tiny inputs (SPEC 8.4).

        Returns:
            None.
        """
        empty_i = np.zeros(0, dtype=np.int64)
        empty_f = np.zeros(0, dtype=np.float64)
        ctx = {"seg_time_dt": self.model.seg_time[0], "r": self.model.ones, "t_now": 0.0,
               "acc_stops": empty_i, "acc_s": empty_f, "egr_stops": empty_i, "egr_s": empty_f,
               "depart": 0.0}
        self.run_raptor(self.no_ban, ctx)
        scratch = np.empty(4, dtype=np.float64)
        segs = np.zeros(1, dtype=np.int32)
        walk_segments(self.model.seg_time[0], segs, 0.0, self.model.ones, 0.0,
                      self.model.live_horizon_s, scratch)
        walk_timetable(self.net.sched_time, segs, 0.0, scratch)


def rank_key(item: dict) -> float:
    """Ranking value: total time plus the transfer penalty (SPEC 8.3 step 9).

    Args:
        item: evaluated candidate.
    Returns:
        Seconds.
    """
    total = item["eval"]["arrive"] - item["eval"]["legs"][0]["start"]
    return total + config.RANK_TRANSFER_PENALTY_S * item["transfers"]


def bucket_label(bucket: int) -> str:
    """HH:MM of the start of a 30-minute bucket.

    Args:
        bucket: 0..47.
    Returns:
        Text.
    """
    minutes = bucket * 30
    return f"{minutes // 60:02d}:{minutes % 60:02d}"
