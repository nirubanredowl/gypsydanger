#!/usr/bin/env python3
"""Probe announcements CSVs for annual-report filter signals.

Runs the loose and strict annual-report classifiers on sample tickers (or all
indexed tickers) and prints counts, excluded rows, and corpus estimates.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from importlib import import_module

asx = import_module("00_asx_api")

DEFAULT_SAMPLE = ("CBA", "BHP", "WOW", "QGL", "TLS")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Probe announcementTypes / headlines for annual report filtering"
    )
    parser.add_argument(
        "--ticker",
        action="append",
        help="Ticker to probe (repeatable). Default: CBA BHP WOW QGL TLS",
    )
    parser.add_argument(
        "--all-indexed",
        action="store_true",
        help="Probe every ticker with an announcements CSV under data/entities/",
    )
    parser.add_argument(
        "--show-excluded",
        action="store_true",
        help="Print loose matches excluded by the strict filter",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable summary JSON on stdout",
    )
    return parser.parse_args()


def tickers_to_probe(args: argparse.Namespace) -> list[str]:
    if args.all_indexed:
        tickers: list[str] = []
        entities_root = asx.data_dir() / "entities"
        for path in sorted(entities_root.iterdir()):
            if not path.is_dir():
                continue
            ticker = path.name.upper()
            if asx.announcements_csv_path(ticker).exists():
                tickers.append(ticker)
        return tickers

    selected = args.ticker or list(DEFAULT_SAMPLE)
    return sorted({t.upper() for t in selected})


def load_rows(ticker: str) -> list[dict[str, str]]:
    path = asx.announcements_csv_path(ticker)
    if not path.exists():
        raise FileNotFoundError(f"{path} not found")
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def summarise_ticker(ticker: str) -> dict[str, object]:
    rows = load_rows(ticker)
    loose = [r for r in rows if asx.is_annual_report_announcement(r, mode="loose")]
    strict = [r for r in rows if asx.is_annual_report_announcement(r, mode="strict")]
    excluded = [r for r in loose if r not in strict]

    type_counter: Counter[str] = Counter()
    for row in strict:
        type_counter.update(asx.parse_announcement_types(row.get("announcementTypes")))

    return {
        "ticker": ticker,
        "total_rows": len(rows),
        "loose_annual_reports": len(loose),
        "strict_annual_reports": len(strict),
        "excluded_by_strict": len(excluded),
        "excluded_samples": [
            {
                "date": (row.get("date") or "")[:10],
                "headline": row.get("headline") or "",
                "announcementTypes": sorted(
                    asx.parse_announcement_types(row.get("announcementTypes"))
                ),
            }
            for row in excluded[:5]
        ],
        "strict_type_counts": dict(type_counter.most_common(12)),
    }


def main() -> int:
    args = parse_args()
    tickers = tickers_to_probe(args)
    if not tickers:
        print("No tickers to probe.")
        return 1

    summaries = [summarise_ticker(ticker) for ticker in tickers]

    if args.json:
        payload = {
            "tickers": summaries,
            "totals": {
                "tickers": len(summaries),
                "announcement_rows": sum(s["total_rows"] for s in summaries),
                "loose_annual_reports": sum(
                    s["loose_annual_reports"] for s in summaries
                ),
                "strict_annual_reports": sum(
                    s["strict_annual_reports"] for s in summaries
                ),
            },
        }
        print(json.dumps(payload, indent=2))
        return 0

    print("Annual report probe")
    print("=" * 72)
    for summary in summaries:
        print(
            f"{summary['ticker']:>5}  rows={summary['total_rows']:>5}  "
            f"loose={summary['loose_annual_reports']:>3}  "
            f"strict={summary['strict_annual_reports']:>3}  "
            f"excluded={summary['excluded_by_strict']:>3}"
        )
        if args.show_excluded and summary["excluded_by_strict"]:
            print("  excluded by strict filter:")
            for sample in summary["excluded_samples"]:
                print(
                    f"    {sample['date']} | {sample['headline'][:70]} | "
                    f"{sample['announcementTypes']}"
                )

    totals = {
        "tickers": len(summaries),
        "announcement_rows": sum(s["total_rows"] for s in summaries),
        "loose_annual_reports": sum(s["loose_annual_reports"] for s in summaries),
        "strict_annual_reports": sum(s["strict_annual_reports"] for s in summaries),
    }
    print("-" * 72)
    print(
        f"TOTAL  tickers={totals['tickers']}  rows={totals['announcement_rows']}  "
        f"loose={totals['loose_annual_reports']}  strict={totals['strict_annual_reports']}"
    )
    if totals["announcement_rows"]:
        pct = 100 * totals["strict_annual_reports"] / totals["announcement_rows"]
        print(f"Strict annual reports are {pct:.2f}% of indexed announcement rows.")
    print("\nFilter implementation: 00_asx_api.is_annual_report_announcement()")
    print("Fetcher flag: 03_fetch_documents.py --annual-reports-only")
    print("Field reference: 0-work/docs/announcements-schema.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
