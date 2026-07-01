#!/usr/bin/env python3
"""Build a shared documentKey pool and per-rung shards for the scaling ladder."""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from importlib import import_module

asx = import_module("00_asx_api")

DEFAULT_POOL_SIZE = 2000
DEFAULT_MIN_ANN = 100
DEFAULT_MAX_ANN = 400
RUNG_WORKERS = [1, 4, 10, 20, 50, 100]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pool-size", type=int, default=DEFAULT_POOL_SIZE)
    parser.add_argument("--min-announcements", type=int, default=DEFAULT_MIN_ANN)
    parser.add_argument("--max-announcements", type=int, default=DEFAULT_MAX_ANN)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=asx.data_dir() / "ladder",
        help="Write pool + shards here (default: data/ladder/)",
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


def announcement_count(ticker: str) -> int:
    path = asx.announcements_csv_path(ticker)
    if not path.exists():
        return 0
    with path.open(encoding="utf-8", newline="") as fh:
        return sum(1 for _ in csv.DictReader(fh))


def load_ticker_keys(ticker: str) -> list[str]:
    path = asx.announcements_csv_path(ticker)
    keys: list[str] = []
    with path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            key = (row.get("documentKey") or "").strip()
            if key:
                keys.append(key)
    return keys


def build_pool(tickers: list[str], pool_size: int, min_ann: int, max_ann: int) -> list[str]:
    eligible: list[tuple[str, list[str]]] = []
    for ticker in tickers:
        count = announcement_count(ticker)
        if count < min_ann or count > max_ann:
            continue
        keys = load_ticker_keys(ticker)
        if keys:
            eligible.append((ticker, keys))

    if not eligible:
        raise RuntimeError(
            f"No tickers with {min_ann}–{max_ann} announcements found. "
            "Adjust --min-announcements / --max-announcements."
        )

    pool: list[str] = []
    indices = [0] * len(eligible)
    while len(pool) < pool_size:
        added = False
        for i, (_, keys) in enumerate(eligible):
            idx = indices[i]
            if idx >= len(keys):
                continue
            pool.append(keys[idx])
            indices[i] += 1
            added = True
            if len(pool) >= pool_size:
                break
        if not added:
            break

    if len(pool) < pool_size:
        raise RuntimeError(
            f"Only collected {len(pool)} keys (need {pool_size}). "
            "Widen announcement count range or increase eligible tickers."
        )
    return pool


def write_shards(pool: list[str], output_dir: Path) -> None:
    pool_dir = output_dir / "pool"
    pool_dir.mkdir(parents=True, exist_ok=True)
    pool_file = pool_dir / "document_keys.txt"
    pool_file.write_text("\n".join(pool) + "\n", encoding="utf-8")
    print(f"Wrote {len(pool)} keys → {pool_file}")

    for workers in RUNG_WORKERS:
        if len(pool) % workers != 0:
            print(f"  skip shards/{workers}workers: {len(pool)} not divisible by {workers}")
            continue
        per_worker = len(pool) // workers
        shard_dir = output_dir / "shards" / f"{workers}workers"
        shard_dir.mkdir(parents=True, exist_ok=True)
        for w in range(workers):
            start = w * per_worker
            shard = pool[start : start + per_worker]
            shard_file = shard_dir / f"shard_{w:02d}.txt"
            shard_file.write_text("\n".join(shard) + "\n", encoding="utf-8")
        print(f"  shards/{workers}workers: {workers} × {per_worker} keys")


def main() -> int:
    args = parse_args()
    tickers = load_entities()
    pool = build_pool(tickers, args.pool_size, args.min_announcements, args.max_announcements)
    write_shards(pool, args.output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
