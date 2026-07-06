#!/usr/bin/env bash
# Wait for AWS auth → launch preflight (loose annual reports) → monitor → exit.
# Safe to leave running in tmux; workers continue after this script exits.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
export PATH="/usr/local/bin:/home/ubuntu/.local/bin:${PATH:-/usr/bin:/bin}"
export AWS_PAGER=""
export AWS_CLI_PAGER=""
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-ap-southeast-2}"

LOG="$ROOT/0-work/scripts/preflight-run.log"
ENV_FILE="$ROOT/0-work/scripts/.env"
CODE_FILE="/tmp/aws_login_code.txt"
MONITOR_MIN="${GYPSY_PREFLIGHT_MONITOR_MIN:-5}"

mkdir -p "$(dirname "$LOG")"
exec > >(tee -a "$LOG") 2>&1

echo ""
echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) preflight autostart ==="

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$ENV_FILE"
  set +a
fi

wait_for_aws() {
  if aws sts get-caller-identity >/dev/null 2>&1; then
    echo "AWS auth: OK ($(aws sts get-caller-identity --query Account --output text))"
    return 0
  fi
  echo "AWS auth: waiting (add AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY to .env or agent secrets,"
  echo "  OR complete login: write authorization code to ${CODE_FILE})"
  echo "Starting aws login --remote..."
  python3 "$ROOT/0-work/scripts/aws_login_wait.py" --code-file "$CODE_FILE"
}

wait_for_aws

echo ""
echo "Launching preflight (loose annual reports, burn rotation test, SNS email)..."
LAUNCH_OUT="/tmp/preflight-launch-$$.out"
"$ROOT/0-work/scripts/aws/run_preflight_fetch.sh" --async 2>&1 | tee "$LAUNCH_OUT"

RUN_ID="$(grep -E '^Run ID:' "$LAUNCH_OUT" | awk '{print $3}' || true)"
BUCKET="${GYPSY_S3_BUCKET:-}"
if [[ -z "$RUN_ID" || -z "$BUCKET" ]]; then
  echo "ERROR: could not parse run id or bucket from launch output"
  exit 1
fi

PREFIX="logs/preflight/${RUN_ID}/"
echo ""
echo "Monitoring for ${MONITOR_MIN} minutes..."
echo "  Run ID:  ${RUN_ID}"
echo "  S3 logs: s3://${BUCKET}/${PREFIX}"
echo "  Summary: s3://${BUCKET}/manifests/preflight/${RUN_ID}/summary.json"

deadline=$(( $(date +%s) + MONITOR_MIN * 60 ))
while [[ $(date +%s) -lt $deadline ]]; do
  found="$(aws s3 ls "s3://${BUCKET}/${PREFIX}" 2>/dev/null | grep -c 'worker_.*\.json' || true)"
  summary="no"
  if aws s3api head-object --bucket "$BUCKET" --key "manifests/preflight/${RUN_ID}/summary.json" >/dev/null 2>&1; then
    summary="yes"
  fi
  echo "  $(date -u +%H:%M:%S) worker_json=${found}/2 summary=${summary}"
  if [[ "$summary" == "yes" ]]; then
    aws s3 cp "s3://${BUCKET}/manifests/preflight/${RUN_ID}/summary.json" - 2>/dev/null || true
    echo "Preflight complete (summary on S3). Check SNS email."
    exit 0
  fi
  sleep 30
done

echo ""
echo "Monitor window ended — preflight still running on EC2."
echo "Waiter on soak-01 will email via SNS when done."
echo "Re-check: aws s3 cp s3://${BUCKET}/manifests/preflight/${RUN_ID}/summary.json -"
