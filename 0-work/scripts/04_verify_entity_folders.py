#!/usr/bin/env python3
"""Verify every ASX entity has a matching data/entities/{TICKER}/ folder."""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from importlib import import_module

asx = import_module("00_asx_api")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify entities.csv tickers have data/entities/{TICKER}/ folders"
    )
    parser.add_argument(
        "--source",
        type=Path,
        help="Ticker list CSV (default: data/entities.csv)",
    )
    parser.add_argument(
        "--require-announcements",
        action="store_true",
        help="Also require an announcements CSV in each folder",
    )
    return parser.parse_args()


def load_tickers(path: Path) -> list[str]:
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        if "ticker" in (reader.fieldnames or []):
            return [
                (row.get("ticker") or "").strip().upper()
                for row in reader
                if (row.get("ticker") or "").strip()
            ]
        if "ASX code" in (reader.fieldnames or []):
            return [
                (row.get("ASX code") or "").strip().upper()
                for row in reader
                if (row.get("ASX code") or "").strip()
            ]
    raise ValueError(f"Unrecognised CSV columns in {path}")


def main() -> int:
    args = parse_args()
    source = args.source or asx.entities_csv_path()
    entities_root = asx.data_dir() / "entities"

    tickers = load_tickers(source)
    ticker_set = set(tickers)

    missing_folders: list[str] = []
    missing_announcements: list[str] = []

    for ticker in tickers:
        folder = entities_root / ticker
        if not folder.is_dir():
            missing_folders.append(ticker)
            continue
        if args.require_announcements and not asx.announcements_csv_path(
            ticker
        ).exists():
            missing_announcements.append(ticker)

    extra_folders = sorted(
        p.name
        for p in entities_root.iterdir()
        if p.is_dir() and p.name.upper() not in ticker_set
    )

    print(f"Source: {source}")
    print(f"Tickers in CSV: {len(tickers)}")
    print(f"Entity folders: {sum(1 for p in entities_root.iterdir() if p.is_dir())}")

    if missing_folders:
        print(f"\nMissing folders ({len(missing_folders)}):")
        for ticker in missing_folders:
            print(f"  {ticker}")
    else:
        print("\nAll CSV tickers have entity folders.")

    if args.require_announcements:
        if missing_announcements:
            print(f"\nMissing announcements CSV ({len(missing_announcements)}):")
            for ticker in missing_announcements:
                print(f"  {ticker}")
        else:
            print("All folders have an announcements CSV.")

    if extra_folders:
        print(f"\nExtra folders not in CSV ({len(extra_folders)}):")
        for ticker in extra_folders[:20]:
            print(f"  {ticker}")
        if len(extra_folders) > 20:
            print(f"  ... and {len(extra_folders) - 20} more")

    if missing_folders or missing_announcements:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
