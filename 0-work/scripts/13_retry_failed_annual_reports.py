#!/usr/bin/env python3
"""Retry failed Phase C annual-report PDF uploads."""

from __future__ import annotations

import argparse
import json
import os
import re
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
    parser.add_argument(
        "--from-logs-dir",
        type=Path,
        help="Parse worker_*.log FAIL lines from this directory",
    )
    parser.add_argument(
        "--from-json",
        type=Path,
        help="JSON list of failures with ticker, document_key, date, s3_key",
    )
    parser.add_argument(
        "--bucket",
        default=os.environ.get("GYPSY_S3_BUCKET", ""),
    )
    parser.add_argument(
        "--run-id",
        default=os.environ.get("GYPSY_FETCH_RUN_ID", "retry"),
    )
    parser.add_argument("--rate-limit-s", type=float, default=0.5)
    parser.add_argument("--retries", type=int, default=5)
    parser.add_argument(
        "--manifest-s3-key",
        default="",
        help="Upload retry summary JSON to this S3 key",
    )
    return parser.parse_args()


def parse_failures_from_logs(log_dir: Path) -> list[dict[str, str]]:
    fails: list[dict[str, str]] = []
    for log in sorted(log_dir.glob("worker_*.log")):
        ticker: str | None = None
        for line in log.read_text(errors="replace").splitlines():
            m = re.match(r"\s*ticker (\w+):", line)
            if m:
                ticker = m.group(1)
            m = re.match(r"\s*FAIL ([^:]+): (.+)$", line)
            if m and ticker:
                fails.append(
                    {
                        "worker": log.stem,
                        "ticker": ticker,
                        "document_key": m.group(1).strip(),
                        "error": m.group(2).strip(),
                    }
                )
    return fails


def enrich_failures(fails: list[dict[str, str]]) -> list[dict[str, str]]:
    import csv

    enriched: list[dict[str, str]] = []
    for row in fails:
        ticker = row["ticker"].upper()
        key = row["document_key"]
        path = asx.announcements_csv_path(ticker)
        date = ""
        with path.open(encoding="utf-8", newline="") as fh:
            for ann in csv.DictReader(fh):
                if (ann.get("documentKey") or "").strip() == key:
                    date = ann.get("date") or ""
                    break
        if not date:
            raise RuntimeError(f"no announcement row for {ticker} {key}")
        enriched.append(
            {
                **row,
                "date": date,
                "s3_key": asx.s3_annual_report_key(ticker, date, key),
            }
        )
    return enriched


def main() -> int:
    args = parse_args()
    if not args.bucket:
        print("error: --bucket or GYPSY_S3_BUCKET required", file=sys.stderr)
        return 2

    if args.from_json:
        fails = json.loads(args.from_json.read_text(encoding="utf-8"))
    elif args.from_logs_dir:
        fails = enrich_failures(parse_failures_from_logs(args.from_logs_dir))
    else:
        print("error: pass --from-logs-dir or --from-json", file=sys.stderr)
        return 2

    client = asx.AsxClient(rate_limit_s=args.rate_limit_s, use_cache=False)
    results: list[dict[str, object]] = []
    ok = skip = fail = 0

    print(f"Retrying {len(fails)} failed PDFs → s3://{args.bucket}/")
    for row in fails:
        ticker = row["ticker"]
        key = row["document_key"]
        s3_key = row["s3_key"]
        url = asx.cdn_pdf_url(key)
        outcome: dict[str, object] = {
            "ticker": ticker,
            "document_key": key,
            "s3_key": s3_key,
            "original_error": row.get("error"),
        }

        if fetch.s3_object_exists(args.bucket, s3_key):
            skip += 1
            outcome.update({"status": "skipped_existing"})
            results.append(outcome)
            print(f"  SKIP {s3_key}")
            continue

        try:
            content = client.get_bytes(url, use_cache=False, retries=args.retries)
            if not asx.is_valid_pdf(content):
                raise ValueError(
                    f"not a valid PDF ({len(content)} bytes, head={content[:8]!r})"
                )
            fetch.s3_upload_bytes(args.bucket, s3_key, content)
            ok += 1
            outcome.update({"status": "uploaded", "bytes": len(content)})
            print(f"  OK   {s3_key} ({len(content)} bytes)")
        except Exception as exc:  # noqa: BLE001
            fail += 1
            outcome.update({"status": "failed", "error": str(exc)})
            print(f"  FAIL {ticker} {key}: {exc}")

        results.append(outcome)

    summary = {
        "run_id": args.run_id,
        "attempted": len(fails),
        "uploaded": ok,
        "skipped_existing": skip,
        "failed": fail,
        "results": results,
    }
    manifest_path = Path("/tmp/retry-failed-summary.json")
    manifest_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(
        f"\nRetry summary: uploaded={ok} skipped={skip} failed={fail} "
        f"(manifest {manifest_path})"
    )

    manifest_key = args.manifest_s3_key or f"manifests/fetch/{args.run_id}/retry_summary.json"
    subprocess.run(
        [
            "aws",
            "s3",
            "cp",
            str(manifest_path),
            f"s3://{args.bucket}/{manifest_key}",
            "--no-cli-pager",
        ],
        check=True,
        env={**os.environ, "AWS_PAGER": ""},
    )
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
