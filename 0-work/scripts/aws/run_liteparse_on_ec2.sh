#!/usr/bin/env bash
# Kick off Stage 3A LiteParse on the orchestrator EC2 (soak-01) via SSM.
# Syncs scripts/shards to S3, then runs run_liteparse_parse.sh --async on the coordinator.
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

WATCH_S="${GYPSY_PARSE_KICKOFF_WATCH_S:-60}"
SOAK_ID="${GYPSY_SOAK_INSTANCE_ID:-i-0812f82dd21298e96}"
BUCKET="${GYPSY_S3_BUCKET:?set GYPSY_S3_BUCKET}"

echo "==> Sync parse assets to s3://${BUCKET}"
aws s3 sync "$ROOT/data/parse_3a/" "s3://${BUCKET}/parse_3a/" --only-show-errors
aws s3 cp "$ROOT/data/catalog/corpus_registry.csv" "s3://${BUCKET}/catalog/corpus_registry.csv" 2>/dev/null || true
aws s3 cp "$ROOT/0-work/scripts/requirements-parse.txt" "s3://${BUCKET}/scripts/requirements-parse.txt"
aws s3 cp "$ROOT/0-work/scripts/00_asx_api.py" "s3://${BUCKET}/scripts/00_asx_api.py"
aws s3 cp "$ROOT/0-work/scripts/11_fetch_annual_reports_s3.py" "s3://${BUCKET}/scripts/11_fetch_annual_reports_s3.py"
aws s3 cp "$ROOT/0-work/scripts/22_liteparse_document.py" "s3://${BUCKET}/scripts/22_liteparse_document.py"
aws s3 cp "$ROOT/0-work/scripts/23_liteparse_shard.py" "s3://${BUCKET}/scripts/23_liteparse_shard.py"
aws s3 cp "$ROOT/0-work/scripts/25_build_parse_shards.py" "s3://${BUCKET}/scripts/25_build_parse_shards.py"
aws s3 cp "$ROOT/0-work/scripts/26_parse_progress.py" "s3://${BUCKET}/scripts/26_parse_progress.py"
aws s3 cp "$ROOT/0-work/scripts/26_parse_progress_watcher.py" "s3://${BUCKET}/scripts/26_parse_progress_watcher.py"
aws s3 sync "$ROOT/0-work/scripts/aws/" "s3://${BUCKET}/scripts/aws/" \
  --exclude '*' \
  --include 'run_liteparse_parse.sh' \
  --include 'launch_liteparse_worker.sh' \
  --include 'liteparse_wait_and_notify.sh' \
  --include 'parse_progress*.sh' \
  --include 'request_parse_progress.sh' \
  --include 'notify_sns.sh' \
  --include 'install_parse_progress_watcher.sh'

REMOTE="$(cat <<'REMOTE'
set -euo pipefail
source /etc/profile.d/gypsy-danger.sh
export AWS_PAGER=""
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-ap-southeast-2}"
ROOT=/opt/gypsy-danger
mkdir -p "$ROOT/0-work/scripts/aws" "$ROOT/data/parse_3a"
aws s3 sync "s3://${GYPSY_S3_BUCKET}/parse_3a/" "$ROOT/data/parse_3a/" --only-show-errors
aws s3 cp "s3://${GYPSY_S3_BUCKET}/scripts/00_asx_api.py" "$ROOT/0-work/scripts/"
aws s3 cp "s3://${GYPSY_S3_BUCKET}/scripts/11_fetch_annual_reports_s3.py" "$ROOT/0-work/scripts/"
aws s3 cp "s3://${GYPSY_S3_BUCKET}/scripts/22_liteparse_document.py" "$ROOT/0-work/scripts/"
aws s3 cp "s3://${GYPSY_S3_BUCKET}/scripts/23_liteparse_shard.py" "$ROOT/0-work/scripts/"
aws s3 cp "s3://${GYPSY_S3_BUCKET}/scripts/25_build_parse_shards.py" "$ROOT/0-work/scripts/"
aws s3 cp "s3://${GYPSY_S3_BUCKET}/scripts/26_parse_progress.py" "$ROOT/0-work/scripts/"
aws s3 cp "s3://${GYPSY_S3_BUCKET}/scripts/26_parse_progress_watcher.py" "$ROOT/0-work/scripts/"
aws s3 cp "s3://${GYPSY_S3_BUCKET}/scripts/requirements-parse.txt" "$ROOT/0-work/scripts/"
aws s3 sync "s3://${GYPSY_S3_BUCKET}/scripts/aws/" "$ROOT/0-work/scripts/aws/" --only-show-errors
chmod +x "$ROOT/0-work/scripts/aws/"*.sh

# Milestone emails while run is active
"$ROOT/0-work/scripts/aws/install_parse_progress_watcher.sh" || true

dnf install -y tmux 2>/dev/null || yum install -y tmux 2>/dev/null || true

cd "$ROOT"
"$ROOT/0-work/scripts/aws/run_liteparse_parse.sh" --async 2>&1 | tee /tmp/gypsy-liteparse-kickoff.log

echo
echo "=== Kickoff log (last 30 lines) ==="
tail -30 /tmp/gypsy-liteparse-kickoff.log

echo
echo "=== Worker instances (parse run) ==="
RUN_ID="$(grep -oE '[0-9]{8}T[0-9]{6}Z-parse' /tmp/gypsy-liteparse-kickoff.log | tail -1 || true)"
if [[ -n "$RUN_ID" ]]; then
  aws ec2 describe-instances \
    --filters "Name=tag:ParseRunId,Values=${RUN_ID}" "Name=instance-state-name,Values=pending,running" \
    --query 'Reservations[].Instances[].[InstanceId,State.Name,Tags[?Key==`Name`].Value|[0]]' \
    --output table || true
  echo "Progress: s3://${GYPSY_S3_BUCKET}/manifests/parse_progress.json"
fi
REMOTE
)"

echo "==> SSM kickoff on orchestrator ${SOAK_ID}"
B64="$(printf '%s' "$REMOTE" | base64 -w0)"
CMD="echo $B64 | base64 -d | bash"

CMD_ID="$(aws ssm send-command \
  --instance-ids "$SOAK_ID" \
  --document-name AWS-RunShellScript \
  --timeout-seconds 600 \
  --parameters "commands=[\"$CMD\"]" \
  --query 'Command.CommandId' \
  --no-cli-pager \
  --output text)"

echo "SSM CommandId: ${CMD_ID}"
echo "Watching ~${WATCH_S}s for kickoff output..."

end=$(( $(date +%s) + WATCH_S ))
while [[ $(date +%s) -lt $end ]]; do
  STATUS="$(aws ssm get-command-invocation \
    --command-id "$CMD_ID" \
    --instance-id "$SOAK_ID" \
    --no-cli-pager \
    --query Status --output text 2>/dev/null || echo Pending)"
  if [[ "$STATUS" == "Success" || "$STATUS" == "Failed" || "$STATUS" == "Cancelled" || "$STATUS" == "TimedOut" ]]; then
    break
  fi
  sleep 5
done

aws ssm get-command-invocation \
  --command-id "$CMD_ID" \
  --instance-id "$SOAK_ID" \
  --no-cli-pager \
  --query '[Status,StandardOutputContent,StandardErrorContent]' \
  --output text

echo
echo "Kickoff handed off on ${SOAK_ID}. Parse workers + waiter continue on orchestrator."
echo "Check later: 0-work/scripts/aws/request_parse_progress.sh --now  (from machine with AWS creds)"
echo "Or:          aws s3 cp s3://${BUCKET}/manifests/parse_progress.json -"
