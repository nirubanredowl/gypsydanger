#!/usr/bin/env python3
"""Collect Stage 3A LiteParse progress metrics for email / watcher."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_TOTAL_DOCS = 22_573
DEFAULT_BASELINE_DOCS_HR = 3_600  # ~20 workers × ~180 docs/hr (50 pg @ 0.2s/pg)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--bucket",
        default=os.environ.get("GYPSY_S3_BUCKET", ""),
    )
    parser.add_argument(
        "--total-docs",
        type=int,
        default=int(os.environ.get("GYPSY_PARSE_TOTAL_DOCS", DEFAULT_TOTAL_DOCS)),
    )
    return parser.parse_args()


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
    raw = s3_read_text(bucket, "manifests/parse_progress.json")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def fmt_duration(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "n/a"
    hours = int(seconds // 3600)
    mins = int((seconds % 3600) // 60)
    if hours >= 48:
        days = hours // 24
        return f"{days}d {hours % 24}h"
    return f"{hours}h {mins}m"


def collect(bucket: str, total_docs: int) -> dict:
    now = datetime.now(timezone.utc)
    manifest = load_progress_manifest(bucket)

    parsed = int(manifest.get("documents_parsed", 0))
    skipped = int(manifest.get("skipped_existing", 0))
    docs_done = int(manifest.get("documents_done", parsed + skipped))
    errors = int(manifest.get("errors", 0))
    pages = int(manifest.get("pages_parsed", 0))

    docs_hr = manifest.get("docs_hr")
    if docs_hr is not None:
        docs_hr = float(docs_hr)
    else:
        docs_hr = float(os.environ.get("GYPSY_PARSE_BASELINE_DOCS_HR", DEFAULT_BASELINE_DOCS_HR))

    status = manifest.get("status", "not_started")
    if docs_done > 0 and docs_done < total_docs:
        status = "running"
    elif docs_done >= total_docs and total_docs > 0:
        status = "complete"

    pct = (100.0 * docs_done / total_docs) if total_docs else 0.0
    remaining = max(total_docs - docs_done, 0)
    eta_s = (remaining / docs_hr * 3600) if docs_hr and remaining else None

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
        "phase": "3a_liteparse",
        "documents_parsed": parsed,
        "documents_skipped": skipped,
        "documents_done": docs_done,
        "documents_total": total_docs,
        "pct_complete": round(pct, 2),
        "pages_parsed": pages,
        "errors": errors,
        "error_pct": round(100.0 * errors / docs_done, 3) if docs_done else 0.0,
        "docs_hr": round(docs_hr),
        "eta": fmt_duration(eta_s),
        "elapsed": fmt_duration(elapsed_s),
        "workers": manifest.get("workers"),
        "complete_workers": manifest.get("complete_workers"),
        "run_id": manifest.get("run_id"),
        "corpus_key": manifest.get("corpus_key", "annual_reports"),
    }


def format_report(data: dict) -> str:
    lines = [
        f"Gypsy Danger parse progress (3A LiteParse) — {data['timestamp_utc']}",
        "",
        f"Status:            {data['status']}",
        f"Documents:         {data['documents_done']:,} / {data['documents_total']:,} ({data['pct_complete']}%)",
        f"  Parsed (new):    {data['documents_parsed']:,}",
        f"  Skipped (exist): {data['documents_skipped']:,}",
        f"Pages parsed:      {data['pages_parsed']:,}",
        f"Errors:            {data['errors']:,} ({data['error_pct']}%)",
        f"Throughput:        ~{data['docs_hr']:,} docs/hr",
        f"Elapsed:           {data['elapsed']}",
        f"ETA:               {data['eta']}",
    ]
    if data.get("workers"):
        lines.append(f"Workers:           {data['workers']} (complete: {data.get('complete_workers', '?')})")
    if data.get("run_id"):
        lines.append(f"Run ID:            {data['run_id']}")
    lines.extend(
        [
            "",
            f"S3: s3://{os.environ.get('GYPSY_S3_BUCKET', '')}/manifests/parse_progress.json",
            "",
            "On-demand email:",
            "  0-work/scripts/aws/request_parse_progress.sh",
            "Instant:",
            "  0-work/scripts/aws/request_parse_progress.sh --now",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    if not args.bucket:
        print("Set GYPSY_S3_BUCKET or pass --bucket", file=sys.stderr)
        return 2
    data = collect(args.bucket, args.total_docs)
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        print(format_report(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
