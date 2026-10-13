#!/usr/bin/env python3
"""Copy Gypsy Danger corpus keys from AWS S3 to Neon Object Storage (or bucket→bucket).

Preserves the exact object key layout documented in 0-work/docs/extraction-pipeline.md.
Idempotent: skips destination objects when ContentLength matches the source.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from importlib import import_module

store_mod = import_module("00_object_store")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-backend",
        choices=("aws", "neon"),
        default="aws",
        help="Source credential profile (default: aws)",
    )
    parser.add_argument(
        "--dest-backend",
        choices=("aws", "neon"),
        default="neon",
        help="Destination credential profile (default: neon)",
    )
    parser.add_argument(
        "--source-bucket",
        default=os.environ.get("GYPSY_S3_BUCKET", ""),
        help="Source bucket (default: GYPSY_S3_BUCKET)",
    )
    parser.add_argument(
        "--dest-bucket",
        default=os.environ.get("GYPSY_STORAGE_BUCKET", "asx-artifacts"),
        help="Destination bucket (default: GYPSY_STORAGE_BUCKET or asx-artifacts)",
    )
    parser.add_argument(
        "--prefix",
        action="append",
        default=[],
        help="Object key prefix to copy (repeatable). Default: standard corpus prefixes.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List keys only; do not copy",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Max objects to copy (0 = no limit)",
    )
    parser.add_argument(
        "--manifest-out",
        type=Path,
        help="Write JSON summary of the run",
    )
    return parser.parse_args()


def _store(backend: str, bucket: str) -> store_mod.ObjectStore:
    """Build source/dest clients without cross-contaminating endpoint env vars."""
    saved_backend = os.environ.get("GYPSY_STORAGE_BACKEND")
    saved_endpoint = None
    os.environ["GYPSY_STORAGE_BACKEND"] = backend
    if backend == "aws":
        saved_endpoint = os.environ.pop("AWS_ENDPOINT_URL_S3", None)
    try:
        return store_mod.from_env(backend=backend, bucket=bucket)
    finally:
        if saved_endpoint is not None:
            os.environ["AWS_ENDPOINT_URL_S3"] = saved_endpoint
        if saved_backend is None:
            os.environ.pop("GYPSY_STORAGE_BACKEND", None)
        else:
            os.environ["GYPSY_STORAGE_BACKEND"] = saved_backend


def main() -> int:
    args = parse_args()
    if not args.source_bucket:
        print("error: --source-bucket or GYPSY_S3_BUCKET required", file=sys.stderr)
        return 2
    if not args.dest_bucket:
        print("error: --dest-bucket or GYPSY_STORAGE_BUCKET required", file=sys.stderr)
        return 2

    prefixes = args.prefix or list(store_mod.DEFAULT_PREFIXES)
    source = _store(args.source_backend, args.source_bucket)
    dest = _store(args.dest_backend, args.dest_bucket)

    t0 = time.time()
    started = datetime.now(timezone.utc).isoformat()
    stats = {
        "started_at": started,
        "source": source.config.uri_prefix,
        "dest": dest.config.uri_prefix,
        "prefixes": prefixes,
        "dry_run": args.dry_run,
        "listed": 0,
        "copied": 0,
        "skipped": 0,
        "errors": 0,
        "error_samples": [],
    }

    copied = 0
    for prefix in prefixes:
        print(f"Listing {source.uri(prefix)} …", flush=True)
        for key in source.list_keys(prefix):
            stats["listed"] += 1
            if args.limit and copied >= args.limit:
                break
            if args.dry_run:
                print(key)
                continue
            try:
                outcome = dest.copy_from(source, key, skip_if_same_size=True)
                stats[outcome] += 1
                copied += 1
                if copied % 500 == 0:
                    print(
                        f"  … {copied} objects ({stats['skipped']} skipped)",
                        flush=True,
                    )
            except Exception as exc:  # noqa: BLE001 — batch job continues
                stats["errors"] += 1
                if len(stats["error_samples"]) < 20:
                    stats["error_samples"].append({"key": key, "error": str(exc)})
                print(f"  ERROR {key}: {exc}", file=sys.stderr)
        if args.limit and copied >= args.limit:
            break

    stats["finished_at"] = datetime.now(timezone.utc).isoformat()
    stats["elapsed_s"] = round(time.time() - t0, 3)
    print(json.dumps(stats, indent=2))

    if args.manifest_out:
        args.manifest_out.parent.mkdir(parents=True, exist_ok=True)
        args.manifest_out.write_text(json.dumps(stats, indent=2) + "\n", encoding="utf-8")

    return 0 if stats["errors"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
