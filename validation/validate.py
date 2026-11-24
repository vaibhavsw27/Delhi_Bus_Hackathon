"""
validation/validate.py - accuracy on held-out days against the timetable (SPEC 11).

Purpose: all-trips metrics (overall, per hour, per date), the 20 OD-pair table chosen by
a fixed rule, the live-replay check with its one fallback rule, the chart and the slide.
Inputs: data/clean/seg_obs/<test dates>.parquet, data/model/*, findings,
validation/google_comparison.csv (optional, for the slide line).
Outputs: validation/results.json, od_table.md, mae_by_hour.png, accuracy_slide.png, and a
6-line console summary.
Run: python validation/validate.py
"""

import argparse
import csv
import json
import math
import sys
from pathlib import Path

# Make the repository root importable when the script is run as a file.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import duckdb  # noqa: E402
import matplotlib  # noqa: E402

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

import config  # noqa: E402
from pipeline import build_live  # noqa: E402
from pipeline.build_model import load_spans, subset_spans  # noqa: E402
from planner.model import predict_runs, timetable_runs  # noqa: E402
from planner.network import Network  # noqa: E402
from planner.search import normalize  # noqa: E402

RESULTS_PATH = config.VALIDATION_DIR / "results.json"
OD_TABLE_PATH = config.VALIDATION_DIR / "od_table.md"
CHART_PATH = config.VALIDATION_DIR / "mae_by_hour.png"
SLIDE_PATH = config.VALIDATION_DIR / "accuracy_slide.png"
GOOGLE_PATH = config.VALIDATION_DIR / "google_comparison.csv"
HOURS = list(range(5, 23))
N_OD_PAIRS = 20
OD_MIN_STOPS = 20
OD_MIN_RUNS = 30
OD_FROM_SHARE = 0.15
OD_TO_SHARE = 0.85
GOOGLE_MIN_ROWS = 5
COLOR_TIMETABLE = "#9CA3AF"
COLOR_MODEL = "#2563EB"
COLOR_TEXT = "#111827"
COLOR_TEXT_2 = "#6B7280"


def metrics(predicted: np.ndarray, actual: np.ndarray) -> dict:
    """MAE (min), MAPE (%), bias (min) of predictions against actual seconds (SPEC 11.1).

    Args:
        predicted: seconds.
        actual: seconds.
    Returns:
        Dict with n, mae_min, mape_pct, bias_min (None values when empty).
    """
    n = int(actual.shape[0])
    if n == 0:
        return {"n": 0, "mae_min": None, "mape_pct": None, "bias_min": None}
    error = predicted - actual
    return {
        "n": n,
        "mae_min": round(float(np.mean(np.abs(error))) / 60.0, 3),
        "mape_pct": round(float(np.mean(np.abs(error) / actual)) * 100.0, 2),
        "bias_min": round(float(np.mean(error)) / 60.0, 3),
    }


def predict_both(spans: dict, seg_time: np.ndarray, sched_time: np.ndarray,
                 horizon_s: float) -> tuple[np.ndarray, np.ndarray]:
    """Model (typical) and timetable predictions for spans.

    Args:
        spans: spans dict with seg_flat, ptr, t0, day_type.
        seg_time: [3, 48, G] float32.
        sched_time: [24, G].
        horizon_s: live horizon (unused in typical mode).
    Returns:
        Tuple (model seconds, timetable seconds).
    """
    dummy = np.ones((1, seg_time.shape[2]), dtype=np.float32)
    ticks = np.full(spans["t0"].shape[0], -1, dtype=np.int64)
    model = predict_runs(seg_time, spans["seg_flat"], spans["ptr"], spans["t0"],
                         spans["day_type"], dummy, ticks, horizon_s)
    baseline = timetable_runs(sched_time, spans["seg_flat"], spans["ptr"], spans["t0"])
    return model, baseline


def sub_spans(spans: dict, chosen: np.ndarray, a: int, b: int) -> dict:
    """Cut spans that cover pattern positions a..b down to exactly that stretch.

    Args:
        spans: spans dict (with tt_flat and sod_flat).
        chosen: indices of spans that cover [a, b].
        a, b: from-stop and to-stop positions.
    Returns:
        Spans dict for the a..b stretch of each chosen span.
    """
    length = b - a
    starts = spans["ptr"][chosen] + (a - spans["pos_a"][chosen])
    rows = (starts[:, None] + np.arange(length)[None, :]).reshape(-1)
    ptr = np.arange(chosen.shape[0] + 1, dtype=np.int64) * length
    seg_flat = spans["seg_flat"][rows]
    actual = spans["tt_flat"][rows].reshape(chosen.shape[0], length).sum(axis=1)
    t0 = spans["sod_flat"][starts]
    return {"seg_flat": seg_flat, "ptr": ptr, "t0": t0, "day_type": spans["day_type"][chosen],
            "actual": actual}


def covering(spans: dict, pattern: int, a: int, b: int) -> np.ndarray:
    """Indices of spans of a pattern that cover positions a..b completely.

    Args:
        spans: spans dict.
        pattern: pattern index.
        a, b: positions.
    Returns:
        int array of span indices.
    """
    mask = (spans["pattern"] == pattern) & (spans["pos_a"] <= a) & (spans["pos_b"] >= b)
    return np.flatnonzero(mask)


def choose_od_pairs(net: Network, spans: dict) -> list[dict]:
    """The 20 OD pairs of SPEC 11.2 step 2 (demo pair first when it exists).

    Args:
        net: network.
        spans: all test spans (any length) with pattern positions.
    Returns:
        List of {"pattern", "a", "b", "runs", "kind"}.
    """
    pairs = []
    used_routes = set()
    names = [normalize(str(n)) for n in net.stop_name]
    # Step 2a: demo pair on one pattern from Kashmere Gate to Nehru Place.
    origin_key = "kashmere gate"
    if not any(origin_key in name for name in names):
        origin_key = "kashmiri gate"
    best = None
    for p in range(net.n_patterns):
        stops = net.pattern_stops(p)
        a = -1
        for pos in range(stops.shape[0]):
            if origin_key in names[stops[pos]]:
                a = pos
                break
        if a < 0:
            continue
        b = -1
        for pos in range(a + 1, stops.shape[0]):
            if "nehru place" in names[stops[pos]]:
                b = pos
                break
        if b < 0:
            continue
        runs = covering(spans, p, a, b).shape[0]
        if runs > 0 and (best is None or runs > best["runs"]):
            best = {"pattern": p, "a": a, "b": b, "runs": runs, "kind": "demo"}
    if best is not None:
        pairs.append(best)
        used_routes.add(int(net.pat_route[best["pattern"]]))

    # Step 2b: long patterns ranked by test runs covering 15%..85% of their stops.
    n_stops = np.diff(net.pat_ptr)
    a_pos = np.floor(OD_FROM_SHARE * n_stops).astype(np.int64)
    b_pos = np.floor(OD_TO_SHARE * n_stops).astype(np.int64)
    span_pat = spans["pattern"]
    cover = (spans["pos_a"] <= a_pos[span_pat]) & (spans["pos_b"] >= b_pos[span_pat])
    counts = np.bincount(span_pat[cover], minlength=net.n_patterns)
    eligible = np.flatnonzero(n_stops >= OD_MIN_STOPS)
    ranked = eligible[np.lexsort((eligible, -counts[eligible]))]
    for p in ranked:
        if len(pairs) >= N_OD_PAIRS:
            break
        route = int(net.pat_route[p])
        if route in used_routes:
            continue
        if counts[p] < OD_MIN_RUNS:
            continue
        pairs.append({"pattern": int(p), "a": int(a_pos[p]), "b": int(b_pos[p]),
                      "runs": int(counts[p]), "kind": "rule"})
        used_routes.add(route)
    return pairs


def live_mae(spans: dict, seg_time: np.ndarray, factors: np.ndarray, horizon_s: float) -> float:
    """Model MAE (min) with r = the live row of the tick at or before each departure.

    Args:
        spans: replay-day spans.
        seg_time: [3, 48, G].
        factors: [288, G].
        horizon_s: LIVE_HORIZON_S.
    Returns:
        MAE in minutes.
    """
    ticks = np.minimum((spans["t0"] // config.LIVE_TICK_S).astype(np.int64), config.LIVE_N_TICKS - 1)
    pred = predict_runs(seg_time, spans["seg_flat"], spans["ptr"], spans["t0"], spans["day_type"],
                        factors.astype(np.float32), ticks, horizon_s)
    return metrics(pred, spans["actual"])["mae_min"]


def google_summary() -> dict | None:
    """Filled rows of the Google comparison CSV, if any (for the slide line).

    Returns:
        {"n", "mean_abs_diff_total_min"} or None.
    """
    if not GOOGLE_PATH.exists():
        return None
    diffs = []
    with open(GOOGLE_PATH, "r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            value = (row.get("google_total_min") or "").strip()
            if value == "":
                continue
            diffs.append(abs(float(row["our_total_min"]) - float(value)))
    if len(diffs) == 0:
        return None
    return {"n": len(diffs), "mean_abs_diff_total_min": round(float(np.mean(diffs)), 1)}


def draw_hour_bars(ax, by_hour: list[dict]) -> None:
    """Grouped bars of MAE per departure hour on an axes.

    Args:
        ax: matplotlib axes.
        by_hour: rows with hour, timetable_mae_min, model_mae_min.
    Returns:
        None.
    """
    hours = np.array([row["hour"] for row in by_hour])
    timetable = np.array([row["timetable_mae_min"] or 0 for row in by_hour])
    model = np.array([row["model_mae_min"] or 0 for row in by_hour])
    width = 0.4
    ax.bar(hours - width / 2, timetable, width, color=COLOR_TIMETABLE, label="Timetable")
    ax.bar(hours + width / 2, model, width, color=COLOR_MODEL, label="BusPulse model")
    ax.set_xticks(hours)
    ax.set_xticklabels([f"{h:02d}" for h in hours])
    ax.set_xlabel("Departure hour (IST)")
    ax.set_ylabel("MAE (minutes)")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.legend(frameon=False)


def make_chart(by_hour: list[dict]) -> None:
    """validation/mae_by_hour.png, 1600 x 900 (SPEC 11.3).

    Args:
        by_hour: per-hour rows.
    Returns:
        None.
    """
    fig = plt.figure(figsize=(16, 9), dpi=100, facecolor="white")
    ax = fig.add_subplot(111)
    draw_hour_bars(ax, by_hour)
    ax.set_title("In-bus time error by departure hour, test days 25-31 Oct 2026", fontsize=16)
    fig.tight_layout()
    fig.savefig(CHART_PATH, dpi=100, facecolor="white")
    plt.close(fig)


def make_slide(results: dict) -> None:
    """validation/accuracy_slide.png, 1920 x 1080 (SPEC 11.3).

    Args:
        results: the results dict.
    Returns:
        None.
    """
    head = results["headline"]
    fig = plt.figure(figsize=(19.2, 10.8), dpi=100, facecolor="white")
    fig.text(0.04, 0.92, "Accuracy on held-out days", fontsize=34, fontweight="bold", color=COLOR_TEXT)
    n_test = len(results["test_dates"])
    tiles = [
        (f"Model MAE {head['mae_model_min']:.1f} min", f"Timetable {head['mae_timetable_min']:.1f} min"),
        (f"Model MAPE {head['mape_model_pct']:.0f} %", f"Timetable {head['mape_timetable_pct']:.0f} %"),
        (f"{head['n_trips']:,} trips", f"{n_test} test days, trained on 1-24 Oct"),
    ]
    # Left third: three stacked tiles.
    for i, (big, small) in enumerate(tiles):
        bottom = 0.62 - i * 0.24
        tile = fig.add_axes([0.04, bottom, 0.27, 0.2])
        tile.set_xticks([])
        tile.set_yticks([])
        tile.set_facecolor("#F6F7F9")
        for spine in tile.spines.values():
            spine.set_visible(False)
        tile.text(0.06, 0.6, big, fontsize=26, fontweight="bold", color=COLOR_MODEL,
                  transform=tile.transAxes, va="center")
        tile.text(0.06, 0.28, small, fontsize=17, color=COLOR_TEXT_2, transform=tile.transAxes,
                  va="center")
    # Right two thirds: the chart.
    ax = fig.add_axes([0.38, 0.16, 0.58, 0.68])
    draw_hour_bars(ax, results["by_hour"])
    ax.set_title("In-bus time error by departure hour, test days", fontsize=16)
    live = results["live_check"]
    footer = (f"Live replay day: MAE typical {live['typical_mae_min']:.1f} min, "
              f"with live {live['live_mae_min']:.1f} min")
    fig.text(0.04, 0.06, footer, fontsize=17, color=COLOR_TEXT)
    google = google_summary()
    if google is not None and google["n"] >= GOOGLE_MIN_ROWS:
        fig.text(0.04, 0.025, f"Against Google Maps ({google['n']} pairs): mean absolute "
                 f"difference {google['mean_abs_diff_total_min']:.1f} min", fontsize=17, color=COLOR_TEXT)
    fig.savefig(SLIDE_PATH, dpi=100, facecolor="white")
    plt.close(fig)


def write_od_table(rows: list[dict]) -> None:
    """validation/od_table.md (SPEC 11.3).

    Args:
        rows: OD pair result rows.
    Returns:
        None.
    """
    lines = ["| # | Route | From stop | To stop | Trips | Actual mean (min) | Timetable MAE | "
             "Model MAE | Timetable MAPE % | Model MAPE % |",
             "|---|-------|-----------|---------|-------|-------------------|---------------|"
             "-----------|------------------|--------------|"]
    for i, row in enumerate(rows):
        lines.append(f"| {i + 1} | {row['route']} | {row['from_stop']} | {row['to_stop']} | "
                     f"{row['trips']} | {row['actual_mean_min']:.1f} | {row['timetable_mae_min']:.1f} | "
                     f"{row['model_mae_min']:.1f} | {row['timetable_mape_pct']:.1f} | "
                     f"{row['model_mape_pct']:.1f} |")
    OD_TABLE_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run() -> dict:
    """Compute every validation number and write all outputs (SPEC 11.2, 11.3).

    Returns:
        The results dict.
    """
    findings = config.load_findings()
    test_dates = findings["test_dates"]
    replay_date = findings["replay_date"]
    net = Network()
    seg_time = np.load(config.SEG_TIME_PATH).astype(np.float32)
    with open(config.MODEL_META_PATH, "r", encoding="utf-8") as handle:
        meta = json.load(handle)
    horizon_s = float(meta.get("live_horizon_s", config.LIVE_HORIZON_S))
    con = duckdb.connect()
    for statement in config.duckdb_settings():
        con.execute(statement)

    # Step 1: all-trips set = full valid stretches of at least 10 segments on test dates.
    all_spans = load_spans(con, test_dates, 1)
    long_index = np.flatnonzero((all_spans["pos_b"] - all_spans["pos_a"]) >= config.CALIB_MIN_SEGS)
    trips = subset_spans(all_spans, long_index)
    model_s, base_s = predict_both(trips, seg_time, net.sched_time, horizon_s)
    actual = trips["actual"]
    overall_model = metrics(model_s, actual)
    overall_base = metrics(base_s, actual)
    hour_of = (trips["t0"] // 3600).astype(np.int64) % 24
    # Phase 2: does "9 in 10 trips arrive by" hold on the test week? Share of test trips
    # whose actual in-bus time is within the estimate times the training p90 ratio.
    reliability_coverage = None
    if "reliability_p90" in meta:
        p90 = np.array(meta["reliability_p90"])[trips["day_type"], hour_of]
        reliability_coverage = round(float(np.mean(actual <= model_s * p90)), 4)
    by_hour = []
    for hour in HOURS:
        mask = hour_of == hour
        m = metrics(model_s[mask], actual[mask])
        t = metrics(base_s[mask], actual[mask])
        by_hour.append({"hour": hour, "n": m["n"], "model_mae_min": m["mae_min"],
                        "timetable_mae_min": t["mae_min"], "model_mape_pct": m["mape_pct"],
                        "timetable_mape_pct": t["mape_pct"], "model_bias_min": m["bias_min"]})
    by_date = []
    for index, date_iso in enumerate(test_dates):
        mask = trips["date_index"] == index
        m = metrics(model_s[mask], actual[mask])
        t = metrics(base_s[mask], actual[mask])
        by_date.append({"date": date_iso, "n": m["n"], "model": m, "timetable": t})

    # Step 2: OD pairs.
    pairs = choose_od_pairs(net, all_spans)
    od_rows = []
    for pair in pairs:
        chosen = covering(all_spans, pair["pattern"], pair["a"], pair["b"])
        sub = sub_spans(all_spans, chosen, pair["a"], pair["b"])
        m_s, b_s = predict_both(sub, seg_time, net.sched_time, horizon_s)
        m = metrics(m_s, sub["actual"])
        t = metrics(b_s, sub["actual"])
        stops = net.pattern_stops(pair["pattern"])
        od_rows.append({
            "kind": pair["kind"], "pattern_idx": pair["pattern"],
            "route": str(net.route_name[net.pat_route[pair["pattern"]]]),
            "route_id": str(net.route_id[net.pat_route[pair["pattern"]]]),
            "from_stop": str(net.stop_name[stops[pair["a"]]]),
            "to_stop": str(net.stop_name[stops[pair["b"]]]),
            "from_stop_id": str(net.stop_id[stops[pair["a"]]]),
            "to_stop_id": str(net.stop_id[stops[pair["b"]]]),
            "from_pos": pair["a"], "to_pos": pair["b"],
            "trips": m["n"], "actual_mean_min": round(float(np.mean(sub["actual"])) / 60.0, 2),
            "timetable_mae_min": t["mae_min"], "model_mae_min": m["mae_min"],
            "timetable_mape_pct": t["mape_pct"], "model_mape_pct": m["mape_pct"],
        })

    # Step 3: live check on the replay date, with the one fallback rule.
    replay_index = test_dates.index(replay_date)
    replay = subset_spans(trips, np.flatnonzero(trips["date_index"] == replay_index))
    replay_model, replay_base = predict_both(replay, seg_time, net.sched_time, horizon_s)
    typical_mae = metrics(replay_model, replay["actual"])["mae_min"]
    factors = np.load(config.LIVE_FACTORS_PATH)
    first_live = live_mae(replay, seg_time, factors, horizon_s)
    live_check = {"replay_date": replay_date, "n_trips": int(replay["t0"].shape[0]),
                  "typical_mae_min": typical_mae, "live_mae_min": first_live,
                  "first": {"prior_k": meta.get("live_prior_k", config.LIVE_PRIOR_K),
                            "horizon_s": horizon_s, "live_mae_min": first_live},
                  "rule_fired": False}
    if not first_live < typical_mae:
        # SPEC 11.2 step 3: change the two constants, rebuild once, keep the result.
        fallback_horizon = 900.0
        fallback_prior = 4.0
        build_live.build(fallback_prior, fallback_horizon)
        factors = np.load(config.LIVE_FACTORS_PATH)
        second_live = live_mae(replay, seg_time, factors, fallback_horizon)
        live_check["rule_fired"] = True
        live_check["second"] = {"prior_k": fallback_prior, "horizon_s": fallback_horizon,
                                "live_mae_min": second_live}
        live_check["live_mae_min"] = second_live

    results = {
        "headline": {
            "mae_model_min": overall_model["mae_min"],
            "mae_timetable_min": overall_base["mae_min"],
            "mape_model_pct": overall_model["mape_pct"],
            "mape_timetable_pct": overall_base["mape_pct"],
            "n_trips": overall_model["n"],
        },
        "all_trips": {"model": overall_model, "timetable": overall_base},
        "by_hour": by_hour,
        "by_date": by_date,
        "od_pairs": od_rows,
        "live_check": live_check,
        "success": {
            "S1_mae_reduction": round(1.0 - overall_model["mae_min"] / overall_base["mae_min"], 4),
            "S1_pass": overall_model["mae_min"] <= 0.75 * overall_base["mae_min"],
            "S2_pass": overall_model["mape_pct"] <= 20.0,
        },
        "r3_bias_rule_triggered": abs(overall_model["bias_min"]) > 1.5,
        "reliability_p90_coverage": reliability_coverage,
        "calibration_k": meta.get("calibration_k"),
        "level_share": meta.get("level_share"),
        "train_dates": meta.get("train_dates"),
        "test_dates": test_dates,
        "constants": meta.get("constants"),
    }
    # Keep the Google comparison score if google_template.py stored one earlier.
    if RESULTS_PATH.exists():
        with open(RESULTS_PATH, "r", encoding="utf-8") as handle:
            previous = json.load(handle)
        if "google" in previous:
            results["google"] = previous["google"]
    config.VALIDATION_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = RESULTS_PATH.with_suffix(".json.tmp")
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(results, handle, indent=1)
    tmp_path.replace(RESULTS_PATH)
    write_od_table(od_rows)
    make_chart(by_hour)
    make_slide(results)

    # Six-line console summary.
    print(f"Test trips: {overall_model['n']:,} on {len(test_dates)} days ({test_dates[0]} .. {test_dates[-1]})")
    print(f"MAE   model {overall_model['mae_min']:.2f} min  timetable {overall_base['mae_min']:.2f} min "
          f"({results['success']['S1_mae_reduction'] * 100:.1f}% lower)")
    print(f"MAPE  model {overall_model['mape_pct']:.1f} %  timetable {overall_base['mape_pct']:.1f} %")
    print(f"Bias  model {overall_model['bias_min']:+.2f} min  timetable {overall_base['bias_min']:+.2f} min")
    print(f"OD pairs: {len(od_rows)}; live day {replay_date}: typical {typical_mae:.2f} min, "
          f"live {live_check['live_mae_min']:.2f} min (rule fired: {live_check['rule_fired']})")
    print(f"S1 {pass_text(results['success']['S1_pass'])}, S2 {pass_text(results['success']['S2_pass'])}, "
          f"R3 bias rule triggered: {results['r3_bias_rule_triggered']}, "
          f"'9 in 10' coverage on test: {reliability_coverage}")
    return results


def pass_text(flag: bool) -> str:
    """PASS or MISSED.

    Args:
        flag: criterion met.
    Returns:
        Text.
    """
    if flag:
        return "PASS"
    return "MISSED"


def main() -> None:
    """Command-line entry point.

    Returns:
        None.
    """
    parser = argparse.ArgumentParser(description="Validation on test dates (SPEC 11).")
    parser.parse_args()
    run()


if __name__ == "__main__":
    main()
