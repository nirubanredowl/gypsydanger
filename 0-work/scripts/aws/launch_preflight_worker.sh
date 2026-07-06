#!/usr/bin/env bash
# Launch one preflight fetch worker EC2 instance.
set -euo pipefail

RUN_ID="${1:?run id}"
WORKER_ID="${2:?worker id 0-based}"
TICKER="${3:?ticker}"
MAX_REPORTS="${4:?max reports}"
START_OFFSET="${5:-0}"
ROTATION="${6:-0}"
SIMULATE_BURN="${7:-0}"

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

BUCKET="${GYPSY_S3_BUCKET:?set GYPSY_S3_BUCKET}"
LOG_PREFIX="logs/preflight/${RUN_ID}"
shard_id="$(printf '%02d' "$WORKER_ID")"
name="gypsy-danger-preflight-w${shard_id}"
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

SIM_ARG=""
if [[ "$SIMULATE_BURN" -gt 0 && "$ROTATION" -eq 0 ]]; then
  SIM_ARG="--simulate-burn-after ${SIMULATE_BURN}"
fi

user_data="$(cat <<USERDATA
#!/bin/bash
set -euxo pipefail
exec > /var/log/gypsy-preflight.log 2>&1
export GYPSY_S3_BUCKET=${BUCKET}
export AWS_DEFAULT_REGION=${AWS_DEFAULT_REGION}
export GYPSY_SNS_TOPIC_ARN=${GYPSY_SNS_TOPIC_ARN:-}
export GYPSY_PREFLIGHT_RUN_ID=${RUN_ID}
export GYPSY_WORKER_ID=${shard_id}
cat > /etc/profile.d/gypsy-danger.sh <<ENV
export GYPSY_S3_BUCKET=${BUCKET}
export AWS_DEFAULT_REGION=${AWS_DEFAULT_REGION}
export GYPSY_SNS_TOPIC_ARN=${GYPSY_SNS_TOPIC_ARN:-}
ENV
dnf install -y python3 aws-cli
ROOT=/opt/gypsy-danger
mkdir -p "\$ROOT/0-work/scripts"
aws s3 cp "s3://${BUCKET}/scripts/00_asx_api.py" "\$ROOT/0-work/scripts/"
aws s3 cp "s3://${BUCKET}/scripts/11_fetch_annual_reports_s3.py" "\$ROOT/0-work/scripts/"
cd "\$ROOT/0-work/scripts"
set +e
python3 11_fetch_annual_reports_s3.py \\
  --ticker ${TICKER} \\
  --bucket ${BUCKET} \\
  --run-id ${RUN_ID} \\
  --worker-id ${shard_id} \\
  --max-reports ${MAX_REPORTS} \\
  --start-offset ${START_OFFSET} \\
  --rotation ${ROTATION} \\
  --annual-filter loose \\
  --no-cache \\
  ${SIM_ARG} \\
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
    "ResourceType=instance,Tags=[{Key=Name,Value=${name}},{Key=Project,Value=gypsy-danger},{Key=Application,Value=gypsy-danger-asx-fetch},{Key=Stage,Value=preflight},{Key=PreflightRunId,Value=${RUN_ID}},{Key=PreflightWorkerId,Value=${shard_id}},{Key=PreflightTicker,Value=${TICKER}},{Key=PreflightRotation,Value=${ROTATION}},{Key=ManagedBy,Value=gypsy-danger-preflight}]" \
  --query 'Instances[0].InstanceId' \
  --no-cli-pager \
  --output text)"

META="$(python3 - <<PY
import json
print(json.dumps({
    "instance_id": "${IID}",
    "worker_id": "${shard_id}",
    "ticker": "${TICKER}",
    "max_reports": int("${MAX_REPORTS}"),
    "start_offset": int("${START_OFFSET}"),
    "rotation": int("${ROTATION}"),
    "simulate_burn_after": int("${SIMULATE_BURN}"),
}))
PY
)"
printf '%s\n' "$META" | aws s3 cp - "s3://${BUCKET}/${LOG_PREFIX}/worker_${shard_id}_meta.json"

echo "$IID"
