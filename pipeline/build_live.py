"""
pipeline/build_live.py - simulated live replay factors for the replay date (SPEC 7.8).

Purpose: for every 5-minute tick of the replay day, the per-segment ratio of recently
observed to expected travel time, exponentially weighted and shrunk toward the citywide
ratio. Only observations finished at or before the tick are used (no look-ahead).
Inputs: data/clean/seg_obs/<replay date>.parquet (valid rows), data/model/seg_time.npy.
Outputs: data/model/live_factors.npy [288, G] float32; model_meta.json gains
live_city_ratio [288], live_prior_k, live_horizon_s.
Run: python -m pipeline.build_live [--prior-k 2.0] [--horizon-s 1800]
"""

import argparse
import json

import duckdb
import numpy as np

import config


def log(message: str) -> None:
    """Print one progress line immediately.

    Args:
        message: text.
    Returns:
        None.
    """
    print(message, flush=True)


def compute_factors(replay_date: str, seg_time: np.ndarray, prior_k: float) -> tuple:
    """Live factors per tick and segment (SPEC 7.8 formulas).

    Args:
        replay_date: YYYY-MM-DD.
        seg_time: final [3, 48, G] table.
        prior_k: LIVE_PRIOR_K.
    Returns:
        Tuple (factors [288, G] float32, city ratio [288] float64).
    """
    n_seg = seg_time.shape[2]
    path = (config.SEG_OBS_DIR / f"{replay_date}.parquet").relative_to(config.ROOT).as_posix()
    # Valid observations of the replay day.
    obs = duckdb.sql(
        f"SELECT segment_idx, sod, bucket, day_type, tt_s FROM read_parquet('{path}') WHERE valid"
    ).fetchnumpy()
    g = obs["segment_idx"].astype(np.int64)
    tt = obs["tt_s"].astype(np.float64)
    expected = seg_time[obs["day_type"].astype(np.int64), obs["bucket"].astype(np.int64), g]
    ratio = np.clip(tt / expected, config.LIVE_RATIO_MIN, config.LIVE_RATIO_MAX)
    t_exit = obs["sod"].astype(np.float64) + tt
    order = np.argsort(t_exit, kind="stable")
    t_exit = t_exit[order]
    ratio = ratio[order]
    g = g[order]

    factors = np.empty((config.LIVE_N_TICKS, n_seg), dtype=np.float32)
    city = np.empty(config.LIVE_N_TICKS, dtype=np.float64)
    for n in range(config.LIVE_N_TICKS):
        tick_t = float(n * config.LIVE_TICK_S)
        # Observations that finished in the window (T - 45 min, T].
        lo = np.searchsorted(t_exit, tick_t - config.LIVE_WINDOW_S, side="right")
        hi = np.searchsorted(t_exit, tick_t, side="right")
        w = np.exp(-(tick_t - t_exit[lo:hi]) / config.LIVE_DECAY_S)
        wr = w * ratio[lo:hi]
        r_city = (wr.sum() + config.CITY_PRIOR_K * 1.0) / (w.sum() + config.CITY_PRIOR_K)
        sum_w = np.bincount(g[lo:hi], weights=w, minlength=n_seg)
        sum_wr = np.bincount(g[lo:hi], weights=wr, minlength=n_seg)
        # Segments without observations get exactly r_city from this formula.
        factors[n] = (sum_wr + prior_k * r_city) / (sum_w + prior_k)
        city[n] = r_city
    factors = np.clip(factors, config.LIVE_RATIO_MIN, config.LIVE_RATIO_MAX).astype(np.float32)
    return factors, city


def build(prior_k: float, horizon_s: float) -> dict:
    """Compute and write live factors; record the live settings in model_meta.json.

    Args:
        prior_k: LIVE_PRIOR_K to use.
        horizon_s: LIVE_HORIZON_S the planner should use with these factors.
    Returns:
        Summary dict.
    """
    with open(config.MODEL_META_PATH, "r", encoding="utf-8") as handle:
        meta = json.load(handle)
    replay_date = meta["replay_date"]
    seg_time = np.load(config.SEG_TIME_PATH).astype(np.float64)
    factors, city = compute_factors(replay_date, seg_time, prior_k)
    tmp_path = config.MODEL_DIR / "live_factors.tmp.npy"
    np.save(tmp_path, factors)
    tmp_path.replace(config.LIVE_FACTORS_PATH)
    meta["live_city_ratio"] = np.round(city, 4).tolist()
    meta["live_prior_k"] = prior_k
    meta["live_horizon_s"] = horizon_s
    tmp_meta = config.MODEL_META_PATH.with_suffix(".json.tmp")
    with open(tmp_meta, "w", encoding="utf-8") as handle:
        json.dump(meta, handle, indent=1)
    tmp_meta.replace(config.MODEL_META_PATH)
    summary = {"replay_date": replay_date, "prior_k": prior_k, "horizon_s": horizon_s,
               "city_min": float(city.min()), "city_max": float(city.max()),
               "factor_min": float(factors.min()), "factor_max": float(factors.max())}
    log(f"live factors for {replay_date}: city ratio {city.min():.3f}..{city.max():.3f}, "
        f"prior_k {prior_k}, horizon {horizon_s} s")
    return summary


def main() -> None:
    """Command-line entry point.

    Returns:
        None.
    """
    parser = argparse.ArgumentParser(description="Build live replay factors (SPEC 7.8).")
    parser.add_argument("--prior-k", type=float, default=config.LIVE_PRIOR_K)
    parser.add_argument("--horizon-s", type=float, default=float(config.LIVE_HORIZON_S))
    args = parser.parse_args()
    build(args.prior_k, args.horizon_s)


if __name__ == "__main__":
    main()
