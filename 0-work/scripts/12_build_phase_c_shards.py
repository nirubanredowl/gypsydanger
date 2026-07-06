#!/usr/bin/env python3
"""Build ticker shards for Phase C annual-report fetch (balanced by report count)."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from importlib import import_module

asx = import_module("00_asx_api")

DEFAULT_WORKERS = 20


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workers",
        type=int,
        default=int(
            __import__("os").environ.get("GYPSY_PHASE_C_WORKERS", DEFAULT_WORKERS)
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=asx.data_dir() / "phase_c",
    )
    parser.add_argument(
        "--annual-filter",
        choices=("strict", "loose"),
        default="loose",
    )
    return parser.parse_args()


def load_entities() -> list[str]:
    path = asx.data_dir() / "entities.csv"
    tickers: list[str] = []
    with path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            ticker = (row.get("ticker") or row.get("Ticker") or "").strip().upper()
            if ticker:
                tickers.append(ticker)
    return tickers


def loose_annual_count(ticker: str, *, mode: str) -> int:
    path = asx.announcements_csv_path(ticker)
    if not path.exists():
        return 0
    count = 0
    with path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            if asx.is_annual_report_announcement(row, mode=mode):
                count += 1
    return count


def balance_shards(
    items: list[tuple[str, int]], workers: int
) -> list[list[str]]:
    """Greedy LPT bin-packing: assign heaviest tickers to lightest shards."""
    shards: list[list[str]] = [[] for _ in range(workers)]
    loads = [0] * workers
    for ticker, weight in sorted(items, key=lambda x: x[1], reverse=True):
        idx = min(range(workers), key=lambda i: loads[i])
        shards[idx].append(ticker)
        loads[idx] += weight
    return shards


def main() -> int:
    args = parse_args()
    if args.workers < 1:
        print("error: --workers must be >= 1", file=sys.stderr)
        return 2

    tickers = load_entities()
    weighted: list[tuple[str, int]] = []
    total_reports = 0
    for ticker in tickers:
        count = loose_annual_count(ticker, mode=args.annual_filter)
        if count > 0:
            weighted.append((ticker, count))
            total_reports += count

    if not weighted:
        print("error: no tickers with annual reports found", file=sys.stderr)
        return 1

    shards = balance_shards(weighted, args.workers)
    shard_dir = args.output_dir / "shards" / f"{args.workers}workers"
    shard_dir.mkdir(parents=True, exist_ok=True)

    shard_meta = []
    for i, tickers_in_shard in enumerate(shards):
        shard_file = shard_dir / f"shard_{i:02d}.txt"
        shard_file.write_text("\n".join(tickers_in_shard) + "\n", encoding="utf-8")
        reports = sum(w for t, w in weighted if t in tickers_in_shard)
        shard_meta.append(
            {
                "worker_id": f"{i:02d}",
                "tickers": len(tickers_in_shard),
                "reports": reports,
                "file": str(shard_file.relative_to(args.output_dir)),
            }
        )
        print(
            f"  shard_{i:02d}: {len(tickers_in_shard)} tickers, "
            f"{reports} {args.annual_filter} annual reports"
        )

    manifest = {
        "workers": args.workers,
        "annual_filter": args.annual_filter,
        "tickers_with_reports": len(weighted),
        "total_reports": total_reports,
        "shards": shard_meta,
    }
    manifest_path = args.output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(
        f"\nWrote {args.workers} shards → {shard_dir}\n"
        f"Total: {len(weighted)} tickers, {total_reports} {args.annual_filter} reports\n"
        f"Manifest: {manifest_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
