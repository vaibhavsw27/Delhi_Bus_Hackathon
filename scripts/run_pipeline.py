"""
scripts/run_pipeline.py - run extract, then ingest and match per date (SPEC 6.5).

Purpose: resumable orchestration; finished days are skipped because each step skips a
day whose output file exists. Progress goes to logs/pipeline.log and the console.
Inputs: the archive or extracted SQLite files, network.npz, findings.
Outputs: data/clean/positions/*.parquet, data/clean/seg_obs/*.parquet.
Run: python scripts/run_pipeline.py --dates all      (or --dates 2026-10-06[,2026-10-07])
"""

import argparse
import datetime as dt_mod
import sys
import time
from pathlib import Path

# Make the repository root importable when the script is run as a file.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import config  # noqa: E402
from pipeline import extract, ingest, match  # noqa: E402
from planner.network import Network  # noqa: E402

LOG_PATH = config.LOGS_DIR / "pipeline.log"


def log(message: str) -> None:
    """Write one timestamped line to the console and logs/pipeline.log.

    Args:
        message: text.
    Returns:
        None.
    """
    stamp = dt_mod.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{stamp} {message}"
    print(line, flush=True)
    config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def main() -> None:
    """Command-line entry point.

    Returns:
        None.
    """
    parser = argparse.ArgumentParser(description="Run the GPS pipeline (SPEC 6.5).")
    parser.add_argument("--dates", required=True,
                        help="'all' or a comma-separated list of YYYY-MM-DD dates")
    args = parser.parse_args()
    extract.extract_all()
    findings = config.load_findings()
    if args.dates == "all":
        dates = sorted(findings["available_dates"])
    else:
        dates = sorted(d.strip() for d in args.dates.split(","))
    net = Network()
    log(f"pipeline start: {len(dates)} dates")
    started = time.time()
    for date_iso in dates:
        day_started = time.time()
        ingest.ingest_day(date_iso, net, findings)
        match.match_day(date_iso, net, findings)
        log(f"{date_iso} done in {time.time() - day_started:.0f} s")
    log(f"pipeline finished in {time.time() - started:.0f} s")


if __name__ == "__main__":
    main()
