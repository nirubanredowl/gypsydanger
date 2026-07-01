#!/usr/bin/env bash
# Create SNS topic for job-completion emails and grant EC2 role publish access.
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

TOPIC_NAME=gypsy-danger-notify
ROLE_NAME=gypsy-danger-fetch-ec2-role
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
TOPIC_ARN="arn:aws:sns:${AWS_DEFAULT_REGION}:${ACCOUNT}:${TOPIC_NAME}"

echo "==> SNS topic: $TOPIC_ARN"

if aws sns get-topic-attributes --topic-arn "$TOPIC_ARN" >/dev/null 2>&1; then
  echo "Topic exists"
else
  TOPIC_ARN="$(aws sns create-topic --name "$TOPIC_NAME" --query TopicArn --output text)"
  echo "Created: $TOPIC_ARN"
fi

if [[ -n "${GYPSY_NOTIFY_EMAIL:-}" ]]; then
  echo "==> Subscribing $GYPSY_NOTIFY_EMAIL (confirm via inbox link)"
  aws sns subscribe \
    --topic-arn "$TOPIC_ARN" \
    --protocol email \
    --notification-endpoint "$GYPSY_NOTIFY_EMAIL" \
    --no-cli-pager \
    --output text || true
else
  echo "Set GYPSY_NOTIFY_EMAIL in .env to subscribe your address"
fi

# EC2 instance role may publish completion messages
SNS_POLICY="$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["sns:Publish"],
      "Resource": "${TOPIC_ARN}"
    }
  ]
}
EOF
)"
aws iam put-role-policy \
  --role-name "$ROLE_NAME" \
  --policy-name gypsy-danger-sns-notify \
  --policy-document "$SNS_POLICY"
echo "IAM role $ROLE_NAME may Publish to SNS"

# Persist for local scripts + EC2 profile
if [[ -f "$ENV_FILE" ]]; then
  last_char="$(tail -c1 "$ENV_FILE" 2>/dev/null || true)"
  if [[ -n "$last_char" && "$last_char" != $'\n' ]]; then
    echo >> "$ENV_FILE"
  fi
  if ! grep -q '^GYPSY_SNS_TOPIC_ARN=' "$ENV_FILE" 2>/dev/null; then
    echo "GYPSY_SNS_TOPIC_ARN=${TOPIC_ARN}" >> "$ENV_FILE"
  fi
fi

# Push topic ARN to soak instance env (best-effort via SSM)
SOAK_ID="${GYPSY_SOAK_INSTANCE_ID:-i-0812f82dd21298e96}"
PATCH="grep -q GYPSY_SNS_TOPIC_ARN /etc/profile.d/gypsy-danger.sh 2>/dev/null || echo 'export GYPSY_SNS_TOPIC_ARN=${TOPIC_ARN}' >> /etc/profile.d/gypsy-danger.sh"
B64="$(printf '%s' "$PATCH" | base64 -w0)"
if aws ssm describe-instance-information \
  --filters "Key=InstanceIds,Values=${SOAK_ID}" \
  --query 'InstanceInformationList[0].PingStatus' --output text 2>/dev/null | grep -q Online; then
  CID="$(aws ssm send-command \
    --instance-ids "$SOAK_ID" \
    --document-name AWS-RunShellScript \
    --parameters "commands=[\"echo $B64 | base64 -d | bash\"]" \
    --query Command.CommandId --output text)"
  echo "Patched soak instance profile (SSM $CID)"
fi

cat <<SUMMARY

=== Notifications ready ===
Topic:  ${TOPIC_ARN}
Email:  ${GYPSY_NOTIFY_EMAIL:-(set GYPSY_NOTIFY_EMAIL in .env and re-run)}

Confirm the SNS subscription email before expecting notifications.

Test:
  0-work/scripts/aws/notify_sns.sh "Gypsy Danger test" "Notifications work."

SUMMARY
