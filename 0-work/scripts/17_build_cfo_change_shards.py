#!/usr/bin/env python3
"""Build ticker shards for CFO change announcement fetch (balanced by match count)."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from importlib import import_module

asx = import_module("00_asx_api")

DEFAULT_WORKERS = 10


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workers",
        type=int,
        default=int(
            __import__("os").environ.get("GYPSY_CFO_FETCH_WORKERS", DEFAULT_WORKERS)
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=asx.data_dir() / "cfo_changes",
    )
    parser.add_argument(
        "--include-tier-b",
        action="store_true",
        help="Include tier-B edge cases in shard counts",
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


def cfo_change_count(ticker: str, *, include_tier_b: bool) -> int:
    path = asx.announcements_csv_path(ticker)
    if not path.exists():
        return 0
    count = 0
    with path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            if asx.is_cfo_change_announcement(row, include_tier_b=include_tier_b):
                count += 1
    return count


def balance_shards(
    items: list[tuple[str, int]], workers: int
) -> list[list[str]]:
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

    weighted: list[tuple[str, int]] = []
    total = 0
    for ticker in load_entities():
        count = cfo_change_count(ticker, include_tier_b=args.include_tier_b)
        if count > 0:
            weighted.append((ticker, count))
            total += count

    if not weighted:
        print("error: no CFO change announcements found", file=sys.stderr)
        return 1

    shards = balance_shards(weighted, args.workers)
    shard_dir = args.output_dir / "shards" / f"{args.workers}workers"
    shard_dir.mkdir(parents=True, exist_ok=True)

    shard_meta = []
    for i, tickers_in_shard in enumerate(shards):
        shard_file = shard_dir / f"shard_{i:02d}.txt"
        shard_file.write_text("\n".join(tickers_in_shard) + "\n", encoding="utf-8")
        docs = sum(w for t, w in weighted if t in tickers_in_shard)
        shard_meta.append(
            {
                "worker_id": f"{i:02d}",
                "tickers": len(tickers_in_shard),
                "documents": docs,
                "file": str(shard_file.relative_to(args.output_dir)),
            }
        )
        print(
            f"  shard_{i:02d}: {len(tickers_in_shard)} tickers, {docs} CFO change PDFs"
        )

    manifest = {
        "workers": args.workers,
        "include_tier_b": args.include_tier_b,
        "tickers_with_matches": len(weighted),
        "total_documents": total,
        "s3_path_pattern": "entities/{TICKER}/cfo_changes/{YYYY-MM-DD}_{documentKey}.pdf",
        "shards": shard_meta,
    }
    manifest_path = args.output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(
        f"\nWrote {args.workers} shards → {shard_dir}\n"
        f"Total: {len(weighted)} tickers, {total} documents\n"
        f"Manifest: {manifest_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
