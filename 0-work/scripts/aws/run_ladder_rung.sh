#!/usr/bin/env bash
# Launch N EC2 ladder workers + async waiter (SNS email when rung completes).
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
RUNG=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --async) ASYNC=1; shift ;;
    -h|--help)
      echo "Usage: run_ladder_rung.sh [--async] RUNG"
      echo "  RUNG 1→1  2→4  3→10  4→20  5→50  6→100 workers"
      exit 0
      ;;
    *)
      RUNG="$1"
      shift
      ;;
  esac
done

if [[ -z "$RUNG" ]]; then
  echo "Usage: run_ladder_rung.sh [--async] RUNG" >&2
  exit 1
fi

declare -A RUNG_WORKERS=([1]=1 [2]=4 [3]=10 [4]=20 [5]=50 [6]=100)
WORKERS="${RUNG_WORKERS[$RUNG]:-}"
if [[ -z "$WORKERS" ]]; then
  echo "Invalid rung: $RUNG (use 1–6)" >&2
  exit 1
fi

BUCKET="${GYPSY_S3_BUCKET:?set GYPSY_S3_BUCKET}"
SOAK_ID="${GYPSY_SOAK_INSTANCE_ID:-i-0812f82dd21298e96}"
KEYS_PER_WORKER=$(( 2000 / WORKERS ))
RATE_LIMIT_S="${GYPSY_LADDER_RATE_LIMIT_S:-1.0}"

echo "==> Rung ${RUNG}: ${WORKERS} workers × ${KEYS_PER_WORKER} keys @ ${RATE_LIMIT_S} req/s"

# Ensure pool + shards exist locally and on S3
if [[ ! -f "$ROOT/data/ladder/pool/document_keys.txt" ]]; then
  echo "Building ladder pool..."
  python3 "$ROOT/0-work/scripts/08_build_ladder_pool.py"
fi
aws s3 sync "$ROOT/data/ladder/" "s3://${BUCKET}/ladder/" --only-show-errors
aws s3 cp "$ROOT/0-work/scripts/00_asx_api.py" "s3://${BUCKET}/scripts/00_asx_api.py"
aws s3 cp "$ROOT/0-work/scripts/07_cdn_soak_test.py" "s3://${BUCKET}/scripts/07_cdn_soak_test.py"

VPC_ID="$(aws ec2 describe-vpcs --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId' --output text)"
SUBNET="$(aws ec2 describe-subnets --filters Name=default-for-az,Values=true Name=vpc-id,Values="$VPC_ID" --query 'Subnets[0].SubnetId' --output text)"
SG_ID="$(aws ec2 describe-security-groups --filters "Name=group-name,Values=gypsy-danger-fetch-sg" "Name=vpc-id,Values=$VPC_ID" --query 'SecurityGroups[0].GroupId' --output text)"
AMI="$(aws ec2 describe-images --owners amazon \
  --filters 'Name=name,Values=al2023-ami-2023*-x86_64' 'Name=state,Values=available' \
  --query 'sort_by(Images,&CreationDate)[-1].ImageId' --output text)"
PROFILE=gypsy-danger-fetch-ec2-profile

launch_worker() {
  local worker_id=$1
  local shard_id
  shard_id="$(printf '%02d' "$worker_id")"
  local name="gypsy-danger-ladder-r${RUNG}-w${shard_id}"

  local user_data
  user_data="$(cat <<USERDATA
#!/bin/bash
set -euxo pipefail
exec > /var/log/gypsy-ladder.log 2>&1
export GYPSY_S3_BUCKET=${BUCKET}
export AWS_DEFAULT_REGION=${AWS_DEFAULT_REGION}
export GYPSY_SNS_TOPIC_ARN=${GYPSY_SNS_TOPIC_ARN:-}
cat > /etc/profile.d/gypsy-danger.sh <<ENV
export GYPSY_S3_BUCKET=${BUCKET}
export AWS_DEFAULT_REGION=${AWS_DEFAULT_REGION}
export GYPSY_SNS_TOPIC_ARN=${GYPSY_SNS_TOPIC_ARN:-}
ENV
dnf install -y python3 aws-cli
ROOT=/opt/gypsy-danger
mkdir -p "\$ROOT/0-work/scripts" "\$ROOT/data/ladder"
aws s3 cp "s3://${BUCKET}/scripts/00_asx_api.py" "\$ROOT/0-work/scripts/"
aws s3 cp "s3://${BUCKET}/scripts/07_cdn_soak_test.py" "\$ROOT/0-work/scripts/"
aws s3 cp "s3://${BUCKET}/ladder/shards/${WORKERS}workers/shard_${shard_id}.txt" "\$ROOT/data/ladder/shard.txt"
cd "\$ROOT/0-work/scripts"
python3 07_cdn_soak_test.py \\
  --keys-file "\$ROOT/data/ladder/shard.txt" \\
  --max-requests ${KEYS_PER_WORKER} \\
  --rate-limit-s ${RATE_LIMIT_S} \\
  --no-cache \\
  --label worker_${shard_id} \\
  --result-json /tmp/result.json \\
  2>&1 | tee /tmp/soak.log
aws s3 cp /tmp/result.json "s3://${BUCKET}/logs/ladder/rung${RUNG}/worker_${shard_id}.json"
aws s3 cp /tmp/soak.log "s3://${BUCKET}/logs/ladder/rung${RUNG}/worker_${shard_id}.log"
USERDATA
)"

  aws ec2 run-instances \
    --image-id "$AMI" \
    --instance-type t3.small \
    --iam-instance-profile "Name=${PROFILE}" \
    --security-group-ids "$SG_ID" \
    --subnet-id "$SUBNET" \
    --user-data "$user_data" \
    --metadata-options HttpTokens=required \
    --tag-specifications \
      "ResourceType=instance,Tags=[{Key=Name,Value=${name}},{Key=Project,Value=gypsy-danger},{Key=Application,Value=gypsy-danger-asx-fetch},{Key=Stage,Value=soak},{Key=LadderRung,Value=${RUNG}},{Key=ManagedBy,Value=gypsy-danger-ladder}]" \
    --query 'Instances[0].InstanceId' \
    --no-cli-pager \
    --output text
}

echo "==> Launching ${WORKERS} worker(s)..."
LAUNCHED=()
for w in $(seq 0 $((WORKERS - 1))); do
  IID="$(launch_worker "$w")"
  echo "  worker $(printf '%02d' "$w"): $IID"
  LAUNCHED+=("$IID")
done

# Start waiter on soak instance (polls S3, sends SNS, terminates workers)
WAITER_B64="$(base64 -w0 < "$ROOT/0-work/scripts/aws/ladder_wait_and_notify.sh")"
WAITER_CMD="echo ${WAITER_B64} | base64 -d > /tmp/ladder_wait.sh && chmod +x /tmp/ladder_wait.sh && /tmp/ladder_wait.sh ${RUNG} ${WORKERS}"
WAIT_TIMEOUT="$(python3 - <<PY
import math
k = int("${KEYS_PER_WORKER}")
print(int(math.ceil(k * 8.0 + 900)))
PY
)"

WAIT_CMD_ID="$(aws ssm send-command \
  --instance-ids "$SOAK_ID" \
  --document-name AWS-RunShellScript \
  --timeout-seconds "$WAIT_TIMEOUT" \
  --parameters "commands=[\"${WAITER_CMD}\"]" \
  --query Command.CommandId \
  --no-cli-pager \
  --output text)"

echo
echo "=== Ladder rung ${RUNG} started ==="
echo "Workers:     ${WORKERS} instance(s): ${LAUNCHED[*]}"
echo "Waiter SSM:  ${WAIT_CMD_ID} on ${SOAK_ID} (timeout ${WAIT_TIMEOUT}s)"
echo "Results:     s3://${BUCKET}/logs/ladder/rung${RUNG}/"
if [[ -n "${GYPSY_SNS_TOPIC_ARN:-}" ]]; then
  echo "Notify:      email via SNS when rung completes"
else
  echo "Notify:      set GYPSY_SNS_TOPIC_ARN (run bootstrap_notifications.sh)"
fi

if [[ "$ASYNC" -eq 1 ]]; then
  echo
  echo "Async mode — safe to close Cursor. Check email or S3 for results."
  exit 0
fi

echo "Sync mode — polling waiter..."
for _ in $(seq 1 $(( (WAIT_TIMEOUT + 4) / 5 ))); do
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
