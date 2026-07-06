#!/usr/bin/env bash
# Launch one Phase C annual-report fetch worker EC2 instance.
set -euo pipefail

RUN_ID="${1:?run id}"
WORKER_ID="${2:?worker id 0-based}"
TICKER_INDEX="${3:-0}"
START_OFFSET="${4:-0}"
ROTATION="${5:-0}"

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

WORKERS="${GYPSY_PHASE_C_WORKERS:-20}"
BUCKET="${GYPSY_S3_BUCKET:?set GYPSY_S3_BUCKET}"
RATE_LIMIT_S="${GYPSY_FETCH_RATE_LIMIT_S:-1.0}"
LOG_PREFIX="logs/fetch/${RUN_ID}"
shard_id="$(printf '%02d' "$WORKER_ID")"
name="gypsy-danger-fetch-w${shard_id}"
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
exec > /var/log/gypsy-fetch.log 2>&1
export GYPSY_S3_BUCKET=${BUCKET}
export AWS_DEFAULT_REGION=${AWS_DEFAULT_REGION}
export GYPSY_SNS_TOPIC_ARN=${GYPSY_SNS_TOPIC_ARN:-}
export GYPSY_FETCH_RUN_ID=${RUN_ID}
export GYPSY_WORKER_ID=${shard_id}
cat > /etc/profile.d/gypsy-danger.sh <<ENV
export GYPSY_S3_BUCKET=${BUCKET}
export AWS_DEFAULT_REGION=${AWS_DEFAULT_REGION}
export GYPSY_SNS_TOPIC_ARN=${GYPSY_SNS_TOPIC_ARN:-}
ENV
dnf install -y python3 aws-cli
ROOT=/opt/gypsy-danger
mkdir -p "\$ROOT/0-work/scripts" "\$ROOT/data/phase_c"
aws s3 cp "s3://${BUCKET}/scripts/00_asx_api.py" "\$ROOT/0-work/scripts/"
aws s3 cp "s3://${BUCKET}/scripts/11_fetch_annual_reports_s3.py" "\$ROOT/0-work/scripts/"
aws s3 cp "s3://${BUCKET}/scripts/12_fetch_phase_c_shard.py" "\$ROOT/0-work/scripts/"
aws s3 cp "s3://${BUCKET}/phase_c/shards/${WORKERS}workers/shard_${shard_id}.txt" "\$ROOT/data/phase_c/shard.txt"
cd "\$ROOT/0-work/scripts"
set +e
python3 12_fetch_phase_c_shard.py \\
  --tickers-file "\$ROOT/data/phase_c/shard.txt" \\
  --bucket ${BUCKET} \\
  --run-id ${RUN_ID} \\
  --worker-id ${shard_id} \\
  --ticker-index ${TICKER_INDEX} \\
  --start-offset ${START_OFFSET} \\
  --rotation ${ROTATION} \\
  --annual-filter loose \\
  --rate-limit-s ${RATE_LIMIT_S} \\
  --no-cache \\
  --progress-s3-uri s3://${BUCKET}/${LOG_PREFIX}/worker_${shard_id}.json \\
  --result-json /tmp/result.json \\
  2>&1 | tee /tmp/fetch.log
EXIT=\$?
set -e
aws s3 cp /tmp/result.json "s3://${BUCKET}/${LOG_PREFIX}/worker_${shard_id}.json" || true
aws s3 cp /tmp/fetch.log "s3://${BUCKET}/${LOG_PREFIX}/worker_${shard_id}.log" || true
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
    "ResourceType=instance,Tags=[{Key=Name,Value=${name}},{Key=Project,Value=gypsy-danger},{Key=Application,Value=gypsy-danger-asx-fetch},{Key=Stage,Value=fetch},{Key=FetchRunId,Value=${RUN_ID}},{Key=FetchWorkerId,Value=${shard_id}},{Key=FetchRotation,Value=${ROTATION}},{Key=ManagedBy,Value=gypsy-danger-fetch}]" \
  --query 'Instances[0].InstanceId' \
  --no-cli-pager \
  --output text)"

META="$(python3 - <<PY
import json
print(json.dumps({
    "instance_id": "${IID}",
    "worker_id": "${shard_id}",
    "ticker_index": int("${TICKER_INDEX}"),
    "start_offset": int("${START_OFFSET}"),
    "rotation": int("${ROTATION}"),
    "workers": int("${WORKERS}"),
}))
PY
)"
printf '%s\n' "$META" | aws s3 cp - "s3://${BUCKET}/${LOG_PREFIX}/worker_${shard_id}_meta.json"

echo "$IID"
