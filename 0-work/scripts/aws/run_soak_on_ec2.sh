#!/usr/bin/env bash
# Run B0 CDN soak on gypsy-danger-soak-01 via SSM.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
ENV_FILE="$ROOT/0-work/scripts/.env"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$ENV_FILE"
  set +a
fi

INSTANCE_ID="${GYPSY_SOAK_INSTANCE_ID:-i-0812f82dd21298e96}"
ASYNC=0
if [[ "${1:-}" == "--async" ]]; then
  ASYNC=1
  shift
fi
MAX_REQUESTS="${1:-50}"
RATE_LIMIT_S="${2:-1.0}"

export AWS_PAGER=""
export AWS_CLI_PAGER=""

# SSM soak runs on EC2; allow rate limit + ~7s avg CDN download per request + buffer.
POLL_MAX_S="$(python3 - <<PY
import math
max_requests = int("${MAX_REQUESTS}")
rate = float("${RATE_LIMIT_S}")
print(int(math.ceil(max_requests * (rate + 7.0) + 300)))
PY
)"
POLL_ITERS=$(( (POLL_MAX_S + 4) / 5 ))

REMOTE_SCRIPT="$(cat <<'REMOTE'
set -euxo pipefail
source /etc/profile.d/gypsy-danger.sh
ROOT=/opt/gypsy-danger
mkdir -p "$ROOT/0-work/scripts" "$ROOT/data/entities/CBA"
aws s3 cp "s3://${GYPSY_S3_BUCKET}/scripts/00_asx_api.py" "$ROOT/0-work/scripts/"
aws s3 cp "s3://${GYPSY_S3_BUCKET}/scripts/07_cdn_soak_test.py" "$ROOT/0-work/scripts/"
aws s3 cp "s3://${GYPSY_S3_BUCKET}/entities/CBA/CBA_Announcements.csv" "$ROOT/data/entities/CBA/"
cd "$ROOT/0-work/scripts"
python3 07_cdn_soak_test.py --ticker CBA --max-requests MAX_REQUESTS --rate-limit-s RATE_LIMIT_S --no-cache \
  2>&1 | tee /tmp/gypsy-soak.log
if [[ -n "${GYPSY_SNS_TOPIC_ARN:-}" ]]; then
  SUBJECT="Gypsy Danger soak — CBA MAX_REQUESTS req @ RATE_LIMIT_S/s"
  BODY="$(tail -40 /tmp/gypsy-soak.log)"
  aws sns publish --topic-arn "$GYPSY_SNS_TOPIC_ARN" --subject "$SUBJECT" --message "$BODY"
fi
aws s3 cp /tmp/gypsy-soak.log "s3://${GYPSY_S3_BUCKET}/logs/soak/$(curl -s http://169.254.169.254/latest/meta-data/instance-id 2>/dev/null || echo local)-$(date -u +%Y%m%dT%H%M%SZ).log" || true
REMOTE
)"
REMOTE_SCRIPT="${REMOTE_SCRIPT/MAX_REQUESTS/$MAX_REQUESTS}"
REMOTE_SCRIPT="${REMOTE_SCRIPT/RATE_LIMIT_S/$RATE_LIMIT_S}"

TIMEOUT_S="$POLL_MAX_S"

B64="$(printf '%s' "$REMOTE_SCRIPT" | base64 -w0)"
CMD="echo $B64 | base64 -d | bash"

CMD_ID="$(aws ssm send-command \
  --instance-ids "$INSTANCE_ID" \
  --document-name AWS-RunShellScript \
  --timeout-seconds "$TIMEOUT_S" \
  --parameters "commands=[\"$CMD\"]" \
  --query 'Command.CommandId' \
  --no-cli-pager \
  --output text)"

echo "SSM CommandId: $CMD_ID (instance $INSTANCE_ID)"
echo "SSM timeout: ${TIMEOUT_S}s | polling up to ${POLL_MAX_S}s for ${MAX_REQUESTS} requests..."

if [[ "$ASYNC" -eq 1 ]]; then
  echo
  echo "Async mode — safe to close Cursor."
  if [[ -n "${GYPSY_SNS_TOPIC_ARN:-}" ]]; then
    echo "You will receive an SNS email when the soak completes."
  else
    echo "Set GYPSY_SNS_TOPIC_ARN (bootstrap_notifications.sh) for email on completion."
  fi
  exit 0
fi

for _ in $(seq 1 "$POLL_ITERS"); do
  STATUS="$(aws ssm get-command-invocation \
    --command-id "$CMD_ID" \
    --instance-id "$INSTANCE_ID" \
    --no-cli-pager \
    --query Status --output text 2>/dev/null || echo Pending)"
  if (( _ % 12 == 0 )); then
    echo "Status: $STATUS (${_} checks, ~$(( _ * 5 ))s elapsed)"
  elif [[ "$STATUS" != "InProgress" && "$STATUS" != "Pending" ]]; then
    echo "Status: $STATUS"
  fi
  if [[ "$STATUS" == "Success" || "$STATUS" == "Failed" || "$STATUS" == "Cancelled" || "$STATUS" == "TimedOut" ]]; then
    break
  fi
  sleep 5
done

FINAL="$(aws ssm get-command-invocation \
  --command-id "$CMD_ID" \
  --instance-id "$INSTANCE_ID" \
  --no-cli-pager \
  --output json)"
STATUS="$(printf '%s' "$FINAL" | python3 -c "import json,sys; print(json.load(sys.stdin).get('Status',''))")"
printf '%s\n' "$FINAL"

if [[ "$STATUS" == "TimedOut" ]]; then
  echo
  echo "SSM timed out — fetching partial log from /tmp/gypsy-soak.log on instance..."
  LOG_CMD='cat /tmp/gypsy-soak.log 2>/dev/null | tail -30 || echo "(no log file)"'
  B64_LOG="$(printf '%s' "$LOG_CMD" | base64 -w0)"
  LOG_ID="$(aws ssm send-command \
    --instance-ids "$INSTANCE_ID" \
    --document-name AWS-RunShellScript \
    --parameters "commands=[\"echo $B64_LOG | base64 -d | bash\"]" \
    --query Command.CommandId --no-cli-pager --output text)"
  sleep 6
  aws ssm get-command-invocation \
    --command-id "$LOG_ID" \
    --instance-id "$INSTANCE_ID" \
    --no-cli-pager \
    --query StandardOutputContent \
    --output text
fi
