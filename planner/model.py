"""
planner/model.py - travel-time model in memory: hop time and the walker (SPEC 7.6).

Purpose: load seg_time, seg_level, model_meta and (optionally) live_factors, and provide
the one numba walker used by RAPTOR, reconstruction, calibration, and validation.
Inputs: data/model/seg_time.npy, seg_level.npy, model_meta.json, live_factors.npy.
Outputs: a Model instance and numba functions.
"""

import json
import math
from pathlib import Path

import numpy as np
from numba import njit

import config


@njit(cache=True)
def hop_time(seg_time_dt: np.ndarray, g: int, t: float, r: np.ndarray, t_now: float,
             horizon_s: float) -> float:
    """Seconds to traverse segment g entered at clock t (SPEC 7.6).

    Args:
        seg_time_dt: [48, G] table for the day type.
        g: segment index.
        t: entry time, seconds since 00:00 IST of the departure date.
        r: live ratio per segment (all ones in typical mode).
        t_now: departure time (start of the live fade).
        horizon_s: LIVE_HORIZON_S.
    Returns:
        Hop seconds.
    """
    bucket = int(t // 1800.0) % 48
    base = seg_time_dt[bucket, g]
    ahead = t - t_now
    if ahead < 0.0:
        ahead = 0.0
    decay = math.exp(-ahead / horizon_s)
    return base * (1.0 + (r[g] - 1.0) * decay)


@njit(cache=True)
def walk_segments(seg_time_dt: np.ndarray, segs: np.ndarray, t_start: float, r: np.ndarray,
                  t_now: float, horizon_s: float, hop_out: np.ndarray) -> float:
    """The walker: add hop times one after another starting at t_start (SPEC 7.6).

    Args:
        seg_time_dt: [48, G] table.
        segs: segment indices in travel order.
        t_start: clock time at the first segment.
        r, t_now, horizon_s: live parameters (see hop_time).
        hop_out: receives the per-hop seconds (length >= len(segs)).
    Returns:
        Total seconds.
    """
    t = t_start
    for i in range(segs.shape[0]):
        h = hop_time(seg_time_dt, segs[i], t, r, t_now, horizon_s)
        hop_out[i] = h
        t += h
    return t - t_start


@njit(cache=True)
def walk_timetable(sched_time: np.ndarray, segs: np.ndarray, t_start: float,
                   hop_out: np.ndarray) -> float:
    """Timetable walker: hop = sched_time[hour of entry, g] (SPEC 7.6).

    Args:
        sched_time: [24, G] timetable seconds.
        segs: segment indices in travel order.
        t_start: clock time at the first segment.
        hop_out: receives per-hop seconds.
    Returns:
        Total seconds.
    """
    t = t_start
    for i in range(segs.shape[0]):
        hour = int(t // 3600.0) % 24
        h = sched_time[hour, segs[i]]
        hop_out[i] = h
        t += h
    return t - t_start


@njit(cache=True)
def predict_runs(seg_time_all: np.ndarray, seg_flat: np.ndarray, run_ptr: np.ndarray,
                 run_t0: np.ndarray, run_dt: np.ndarray, live_rows: np.ndarray,
                 run_tick: np.ndarray, horizon_s: float) -> np.ndarray:
    """Walker over many spans at once (calibration and validation).

    Args:
        seg_time_all: [3, 48, G] table.
        seg_flat: concatenated segment indices of all spans.
        run_ptr: CSR pointer into seg_flat, length n + 1.
        run_t0: start clock time of each span (seconds of day IST).
        run_dt: day type of each span.
        live_rows: [T, G] live factors, or a [1, G] array of ones for typical mode.
        run_tick: row of live_rows to use per span (-1 = typical, r = 1).
        horizon_s: LIVE_HORIZON_S.
    Returns:
        Predicted seconds per span.
    """
    n = run_t0.shape[0]
    out = np.empty(n, dtype=np.float64)
    n_seg = seg_time_all.shape[2]
    ones = np.ones(n_seg, dtype=np.float32)
    scratch = np.empty(4096, dtype=np.float64)
    for i in range(n):
        segs = seg_flat[run_ptr[i]:run_ptr[i + 1]]
        if scratch.shape[0] < segs.shape[0]:
            scratch = np.empty(segs.shape[0], dtype=np.float64)
        r = ones
        if run_tick[i] >= 0:
            r = live_rows[run_tick[i]]
        out[i] = walk_segments(seg_time_all[run_dt[i]], segs, run_t0[i], r, run_t0[i],
                               horizon_s, scratch)
    return out


@njit(cache=True)
def timetable_runs(sched_time: np.ndarray, seg_flat: np.ndarray, run_ptr: np.ndarray,
                   run_t0: np.ndarray) -> np.ndarray:
    """Timetable walker over many spans (validation baseline).

    Args:
        sched_time: [24, G].
        seg_flat, run_ptr, run_t0: spans as in predict_runs.
    Returns:
        Timetable seconds per span.
    """
    n = run_t0.shape[0]
    out = np.empty(n, dtype=np.float64)
    scratch = np.empty(4096, dtype=np.float64)
    for i in range(n):
        segs = seg_flat[run_ptr[i]:run_ptr[i + 1]]
        if scratch.shape[0] < segs.shape[0]:
            scratch = np.empty(segs.shape[0], dtype=np.float64)
        out[i] = walk_timetable(sched_time, segs, run_t0[i], scratch)
    return out


class Model:
    """Travel-time tables, fallback levels, metadata, and live replay factors."""

    def __init__(self, model_dir: Path = config.MODEL_DIR) -> None:
        """Load the model artifacts.

        Args:
            model_dir: folder with seg_time.npy, seg_level.npy, model_meta.json.
        Returns:
            None.
        """
        self.seg_time = np.load(model_dir / "seg_time.npy").astype(np.float32)
        self.seg_level = np.load(model_dir / "seg_level.npy").astype(np.uint8)
        with open(model_dir / "model_meta.json", "r", encoding="utf-8") as handle:
            self.meta = json.load(handle)
        self.quiet_bucket = [int(b) for b in self.meta["quiet_bucket"]]
        self.live_horizon_s = float(self.meta.get("live_horizon_s", config.LIVE_HORIZON_S))
        self.live_factors = None
        self.live_city = None
        live_path = model_dir / "live_factors.npy"
        if live_path.exists():
            self.live_factors = np.load(live_path).astype(np.float32)
            self.live_city = self.meta.get("live_city_ratio")
        self.ones = np.ones(self.seg_time.shape[2], dtype=np.float32)
        # Training-day 90th-percentile ratio actual / estimate per (day type, hour).
        self.reliability_p90 = None
        if "reliability_p90" in self.meta:
            self.reliability_p90 = np.array(self.meta["reliability_p90"], dtype=np.float64)
        self.level_scores = np.array(config.LEVEL_SCORES, dtype=np.float64)

    def live_row(self, depart_sod: float) -> tuple[np.ndarray, float]:
        """Live ratios for the tick of a departure time (SPEC 7.8).

        Args:
            depart_sod: departure, seconds of day IST.
        Returns:
            Tuple (r array, city factor).
        """
        tick = int(depart_sod // config.LIVE_TICK_S)
        if tick >= config.LIVE_N_TICKS:
            tick = config.LIVE_N_TICKS - 1
        city = 1.0
        if self.live_city is not None:
            city = float(self.live_city[tick])
        return self.live_factors[tick], city
