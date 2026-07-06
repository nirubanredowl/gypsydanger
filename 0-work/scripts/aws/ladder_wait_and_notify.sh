#!/usr/bin/env bash
# Poll S3 for ladder worker results, rotate burned EC2 IPs, aggregate, email, terminate.
# Runs on gypsy-danger-soak-01 (or any instance with EC2 + S3 + SNS IAM access).
set -euo pipefail

RUNG="${1:?rung number required}"
WORKERS="${2:?worker count required}"
RUN_ID="${3:-}"

source /etc/profile.d/gypsy-danger.sh
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-ap-southeast-2}"

BUCKET="${GYPSY_S3_BUCKET:?GYPSY_S3_BUCKET required}"
MAX_ROTATIONS="${GYPSY_BURN_MAX_ROTATIONS:-3}"
KEYS_PER_WORKER=$(( 2000 / WORKERS ))
PREFIX="logs/ladder/rung${RUNG}/${RUN_ID}/"
BASELINE_DOCS_HR="${GYPSY_LADDER_BASELINE_DOCS_HR:-1271}"
ROOT="/opt/gypsy-danger"
LAUNCH="${ROOT}/launch_ladder_worker.sh"

mkdir -p "$ROOT"
aws s3 cp "s3://${BUCKET}/scripts/launch_ladder_worker.sh" "$LAUNCH"
chmod +x "$LAUNCH"

WAIT_MAX_S="$(python3 - <<PY
import math
k = int("${KEYS_PER_WORKER}")
print(int(math.ceil(k * 8.0 * (1 + int("${MAX_ROTATIONS}")) + 900)))
PY
)"
DEADLINE=$(( $(date +%s) + WAIT_MAX_S ))

echo "Waiter: rung=${RUNG} run=${RUN_ID} workers=${WORKERS} prefix=s3://${BUCKET}/${PREFIX} max_wait=${WAIT_MAX_S}s max_rotations=${MAX_ROTATIONS}"

worker_done() {
  local wid=$1
  aws s3 cp "s3://${BUCKET}/${PREFIX}worker_${wid}.json" "/tmp/worker_${wid}.json" 2>/dev/null || return 1
  python3 - <<PY
import json
from pathlib import Path
row = json.loads(Path("/tmp/worker_${wid}.json").read_text())
print("yes" if row.get("complete") else "no")
PY
}

maybe_rotate_burned() {
  local wid=$1
  local result="/tmp/worker_${wid}.json"
  aws s3 cp "s3://${BUCKET}/${PREFIX}worker_${wid}.json" "$result" 2>/dev/null || return 0

  python3 - <<PY || return 0
import json
from pathlib import Path
row = json.loads(Path("${result}").read_text())
if not row.get("burned") or row.get("complete"):
    raise SystemExit(1)
PY

  local meta="/tmp/worker_${wid}_meta.json"
  aws s3 cp "s3://${BUCKET}/${PREFIX}worker_${wid}_meta.json" "$meta" 2>/dev/null || return 0

  python3 - <<PY > "/tmp/rotate_${wid}.json"
import json
from pathlib import Path
result = json.loads(Path("${result}").read_text())
meta = json.loads(Path("${meta}").read_text())
rotation = int(meta.get("rotation", 0))
if rotation >= int("${MAX_ROTATIONS}"):
    raise SystemExit(0)
keys_per_worker = int("${KEYS_PER_WORKER}")
absolute = int(result.get("absolute_offset", 0))
remaining = max(keys_per_worker - absolute, 0)
if remaining <= 0:
    raise SystemExit(0)
print(json.dumps({
    "old_instance_id": meta.get("instance_id"),
    "worker_id": int("${wid}"),
    "next_offset": absolute,
    "next_rotation": rotation + 1,
    "remaining": remaining,
}))
PY

  if [[ ! -s "/tmp/rotate_${wid}.json" ]]; then
    return 0
  fi

  local old_id next_offset next_rotation remaining worker_id
  old_id="$(python3 -c "import json; print(json.load(open('/tmp/rotate_${wid}.json'))['old_instance_id'])")"
  next_offset="$(python3 -c "import json; print(json.load(open('/tmp/rotate_${wid}.json'))['next_offset'])")"
  next_rotation="$(python3 -c "import json; print(json.load(open('/tmp/rotate_${wid}.json'))['next_rotation'])")"
  remaining="$(python3 -c "import json; print(json.load(open('/tmp/rotate_${wid}.json'))['remaining'])")"
  worker_id="$(python3 -c "import json; print(json.load(open('/tmp/rotate_${wid}.json'))['worker_id'])")"

  echo "  rotate worker_${wid}: terminate ${old_id}, relaunch offset=${next_offset} rotation=${next_rotation} remaining=${remaining}"
  if [[ -n "$old_id" && "$old_id" != "None" ]]; then
    aws ec2 terminate-instances --instance-ids "$old_id" >/dev/null || true
  fi
  aws s3 rm "s3://${BUCKET}/${PREFIX}worker_${wid}.json" || true
  "$LAUNCH" "$RUNG" "$RUN_ID" "$((10#$worker_id))" "$next_offset" "$next_rotation" "$remaining" >"/tmp/new_${wid}.id"
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
  echo "  complete: ${done}/${WORKERS}"
  if [[ "$done" -ge "$WORKERS" ]]; then
    break
  fi
  sleep 30
done

TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT

aws s3 sync "s3://${BUCKET}/${PREFIX}" "$TMPDIR/" --exclude '*' --include 'worker_*.json'

AGG="$(python3 - <<PY
import json
from pathlib import Path

tmp = Path("${TMPDIR}")
workers = int("${WORKERS}")
baseline = float("${BASELINE_DOCS_HR}")
files = sorted(tmp.glob("worker_*.json"))
rows = []
for f in files:
    if f.name.endswith("_meta.json"):
        continue
    rows.append(json.loads(f.read_text()))
total_success = sum(r.get("success", 0) for r in rows)
total_req = sum(r.get("requests", 0) for r in rows)
total_err = sum(r.get("429", 0) + r.get("503", 0) + r.get("other_errors", 0) for r in rows)
burned_workers = sum(1 for r in rows if r.get("burned"))
rotations = sum(int(r.get("rotation", 0)) for r in rows)
max_elapsed = max((r.get("elapsed_s", 0) for r in rows), default=0)
agg_docs_hr = int(3600 * total_success / max_elapsed) if max_elapsed else 0
linear_target = int(baseline * workers * 0.8)
err_pct = (100.0 * total_err / total_req) if total_req else 0
passed = agg_docs_hr >= linear_target and err_pct <= 1.0
print(json.dumps({
    "rung": int("${RUNG}"),
    "run_id": "${RUN_ID}" or None,
    "workers": workers,
    "worker_results": len(rows),
    "total_requests": total_req,
    "total_success": total_success,
    "total_errors": total_err,
    "error_pct": round(err_pct, 2),
    "burned_workers": burned_workers,
    "total_rotations": rotations,
    "max_worker_elapsed_s": max_elapsed,
    "aggregate_docs_hr": agg_docs_hr,
    "linear_target_docs_hr": linear_target,
    "baseline_per_worker_docs_hr": int(baseline),
    "passed": passed,
}, indent=2))
PY
)"

echo "$AGG" | tee /tmp/ladder-rung${RUNG}-summary.json
aws s3 cp /tmp/ladder-rung${RUNG}-summary.json "s3://${BUCKET}/${PREFIX}summary.json"

MSG="$(python3 - <<PY
import json
from pathlib import Path
s = json.loads(Path("/tmp/ladder-rung${RUNG}-summary.json").read_text())
verdict = "PASS" if s["passed"] else "FAIL / plateau"
lines = [
    f"Gypsy Danger ladder rung {s['rung']} complete — {verdict}",
    "",
    f"Workers:           {s['workers']} (results: {s['worker_results']})",
    f"Total requests:    {s['total_requests']}",
    f"Total success:     {s['total_success']}",
    f"Errors:            {s['total_errors']} ({s['error_pct']}%)",
    f"Burned workers:    {s['burned_workers']} (rotations: {s['total_rotations']})",
    f"Aggregate docs/hr: {s['aggregate_docs_hr']}",
    f"Linear target:     {s['linear_target_docs_hr']} (80% of {s['workers']}×{s['baseline_per_worker_docs_hr']})",
    "",
    f"S3: s3://${BUCKET}/${PREFIX}summary.json",
]
print("\\n".join(lines))
PY
)"

if [[ -n "${GYPSY_SNS_TOPIC_ARN:-}" ]]; then
  LOCK_KEY="${PREFIX}.notify_sent"
  if aws s3api head-object --bucket "$BUCKET" --key "$LOCK_KEY" >/dev/null 2>&1; then
    echo "SNS already sent for ${PREFIX} — skipping duplicate notification"
  else
    printf notify > "/tmp/ladder-notify-$$.lock"
    if aws s3api put-object \
        --bucket "$BUCKET" \
        --key "$LOCK_KEY" \
        --body "/tmp/ladder-notify-$$.lock" \
        --if-none-match '*' \
        --no-cli-pager >/dev/null 2>&1; then
      VERDICT="$(python3 -c "import json; print('PASS' if json.load(open('/tmp/ladder-rung${RUNG}-summary.json'))['passed'] else 'DONE')")"
      aws sns publish \
        --topic-arn "$GYPSY_SNS_TOPIC_ARN" \
        --subject "Gypsy Danger ladder rung ${RUNG} — ${VERDICT}" \
        --message "$MSG"
      echo "SNS notification sent"
    else
      echo "Another waiter already sent notification for ${PREFIX}"
    fi
  fi
else
  echo "$MSG"
fi

IDS="$(aws ec2 describe-instances \
  --filters "Name=tag:Project,Values=gypsy-danger" "Name=tag:LadderRunId,Values=${RUN_ID}" \
    "Name=instance-state-name,Values=pending,running,stopping,stopped" \
  --query 'Reservations[].Instances[].InstanceId' --output text)"
if [[ -n "$IDS" && "$IDS" != "None" ]]; then
  echo "Terminating ladder instances: $IDS"
  aws ec2 terminate-instances --instance-ids $IDS
fi
