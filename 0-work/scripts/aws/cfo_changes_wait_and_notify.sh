#!/usr/bin/env bash
# Poll CFO fetch worker results, rotate burned EC2s, update progress manifest, email on completion.
set -euo pipefail

RUN_ID="${1:?run id}"
WORKERS="${2:?worker count}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f /etc/profile.d/gypsy-danger.sh ]]; then
  source /etc/profile.d/gypsy-danger.sh
else
  ENV_FILE="${SCRIPT_DIR}/../.env"
  if [[ -f "$ENV_FILE" ]]; then
    set -a
    # shellcheck source=/dev/null
    source "$ENV_FILE"
    set +a
  fi
fi
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-ap-southeast-2}"
export AWS_PAGER=""
export AWS_CLI_PAGER=""

BUCKET="${GYPSY_S3_BUCKET:?GYPSY_S3_BUCKET required}"
MAX_ROTATIONS="${GYPSY_BURN_MAX_ROTATIONS:-3}"
TOTAL_DOCS="${GYPSY_CFO_TOTAL_DOCS:-2133}"
PREFIX="logs/cfo_fetch/${RUN_ID}/"
LAUNCH="${SCRIPT_DIR}/launch_cfo_changes_worker.sh"
if [[ ! -x "$LAUNCH" ]]; then
  ROOT="/opt/gypsy-danger"
  mkdir -p "$ROOT"
  aws s3 cp "s3://${BUCKET}/scripts/launch_cfo_changes_worker.sh" "$LAUNCH"
  chmod +x "$LAUNCH"
fi

WAIT_MAX_S="${GYPSY_FETCH_WAIT_MAX_S:-14400}"
DEADLINE=$(( $(date +%s) + WAIT_MAX_S ))
POLL_S="${GYPSY_FETCH_POLL_S:-60}"
echo "CFO fetch waiter: run=${RUN_ID} workers=${WORKERS} prefix=s3://${BUCKET}/${PREFIX} max_wait=${WAIT_MAX_S}s"

worker_done() {
  local wid=$1
  aws s3 cp "s3://${BUCKET}/${PREFIX}worker_${wid}.json" "/tmp/fetch_${wid}.json" 2>/dev/null || return 1
  python3 - <<PY
import json
from pathlib import Path
row = json.loads(Path("/tmp/fetch_${wid}.json").read_text())
print("yes" if row.get("complete") else "no")
PY
}

update_progress_manifest() {
  TMPDIR="$(mktemp -d)"
  aws s3 sync "s3://${BUCKET}/${PREFIX}" "$TMPDIR/" --exclude '*' --include 'worker_*.json' --only-show-errors
  python3 - <<PY
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

tmp = Path("${TMPDIR}")
rows = []
for f in sorted(tmp.glob("worker_*.json")):
    if "_meta.json" in f.name or "_burned.json" in f.name:
        continue
    rows.append(json.loads(f.read_text()))

uploaded = sum(int(r.get("uploaded", 0)) for r in rows)
skipped = sum(int(r.get("skipped_existing", 0)) for r in rows)
failed = sum(int(r.get("failed", 0)) for r in rows)
documents_done = uploaded + skipped
tickers_done = sum(int(r.get("tickers_done", 0)) for r in rows)
elapsed = max((float(r.get("elapsed_s", 0)) for r in rows), default=0)
docs_hr = int(3600 * documents_done / elapsed) if elapsed else 0
burned = sum(1 for r in rows if r.get("burned"))
rotations = sum(int(r.get("rotation", 0)) for r in rows)
complete_workers = sum(1 for r in rows if r.get("complete"))

total = int("${TOTAL_DOCS}")
status = "running"
if complete_workers >= int("${WORKERS}") and all(r.get("complete") for r in rows if len(rows) >= int("${WORKERS}")):
    status = "complete" if documents_done >= total * 0.99 else "running"
if documents_done >= total:
    status = "complete"

manifest_path = Path("/tmp/cfo_fetch_progress.json")
existing_raw = subprocess.run(
    ["aws", "s3", "cp", f"s3://${BUCKET}/manifests/cfo_fetch_progress.json", "-", "--no-cli-pager"],
    capture_output=True,
    text=True,
    env={**os.environ, "AWS_PAGER": ""},
).stdout
existing = {}
if existing_raw.strip():
    try:
        existing = json.loads(existing_raw)
    except json.JSONDecodeError:
        existing = {}
started_at = existing.get("started_at") or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

manifest = {
    "status": status,
    "started_at": started_at,
    "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "run_id": "${RUN_ID}",
    "workers": int("${WORKERS}"),
    "worker_results": len(rows),
    "complete_workers": complete_workers,
    "documents_uploaded": uploaded,
    "documents_done": documents_done,
    "documents_total": total,
    "skipped_existing": skipped,
    "errors": failed,
    "completed_tickers": tickers_done,
    "docs_hr": docs_hr,
    "burned_workers": burned,
    "total_rotations": rotations,
    "include_tier_b": existing.get("include_tier_b", False),
}
manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
print(json.dumps(manifest, indent=2))
PY
  aws s3 cp /tmp/cfo_fetch_progress.json "s3://${BUCKET}/manifests/cfo_fetch_progress.json"
  rm -rf "$TMPDIR"
}

maybe_rotate_burned() {
  local wid=$1
  local result="/tmp/fetch_${wid}.json"
  aws s3 cp "s3://${BUCKET}/${PREFIX}worker_${wid}.json" "$result" 2>/dev/null || return 0
  python3 - <<PY || return 0
import json
from pathlib import Path
row = json.loads(Path("${result}").read_text())
if not row.get("burned") or row.get("complete"):
    raise SystemExit(1)
PY

  local meta="/tmp/fetch_${wid}_meta.json"
  aws s3 cp "s3://${BUCKET}/${PREFIX}worker_${wid}_meta.json" "$meta" 2>/dev/null || return 0

  python3 - <<PY > "/tmp/fetch_rotate_${wid}.json"
import json
from pathlib import Path
result = json.loads(Path("${result}").read_text())
meta = json.loads(Path("${meta}").read_text())
rotation = int(meta.get("rotation", 0))
if rotation >= int("${MAX_ROTATIONS}"):
    raise SystemExit(0)
ticker_index = int(result.get("ticker_index", 0))
start_offset = int(result.get("start_offset", 0))
tickers_total = int(result.get("tickers_total", 0))
tickers_done = int(result.get("tickers_done", 0))
if tickers_done >= tickers_total and start_offset == 0:
    raise SystemExit(0)
print(json.dumps({
    "old_instance_id": meta.get("instance_id"),
    "ticker_index": ticker_index,
    "start_offset": start_offset,
    "next_rotation": rotation + 1,
}))
PY

  if [[ ! -s "/tmp/fetch_rotate_${wid}.json" ]]; then
    return 0
  fi

  local old_id ticker_index start_offset next_rotation
  old_id="$(python3 -c "import json; print(json.load(open('/tmp/fetch_rotate_${wid}.json'))['old_instance_id'])")"
  ticker_index="$(python3 -c "import json; print(json.load(open('/tmp/fetch_rotate_${wid}.json'))['ticker_index'])")"
  start_offset="$(python3 -c "import json; print(json.load(open('/tmp/fetch_rotate_${wid}.json'))['start_offset'])")"
  next_rotation="$(python3 -c "import json; print(json.load(open('/tmp/fetch_rotate_${wid}.json'))['next_rotation'])")"

  echo "  rotate worker_${wid}: resume ticker_index=${ticker_index} offset=${start_offset} rotation=${next_rotation}"
  aws s3 cp "$result" "s3://${BUCKET}/${PREFIX}worker_${wid}_burned.json" || true
  NEW_IID="$("$LAUNCH" "$RUN_ID" "$((10#$wid))" "$ticker_index" "$start_offset" "$next_rotation")"
  if [[ -z "$NEW_IID" || "$NEW_IID" == "None" ]]; then
    echo "  ERROR: failed to launch replacement worker for ${wid}"
    return 1
  fi
  echo "  replacement worker_${wid}: ${NEW_IID}"
  if [[ -n "$old_id" && "$old_id" != "None" ]]; then
    aws ec2 terminate-instances --instance-ids "$old_id" >/dev/null
  fi
  aws s3 rm "s3://${BUCKET}/${PREFIX}worker_${wid}.json" || true
}

while [[ $(date +%s) -lt $DEADLINE ]]; do
  done=0
  for w in $(seq 0 $((WORKERS - 1))); do
    wid="$(printf '%02d' "$w")"
    maybe_rotate_burned "$wid" || true
    if worker_done "$wid"; then
      done=$((done + 1))
    fi
  done
  update_progress_manifest || true
  echo "  complete: ${done}/${WORKERS}"
  if [[ "$done" -ge "$WORKERS" ]]; then
    break
  fi
  sleep "$POLL_S"
done

TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT
aws s3 sync "s3://${BUCKET}/${PREFIX}" "$TMPDIR/" --exclude '*' --include 'worker_*.json'

SUMMARY="$(python3 - <<PY
import json
from pathlib import Path

tmp = Path("${TMPDIR}")
rows = []
for f in sorted(tmp.glob("worker_*.json")):
    if "_meta.json" in f.name or "_burned.json" in f.name:
        continue
    rows.append(json.loads(f.read_text()))

burned_archives = list(tmp.glob("worker_*_burned.json"))
uploaded = sum(int(r.get("uploaded", 0)) for r in rows)
skipped = sum(int(r.get("skipped_existing", 0)) for r in rows)
failed = sum(int(r.get("failed", 0)) for r in rows)
burned = sum(1 for r in rows if r.get("burned")) + len(burned_archives)
rotations = sum(int(r.get("rotation", 0)) for r in rows)
tickers_done = sum(int(r.get("tickers_done", 0)) for r in rows)

passed = (
    len(rows) >= int("${WORKERS}")
    and all(r.get("complete") for r in rows)
    and failed == 0
)

summary = {
    "run_id": "${RUN_ID}",
    "workers": int("${WORKERS}"),
    "worker_results": len(rows),
    "uploaded": uploaded,
    "skipped_existing": skipped,
    "failed": failed,
    "burned_events": burned,
    "total_rotations": rotations,
    "tickers_done": tickers_done,
    "include_tier_b": bool(int("${GYPSY_CFO_INCLUDE_TIER_B:-0}")),
    "documents_total": int("${TOTAL_DOCS}"),
    "passed": passed,
    "s3_pdf_prefix": f"s3://${BUCKET}/entities/",
    "s3_path_pattern": "entities/{{TICKER}}/cfo_changes/{{YYYY-MM-DD}}_{{documentKey}}.pdf",
    "s3_logs_prefix": f"s3://${BUCKET}/${PREFIX}",
}
print(json.dumps(summary, indent=2))
Path("/tmp/cfo-fetch-summary.json").write_text(json.dumps(summary, indent=2) + "\n")
PY
)"

echo "$SUMMARY"
aws s3 cp /tmp/cfo-fetch-summary.json "s3://${BUCKET}/manifests/cfo_fetch/${RUN_ID}/summary.json"
update_progress_manifest || true

MSG="$(python3 - <<PY
import json
from pathlib import Path
s = json.loads(Path("/tmp/cfo-fetch-summary.json").read_text())
verdict = "PASS" if s["passed"] else "DONE"
lines = [
    f"Gypsy Danger CFO change fetch — {verdict}",
    "",
    f"Run ID:            {s['run_id']}",
    f"Workers:           {s['workers']} (results: {s['worker_results']})",
    f"PDFs uploaded:     {s['uploaded']:,}",
    f"Skipped (exists):  {s['skipped_existing']:,}",
    f"Failed:            {s['failed']:,}",
    f"Burn events:       {s['burned_events']} (rotations: {s['total_rotations']})",
    f"Tier B included:   {s['include_tier_b']} (~{s['documents_total']:,} target)",
    "",
    f"S3 PDFs:   {s['s3_pdf_prefix']} (cfo_changes/)",
    f"S3 logs:   {s['s3_logs_prefix']}",
    f"Summary:   s3://${BUCKET}/manifests/cfo_fetch/{s['run_id']}/summary.json",
    f"Progress:  s3://${BUCKET}/manifests/cfo_fetch_progress.json",
]
print("\\n".join(lines))
PY
)"

if [[ -n "${GYPSY_SNS_TOPIC_ARN:-}" ]]; then
  VERDICT="$(python3 -c "import json; print('PASS' if json.load(open('/tmp/cfo-fetch-summary.json'))['passed'] else 'DONE')")"
  aws sns publish \
    --topic-arn "$GYPSY_SNS_TOPIC_ARN" \
    --subject "Gypsy Danger CFO change fetch — ${VERDICT}" \
    --message "$MSG"
  echo "SNS notification sent"
else
  echo "$MSG"
fi

IDS="$(aws ec2 describe-instances \
  --filters "Name=tag:Project,Values=gypsy-danger" "Name=tag:CfoFetchRunId,Values=${RUN_ID}" \
    "Name=instance-state-name,Values=pending,running,stopping,stopped" \
  --query 'Reservations[].Instances[].InstanceId' --output text)"
if [[ -n "$IDS" && "$IDS" != "None" ]]; then
  echo "Terminating CFO fetch instances: $IDS"
  aws ec2 terminate-instances --instance-ids $IDS
fi
