#!/usr/bin/env bash
# Create baseline Gypsy Danger AWS resources (S3 + IAM + SG + one soak EC2).
# Idempotent where AWS APIs allow. Requires 0-work/scripts/.env credentials.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
ENV_FILE="$ROOT/0-work/scripts/.env"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$ENV_FILE"
  set +a
fi

export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-ap-southeast-2}"

PROJECT=gypsy-danger
APP=gypsy-danger-asx-fetch
ENV=lab
STAGE=soak
OWNER=niruban
MANAGED_BY=gypsy-danger-bootstrap

ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
BUCKET="${GYPSY_S3_BUCKET:-gypsy-danger-asx-${ACCOUNT}}"
ROLE_NAME=gypsy-danger-fetch-ec2-role
PROFILE_NAME=gypsy-danger-fetch-ec2-profile
SG_NAME=gypsy-danger-fetch-sg
INSTANCE_NAME=gypsy-danger-soak-01

TAGS=(
  "Key=Project,Value=${PROJECT}"
  "Key=Application,Value=${APP}"
  "Key=Environment,Value=${ENV}"
  "Key=Stage,Value=${STAGE}"
  "Key=ManagedBy,Value=${MANAGED_BY}"
  "Key=Owner,Value=${OWNER}"
)

tag_spec() {
  local resource_type=$1
  printf 'ResourceType=%s,' "$resource_type"
  local first=1
  for t in "${TAGS[@]}"; do
    if [[ $first -eq 1 ]]; then first=0; else printf ','; fi
    printf '%s' "$t"
  done
}

echo "==> Account: $ACCOUNT  Region: $AWS_DEFAULT_REGION"
echo "==> Bucket:  $BUCKET"

# --- S3 bucket ---
if aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
  echo "S3 bucket exists: $BUCKET"
else
  echo "Creating S3 bucket: $BUCKET"
  aws s3api create-bucket \
    --bucket "$BUCKET" \
    --region "$AWS_DEFAULT_REGION" \
    --create-bucket-configuration "LocationConstraint=${AWS_DEFAULT_REGION}"
fi

aws s3api put-public-access-block --bucket "$BUCKET" --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

aws s3api put-bucket-encryption --bucket "$BUCKET" --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'

aws s3api put-bucket-tagging --bucket "$BUCKET" --tagging "TagSet=[
  {Key=Project,Value=${PROJECT}},
  {Key=Application,Value=${APP}},
  {Key=Environment,Value=${ENV}},
  {Key=Stage,Value=${STAGE}},
  {Key=ManagedBy,Value=${MANAGED_BY}},
  {Key=Owner,Value=${OWNER}}
]"

echo "S3 bucket ready: s3://${BUCKET}/"

# --- IAM role + instance profile (EC2 → S3, no access keys on instance) ---
TRUST='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}'

if aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
  echo "IAM role exists: $ROLE_NAME"
else
  echo "Creating IAM role: $ROLE_NAME"
  aws iam create-role --role-name "$ROLE_NAME" --assume-role-policy-document "$TRUST"
fi

ROLE_POLICY="$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:ListBucket"],
      "Resource": "arn:aws:s3:::${BUCKET}"
    },
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:HeadObject", "s3:DeleteObject"],
      "Resource": "arn:aws:s3:::${BUCKET}/*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "ssm:UpdateInstanceInformation",
        "ssmmessages:CreateControlChannel",
        "ssmmessages:CreateDataChannel",
        "ssmmessages:OpenControlChannel",
        "ssmmessages:OpenDataChannel"
      ],
      "Resource": "*"
    }
  ]
}
EOF
)"
aws iam put-role-policy --role-name "$ROLE_NAME" --policy-name gypsy-danger-fetch-s3-ssm \
  --policy-document "$ROLE_POLICY"

if aws iam get-instance-profile --instance-profile-name "$PROFILE_NAME" >/dev/null 2>&1; then
  echo "Instance profile exists: $PROFILE_NAME"
else
  echo "Creating instance profile: $PROFILE_NAME"
  aws iam create-instance-profile --instance-profile-name "$PROFILE_NAME"
  aws iam add-role-to-instance-profile --instance-profile-name "$PROFILE_NAME" \
    --role-name "$ROLE_NAME"
  echo "Waiting for instance profile propagation..."
  sleep 10
fi

# --- Security group (egress HTTPS only) ---
VPC_ID="$(aws ec2 describe-vpcs --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId' --output text)"
SG_ID="$(aws ec2 describe-security-groups --filters "Name=group-name,Values=${SG_NAME}" "Name=vpc-id,Values=${VPC_ID}" --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || true)"

if [[ -z "$SG_ID" || "$SG_ID" == "None" ]]; then
  echo "Creating security group: $SG_NAME"
  SG_ID="$(aws ec2 create-security-group --group-name "$SG_NAME" --description "Gypsy Danger fetch workers - egress only" --vpc-id "$VPC_ID" --query GroupId --output text)"
  aws ec2 create-tags --resources "$SG_ID" --tags "${TAGS[@]}" "Key=Name,Value=${SG_NAME}"
  aws ec2 authorize-security-group-egress --group-id "$SG_ID" --ip-permissions \
    IpProtocol=tcp,FromPort=443,ToPort=443,IpRanges='[{CidrIp=0.0.0.0/0,Description=HTTPS}]' || true
  aws ec2 authorize-security-group-egress --group-id "$SG_ID" --ip-permissions \
    IpProtocol=udp,FromPort=443,ToPort=443,IpRanges='[{CidrIp=0.0.0.0/0,Description=HTTPS-QUIC}]' || true
else
  echo "Security group exists: $SG_ID ($SG_NAME)"
fi

# --- Soak EC2 (skip if one already running with our Name tag) ---
EXISTING="$(aws ec2 describe-instances \
  --filters "Name=tag:Name,Values=${INSTANCE_NAME}" "Name=instance-state-name,Values=pending,running,stopping,stopped" \
  --query 'Reservations[0].Instances[0].InstanceId' --output text 2>/dev/null || true)"

if [[ -n "$EXISTING" && "$EXISTING" != "None" ]]; then
  echo "Soak instance already exists: $EXISTING ($INSTANCE_NAME)"
else
  AMI="$(aws ec2 describe-images --owners amazon \
    --filters 'Name=name,Values=al2023-ami-2023*-x86_64' 'Name=state,Values=available' \
    --query 'sort_by(Images,&CreationDate)[-1].ImageId' --output text)"
  SUBNET="$(aws ec2 describe-subnets --filters Name=default-for-az,Values=true Name=vpc-id,Values="$VPC_ID" --query 'Subnets[0].SubnetId' --output text)"

  USER_DATA="$(cat <<'USERDATA'
#!/bin/bash
set -euxo pipefail
dnf install -y python3 git aws-cli
mkdir -p /opt/gypsy-danger
cat > /etc/profile.d/gypsy-danger.sh <<'ENV'
export GYPSY_S3_BUCKET=__BUCKET__
export AWS_DEFAULT_REGION=ap-southeast-2
ENV
USERDATA
)"
  USER_DATA="${USER_DATA/__BUCKET__/$BUCKET}"

  echo "Launching soak EC2: $INSTANCE_NAME (ami=$AMI)"
  INSTANCE_ID="$(aws ec2 run-instances \
    --image-id "$AMI" \
    --instance-type t3.small \
    --iam-instance-profile "Name=${PROFILE_NAME}" \
    --security-group-ids "$SG_ID" \
    --subnet-id "$SUBNET" \
    --user-data "$USER_DATA" \
    --metadata-options HttpTokens=required \
    --block-device-mappings '[{"DeviceName":"/dev/xvda","Ebs":{"VolumeSize":20,"VolumeType":"gp3","DeleteOnTermination":true}}]' \
    --tag-specifications \
      "ResourceType=instance,Tags=[{Key=Name,Value=${INSTANCE_NAME}},{Key=Project,Value=${PROJECT}},{Key=Application,Value=${APP}},{Key=Environment,Value=${ENV}},{Key=Stage,Value=${STAGE}},{Key=ManagedBy,Value=${MANAGED_BY}},{Key=Owner,Value=${OWNER}}]" \
    --query 'Instances[0].InstanceId' --output text)"
  echo "Launched: $INSTANCE_ID"
fi

# --- Write env hint for local scripts ---
if [[ -f "$ENV_FILE" ]] && ! grep -q '^GYPSY_S3_BUCKET=' "$ENV_FILE" 2>/dev/null; then
  # Ensure previous line ends with newline (avoid concatenating onto AWS_SECRET_ACCESS_KEY)
  last_char="$(tail -c1 "$ENV_FILE" 2>/dev/null || true)"
  if [[ -n "$last_char" && "$last_char" != $'\n' ]]; then
    echo >> "$ENV_FILE"
  fi
  echo "GYPSY_S3_BUCKET=${BUCKET}" >> "$ENV_FILE"
  echo "Appended GYPSY_S3_BUCKET to $ENV_FILE"
fi

cat <<SUMMARY

=== Gypsy Danger baseline ready ===
Bucket:     s3://${BUCKET}/
IAM role:   ${ROLE_NAME}
Profile:    ${PROFILE_NAME}
SG:         ${SG_NAME} (${SG_ID})
Soak VM:    ${INSTANCE_NAME}

Next:
  1. aws s3 sync data/entities/ s3://${BUCKET}/entities/ --exclude '*/raw/*' --include '*_Announcements.csv'
  2. SSM or user-data: run 07_cdn_soak_test.py on soak instance
  3. See 0-work/docs/aws-naming.md

SUMMARY
