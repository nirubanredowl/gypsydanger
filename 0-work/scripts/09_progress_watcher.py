#!/usr/bin/env python3
"""Evaluate fetch progress events and send SNS notifications (with dedupe)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import importlib

progress = import_module("09_fetch_progress")

STATE_KEY = "manifests/progress_notify_state.json"
TRIGGER_KEY = "manifests/request_progress.trigger"
MILESTONES = tuple(range(10, 100, 10))
STALL_SECONDS = 45 * 60
DAILY_SECONDS = 24 * 3600
ERROR_SPIKE_DELTA = 100


def aws(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["AWS_PAGER"] = ""
    return subprocess.run(
        ["aws", *args, "--no-cli-pager"],
        capture_output=True,
        text=True,
        env=env,
        check=check,
    )


def s3_read(key: str) -> str | None:
    r = aws("s3", "cp", f"s3://{bucket()}/{key}", "-", check=False)
    if r.returncode != 0:
        return None
    return r.stdout


def s3_write(key: str, body: str) -> None:
    proc = subprocess.run(
        ["aws", "s3", "cp", "-", f"s3://{bucket()}/{key}", "--no-cli-pager"],
        input=body,
        text=True,
        capture_output=True,
        env={**os.environ, "AWS_PAGER": ""},
        check=True,
    )


def s3_delete(key: str) -> None:
    aws("s3", "rm", f"s3://{bucket()}/{key}", check=False)


def bucket() -> str:
    b = os.environ.get("GYPSY_S3_BUCKET", "")
    if not b:
        raise SystemExit("GYPSY_S3_BUCKET not set")
    return b


def notify(subject: str, body: str) -> None:
    script = Path(__file__).resolve().parent / "aws" / "notify_sns.sh"
    subprocess.run([str(script), subject, body], check=True)


def load_state() -> dict:
    raw = s3_read(STATE_KEY)
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def save_state(state: dict) -> None:
    s3_write(STATE_KEY, json.dumps(state, indent=2) + "\n")


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def main() -> int:
    metrics = progress.collect(bucket(), progress.DEFAULT_TOTAL_PDFS)
    report = progress.format_report(metrics)
    state = load_state()
    now = datetime.now(timezone.utc)

    def send(subject: str, event: str) -> None:
        notify(subject, report)
        state["last_event"] = event
        state["last_notify_utc"] = now.isoformat()

    # On-demand trigger file
    if s3_read(TRIGGER_KEY) is not None:
        send("Gypsy Danger progress — on-demand", "on_demand")
        s3_delete(TRIGGER_KEY)

    pdfs = metrics["pdfs_done"]
    pct = metrics["pct_complete"]
    status = metrics["status"]
    errors = metrics["errors"]
    last_pdfs = int(state.get("last_pdfs_done", 0))
    last_milestone = int(state.get("last_milestone_pct", 0))
    last_errors = int(state.get("last_errors", 0))

    if status == "running" and not state.get("fetch_started_sent") and pdfs > 0:
        send("Gypsy Danger fetch started", "fetch_started")
        state["fetch_started_sent"] = True

    milestone = int(pct // 10) * 10
    if milestone > last_milestone and milestone in MILESTONES:
        send(f"Gypsy Danger fetch — {milestone}% complete", f"milestone_{milestone}")
        state["last_milestone_pct"] = milestone

    if status == "running" and pct < 100:
        last_daily = parse_ts(state.get("last_daily_utc"))
        if last_daily is None:
            state["last_daily_utc"] = now.isoformat()
        elif (now - last_daily).total_seconds() >= DAILY_SECONDS:
            send("Gypsy Danger fetch — daily digest", "daily_digest")
            state["last_daily_utc"] = now.isoformat()

    if status == "running" and pdfs == last_pdfs and pdfs > 0 and pct < 100:
        stall_since = parse_ts(state.get("stall_since_utc"))
        if stall_since is None:
            state["stall_since_utc"] = now.isoformat()
        elif (now - stall_since).total_seconds() >= STALL_SECONDS:
            last_stall = parse_ts(state.get("last_stall_notify_utc"))
            if last_stall is None or (now - last_stall).total_seconds() >= 3600:
                send("Gypsy Danger fetch — stalled?", "stall")
                state["last_stall_notify_utc"] = now.isoformat()
    else:
        state.pop("stall_since_utc", None)

    if errors - last_errors >= ERROR_SPIKE_DELTA and errors > 0:
        send(f"Gypsy Danger fetch — errors now {errors:,}", "error_spike")

    if status == "complete" and not state.get("fetch_complete_sent"):
        send("Gypsy Danger fetch complete", "fetch_complete")
        state["fetch_complete_sent"] = True

    state["last_pdfs_done"] = pdfs
    state["last_errors"] = errors
    state["last_pct"] = pct
    state["last_status"] = status
    save_state(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
