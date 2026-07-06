#!/usr/bin/env python3
"""Fetch CFO change announcement PDFs from CDN and upload to S3."""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from importlib import import_module

asx = import_module("00_asx_api")
fetch = import_module("11_fetch_annual_reports_s3")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", required=True, help="ASX ticker to fetch")
    parser.add_argument(
        "--bucket",
        default=os.environ.get("GYPSY_S3_BUCKET", ""),
        help="S3 bucket (default: GYPSY_S3_BUCKET)",
    )
    parser.add_argument(
        "--run-id",
        default=os.environ.get("GYPSY_CFO_FETCH_RUN_ID", ""),
        help="Run id for logs/manifests",
    )
    parser.add_argument(
        "--worker-id",
        default=os.environ.get("GYPSY_WORKER_ID", "00"),
        help="Worker label for result JSON",
    )
    parser.add_argument(
        "--max-documents",
        type=int,
        default=0,
        help="Max CFO change PDFs to fetch (0 = all matches)",
    )
    parser.add_argument(
        "--start-offset",
        type=int,
        default=0,
        help="Skip first N matched rows (burn rotation resume)",
    )
    parser.add_argument(
        "--rotation",
        type=int,
        default=0,
        help="IP rotation count metadata",
    )
    parser.add_argument(
        "--include-tier-b",
        action="store_true",
        help="Include tier-B edge cases (default: tier A only)",
    )
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument(
        "--announcements-source",
        choices=("s3", "local"),
        default="s3",
        help="Where to load the announcements CSV",
    )
    parser.add_argument(
        "--result-json",
        type=Path,
        help="Write machine-readable worker summary here",
    )
    parser.add_argument("--burn-error-pct", type=float, default=1.0)
    parser.add_argument("--burn-consecutive-429", type=int, default=5)
    parser.add_argument(
        "--simulate-burn-after",
        type=int,
        default=0,
        help="Preflight only: mark IP burned after N successful uploads",
    )
    return parser.parse_args()


def cfo_change_rows(
    rows: list[dict[str, str]], *, include_tier_b: bool, start_offset: int
) -> list[dict[str, str]]:
    matched = [
        row
        for row in rows
        if asx.is_cfo_change_announcement(row, include_tier_b=include_tier_b)
    ]
    if start_offset:
        matched = matched[start_offset:]
    return matched


def main() -> int:
    args = parse_args()
    ticker = args.ticker.upper()
    if not args.bucket:
        print("error: --bucket or GYPSY_S3_BUCKET required", file=sys.stderr)
        return 2

    simulate = args.simulate_burn_after if args.simulate_burn_after > 0 else None
    burn = asx.CdnBurnTracker(
        burn_error_pct=args.burn_error_pct,
        consecutive_429_limit=args.burn_consecutive_429,
        simulate_burn_after=simulate,
    )
    client = asx.AsxClient(use_cache=not args.no_cache)

    rows = fetch.load_announcements_csv(
        ticker, bucket=args.bucket, source=args.announcements_source
    )
    targets = cfo_change_rows(
        rows, include_tier_b=args.include_tier_b, start_offset=args.start_offset
    )
    if args.max_documents > 0:
        targets = targets[: args.max_documents]

    tier_label = "A+B" if args.include_tier_b else "A"
    print(
        f"Fetch→S3 CFO: ticker={ticker} tier={tier_label} "
        f"targets={len(targets)} start_offset={args.start_offset} "
        f"rotation={args.rotation} bucket={args.bucket}"
    )

    started = time.monotonic()
    ok = skip = fail = 0
    uploaded_keys: list[str] = []
    burned = False

    for row in targets:
        document_key = (row.get("documentKey") or "").strip()
        if not document_key:
            continue
        date_str = row.get("date") or ""
        s3_key = asx.s3_cfo_change_key(ticker, date_str, document_key)

        if fetch.s3_object_exists(args.bucket, s3_key):
            skip += 1
            uploaded_keys.append(s3_key)
            continue

        url = asx.cdn_pdf_url(document_key)
        try:
            content = client.get_bytes(url, use_cache=not args.no_cache)
            if not asx.is_valid_pdf(content):
                raise ValueError(
                    f"download not a valid PDF ({len(content)} bytes, "
                    f"head={content[:8]!r})"
                )
            fetch.s3_upload_bytes(args.bucket, s3_key, content)
            ok += 1
            uploaded_keys.append(s3_key)
            tier = asx.cfo_change_tier(row)
            print(f"  OK [{tier}] {s3_key} ({len(content)} bytes)")
            burned = burn.record_success()
        except Exception as exc:  # noqa: BLE001
            status = asx.http_error_status(exc)
            fail += 1
            burned = burn.record_error(status)
            print(f"  FAIL {document_key}: {exc}")

        if burned:
            print(
                f"  BURNED after {burn.total_requests} CDN requests "
                f"(simulated={burn.snapshot()['simulated_burn']})"
            )
            break

    elapsed = time.monotonic() - started
    snap = burn.snapshot()
    docs_done = ok + skip + fail
    complete = (not burned) and docs_done >= len(targets)

    payload: dict[str, object] = {
        "worker_id": args.worker_id,
        "ticker": ticker,
        "run_id": args.run_id or None,
        "rotation": args.rotation,
        "start_offset": args.start_offset,
        "documents_target": len(targets),
        "documents_done": docs_done,
        "absolute_offset": args.start_offset + docs_done,
        "uploaded": ok,
        "skipped_existing": skip,
        "failed": fail,
        "include_tier_b": args.include_tier_b,
        "uploaded_keys": uploaded_keys,
        "elapsed_s": round(elapsed, 1),
        "requests": snap["requests"],
        "success": snap["success"],
        "429": snap["429"],
        "503": snap["503"],
        "other_errors": snap["other_errors"],
        "error_pct": snap["error_pct"],
        "burned": bool(snap["burned"]),
        "simulated_burn": bool(snap["simulated_burn"]),
        "complete": complete,
        "bucket": args.bucket,
        "s3_prefix": f"entities/{ticker}/cfo_changes/",
    }
    fetch.write_result(args.result_json, payload)
    if args.result_json:
        print(f"result_json: {args.result_json}")

    print(
        f"\nSummary: uploaded={ok} skipped={skip} failed={fail} "
        f"burned={snap['burned']} complete={complete}"
    )
    if snap["burned"]:
        return asx.BURN_EXIT_CODE
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
