#!/usr/bin/env bash
# Poll S3 for ladder worker results, aggregate, email summary, terminate workers.
# Runs on gypsy-danger-soak-01 (or any instance with S3 + SNS IAM access).
set -euo pipefail

RUNG="${1:?rung number required}"
WORKERS="${2:?worker count required}"
RUN_ID="${3:-}"

source /etc/profile.d/gypsy-danger.sh
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-ap-southeast-2}"

BUCKET="${GYPSY_S3_BUCKET:?GYPSY_S3_BUCKET required}"
if [[ -n "$RUN_ID" ]]; then
  PREFIX="logs/ladder/rung${RUNG}/${RUN_ID}/"
else
  PREFIX="logs/ladder/rung${RUNG}/"
fi
BASELINE_DOCS_HR="${GYPSY_LADDER_BASELINE_DOCS_HR:-1271}"

# ~keys/worker × (1s rate + 7s download) + buffer
KEYS_PER_WORKER=$(( 2000 / WORKERS ))
WAIT_MAX_S="$(python3 - <<PY
import math
w = int("${WORKERS}")
k = int("${KEYS_PER_WORKER}")
print(int(math.ceil(k * 8.0 + 600)))
PY
)"
DEADLINE=$(( $(date +%s) + WAIT_MAX_S ))

echo "Waiter: rung=${RUNG} run=${RUN_ID:-legacy} workers=${WORKERS} prefix=s3://${BUCKET}/${PREFIX} max_wait=${WAIT_MAX_S}s"

while [[ $(date +%s) -lt $DEADLINE ]]; do
  FOUND="$(aws s3 ls "s3://${BUCKET}/${PREFIX}" 2>/dev/null | grep -c 'worker_.*\.json' || true)"
  echo "  results: ${FOUND}/${WORKERS}"
  if [[ "$FOUND" -ge "$WORKERS" ]]; then
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
    rows.append(json.loads(f.read_text()))
total_success = sum(r.get("success", 0) for r in rows)
total_req = sum(r.get("requests", 0) for r in rows)
total_err = sum(r.get("429", 0) + r.get("503", 0) + r.get("other_errors", 0) for r in rows)
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

# Terminate ladder workers for this run
if [[ -n "$RUN_ID" ]]; then
  FILTER_RUN="Name=tag:LadderRunId,Values=${RUN_ID}"
else
  FILTER_RUN="Name=tag:LadderRung,Values=${RUNG}"
fi
IDS="$(aws ec2 describe-instances \
  --filters "Name=tag:Project,Values=gypsy-danger" "$FILTER_RUN" \
    "Name=instance-state-name,Values=pending,running,stopping,stopped" \
  --query 'Reservations[].Instances[].InstanceId' --output text)"
if [[ -n "$IDS" && "$IDS" != "None" ]]; then
  echo "Terminating ladder instances: $IDS"
  aws ec2 terminate-instances --instance-ids $IDS
fi
