#!/usr/bin/env bash
# Install progress watcher cron on gypsy-danger-soak-01 (every 5 minutes).
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
SOAK_ID="${GYPSY_SOAK_INSTANCE_ID:-i-0812f82dd21298e96}"

REMOTE="$(cat <<REMOTE
set -euo pipefail
source /etc/profile.d/gypsy-danger.sh
ROOT=/opt/gypsy-danger
mkdir -p "\$ROOT/0-work/scripts/aws"
aws s3 cp "s3://\${GYPSY_S3_BUCKET}/scripts/09_fetch_progress.py" "\$ROOT/0-work/scripts/"
aws s3 cp "s3://\${GYPSY_S3_BUCKET}/scripts/09_progress_watcher.py" "\$ROOT/0-work/scripts/"
aws s3 sync "s3://\${GYPSY_S3_BUCKET}/scripts/aws/" "\$ROOT/0-work/scripts/aws/" --exclude '*' --include '*.sh'
chmod +x "\$ROOT/0-work/scripts/aws/"*.sh 2>/dev/null || true
CRON_LINE='*/5 * * * * root source /etc/profile.d/gypsy-danger.sh && /opt/gypsy-danger/0-work/scripts/aws/progress_watcher.sh >> /var/log/gypsy-progress-watcher.log 2>&1'
grep -q gypsy-progress-watcher /etc/cron.d/gypsy-danger 2>/dev/null || {
  echo "\$CRON_LINE" >> /etc/cron.d/gypsy-danger
  chmod 644 /etc/cron.d/gypsy-danger
}
echo "Cron installed: /etc/cron.d/gypsy-danger"
REMOTE
)"

# Sync scripts to S3 first from local
aws s3 cp "$ROOT/0-work/scripts/09_fetch_progress.py" "s3://${GYPSY_S3_BUCKET}/scripts/09_fetch_progress.py"
aws s3 cp "$ROOT/0-work/scripts/09_progress_watcher.py" "s3://${GYPSY_S3_BUCKET}/scripts/09_progress_watcher.py"
aws s3 sync "$ROOT/0-work/scripts/aws/" "s3://${GYPSY_S3_BUCKET}/scripts/aws/" --exclude '*' --include 'progress*.sh' --include 'request_progress.sh' --include 'notify_sns.sh'

B64="$(printf '%s' "$REMOTE" | base64 -w0)"
CMD="echo $B64 | base64 -d | bash"
CID="$(aws ssm send-command \
  --instance-ids "$SOAK_ID" \
  --document-name AWS-RunShellScript \
  --parameters "commands=[\"$CMD\"]" \
  --query Command.CommandId --no-cli-pager --output text)"
echo "SSM install cron: $CID on $SOAK_ID"
sleep 8
aws ssm get-command-invocation --command-id "$CID" --instance-id "$SOAK_ID" \
  --no-cli-pager --query StandardOutputContent --output text
