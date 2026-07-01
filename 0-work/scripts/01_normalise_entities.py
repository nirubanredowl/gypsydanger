#!/usr/bin/env python3
"""Step 1 — Normalise ASX entity directory CSV to data/entities.csv."""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

# Allow running as script from repo root or scripts dir
sys.path.insert(0, str(Path(__file__).resolve().parent))

from importlib import import_module

asx = import_module("00_asx_api")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Step 1: normalise entity directory → data/entities.csv"
    )
    parser.add_argument(
        "--pilot-only",
        action="store_true",
        help="Keep only tickers listed in data/pilot_tickers.txt",
    )
    parser.add_argument(
        "--source",
        type=Path,
        help="Override source CSV (default: latest ASX_Listed_Companies_*.csv)",
    )
    return parser.parse_args()


def load_existing_entity_xids(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        out: dict[str, str] = {}
        for row in reader:
            ticker = (row.get("ticker") or "").strip().upper()
            xid = (row.get("entity_xid") or "").strip()
            if ticker and xid:
                out[ticker] = xid
        return out


def main() -> int:
    args = parse_args()
    source = args.source or asx.find_source_entities_csv()
    out_path = asx.entities_csv_path()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    preserved_xids = load_existing_entity_xids(out_path)
    pilot = asx.load_pilot_tickers() if args.pilot_only else None

    rows: list[dict[str, str]] = []
    with source.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for raw in reader:
            ticker = (raw.get("ASX code") or "").strip().upper()
            if not ticker:
                continue
            if pilot is not None and ticker not in pilot:
                continue
            rows.append(
                {
                    "ticker": ticker,
                    "name": (raw.get("Company name") or "").strip(),
                    "gics_industry_group": (
                        raw.get("GICs industry group") or ""
                    ).strip(),
                    "listing_date": (raw.get("Listing date") or "").strip(),
                    "market_cap_aud": asx.normalise_market_cap(
                        raw.get("Market Cap") or ""
                    ),
                    "entity_xid": preserved_xids.get(ticker, ""),
                }
            )

    rows.sort(key=lambda r: r["ticker"])

    with out_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=asx.ENTITY_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Wrote {len(rows)} entities → {out_path}")
    if args.pilot_only:
        print(f"Pilot filter: {len(pilot or [])} tickers from {asx.pilot_tickers_path()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
