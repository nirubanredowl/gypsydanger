#!/usr/bin/env python3
"""Fetch loose annual report PDFs for a ticker shard and upload to S3."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from importlib import import_module

asx = import_module("00_asx_api")
fetch = import_module("11_fetch_annual_reports_s3")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tickers-file", type=Path, required=True)
    parser.add_argument(
        "--bucket",
        default=os.environ.get("GYPSY_S3_BUCKET", ""),
    )
    parser.add_argument("--run-id", default=os.environ.get("GYPSY_FETCH_RUN_ID", ""))
    parser.add_argument(
        "--worker-id",
        default=os.environ.get("GYPSY_WORKER_ID", "00"),
    )
    parser.add_argument(
        "--ticker-index",
        type=int,
        default=0,
        help="Resume at this ticker index in the shard (burn rotation)",
    )
    parser.add_argument(
        "--start-offset",
        type=int,
        default=0,
        help="Skip first N loose annual rows in the current ticker",
    )
    parser.add_argument("--rotation", type=int, default=0)
    parser.add_argument(
        "--annual-filter",
        choices=("strict", "loose"),
        default="loose",
    )
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument(
        "--rate-limit-s",
        type=float,
        default=float(os.environ.get("GYPSY_FETCH_RATE_LIMIT_S", "1.0")),
    )
    parser.add_argument("--result-json", type=Path)
    parser.add_argument("--burn-error-pct", type=float, default=1.0)
    parser.add_argument("--burn-consecutive-429", type=int, default=5)
    return parser.parse_args()


def load_tickers(path: Path) -> list[str]:
    tickers = [
        line.strip().upper()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    if not tickers:
        raise ValueError(f"empty ticker shard: {path}")
    return tickers


def shard_report_count(
    tickers: list[str],
    *,
    bucket: str,
    mode: str,
    from_index: int,
    from_offset: int,
) -> int:
    total = 0
    for idx in range(from_index, len(tickers)):
        rows = fetch.load_announcements_csv(tickers[idx], bucket=bucket, source="s3")
        matched = fetch.annual_report_rows(rows, mode=mode, start_offset=0)
        if idx == from_index:
            matched = matched[from_offset:]
        total += len(matched)
    return total


def main() -> int:
    args = parse_args()
    if not args.bucket:
        print("error: --bucket or GYPSY_S3_BUCKET required", file=sys.stderr)
        return 2

    tickers = load_tickers(args.tickers_file)
    if args.ticker_index >= len(tickers):
        print(
            f"error: ticker_index {args.ticker_index} >= shard size {len(tickers)}",
            file=sys.stderr,
        )
        return 2

    burn = asx.CdnBurnTracker(
        burn_error_pct=args.burn_error_pct,
        consecutive_429_limit=args.burn_consecutive_429,
    )
    client = asx.AsxClient(
        rate_limit_s=args.rate_limit_s,
        use_cache=not args.no_cache,
    )

    reports_target = shard_report_count(
        tickers,
        bucket=args.bucket,
        mode=args.annual_filter,
        from_index=args.ticker_index,
        from_offset=args.start_offset,
    )

    print(
        f"Phase C shard: worker={args.worker_id} rotation={args.rotation} "
        f"tickers={len(tickers)} resume_index={args.ticker_index} "
        f"start_offset={args.start_offset} targets={reports_target} "
        f"filter={args.annual_filter} bucket={args.bucket}"
    )

    started = time.monotonic()
    ok = skip = fail = 0
    uploaded_keys: list[str] = []
    reports_done = 0
    tickers_completed = args.ticker_index
    resume_ticker_index = args.ticker_index
    resume_offset = args.start_offset
    current_ticker = tickers[args.ticker_index]
    burned = False

    for idx in range(args.ticker_index, len(tickers)):
        ticker = tickers[idx]
        current_ticker = ticker
        offset = args.start_offset if idx == args.ticker_index else 0
        rows = fetch.load_announcements_csv(ticker, bucket=args.bucket, source="s3")
        targets = fetch.annual_report_rows(
            rows, mode=args.annual_filter, start_offset=offset
        )

        print(f"  ticker {ticker}: {len(targets)} targets (offset={offset})")
        processed_in_ticker = 0

        for row in targets:
            document_key = (row.get("documentKey") or "").strip()
            if not document_key:
                continue
            date_str = row.get("date") or ""
            s3_key = asx.s3_annual_report_key(ticker, date_str, document_key)

            if fetch.s3_object_exists(args.bucket, s3_key):
                skip += 1
                uploaded_keys.append(s3_key)
                reports_done += 1
                processed_in_ticker += 1
                continue

            url = asx.cdn_pdf_url(document_key)
            try:
                content = client.get_bytes(url, use_cache=not args.no_cache)
                if len(content) < asx.MIN_PDF_BYTES:
                    raise ValueError(f"download too small ({len(content)} bytes)")
                fetch.s3_upload_bytes(args.bucket, s3_key, content)
                ok += 1
                uploaded_keys.append(s3_key)
                reports_done += 1
                processed_in_ticker += 1
                print(f"    OK {s3_key} ({len(content)} bytes)")
                burned = burn.record_success()
            except Exception as exc:  # noqa: BLE001
                status = asx.http_error_status(exc)
                fail += 1
                reports_done += 1
                processed_in_ticker += 1
                burned = burn.record_error(status)
                print(f"    FAIL {document_key}: {exc}")

            if burned:
                resume_ticker_index = idx
                resume_offset = offset + processed_in_ticker
                print(
                    f"    BURNED after {burn.total_requests} CDN requests "
                    f"at ticker={ticker} resume_offset={resume_offset}"
                )
                break

        if burned:
            break

        tickers_completed = idx + 1
        resume_ticker_index = tickers_completed
        resume_offset = 0

    elapsed = time.monotonic() - started
    snap = burn.snapshot()
    complete = (not burned) and tickers_completed >= len(tickers)

    payload: dict[str, object] = {
        "worker_id": args.worker_id,
        "run_id": args.run_id or None,
        "rotation": args.rotation,
        "ticker_index": resume_ticker_index,
        "start_offset": resume_offset,
        "current_ticker": current_ticker,
        "tickers_total": len(tickers),
        "tickers_done": tickers_completed,
        "reports_target": reports_target,
        "reports_done": reports_done,
        "uploaded": ok,
        "skipped_existing": skip,
        "failed": fail,
        "annual_filter": args.annual_filter,
        "uploaded_keys": uploaded_keys[-20:],
        "uploaded_keys_count": len(uploaded_keys),
        "elapsed_s": round(elapsed, 1),
        "requests": snap["requests"],
        "success": snap["success"],
        "429": snap["429"],
        "503": snap["503"],
        "other_errors": snap["other_errors"],
        "error_pct": snap["error_pct"],
        "burned": bool(snap["burned"]),
        "complete": complete,
        "bucket": args.bucket,
    }
    fetch.write_result(args.result_json, payload)
    if args.result_json:
        print(f"result_json: {args.result_json}")

    print(
        f"\nSummary: uploaded={ok} skipped={skip} failed={fail} "
        f"tickers_done={tickers_completed}/{len(tickers)} burned={snap['burned']} "
        f"complete={complete}"
    )
    if snap["burned"]:
        return asx.BURN_EXIT_CODE
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
