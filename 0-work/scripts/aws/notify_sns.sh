#!/usr/bin/env bash
# Publish a notification to GYPSY_SNS_TOPIC_ARN (email subscription).
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

SUBJECT="${1:?subject required}"
MESSAGE="${2:?message required}"

if [[ -z "${GYPSY_SNS_TOPIC_ARN:-}" ]]; then
  echo "GYPSY_SNS_TOPIC_ARN not set — run bootstrap_notifications.sh first" >&2
  exit 1
fi

aws sns publish \
  --topic-arn "$GYPSY_SNS_TOPIC_ARN" \
  --subject "$SUBJECT" \
  --message "$MESSAGE" \
  --no-cli-pager \
  --output text
