#!/usr/bin/env python3
"""Phase 0 — Randomly sample tickers from entities.csv → data/pilot_tickers.txt."""

from __future__ import annotations

import argparse
import csv
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from importlib import import_module

asx = import_module("00_asx_api")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Pick random pilot tickers from data/entities.csv"
    )
    parser.add_argument(
        "--count",
        type=int,
        default=10,
        help="Number of tickers to pick (default 10)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Random seed for reproducible selection",
    )
    parser.add_argument(
        "--include-suspended",
        action="store_true",
        help="Allow SUSPENDED market-cap rows in the pool (default: excluded)",
    )
    return parser.parse_args()


def load_tickers(entities_path: Path, *, exclude_suspended: bool) -> list[str]:
    if not entities_path.exists():
        raise FileNotFoundError(
            f"{entities_path} not found. Run 01_normalise_entities.py first."
        )
    tickers: list[str] = []
    with entities_path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            ticker = (row.get("ticker") or "").strip().upper()
            if not ticker:
                continue
            if exclude_suspended and (
                row.get("market_cap_aud") or ""
            ).upper() == "SUSPENDED":
                continue
            tickers.append(ticker)
    return tickers


def main() -> int:
    args = parse_args()
    if args.count < 1:
        print("--count must be >= 1")
        return 1

    exclude_suspended = not args.include_suspended
    pool = load_tickers(asx.entities_csv_path(), exclude_suspended=exclude_suspended)
    if len(pool) < args.count:
        print(
            f"Pool has {len(pool)} tickers; requested {args.count}. "
            "Lower --count or use --include-suspended."
        )
        return 1

    rng = random.Random(args.seed)
    picked = sorted(rng.sample(pool, args.count))

    out_path = asx.pilot_tickers_path()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"# Pilot tickers — randomly picked {args.count} from entities.csv",
        f"# Generated: {generated}",
    ]
    if args.seed is not None:
        lines.append(f"# Seed: {args.seed}")
    lines.append("")
    lines.extend(picked)
    lines.append("")

    out_path.write_text("\n".join(lines), encoding="utf-8")

    print(f"Wrote {len(picked)} tickers → {out_path}")
    print(", ".join(picked))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
