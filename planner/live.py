"""
planner/live.py - real-time engine on the Delhi GTFS-RT feed (SPEC 7.8 "real feed" path).

Purpose: a background thread polls VehiclePositions every LIVE_POLL_S seconds, keeps a
rolling LIVE_BUFFER_S history per bus, and every LIVE_RECOMPUTE_S seconds runs the same
projection and run logic as pipeline/match.py on that buffer. The resulting segment
observations feed the same live-factor formula as the replay (SPEC 7.8), evaluated at
the current time. The planner reads the latest factors; nothing else changes.
Inputs: secrets/otd_api_key.txt (the private key, never logged), network, model.
Outputs: LiveEngine.snapshot() with factors, city ratio, vehicles, and status.
"""

import datetime as dt_mod
import threading
import time
import urllib.request

import numpy as np

import config
from pipeline import geo
from pipeline.match import project_work, runs_and_obs
from planner.gtfs_rt import decode_feed
from planner.model import Model
from planner.network import Network


def ist_now() -> dt_mod.datetime:
    """Current wall-clock time in IST (naive datetime).

    Returns:
        datetime.
    """
    return dt_mod.datetime.now(dt_mod.timezone.utc).replace(tzinfo=None) + \
        dt_mod.timedelta(seconds=config.IST_OFFSET_S)


class LiveEngine:
    """Polls the real-time feed and maintains live factors for today."""

    def __init__(self, net: Network, model: Model) -> None:
        """Prepare lookups; the thread starts with start().

        Args:
            net: network.
            model: model (expected times for ratios).
        Returns:
            None.
        """
        self.net = net
        self.model = model
        self.key = None
        if config.OTD_KEY_PATH.exists():
            self.key = config.OTD_KEY_PATH.read_text(encoding="utf-8").strip()
        self.route_index = {}
        trips = np.zeros(net.n_routes, dtype=np.int64)
        np.add.at(trips, net.pat_route, net.pat_trips)
        # When several routes share a key, the route with the most trips wins (SPEC 6.3).
        for index in np.argsort(trips, kind="stable"):
            self.route_index[str(net.route_key[index])] = int(index)
        order = np.argsort(net.pat_route, kind="stable")
        self.route_pat_order = order
        self.route_pat_ptr = np.zeros(net.n_routes + 1, dtype=np.int64)
        self.route_pat_ptr[1:] = np.cumsum(np.bincount(net.pat_route, minlength=net.n_routes))
        self.poly_x = net.poly_x.astype(np.float64)
        self.poly_y = net.poly_y.astype(np.float64)
        self.poly_cum = net.poly_cum.astype(np.float64)
        self.pat_stop_dist = net.pat_stop_dist.astype(np.float64)
        self.lock = threading.Lock()
        self.buffer = {}
        self.latest = {}
        self.state = {
            "enabled": self.key is not None, "connected": False, "error": None,
            "last_fetch_unix": None, "feed_unix": None, "updated_unix": None,
            "vehicles": 0, "observations": 0, "factors": None, "city": 1.0,
        }
        self.thread = None
        self.stop_event = threading.Event()

    def start(self) -> None:
        """Start the polling thread when a key is configured.

        Returns:
            None.
        """
        if self.key is None:
            return
        self.thread = threading.Thread(target=self.loop, name="live-feed", daemon=True)
        self.thread.start()

    def loop(self) -> None:
        """Poll, buffer, and recompute until stopped.

        Returns:
            None.
        """
        last_compute = 0.0
        while not self.stop_event.is_set():
            started = time.time()
            try:
                self.poll()
                if started - last_compute >= config.LIVE_RECOMPUTE_S:
                    self.recompute()
                    last_compute = started
            except Exception as error:  # the feed can drop; keep the last good factors
                with self.lock:
                    self.state["connected"] = False
                    self.state["error"] = type(error).__name__ + ": " + str(error)[:200]
            self.stop_event.wait(max(1.0, config.LIVE_POLL_S - (time.time() - started)))

    def poll(self) -> None:
        """Fetch one feed snapshot and add new points to the per-bus buffer.

        Returns:
            None.
        """
        url = config.OTD_FEED_URL + "?key=" + self.key
        request = urllib.request.Request(url, headers={"User-Agent": "BusPulse/1.0"})
        with urllib.request.urlopen(request, timeout=config.LIVE_HTTP_TIMEOUT_S) as response:
            payload = response.read()
        feed_ts, rows = decode_feed(payload)
        now = time.time()
        cutoff = now - config.LIVE_BUFFER_S
        with self.lock:
            for row in rows:
                if row["lat"] is None or row["ts"] is None or row["vehicle_id"] is None:
                    continue
                inside = config.BBOX_LAT_MIN <= row["lat"] <= config.BBOX_LAT_MAX and \
                    config.BBOX_LON_MIN <= row["lon"] <= config.BBOX_LON_MAX
                route = self.route_index.get((row["route_id"] or "").strip())
                if not inside or route is None:
                    continue
                points = self.buffer.setdefault(row["vehicle_id"], [])
                # Keep one point per timestamp (the feed repeats unchanged positions).
                if len(points) == 0 or row["ts"] > points[-1][0]:
                    points.append((int(row["ts"]), route, float(row["lat"]), float(row["lon"])))
                self.latest[row["vehicle_id"]] = (int(row["ts"]), route, float(row["lat"]),
                                                  float(row["lon"]))
            # Drop old points and buses that stopped reporting.
            for vehicle in list(self.buffer.keys()):
                kept = [p for p in self.buffer[vehicle] if p[0] >= cutoff]
                if len(kept) == 0:
                    del self.buffer[vehicle]
                else:
                    self.buffer[vehicle] = kept
            for vehicle in list(self.latest.keys()):
                if self.latest[vehicle][0] < now - config.LIVE_VEHICLE_STALE_S:
                    del self.latest[vehicle]
            self.state["connected"] = True
            self.state["error"] = None
            self.state["last_fetch_unix"] = now
            self.state["feed_unix"] = feed_ts
            self.state["vehicles"] = len(self.latest)

    def recompute(self) -> None:
        """Match the buffer to segments and recompute today's live factors (SPEC 7.8).

        Returns:
            None.
        """
        with self.lock:
            snapshot = {vehicle: list(points) for vehicle, points in self.buffer.items()}
        rows = []
        for vcode, (vehicle, points) in enumerate(sorted(snapshot.items())):
            for ts, route, lat, lon in points:
                rows.append((route, vcode, ts, lat, lon))
        if len(rows) < 2:
            return
        data = np.array(rows, dtype=np.float64)
        order = np.lexsort((data[:, 2], data[:, 1], data[:, 0]))
        data = data[order]
        route_idx = data[:, 0].astype(np.int64)
        vcode = data[:, 1].astype(np.int64)
        ts = data[:, 2].astype(np.int64)
        px, py = geo.to_xy(data[:, 3], data[:, 4])

        # Groups of (route, bus) and one work item per pattern of the route, as in match.py.
        change = np.ones(ts.shape[0], dtype=bool)
        change[1:] = (route_idx[1:] != route_idx[:-1]) | (vcode[1:] != vcode[:-1])
        g_start = np.flatnonzero(change)
        g_end = np.append(g_start[1:], ts.shape[0])
        g_route = route_idx[g_start]
        per_group = self.route_pat_ptr[g_route + 1] - self.route_pat_ptr[g_route]
        work_group = np.repeat(np.arange(g_start.shape[0]), per_group)
        first_work = np.repeat(np.cumsum(per_group) - per_group, per_group)
        within = np.arange(work_group.shape[0]) - first_work
        work_pat = self.route_pat_order[self.route_pat_ptr[g_route[work_group]] + within].astype(np.int64)
        work_start = g_start[work_group].astype(np.int64)
        work_end = g_end[work_group].astype(np.int64)
        sizes = work_end - work_start
        work_off = np.zeros(sizes.shape[0], dtype=np.int64)
        work_off[1:] = np.cumsum(sizes)[:-1]
        d_all = np.empty(int(sizes.sum()), dtype=np.float64)
        off_all = np.empty(int(sizes.sum()), dtype=np.float64)
        project_work(px, py, work_start, work_end, work_pat, work_off,
                     self.net.poly_ptr.astype(np.int64), self.poly_x, self.poly_y, self.poly_cum,
                     d_all, off_all)
        result = runs_and_obs(ts, vcode, work_start, work_end, work_pat, work_off, d_all, off_all,
                              self.net.pat_ptr.astype(np.int64), self.net.pat_seg.astype(np.int64),
                              self.pat_stop_dist, float(config.load_findings().get("max_offset_m", config.MAX_OFFSET_M_DEFAULT)),
                              float(config.RUN_GAP_S_DEFAULT), float(config.BACKTRACK_M),
                              config.MIN_RUN_SEGS, float(config.MIN_RUN_M))
        o_seg = result[4]
        o_tenter = result[5]
        o_tt = result[6]

        # Plausibility filter of SPEC 6.4 step 7.
        seg_len = self.net.seg_len_m[o_seg].astype(np.float64)
        speed = np.zeros(o_tt.shape[0])
        positive = o_tt > 0
        speed[positive] = seg_len[positive] / o_tt[positive] * 3.6
        valid = positive & (o_tt <= config.OBS_MAX_S) & (speed >= config.OBS_MIN_KMH) & \
            (speed <= config.OBS_MAX_KMH)
        g = o_seg[valid]
        tt = o_tt[valid]
        sod_enter = (o_tenter[valid] + config.IST_OFFSET_S) % config.SECONDS_PER_DAY
        now_ist = ist_now()
        day_type = config.day_type_of(now_ist.date().isoformat())
        bucket = (sod_enter // config.BUCKET_S).astype(np.int64) % config.N_BUCKETS
        expected = self.model.seg_time[day_type, bucket, g].astype(np.float64)
        ratio = np.clip(tt / expected, config.LIVE_RATIO_MIN, config.LIVE_RATIO_MAX)

        # SPEC 7.8 formula at T = now, using observations that finished in the window.
        t_exit = o_tenter[valid] + tt
        now = time.time()
        in_window = (t_exit > now - config.LIVE_WINDOW_S) & (t_exit <= now)
        w = np.exp(-(now - t_exit[in_window]) / config.LIVE_DECAY_S)
        wr = w * ratio[in_window]
        r_city = (wr.sum() + config.CITY_PRIOR_K) / (w.sum() + config.CITY_PRIOR_K)
        n_seg = self.net.n_segments
        sum_w = np.bincount(g[in_window], weights=w, minlength=n_seg)
        sum_wr = np.bincount(g[in_window], weights=wr, minlength=n_seg)
        prior_k = float(self.model.meta.get("live_prior_k", config.LIVE_PRIOR_K))
        factors = (sum_wr + prior_k * r_city) / (sum_w + prior_k)
        factors = np.clip(factors, config.LIVE_RATIO_MIN, config.LIVE_RATIO_MAX).astype(np.float32)
        with self.lock:
            self.state["factors"] = factors
            self.state["city"] = float(r_city)
            self.state["observations"] = int(np.count_nonzero(in_window))
            self.state["updated_unix"] = now

    def snapshot(self) -> dict:
        """Copy of the current state (factors array shared, read-only).

        Returns:
            Dict.
        """
        with self.lock:
            return dict(self.state)

    def is_fresh(self) -> bool:
        """True when factors exist and were updated recently.

        Returns:
            Bool.
        """
        state = self.snapshot()
        if state["factors"] is None or state["updated_unix"] is None:
            return False
        return time.time() - state["updated_unix"] <= config.LIVE_FRESH_S

    def vehicles(self) -> list[dict]:
        """Latest position of every bus reporting recently.

        Returns:
            List of {"id", "lat", "lon", "route_name", "age_s"}.
        """
        now = time.time()
        with self.lock:
            items = list(self.latest.items())
        out = []
        for vehicle, (ts, route, lat, lon) in items:
            out.append({"id": vehicle, "lat": round(lat, 5), "lon": round(lon, 5),
                        "route_name": str(self.net.route_name[route]), "age_s": int(now - ts)})
        return out
