#!/usr/bin/env python3
"""Fetch loose annual report PDFs from CDN and upload to S3."""

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
        default=os.environ.get("GYPSY_PREFLIGHT_RUN_ID", ""),
        help="Run id for logs/manifests",
    )
    parser.add_argument(
        "--worker-id",
        default=os.environ.get("GYPSY_WORKER_ID", "00"),
        help="Worker label for result JSON",
    )
    parser.add_argument(
        "--max-reports",
        type=int,
        default=0,
        help="Max annual reports to fetch (0 = all loose matches)",
    )
    parser.add_argument(
        "--start-offset",
        type=int,
        default=0,
        help="Skip first N loose annual report rows (burn rotation resume)",
    )
    parser.add_argument(
        "--rotation",
        type=int,
        default=0,
        help="IP rotation count metadata",
    )
    parser.add_argument(
        "--annual-filter",
        choices=("strict", "loose"),
        default="loose",
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


def aws_cmd(*parts: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["AWS_PAGER"] = ""
    return subprocess.run(
        ["aws", *parts, "--no-cli-pager"],
        capture_output=True,
        text=True,
        env=env,
    )


def s3_object_exists(bucket: str, key: str) -> bool:
    result = aws_cmd("s3api", "head-object", "--bucket", bucket, "--key", key)
    return result.returncode == 0


def s3_upload_bytes(bucket: str, key: str, payload: bytes) -> None:
    tmp = Path(f"/tmp/upload-{os.getpid()}.pdf")
    tmp.write_bytes(payload)
    try:
        result = aws_cmd("s3", "cp", str(tmp), f"s3://{bucket}/{key}")
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or f"s3 cp failed: {key}")
    finally:
        tmp.unlink(missing_ok=True)


def load_announcements_csv(
    ticker: str, *, bucket: str, source: str
) -> list[dict[str, str]]:
    ticker = ticker.upper()
    if source == "local":
        path = asx.announcements_csv_path(ticker)
        if not path.exists():
            raise FileNotFoundError(path)
        with path.open(encoding="utf-8", newline="") as fh:
            return list(csv.DictReader(fh))

    key = f"entities/{ticker}/{ticker}_Announcements.csv"
    tmp = Path(f"/tmp/{ticker}_Announcements.csv")
    result = aws_cmd("s3", "cp", f"s3://{bucket}/{key}", str(tmp))
    if result.returncode != 0:
        raise FileNotFoundError(
            f"s3://{bucket}/{key} not found. Upload index CSVs first."
        )
    with tmp.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def annual_report_rows(
    rows: list[dict[str, str]], *, mode: str, start_offset: int
) -> list[dict[str, str]]:
    matched = [
        row
        for row in rows
        if asx.is_annual_report_announcement(row, mode=mode)
    ]
    if start_offset:
        matched = matched[start_offset:]
    return matched


def write_result(path: Path | None, payload: dict[str, object]) -> None:
    if not path:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


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

    rows = load_announcements_csv(
        ticker, bucket=args.bucket, source=args.announcements_source
    )
    targets = annual_report_rows(
        rows, mode=args.annual_filter, start_offset=args.start_offset
    )
    if args.max_reports > 0:
        targets = targets[: args.max_reports]

    print(
        f"Fetch→S3: ticker={ticker} filter={args.annual_filter} "
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
        s3_key = asx.s3_annual_report_key(ticker, date_str, document_key)

        if s3_object_exists(args.bucket, s3_key):
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
            s3_upload_bytes(args.bucket, s3_key, content)
            ok += 1
            uploaded_keys.append(s3_key)
            print(f"  OK {s3_key} ({len(content)} bytes)")
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
    reports_done = ok + skip + fail
    complete = (not burned) and reports_done >= len(targets)

    payload: dict[str, object] = {
        "worker_id": args.worker_id,
        "ticker": ticker,
        "run_id": args.run_id or None,
        "rotation": args.rotation,
        "start_offset": args.start_offset,
        "reports_target": len(targets),
        "reports_done": reports_done,
        "absolute_offset": args.start_offset + reports_done,
        "uploaded": ok,
        "skipped_existing": skip,
        "failed": fail,
        "annual_filter": args.annual_filter,
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
    }
    write_result(args.result_json, payload)
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
