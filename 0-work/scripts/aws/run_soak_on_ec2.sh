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
MAX_REQUESTS="${1:-50}"
RATE_LIMIT_S="${2:-1.0}"

REMOTE_SCRIPT="$(cat <<'REMOTE'
set -euxo pipefail
source /etc/profile.d/gypsy-danger.sh
ROOT=/opt/gypsy-danger
mkdir -p "$ROOT/0-work/scripts" "$ROOT/data/entities/CBA"
aws s3 cp "s3://${GYPSY_S3_BUCKET}/scripts/00_asx_api.py" "$ROOT/0-work/scripts/"
aws s3 cp "s3://${GYPSY_S3_BUCKET}/scripts/07_cdn_soak_test.py" "$ROOT/0-work/scripts/"
aws s3 cp "s3://${GYPSY_S3_BUCKET}/entities/CBA/CBA_Announcements.csv" "$ROOT/data/entities/CBA/"
cd "$ROOT/0-work/scripts"
python3 07_cdn_soak_test.py --ticker CBA --max-requests MAX_REQUESTS --rate-limit-s RATE_LIMIT_S --no-cache
REMOTE
)"
REMOTE_SCRIPT="${REMOTE_SCRIPT/MAX_REQUESTS/$MAX_REQUESTS}"
REMOTE_SCRIPT="${REMOTE_SCRIPT/RATE_LIMIT_S/$RATE_LIMIT_S}"

B64="$(printf '%s' "$REMOTE_SCRIPT" | base64 -w0)"
CMD="echo $B64 | base64 -d | bash"

CMD_ID="$(aws ssm send-command \
  --instance-ids "$INSTANCE_ID" \
  --document-name AWS-RunShellScript \
  --parameters "commands=[\"$CMD\"]" \
  --query 'Command.CommandId' \
  --output text)"

echo "SSM CommandId: $CMD_ID (instance $INSTANCE_ID)"
for _ in $(seq 1 60); do
  STATUS="$(aws ssm get-command-invocation \
    --command-id "$CMD_ID" \
    --instance-id "$INSTANCE_ID" \
    --query Status --output text 2>/dev/null || echo Pending)"
  echo "Status: $STATUS"
  if [[ "$STATUS" == "Success" || "$STATUS" == "Failed" || "$STATUS" == "Cancelled" || "$STATUS" == "TimedOut" ]]; then
    break
  fi
  sleep 5
done

aws ssm get-command-invocation \
  --command-id "$CMD_ID" \
  --instance-id "$INSTANCE_ID" \
  --output json
