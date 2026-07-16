#!/usr/bin/env bash
# Ensure 0-work/scripts/.env exists — from file, or build from injected env vars (Cursor secrets).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
ENV_FILE="$ROOT/0-work/scripts/.env"
EXAMPLE="$ROOT/0-work/scripts/.env.example"

if [[ -f "$ENV_FILE" ]]; then
  exit 0
fi

# Build from environment (Cursor Cloud Agent secrets use these exact names).
missing=()
[[ -z "${AWS_ACCESS_KEY_ID:-}" ]] && missing+=("AWS_ACCESS_KEY_ID")
[[ -z "${AWS_SECRET_ACCESS_KEY:-}" ]] && missing+=("AWS_SECRET_ACCESS_KEY")

if [[ ${#missing[@]} -gt 0 ]]; then
  echo "error: ${ENV_FILE} missing and env vars not set: ${missing[*]}" >&2
  echo "Add to Cursor Cloud Agent secrets (or create ${ENV_FILE}):" >&2
  echo "  AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_DEFAULT_REGION=ap-southeast-2" >&2
  echo "  GYPSY_S3_BUCKET, GYPSY_SNS_TOPIC_ARN, GYPSY_NOTIFY_EMAIL" >&2
  echo "See ${EXAMPLE}" >&2
  exit 1
fi

{
  echo "AWS_ACCESS_KEY_ID=${AWS_ACCESS_KEY_ID}"
  echo "AWS_SECRET_ACCESS_KEY=${AWS_SECRET_ACCESS_KEY}"
  echo "AWS_DEFAULT_REGION=${AWS_DEFAULT_REGION:-ap-southeast-2}"
  [[ -n "${GYPSY_S3_BUCKET:-}" ]] && echo "GYPSY_S3_BUCKET=${GYPSY_S3_BUCKET}"
  [[ -n "${GYPSY_SNS_TOPIC_ARN:-}" ]] && echo "GYPSY_SNS_TOPIC_ARN=${GYPSY_SNS_TOPIC_ARN}"
  [[ -n "${GYPSY_NOTIFY_EMAIL:-}" ]] && echo "GYPSY_NOTIFY_EMAIL=${GYPSY_NOTIFY_EMAIL}"
  [[ -n "${GYPSY_SOAK_INSTANCE_ID:-}" ]] && echo "GYPSY_SOAK_INSTANCE_ID=${GYPSY_SOAK_INSTANCE_ID}"
  echo
} > "$ENV_FILE"

chmod 600 "$ENV_FILE"
echo "Created ${ENV_FILE} from environment variables"
