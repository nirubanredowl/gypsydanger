#!/usr/bin/env bash
# Launch one ladder CDN soak worker (new EC2 instance = new public IP).
# Called by run_ladder_rung.sh and ladder_wait_and_notify.sh (burn rotation).
set -euo pipefail

RUNG="${1:?rung}"
RUN_ID="${2:?run id}"
WORKER_ID="${3:?worker id 0-based}"
START_OFFSET="${4:-0}"
ROTATION="${5:-0}"
MAX_REQUESTS="${6:-}"

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

declare -A RUNG_WORKERS=([1]=1 [2]=4 [3]=10 [4]=20 [5]=50 [6]=100)
WORKERS="${RUNG_WORKERS[$RUNG]:-}"
if [[ -z "$WORKERS" ]]; then
  echo "Invalid rung: $RUNG" >&2
  exit 1
fi

BUCKET="${GYPSY_S3_BUCKET:?set GYPSY_S3_BUCKET}"
KEYS_PER_WORKER=$(( 2000 / WORKERS ))
if [[ -z "$MAX_REQUESTS" ]]; then
  MAX_REQUESTS="$KEYS_PER_WORKER"
fi
RATE_LIMIT_S="${GYPSY_LADDER_RATE_LIMIT_S:-1.0}"
LOG_PREFIX="logs/ladder/rung${RUNG}/${RUN_ID}"
shard_id="$(printf '%02d' "$WORKER_ID")"
name="gypsy-danger-ladder-r${RUNG}-w${shard_id}"
if [[ "$ROTATION" -gt 0 ]]; then
  name="${name}-r${ROTATION}"
fi

VPC_ID="$(aws ec2 describe-vpcs --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId' --output text)"
SUBNET="$(aws ec2 describe-subnets --filters Name=default-for-az,Values=true Name=vpc-id,Values="$VPC_ID" --query 'Subnets[0].SubnetId' --output text)"
SG_ID="$(aws ec2 describe-security-groups --filters "Name=group-name,Values=gypsy-danger-fetch-sg" "Name=vpc-id,Values=$VPC_ID" --query 'SecurityGroups[0].GroupId' --output text)"
AMI="$(aws ec2 describe-images --owners amazon \
  --filters 'Name=name,Values=al2023-ami-2023*-x86_64' 'Name=state,Values=available' \
  --query 'sort_by(Images,&CreationDate)[-1].ImageId' --output text)"
PROFILE=gypsy-danger-fetch-ec2-profile

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
set +e
python3 07_cdn_soak_test.py \\
  --keys-file "\$ROOT/data/ladder/shard.txt" \\
  --max-requests ${MAX_REQUESTS} \\
  --start-offset ${START_OFFSET} \\
  --rate-limit-s ${RATE_LIMIT_S} \\
  --no-cache \\
  --label worker_${shard_id} \\
  --rotation ${ROTATION} \\
  --result-json /tmp/result.json \\
  2>&1 | tee /tmp/soak.log
EXIT=\$?
set -e
aws s3 cp /tmp/result.json "s3://${BUCKET}/${LOG_PREFIX}/worker_${shard_id}.json" || true
aws s3 cp /tmp/soak.log "s3://${BUCKET}/${LOG_PREFIX}/worker_${shard_id}.log" || true
exit \$EXIT
USERDATA
)"

IID="$(aws ec2 run-instances \
  --image-id "$AMI" \
  --instance-type t3.small \
  --iam-instance-profile "Name=${PROFILE}" \
  --security-group-ids "$SG_ID" \
  --subnet-id "$SUBNET" \
  --user-data "$user_data" \
  --metadata-options HttpTokens=required \
  --tag-specifications \
    "ResourceType=instance,Tags=[{Key=Name,Value=${name}},{Key=Project,Value=gypsy-danger},{Key=Application,Value=gypsy-danger-asx-fetch},{Key=Stage,Value=soak},{Key=LadderRung,Value=${RUNG}},{Key=LadderRunId,Value=${RUN_ID}},{Key=LadderWorkerId,Value=${shard_id}},{Key=LadderRotation,Value=${ROTATION}},{Key=ManagedBy,Value=gypsy-danger-ladder}]" \
  --query 'Instances[0].InstanceId' \
  --no-cli-pager \
  --output text)"

META="$(python3 - <<PY
import json
print(json.dumps({
    "instance_id": "${IID}",
    "worker_id": "${shard_id}",
    "start_offset": int("${START_OFFSET}"),
    "rotation": int("${ROTATION}"),
    "keys_per_worker": int("${KEYS_PER_WORKER}"),
    "max_requests": int("${MAX_REQUESTS}"),
}))
PY
)"
printf '%s\n' "$META" | aws s3 cp - "s3://${BUCKET}/${LOG_PREFIX}/worker_${shard_id}_meta.json"

echo "$IID"
