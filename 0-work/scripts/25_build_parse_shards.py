#!/usr/bin/env python3
"""Build balanced ticker shards for Stage 3A LiteParse (annual reports)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from importlib import import_module

asx = import_module("00_asx_api")
phase_c = import_module("12_build_phase_c_shards")

DEFAULT_WORKERS = 20


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workers",
        type=int,
        default=int(
            __import__("os").environ.get("GYPSY_PARSE_WORKERS", DEFAULT_WORKERS)
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=asx.data_dir() / "parse_3a",
    )
    parser.add_argument(
        "--annual-filter",
        choices=("strict", "loose"),
        default="loose",
    )
    parser.add_argument(
        "--corpus-key",
        default="annual_reports",
        help="Corpus to shard (annual_reports for 3A pilot)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.workers < 1:
        print("error: --workers must be >= 1", file=sys.stderr)
        return 2

    # Reuse Phase C balancing — same tickers, same report counts.
    tickers = phase_c.load_entities()
    weighted: list[tuple[str, int]] = []
    total_reports = 0
    for ticker in tickers:
        count = phase_c.loose_annual_count(ticker, mode=args.annual_filter)
        if count > 0:
            weighted.append((ticker, count))
            total_reports += count

    if not weighted:
        print("error: no tickers with annual reports found", file=sys.stderr)
        return 1

    shards = phase_c.balance_shards(weighted, args.workers)
    shard_dir = args.output_dir / "shards" / f"{args.workers}workers"
    shard_dir.mkdir(parents=True, exist_ok=True)

    corpus_id, parse_folder, fetch_folder = asx.corpus_info(args.corpus_key)
    shard_meta = []
    for i, tickers_in_shard in enumerate(shards):
        shard_file = shard_dir / f"shard_{i:02d}.txt"
        shard_file.write_text("\n".join(tickers_in_shard) + "\n", encoding="utf-8")
        reports = sum(w for t, w in weighted if t in tickers_in_shard)
        shard_meta.append(
            {
                "worker_id": f"{i:02d}",
                "tickers": len(tickers_in_shard),
                "documents_est": reports,
                "file": str(shard_file.relative_to(args.output_dir)),
            }
        )
        print(
            f"  shard_{i:02d}: {len(tickers_in_shard)} tickers, "
            f"{reports} {args.annual_filter} annual reports"
        )

    manifest = {
        "phase": "3a_liteparse",
        "corpus_key": args.corpus_key,
        "corpus_id": corpus_id,
        "parse_folder": parse_folder,
        "fetch_s3_folder": fetch_folder,
        "workers": args.workers,
        "annual_filter": args.annual_filter,
        "tickers_with_reports": len(weighted),
        "documents_total": total_reports,
        "shards": shard_meta,
    }
    manifest_path = args.output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(
        f"\nWrote {args.workers} shards → {shard_dir}\n"
        f"Total: {len(weighted)} tickers, {total_reports} documents\n"
        f"Manifest: {manifest_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
