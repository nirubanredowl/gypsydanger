#!/usr/bin/env python3
"""Collect Gypsy Danger fetch progress metrics for email / watcher."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_TOTAL_PDFS = 1_260_000
DEFAULT_BASELINE_DOCS_HR = 17_870  # rung 4 aggregate


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Print JSON only")
    parser.add_argument(
        "--bucket",
        default=os.environ.get("GYPSY_S3_BUCKET", ""),
        help="S3 bucket (default: GYPSY_S3_BUCKET)",
    )
    parser.add_argument(
        "--total-pdfs",
        type=int,
        default=int(os.environ.get("GYPSY_TOTAL_PDFS", DEFAULT_TOTAL_PDFS)),
    )
    return parser.parse_args()


def aws_json(*cmd: str) -> dict | list | None:
    env = os.environ.copy()
    env["AWS_PAGER"] = ""
    result = subprocess.run(
        ["aws", *cmd, "--no-cli-pager", "--output", "json"],
        capture_output=True,
        text=True,
        env=env,
    )
    if result.returncode != 0:
        return None
    if not result.stdout.strip():
        return None
    return json.loads(result.stdout)


def s3_read_text(bucket: str, key: str) -> str | None:
    env = os.environ.copy()
    env["AWS_PAGER"] = ""
    result = subprocess.run(
        ["aws", "s3", "cp", f"s3://{bucket}/{key}", "-", "--no-cli-pager"],
        capture_output=True,
        text=True,
        env=env,
    )
    if result.returncode != 0:
        return None
    return result.stdout


def load_progress_manifest(bucket: str) -> dict:
    raw = s3_read_text(bucket, "manifests/fetch_progress.json")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def count_completed_tickers(bucket: str) -> int:
    raw = s3_read_text(bucket, "manifests/completed_tickers.txt")
    if not raw:
        return 0
    return sum(1 for line in raw.splitlines() if line.strip() and not line.startswith("#"))


def sqs_depth(queue_url: str) -> dict:
    attrs = aws_json(
        "sqs",
        "get-queue-attributes",
        "--queue-url",
        queue_url,
        "--attribute-names",
        "ApproximateNumberOfMessages",
        "ApproximateNumberOfMessagesNotVisible",
        "ApproximateNumberOfMessagesDelayed",
    )
    if not isinstance(attrs, dict):
        return {"visible": None, "inflight": None, "delayed": None}
    a = attrs.get("Attributes", {})
    return {
        "visible": int(a.get("ApproximateNumberOfMessages", 0)),
        "inflight": int(a.get("ApproximateNumberOfMessagesNotVisible", 0)),
        "delayed": int(a.get("ApproximateNumberOfMessagesDelayed", 0)),
    }


def estimate_pdfs_done(manifest: dict, completed_tickers: int, total_pdfs: int) -> int:
    if "pdfs_uploaded" in manifest:
        return int(manifest["pdfs_uploaded"])
    if "pdfs_done" in manifest:
        return int(manifest["pdfs_done"])
    if completed_tickers and "avg_pdfs_per_ticker" in manifest:
        return int(completed_tickers * manifest["avg_pdfs_per_ticker"])
    return 0


def fmt_duration(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "n/a"
    hours = int(seconds // 3600)
    mins = int((seconds % 3600) // 60)
    if hours >= 48:
        days = hours // 24
        return f"{days}d {hours % 24}h"
    return f"{hours}h {mins}m"


def collect(bucket: str, total_pdfs: int) -> dict:
    now = datetime.now(timezone.utc)
    manifest = load_progress_manifest(bucket)
    completed_tickers = count_completed_tickers(bucket)
    pdfs_done = estimate_pdfs_done(manifest, completed_tickers, total_pdfs)
    pdfs_done = min(pdfs_done, total_pdfs) if pdfs_done else 0

    errors = int(manifest.get("errors", manifest.get("failed_documents", 0)))
    docs_hr = manifest.get("docs_hr") or manifest.get("rolling_docs_hr")
    if docs_hr is not None:
        docs_hr = float(docs_hr)
    else:
        docs_hr = float(os.environ.get("GYPSY_BASELINE_DOCS_HR", DEFAULT_BASELINE_DOCS_HR))

    status = manifest.get("status", "not_started")
    if pdfs_done > 0 and pdfs_done < total_pdfs:
        status = "running"
    elif pdfs_done >= total_pdfs and total_pdfs > 0:
        status = "complete"

    pct = (100.0 * pdfs_done / total_pdfs) if total_pdfs else 0.0
    remaining = max(total_pdfs - pdfs_done, 0)
    eta_s = (remaining / docs_hr * 3600) if docs_hr and remaining else None

    queue_url = os.environ.get("GYPSY_SQS_QUEUE_URL", "")
    queue = sqs_depth(queue_url) if queue_url else {"visible": None, "inflight": None, "delayed": None}

    started_at = manifest.get("started_at")
    elapsed_s = None
    if started_at:
        try:
            started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
            elapsed_s = (now - started).total_seconds()
        except ValueError:
            pass

    return {
        "timestamp_utc": now.strftime("%Y-%m-%d %H:%M UTC"),
        "status": status,
        "pdfs_done": pdfs_done,
        "pdfs_total": total_pdfs,
        "pct_complete": round(pct, 2),
        "errors": errors,
        "error_pct": round(100.0 * errors / pdfs_done, 3) if pdfs_done else 0.0,
        "completed_tickers": completed_tickers,
        "docs_hr": round(docs_hr),
        "eta": fmt_duration(eta_s),
        "elapsed": fmt_duration(elapsed_s),
        "queue_visible": queue["visible"],
        "queue_inflight": queue["inflight"],
        "workers": manifest.get("workers"),
        "run_id": manifest.get("run_id"),
    }


def format_report(data: dict) -> str:
    lines = [
        f"Gypsy Danger fetch progress — {data['timestamp_utc']}",
        "",
        f"Status:            {data['status']}",
        f"PDFs:              {data['pdfs_done']:,} / {data['pdfs_total']:,} ({data['pct_complete']}%)",
        f"Errors:            {data['errors']:,} ({data['error_pct']}%)",
        f"Tickers complete:  {data['completed_tickers']:,}",
        f"Throughput:        ~{data['docs_hr']:,} docs/hr",
        f"Elapsed:           {data['elapsed']}",
        f"ETA:               {data['eta']}",
    ]
    if data["queue_visible"] is not None:
        lines.extend(
            [
                "",
                f"SQS visible:       {data['queue_visible']:,}",
                f"SQS in-flight:     {data['queue_inflight']:,}",
            ]
        )
    if data.get("workers"):
        lines.append(f"Workers:           {data['workers']}")
    if data.get("run_id"):
        lines.append(f"Run ID:            {data['run_id']}")
    lines.extend(
        [
            "",
            f"S3: s3://{os.environ.get('GYPSY_S3_BUCKET', '')}/manifests/fetch_progress.json",
            "",
            "On-demand: aws s3 cp /dev/null s3://$GYPSY_S3_BUCKET/manifests/request_progress.trigger",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    if not args.bucket:
        print("Set GYPSY_S3_BUCKET or pass --bucket", file=sys.stderr)
        return 2
    data = collect(args.bucket, args.total_pdfs)
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        print(format_report(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
