"""
pipeline/extract.py - unpack the daily SQLite databases from the 7z archive (SPEC 6.2).

Inputs: the single data/raw/*.7z archive.
Outputs: data/raw/sqlite/bus_movements_YYYY_MM_DD.db, one file per day.
Run: python -m pipeline.extract
"""

import argparse
import shutil
import sys
import time
from pathlib import Path

import py7zr

import config


def find_archive() -> Path:
    """Locate the one .7z archive in data/raw/.

    Returns:
        Path of the archive. Exits with both expected paths printed when it is missing (A5).
    """
    archives = sorted(config.RAW_DIR.glob("*.7z"))
    if len(archives) == 0:
        print(f"Missing archive. Expected: {config.RAW_DIR / '*.7z'}")
        print(f"Expected GTFS files in: {config.GTFS_DIR}")
        sys.exit(1)
    return archives[0]


def flatten_db_files(target_dir: Path) -> int:
    """Move any .db files found in subfolders up into target_dir (SPEC 6.2 step 3).

    Args:
        target_dir: the sqlite folder.
    Returns:
        Number of files moved.
    """
    moved = 0
    for db_path in target_dir.rglob("*.db"):
        # Files already at the top level stay where they are.
        if db_path.parent == target_dir:
            continue
        shutil.move(str(db_path), str(target_dir / db_path.name))
        moved += 1
    return moved


def extract_all() -> None:
    """Extract every daily database unless at least one is already present.

    Returns:
        None. Prints progress lines for the log.
    """
    target_dir = config.SQLITE_DIR
    target_dir.mkdir(parents=True, exist_ok=True)

    # Step 1: an existing .db file means extraction already ran; do nothing.
    existing = list(target_dir.glob("*.db"))
    if len(existing) > 0:
        print(f"extract: {len(existing)} .db files already present, skipping")
        return

    # Step 2: extract into a .tmp folder first so an interrupted run never leaves
    # partial .db files that look finished (write to a .tmp name, then rename).
    archive_path = find_archive()
    tmp_dir = target_dir / ".tmp_extract"
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir()
    started = time.time()
    print(f"extract: unpacking {archive_path.name} into {tmp_dir}", flush=True)
    with py7zr.SevenZipFile(archive_path, mode="r") as archive:
        archive.extractall(path=tmp_dir)

    # Step 3: move every extracted .db file up into data/raw/sqlite/.
    count = 0
    for db_path in sorted(tmp_dir.rglob("*.db")):
        shutil.move(str(db_path), str(target_dir / db_path.name))
        count += 1
    shutil.rmtree(tmp_dir)
    flatten_db_files(target_dir)
    elapsed_s = time.time() - started
    print(f"extract: done, {count} files in {elapsed_s:.0f} s", flush=True)


def main() -> None:
    """Command-line entry point.

    Returns:
        None.
    """
    parser = argparse.ArgumentParser(description="Extract the GPS archive (SPEC 6.2).")
    parser.parse_args()
    extract_all()


if __name__ == "__main__":
    main()
