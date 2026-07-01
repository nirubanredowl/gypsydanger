#!/usr/bin/env python3
"""Rename entities/{TICKER}/announcements.csv → {TICKER}_Announcements.csv."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from importlib import import_module

asx = import_module("00_asx_api")

LEGACY_NAME = "announcements.csv"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rename per-ticker announcements.csv to {TICKER}_Announcements.csv"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print renames without changing files",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    entities_root = asx.data_dir() / "entities"

    renamed = 0
    skipped = 0
    already = 0

    for folder in sorted(entities_root.iterdir()):
        if not folder.is_dir():
            continue
        ticker = folder.name.upper()
        legacy = folder / LEGACY_NAME
        target = folder / asx.announcements_csv_filename(ticker)

        if target.exists() and not legacy.exists():
            already += 1
            continue
        if not legacy.exists():
            skipped += 1
            print(f"  skip {ticker}: no {LEGACY_NAME}")
            continue
        if target.exists() and legacy.exists():
            skipped += 1
            print(f"  skip {ticker}: both legacy and target exist")
            continue

        print(f"  {legacy.name} → {target.name}")
        if not args.dry_run:
            legacy.rename(target)
        renamed += 1

    print(
        f"\nDone. renamed={renamed} already={already} skipped={skipped}"
        + (" (dry run)" if args.dry_run else "")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
