#!/usr/bin/env bash
# Poll LiteParse worker results, resume failed workers, update progress manifest, email on completion.
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
MAX_ROTATIONS="${GYPSY_PARSE_MAX_ROTATIONS:-3}"
TOTAL_DOCS="${GYPSY_PARSE_TOTAL_DOCS:-22573}"
PREFIX="logs/parse/${RUN_ID}/"
LAUNCH="${SCRIPT_DIR}/launch_liteparse_worker.sh"
if [[ ! -x "$LAUNCH" ]]; then
  ROOT="/opt/gypsy-danger"
  mkdir -p "$ROOT"
  aws s3 cp "s3://${BUCKET}/scripts/launch_liteparse_worker.sh" "$LAUNCH"
  chmod +x "$LAUNCH"
fi

WAIT_MAX_S="${GYPSY_PARSE_WAIT_MAX_S:-172800}"
DEADLINE=$(( $(date +%s) + WAIT_MAX_S ))
POLL_S="${GYPSY_PARSE_POLL_S:-120}"
echo "LiteParse waiter: run=${RUN_ID} workers=${WORKERS} prefix=s3://${BUCKET}/${PREFIX} max_wait=${WAIT_MAX_S}s"

worker_done() {
  local wid=$1
  aws s3 cp "s3://${BUCKET}/${PREFIX}worker_${wid}.json" "/tmp/parse_${wid}.json" 2>/dev/null || return 1
  python3 - <<PY
import json
import sys
from pathlib import Path
row = json.loads(Path("/tmp/parse_${wid}.json").read_text())
sys.exit(0 if row.get("complete") else 1)
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
    if "_meta.json" in f.name or "_failed.json" in f.name:
        continue
    rows.append(json.loads(f.read_text()))

parsed = sum(int(r.get("parsed", 0)) for r in rows)
skipped = sum(int(r.get("skipped_existing", 0)) for r in rows)
failed = sum(int(r.get("failed", 0)) for r in rows)
documents_done = sum(int(r.get("documents_done", 0)) for r in rows)
pages = sum(int(r.get("pages_parsed", 0)) for r in rows)
tickers_done = sum(int(r.get("tickers_done", 0)) for r in rows)
elapsed = max((float(r.get("elapsed_s", 0)) for r in rows), default=0)
docs_hr = int(3600 * documents_done / elapsed) if elapsed else 0
rotations = sum(int(r.get("rotation", 0)) for r in rows)
complete_workers = sum(1 for r in rows if r.get("complete"))

total = int("${TOTAL_DOCS}")
status = "running"
if complete_workers >= int("${WORKERS}") and all(r.get("complete") for r in rows if len(rows) >= int("${WORKERS}")):
    status = "complete" if documents_done >= total * 0.99 else "running"
if documents_done >= total:
    status = "complete"

manifest_path = Path("/tmp/parse_progress.json")
existing_raw = subprocess.run(
    ["aws", "s3", "cp", f"s3://${BUCKET}/manifests/parse_progress.json", "-", "--no-cli-pager"],
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
    "phase": "3a_liteparse",
    "corpus_key": "annual_reports",
    "started_at": started_at,
    "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "run_id": "${RUN_ID}",
    "workers": int("${WORKERS}"),
    "worker_results": len(rows),
    "complete_workers": complete_workers,
    "documents_parsed": parsed,
    "skipped_existing": skipped,
    "documents_done": documents_done,
    "documents_total": total,
    "pages_parsed": pages,
    "errors": failed,
    "completed_tickers": tickers_done,
    "docs_hr": docs_hr,
    "total_rotations": rotations,
}
manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
print(json.dumps(manifest, indent=2))
PY
  aws s3 cp /tmp/parse_progress.json "s3://${BUCKET}/manifests/parse_progress.json"
  rm -rf "$TMPDIR"
}

maybe_resume_failed() {
  local wid=$1
  local result="/tmp/parse_${wid}.json"
  aws s3 cp "s3://${BUCKET}/${PREFIX}worker_${wid}.json" "$result" 2>/dev/null || return 0
  python3 - <<PY || return 0
import json
from pathlib import Path
row = json.loads(Path("${result}").read_text())
if row.get("complete"):
    raise SystemExit(1)
PY

  local meta="/tmp/parse_${wid}_meta.json"
  aws s3 cp "s3://${BUCKET}/${PREFIX}worker_${wid}_meta.json" "$meta" 2>/dev/null || return 0

  local instance_id
  instance_id="$(python3 -c "import json; print(json.load(open('${meta}')).get('instance_id',''))")"
  if [[ -n "$instance_id" && "$instance_id" != "None" ]]; then
    state="$(aws ec2 describe-instances --instance-ids "$instance_id" \
      --query 'Reservations[0].Instances[0].State.Name' --output text 2>/dev/null || echo unknown)"
    if [[ "$state" == "running" || "$state" == "pending" ]]; then
      return 0
    fi
  fi

  python3 - <<PY > "/tmp/parse_resume_${wid}.json"
import json
from pathlib import Path
result = json.loads(Path("${result}").read_text())
meta = json.loads(Path("${meta}").read_text())
rotation = int(meta.get("rotation", 0))
if rotation >= int("${MAX_ROTATIONS}"):
    raise SystemExit(0)
ticker_index = int(result.get("ticker_index", 0))
doc_offset = int(result.get("doc_offset", 0))
if result.get("complete"):
    raise SystemExit(0)
print(json.dumps({
    "old_instance_id": meta.get("instance_id"),
    "ticker_index": ticker_index,
    "doc_offset": doc_offset,
    "next_rotation": rotation + 1,
}))
PY

  if [[ ! -s "/tmp/parse_resume_${wid}.json" ]]; then
    return 0
  fi

  local old_id ticker_index doc_offset next_rotation
  old_id="$(python3 -c "import json; print(json.load(open('/tmp/parse_resume_${wid}.json'))['old_instance_id'])")"
  ticker_index="$(python3 -c "import json; print(json.load(open('/tmp/parse_resume_${wid}.json'))['ticker_index'])")"
  doc_offset="$(python3 -c "import json; print(json.load(open('/tmp/parse_resume_${wid}.json'))['doc_offset'])")"
  next_rotation="$(python3 -c "import json; print(json.load(open('/tmp/parse_resume_${wid}.json'))['next_rotation'])")"

  echo "  resume worker_${wid}: ticker_index=${ticker_index} doc_offset=${doc_offset} rotation=${next_rotation}"
  aws s3 cp "$result" "s3://${BUCKET}/${PREFIX}worker_${wid}_failed.json" || true
  NEW_IID="$("$LAUNCH" "$RUN_ID" "$((10#$wid))" "$ticker_index" "$doc_offset" "$next_rotation")"
  if [[ -z "$NEW_IID" || "$NEW_IID" == "None" ]]; then
    echo "  ERROR: failed to launch replacement worker for ${wid}"
    return 1
  fi
  echo "  replacement worker_${wid}: ${NEW_IID}"
  if [[ -n "$old_id" && "$old_id" != "None" ]]; then
    aws ec2 terminate-instances --instance-ids "$old_id" >/dev/null 2>&1 || true
  fi
  aws s3 rm "s3://${BUCKET}/${PREFIX}worker_${wid}.json" || true
}

while [[ $(date +%s) -lt $DEADLINE ]]; do
  done=0
  for w in $(seq 0 $((WORKERS - 1))); do
    wid="$(printf '%02d' "$w")"
    maybe_resume_failed "$wid" || true
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
    if "_meta.json" in f.name or "_failed.json" in f.name:
        continue
    rows.append(json.loads(f.read_text()))

failed_archives = list(tmp.glob("worker_*_failed.json"))
parsed = sum(int(r.get("parsed", 0)) for r in rows)
skipped = sum(int(r.get("skipped_existing", 0)) for r in rows)
failed = sum(int(r.get("failed", 0)) for r in rows)
pages = sum(int(r.get("pages_parsed", 0)) for r in rows)
rotations = sum(int(r.get("rotation", 0)) for r in rows)
tickers_done = sum(int(r.get("tickers_done", 0)) for r in rows)

passed = (
    len(rows) >= int("${WORKERS}")
    and all(r.get("complete") for r in rows)
    and failed == 0
)

summary = {
    "run_id": "${RUN_ID}",
    "phase": "3a_liteparse",
    "corpus_key": "annual_reports",
    "workers": int("${WORKERS}"),
    "worker_results": len(rows),
    "documents_parsed": parsed,
    "skipped_existing": skipped,
    "failed": failed,
    "pages_parsed": pages,
    "resume_events": len(failed_archives),
    "total_rotations": rotations,
    "tickers_done": tickers_done,
    "documents_total": int("${TOTAL_DOCS}"),
    "passed": passed,
    "s3_parsed_prefix": f"s3://${BUCKET}/parsed/",
    "s3_logs_prefix": f"s3://${BUCKET}/${PREFIX}",
}
print(json.dumps(summary, indent=2))
Path("/tmp/parse-summary.json").write_text(json.dumps(summary, indent=2) + "\n")
PY
)"

echo "$SUMMARY"
aws s3 cp /tmp/parse-summary.json "s3://${BUCKET}/manifests/parse/${RUN_ID}/summary.json"
update_progress_manifest || true

MSG="$(python3 - <<PY
import json
from pathlib import Path
s = json.loads(Path("/tmp/parse-summary.json").read_text())
verdict = "PASS" if s["passed"] else "DONE"
lines = [
    f"Gypsy Danger Stage 3A LiteParse — {verdict}",
    "",
    f"Run ID:            {s['run_id']}",
    f"Workers:           {s['workers']} (results: {s['worker_results']})",
    f"Documents parsed:  {s['documents_parsed']:,}",
    f"Skipped (exists):  {s['skipped_existing']:,}",
    f"Failed:            {s['failed']:,}",
    f"Pages parsed:      {s['pages_parsed']:,}",
    f"Resume events:     {s['resume_events']} (rotations: {s['total_rotations']})",
    f"Corpus:            {s['corpus_key']} (~{s['documents_total']:,} annual reports)",
    "",
    f"S3 parsed: s3://${BUCKET}/parsed/",
    f"S3 logs:   {s['s3_logs_prefix']}",
    f"Summary:   s3://${BUCKET}/manifests/parse/{s['run_id']}/summary.json",
    f"Progress:  s3://${BUCKET}/manifests/parse_progress.json",
    "",
    "On-demand status: 0-work/scripts/aws/request_parse_progress.sh",
]
print("\\n".join(lines))
PY
)"

if [[ -n "${GYPSY_SNS_TOPIC_ARN:-}" ]]; then
  VERDICT="$(python3 -c "import json; print('PASS' if json.load(open('/tmp/parse-summary.json'))['passed'] else 'DONE')")"
  aws sns publish \
    --topic-arn "$GYPSY_SNS_TOPIC_ARN" \
    --subject "Gypsy Danger Stage 3A LiteParse — ${VERDICT}" \
    --message "$MSG"
  echo "SNS notification sent"
else
  echo "$MSG"
fi

IDS="$(aws ec2 describe-instances \
  --filters "Name=tag:Project,Values=gypsy-danger" "Name=tag:ParseRunId,Values=${RUN_ID}" \
    "Name=instance-state-name,Values=pending,running,stopping,stopped" \
  --query 'Reservations[].Instances[].InstanceId' --output text)"
if [[ -n "$IDS" && "$IDS" != "None" ]]; then
  echo "Terminating parse instances: $IDS"
  aws ec2 terminate-instances --instance-ids $IDS
fi
