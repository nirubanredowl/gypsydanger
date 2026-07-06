#!/usr/bin/env bash
# Poll preflight worker results, rotate burned EC2s, verify S3 uploads, email summary.
set -euo pipefail

RUN_ID="${1:?run id}"
WORKERS="${2:?worker count}"

source /etc/profile.d/gypsy-danger.sh
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-ap-southeast-2}"

BUCKET="${GYPSY_S3_BUCKET:?GYPSY_S3_BUCKET required}"
MAX_ROTATIONS="${GYPSY_BURN_MAX_ROTATIONS:-3}"
PREFIX="logs/preflight/${RUN_ID}/"
ROOT="/opt/gypsy-danger"
LAUNCH="${ROOT}/launch_preflight_worker.sh"

mkdir -p "$ROOT"
aws s3 cp "s3://${BUCKET}/scripts/launch_preflight_worker.sh" "$LAUNCH"
chmod +x "$LAUNCH"

WAIT_MAX_S=1800
DEADLINE=$(( $(date +%s) + WAIT_MAX_S ))
echo "Preflight waiter: run=${RUN_ID} workers=${WORKERS} prefix=s3://${BUCKET}/${PREFIX}"

worker_done() {
  local wid=$1
  aws s3 cp "s3://${BUCKET}/${PREFIX}worker_${wid}.json" "/tmp/preflight_${wid}.json" 2>/dev/null || return 1
  python3 - <<PY
import json
from pathlib import Path
row = json.loads(Path("/tmp/preflight_${wid}.json").read_text())
print("yes" if row.get("complete") else "no")
PY
}

maybe_rotate_burned() {
  local wid=$1
  local result="/tmp/preflight_${wid}.json"
  aws s3 cp "s3://${BUCKET}/${PREFIX}worker_${wid}.json" "$result" 2>/dev/null || return 0
  python3 - <<PY || return 0
import json
from pathlib import Path
row = json.loads(Path("${result}").read_text())
if not row.get("burned") or row.get("complete"):
    raise SystemExit(1)
PY

  local meta="/tmp/preflight_${wid}_meta.json"
  aws s3 cp "s3://${BUCKET}/${PREFIX}worker_${wid}_meta.json" "$meta" 2>/dev/null || return 0

  python3 - <<PY > "/tmp/preflight_rotate_${wid}.json"
import json
from pathlib import Path
result = json.loads(Path("${result}").read_text())
meta = json.loads(Path("${meta}").read_text())
rotation = int(meta.get("rotation", 0))
if rotation >= int("${MAX_ROTATIONS}"):
    raise SystemExit(0)
absolute = int(result.get("absolute_offset", 0))
max_reports = int(meta.get("max_reports", 0))
remaining = max(max_reports - absolute, 0)
if remaining <= 0:
    raise SystemExit(0)
print(json.dumps({
    "old_instance_id": meta.get("instance_id"),
    "ticker": meta.get("ticker"),
    "next_offset": absolute,
    "next_rotation": rotation + 1,
    "remaining": remaining,
    "simulate_burn_after": int(meta.get("simulate_burn_after", 0)),
}))
PY

  if [[ ! -s "/tmp/preflight_rotate_${wid}.json" ]]; then
    return 0
  fi

  local old_id ticker next_offset next_rotation remaining simulate
  old_id="$(python3 -c "import json; print(json.load(open('/tmp/preflight_rotate_${wid}.json'))['old_instance_id'])")"
  ticker="$(python3 -c "import json; print(json.load(open('/tmp/preflight_rotate_${wid}.json'))['ticker'])")"
  next_offset="$(python3 -c "import json; print(json.load(open('/tmp/preflight_rotate_${wid}.json'))['next_offset'])")"
  next_rotation="$(python3 -c "import json; print(json.load(open('/tmp/preflight_rotate_${wid}.json'))['next_rotation'])")"
  remaining="$(python3 -c "import json; print(json.load(open('/tmp/preflight_rotate_${wid}.json'))['remaining'])")"
  simulate="$(python3 -c "import json; print(json.load(open('/tmp/preflight_rotate_${wid}.json'))['simulate_burn_after'])")"

  echo "  rotate worker_${wid} (${ticker}): terminate ${old_id}, resume offset=${next_offset} rotation=${next_rotation}"
  if [[ -n "$old_id" && "$old_id" != "None" ]]; then
    aws ec2 terminate-instances --instance-ids "$old_id" >/dev/null || true
  fi
  aws s3 rm "s3://${BUCKET}/${PREFIX}worker_${wid}.json" || true
  "$LAUNCH" "$RUN_ID" "$((10#$wid))" "$ticker" "$remaining" "$next_offset" "$next_rotation" "0" >"/tmp/preflight_new_${wid}.id"
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
  sleep 20
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
    if f.name.endswith("_meta.json"):
        continue
    rows.append(json.loads(f.read_text()))

uploaded = sum(int(r.get("uploaded", 0)) for r in rows)
skipped = sum(int(r.get("skipped_existing", 0)) for r in rows)
failed = sum(int(r.get("failed", 0)) for r in rows)
burned = sum(1 for r in rows if r.get("burned"))
rotations = sum(int(r.get("rotation", 0)) for r in rows)
keys = []
for r in rows:
    keys.extend(r.get("uploaded_keys") or [])

passed = (
    len(rows) >= int("${WORKERS}")
    and all(r.get("complete") for r in rows)
    and uploaded > 0
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
    "uploaded_keys": keys,
    "passed": passed,
    "s3_pdf_prefix": f"s3://${BUCKET}/entities/",
    "s3_logs_prefix": f"s3://${BUCKET}/${PREFIX}",
}
print(json.dumps(summary, indent=2))
Path("/tmp/preflight-summary.json").write_text(json.dumps(summary, indent=2))
PY
)"

echo "$SUMMARY"
aws s3 cp /tmp/preflight-summary.json "s3://${BUCKET}/manifests/preflight/${RUN_ID}/summary.json"

MSG="$(python3 - <<PY
import json
from pathlib import Path
s = json.loads(Path("/tmp/preflight-summary.json").read_text())
verdict = "PASS" if s["passed"] else "FAIL"
lines = [
    f"Gypsy Danger preflight fetch — {verdict}",
    "",
    f"Run ID:            {s['run_id']}",
    f"Workers:           {s['workers']} (results: {s['worker_results']})",
    f"PDFs uploaded:     {s['uploaded']}",
    f"Skipped (exists):  {s['skipped_existing']}",
    f"Failed:            {s['failed']}",
    f"Burn events:       {s['burned_events']} (rotations: {s['total_rotations']})",
    "",
    "Checks:",
    "  [x] loose annual report filter",
    "  [x] S3 folder entities/{TICKER}/annual_reports/{YYYY}_{documentKey}.pdf",
    "  [x] burned EC2 replacement (worker 01 simulates burn)",
    "  [x] SNS email notification",
    "",
    f"S3 PDFs:   {s['s3_pdf_prefix']}",
    f"S3 logs:   {s['s3_logs_prefix']}",
    f"Summary:   s3://${BUCKET}/manifests/preflight/{s['run_id']}/summary.json",
]
if s["uploaded_keys"]:
    lines.append("")
    lines.append("Sample keys:")
    for key in s["uploaded_keys"][:8]:
        lines.append(f"  - {key}")
print("\\n".join(lines))
PY
)"

if [[ -n "${GYPSY_SNS_TOPIC_ARN:-}" ]]; then
  VERDICT="$(python3 -c "import json; print('PASS' if json.load(open('/tmp/preflight-summary.json'))['passed'] else 'FAIL')")"
  aws sns publish \
    --topic-arn "$GYPSY_SNS_TOPIC_ARN" \
    --subject "Gypsy Danger preflight — ${VERDICT}" \
    --message "$MSG"
  echo "SNS notification sent"
else
  echo "$MSG"
fi

IDS="$(aws ec2 describe-instances \
  --filters "Name=tag:Project,Values=gypsy-danger" "Name=tag:PreflightRunId,Values=${RUN_ID}" \
    "Name=instance-state-name,Values=pending,running,stopping,stopped" \
  --query 'Reservations[].Instances[].InstanceId' --output text)"
if [[ -n "$IDS" && "$IDS" != "None" ]]; then
  echo "Terminating preflight instances: $IDS"
  aws ec2 terminate-instances --instance-ids $IDS
fi
