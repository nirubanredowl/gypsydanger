#!/usr/bin/env bash
# End-to-end preflight: loose annual reports → S3, burn rotation, SNS email.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
ENV_FILE="$ROOT/0-work/scripts/.env"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$ENV_FILE"
  set +a
fi

export AWS_PAGER=""
export AWS_CLI_PAGER=""
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-ap-southeast-2}"

ASYNC=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --async) ASYNC=1; shift ;;
    -h|--help)
      cat <<EOF
Usage: run_preflight_fetch.sh [--async]

Preflight validates before Phase C:
  - loose annual report filter
  - S3 layout entities/{TICKER}/annual_reports/{YYYY}_{documentKey}.pdf
  - burned EC2 replacement (worker 01 simulates burn after 2 uploads)
  - SNS email summary

Workers:
  w00 → CBA, 5 loose annual reports
  w01 → QGL, 5 loose annual reports (simulate burn → rotate → finish)
EOF
      exit 0
      ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

BUCKET="${GYPSY_S3_BUCKET:?set GYPSY_S3_BUCKET in .env}"
SOAK_ID="${GYPSY_SOAK_INSTANCE_ID:-i-0812f82dd21298e96}"
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-preflight"
WORKERS=2
LAUNCH="$ROOT/0-work/scripts/aws/launch_preflight_worker.sh"

echo "==> Preflight run ${RUN_ID}"
echo "    Bucket: s3://${BUCKET}"
echo "    PDF path: entities/{TICKER}/annual_reports/{YYYY}_{documentKey}.pdf"

# Upload scripts + ensure index CSVs for CBA and QGL exist on S3
aws s3 cp "$ROOT/0-work/scripts/00_asx_api.py" "s3://${BUCKET}/scripts/00_asx_api.py"
aws s3 cp "$ROOT/0-work/scripts/11_fetch_annual_reports_s3.py" "s3://${BUCKET}/scripts/11_fetch_annual_reports_s3.py"
aws s3 cp "$ROOT/0-work/scripts/aws/launch_preflight_worker.sh" "s3://${BUCKET}/scripts/launch_preflight_worker.sh"
for T in CBA QGL; do
  aws s3 cp "s3://${BUCKET}/entities/${T}/${T}_Announcements.csv" - >/dev/null 2>&1 || \
    aws s3 cp "$ROOT/data/entities/${T}/${T}_Announcements.csv" "s3://${BUCKET}/entities/${T}/${T}_Announcements.csv"
done

chmod +x "$LAUNCH" "$ROOT/0-work/scripts/aws/preflight_wait_and_notify.sh"

echo "==> Launching ${WORKERS} preflight workers..."
W0="$("$LAUNCH" "$RUN_ID" 0 CBA 5 0 0 0)"
echo "  worker 00 (CBA): $W0"
W1="$("$LAUNCH" "$RUN_ID" 1 QGL 5 0 0 2)"
echo "  worker 01 (QGL, simulate burn after 2): $W1"

WAITER_B64="$(base64 -w0 < "$ROOT/0-work/scripts/aws/preflight_wait_and_notify.sh")"
WAITER_CMD="echo ${WAITER_B64} | base64 -d > /tmp/preflight_wait.sh && chmod +x /tmp/preflight_wait.sh && /tmp/preflight_wait.sh ${RUN_ID} ${WORKERS}"
WAIT_CMD_ID="$(aws ssm send-command \
  --instance-ids "$SOAK_ID" \
  --document-name AWS-RunShellScript \
  --timeout-seconds 1800 \
  --parameters "commands=[\"${WAITER_CMD}\"]" \
  --query Command.CommandId \
  --no-cli-pager \
  --output text)"

echo
echo "=== Preflight started ==="
echo "Run ID:      ${RUN_ID}"
echo "Workers:     ${W0} ${W1}"
echo "Waiter SSM:  ${WAIT_CMD_ID} on ${SOAK_ID}"
echo "Results:     s3://${BUCKET}/logs/preflight/${RUN_ID}/"
echo "Summary:     s3://${BUCKET}/manifests/preflight/${RUN_ID}/summary.json"
if [[ -n "${GYPSY_SNS_TOPIC_ARN:-}" ]]; then
  echo "Notify:      SNS email when preflight completes"
else
  echo "Notify:      set GYPSY_SNS_TOPIC_ARN for email"
fi

if [[ "$ASYNC" -eq 1 ]]; then
  echo
  echo "Async mode — safe to close. Check email or S3 summary."
  exit 0
fi

echo "Sync mode — polling waiter..."
for _ in $(seq 1 360); do
  STATUS="$(aws ssm get-command-invocation \
    --command-id "$WAIT_CMD_ID" \
    --instance-id "$SOAK_ID" \
    --no-cli-pager \
    --query Status --output text 2>/dev/null || echo Pending)"
  if [[ "$STATUS" == "Success" || "$STATUS" == "Failed" || "$STATUS" == "TimedOut" ]]; then
    echo "Waiter: $STATUS"
    break
  fi
  sleep 5
done
aws ssm get-command-invocation \
  --command-id "$WAIT_CMD_ID" \
  --instance-id "$SOAK_ID" \
  --no-cli-pager \
  --query StandardOutputContent \
  --output text
