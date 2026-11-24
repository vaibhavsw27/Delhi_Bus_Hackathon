"""
validation/google_template.py - manual Google Maps comparison (SPEC 11.4).

Purpose: `make` writes a 10-row CSV with our planned journeys for the first 10 OD pairs and
a Google Maps transit link per row; the user fills the Google columns by hand. `score`
reads the filled CSV and stores the comparison in validation/results.json.
Inputs: validation/results.json (OD pairs), data/model/*.
Outputs: validation/google_comparison.csv; results.json key "google".
Run: python validation/google_template.py make   |   python validation/google_template.py score
"""

import argparse
import csv
import json
import sys
from pathlib import Path

# Make the repository root importable when the script is run as a file.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import numpy as np  # noqa: E402

import config  # noqa: E402

RESULTS_PATH = config.VALIDATION_DIR / "results.json"
CSV_PATH = config.VALIDATION_DIR / "google_comparison.csv"
N_ROWS = 10
DEPART_AT = "2026-10-27T08:30"
COLUMNS = ["pair", "from_stop", "to_stop", "maps_url", "depart", "our_total_min",
           "our_in_bus_min", "our_routes", "google_total_min", "google_in_bus_min", "google_routes"]
MAPS_URL = ("https://www.google.com/maps/dir/?api=1&origin={olat},{olon}"
            "&destination={dlat},{dlon}&travelmode=transit")


def make() -> None:
    """Write the CSV with our side filled for the first 10 OD pairs.

    Returns:
        None.
    """
    from planner.journeys import Planner
    from planner.model import Model
    from planner.network import Network
    with open(RESULTS_PATH, "r", encoding="utf-8") as handle:
        results = json.load(handle)
    net = Network()
    planner = Planner(net, Model())
    planner.warmup()
    index_of = {}
    for i, sid in enumerate(net.stop_id):
        index_of[str(sid)] = i
    rows = []
    for number, pair in enumerate(results["od_pairs"][:N_ROWS], start=1):
        a = index_of[pair["from_stop_id"]]
        b = index_of[pair["to_stop_id"]]
        olat = round(float(net.stop_lat[a]), 6)
        olon = round(float(net.stop_lon[a]), 6)
        dlat = round(float(net.stop_lat[b]), 6)
        dlon = round(float(net.stop_lon[b]), 6)
        # Plan with the same logic as /api/plan, using the stop coordinates.
        plan = planner.plan(f"{olat},{olon}", f"{dlat},{dlon}", DEPART_AT, "typical")
        total = ""
        in_bus = ""
        routes = ""
        if len(plan["options"]) > 0:
            first = plan["options"][0]
            total = round(first["total_s"] / 60.0, 1)
            in_bus = round(first["in_bus_s"] / 60.0, 1)
            routes = first["summary"]
        rows.append({
            "pair": number, "from_stop": pair["from_stop"], "to_stop": pair["to_stop"],
            "maps_url": MAPS_URL.format(olat=olat, olon=olon, dlat=dlat, dlon=dlon),
            "depart": "Tue 08:30", "our_total_min": total, "our_in_bus_min": in_bus,
            "our_routes": routes, "google_total_min": "", "google_in_bus_min": "",
            "google_routes": "",
        })
    with open(CSV_PATH, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    print(f"wrote {CSV_PATH} with {len(rows)} rows")
    print("For each row: open maps_url, set 'Depart at' to the next Tuesday 08:30, set Options to")
    print("Bus only, take the first listed option, and enter its total minutes, the sum of its")
    print("bus-leg minutes, and its route numbers in the three google_* columns.")
    print("Then run: python validation/google_template.py score")


def score() -> None:
    """Score the filled CSV and store the result in results.json.

    Returns:
        None.
    """
    diffs_total = []
    diffs_bus = []
    with open(CSV_PATH, "r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            google_total = row["google_total_min"].strip()
            if google_total == "" or row["our_total_min"].strip() == "":
                continue
            diffs_total.append(float(row["our_total_min"]) - float(google_total))
            google_bus = row["google_in_bus_min"].strip()
            if google_bus != "" and row["our_in_bus_min"].strip() != "":
                diffs_bus.append(float(row["our_in_bus_min"]) - float(google_bus))
    summary = {"filled_rows": len(diffs_total)}
    if len(diffs_total) > 0:
        summary["total_mean_abs_diff_min"] = round(float(np.mean(np.abs(diffs_total))), 2)
        summary["total_mean_signed_diff_min"] = round(float(np.mean(diffs_total)), 2)
    if len(diffs_bus) > 0:
        summary["in_bus_mean_abs_diff_min"] = round(float(np.mean(np.abs(diffs_bus))), 2)
        summary["in_bus_mean_signed_diff_min"] = round(float(np.mean(diffs_bus)), 2)
    with open(RESULTS_PATH, "r", encoding="utf-8") as handle:
        results = json.load(handle)
    results["google"] = summary
    tmp_path = RESULTS_PATH.with_suffix(".json.tmp")
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(results, handle, indent=1)
    tmp_path.replace(RESULTS_PATH)
    for key, value in summary.items():
        print(f"{key}: {value}")


def main() -> None:
    """Command-line entry point.

    Returns:
        None.
    """
    parser = argparse.ArgumentParser(description="Google Maps comparison (SPEC 11.4).")
    parser.add_argument("action", choices=["make", "score"])
    args = parser.parse_args()
    if args.action == "make":
        make()
    else:
        score()


if __name__ == "__main__":
    main()
