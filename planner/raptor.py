"""
planner/raptor.py - numba RAPTOR for frequency-based service (SPEC 8.4).

Purpose: earliest-arrival search over up to MAX_ROUNDS bus legs with time-dependent
hop times, half-headway waits, change time, and one footpath step per round.
Inputs: flat numpy arrays from network.npz and the model, plus the query.
Outputs: tau and parent arrays per (round, stop).
"""

import numpy as np
from numba import njit

from planner.model import hop_time

INF = 1e18


@njit(cache=True)
def raptor(pat_ptr: np.ndarray, pat_stops: np.ndarray, pat_seg: np.ndarray,
           headway_s: np.ndarray, sp_ptr: np.ndarray, sp_pat: np.ndarray, sp_pos: np.ndarray,
           foot_ptr: np.ndarray, foot_to: np.ndarray, foot_s: np.ndarray,
           seg_time_dt: np.ndarray, r: np.ndarray, t_now: float, horizon_s: float,
           banned: np.ndarray, access_stops: np.ndarray, access_s: np.ndarray,
           egress_stops: np.ndarray, egress_s: np.ndarray, depart: float,
           max_rounds: int, max_wait_s: float, change_s: float) -> tuple:
    """Run one RAPTOR query (SPEC 8.4).

    Args:
        pat_ptr, pat_stops, pat_seg: pattern CSR arrays.
        headway_s: [P, 24] headways, inf when not running.
        sp_ptr, sp_pat, sp_pos: stop -> (pattern, position) CSR.
        foot_ptr, foot_to, foot_s: footpath CSR.
        seg_time_dt: [48, G] model table for the day type.
        r, t_now, horizon_s: live parameters (r all ones in typical mode).
        banned: bool per pattern; banned patterns are never boarded.
        access_stops, access_s: access stops and their walk seconds from the origin.
        egress_stops, egress_s: egress stops and their walk seconds to the destination.
        depart: departure clock, seconds since 00:00 IST.
        max_rounds: MAX_ROUNDS.
        max_wait_s: MAX_WAIT_S.
        change_s: TRANSFER_CHANGE_S.
    Returns:
        Tuple (tau, par_pat, par_board_pos, par_alight_pos, par_board_time, par_foot_from),
        each with shape [max_rounds + 1, S].
    """
    n_stops = sp_ptr.shape[0] - 1
    n_pat = pat_ptr.shape[0] - 1
    k_max = max_rounds + 1
    tau = np.full((k_max, n_stops), INF)
    best = np.full(n_stops, INF)
    par_pat = np.full((k_max, n_stops), -1, dtype=np.int32)
    par_board_pos = np.full((k_max, n_stops), -1, dtype=np.int32)
    par_alight_pos = np.full((k_max, n_stops), -1, dtype=np.int32)
    par_board_time = np.full((k_max, n_stops), INF)
    par_foot_from = np.full((k_max, n_stops), -1, dtype=np.int32)
    marked = np.zeros(n_stops, dtype=np.bool_)

    # Round 0: walk from the origin to each access stop.
    for i in range(access_stops.shape[0]):
        s = access_stops[i]
        t = depart + access_s[i]
        if t < tau[0, s]:
            tau[0, s] = t
            best[s] = t
            marked[s] = True

    best_target = INF
    pat_start = np.full(n_pat, 1 << 30, dtype=np.int64)
    queue = np.empty(n_pat, dtype=np.int64)
    bus_marked = np.zeros(n_stops, dtype=np.bool_)
    for k in range(1, k_max):
        # 1) Collect patterns through marked stops, earliest position per pattern.
        n_queue = 0
        for s in range(n_stops):
            if not marked[s]:
                continue
            for e in range(sp_ptr[s], sp_ptr[s + 1]):
                p = sp_pat[e]
                if banned[p]:
                    continue
                pos = sp_pos[e]
                if pat_start[p] == (1 << 30):
                    queue[n_queue] = p
                    n_queue += 1
                    pat_start[p] = pos
                elif pos < pat_start[p]:
                    pat_start[p] = pos
        marked[:] = False
        bus_marked[:] = False

        # 2) Scan each collected pattern from its first marked position to its end.
        for qi in range(n_queue):
            p = queue[qi]
            pos0 = pat_start[p]
            pat_start[p] = 1 << 30
            base = pat_ptr[p]
            last = pat_ptr[p + 1] - base - 1
            on_time = INF
            board_pos = -1
            board_time = INF
            for pos in range(pos0, last + 1):
                s = pat_stops[base + pos]
                # Alight here if the bus beats every known arrival and the target bound.
                if on_time < INF:
                    bound = best[s]
                    if best_target < bound:
                        bound = best_target
                    if on_time < bound:
                        tau[k, s] = on_time
                        best[s] = on_time
                        par_pat[k, s] = p
                        par_board_pos[k, s] = board_pos
                        par_alight_pos[k, s] = pos
                        par_board_time[k, s] = board_time
                        par_foot_from[k, s] = -1
                        bus_marked[s] = True
                # Board here if we arrived with k-1 bus legs and this beats the current bus.
                prev = tau[k - 1, s]
                if prev < INF:
                    h = headway_s[p, int(prev // 3600.0) % 24]
                    if h < INF:
                        wait = h / 2.0
                        if wait > max_wait_s:
                            wait = max_wait_s
                        cand = prev + wait
                        if k > 1:
                            cand += change_s
                        if cand < on_time:
                            on_time = cand
                            board_pos = pos
                            board_time = cand
                # Ride on to the next stop with the time-dependent hop time.
                if on_time < INF and pos < last:
                    on_time += hop_time(seg_time_dt, pat_seg[base + pos], on_time, r, t_now,
                                        horizon_s)

        # 3) One footpath step from stops improved by bus in this round (no chaining).
        # Bus arrivals are copied first so a walk never starts from a walk.
        any_marked = False
        n_src = 0
        for s in range(n_stops):
            if bus_marked[s]:
                n_src += 1
        src_stop = np.empty(n_src, dtype=np.int64)
        src_time = np.empty(n_src)
        j = 0
        for s in range(n_stops):
            if bus_marked[s]:
                src_stop[j] = s
                src_time[j] = tau[k, s]
                marked[s] = True
                any_marked = True
                j += 1
        for j in range(n_src):
            s = src_stop[j]
            for e in range(foot_ptr[s], foot_ptr[s + 1]):
                n = foot_to[e]
                t = src_time[j] + foot_s[e]
                bound = best[n]
                if best_target < bound:
                    bound = best_target
                if t < bound:
                    tau[k, n] = t
                    best[n] = t
                    par_foot_from[k, n] = s
                    par_pat[k, n] = -1
                    marked[n] = True

        # 4) Best arrival at the destination so far, used to prune later alightings.
        for i in range(egress_stops.shape[0]):
            s = egress_stops[i]
            t = best[s] + egress_s[i]
            if t < best_target:
                best_target = t

        # 5) Stop early when no stop improved.
        if not any_marked:
            break
    return tau, par_pat, par_board_pos, par_alight_pos, par_board_time, par_foot_from
