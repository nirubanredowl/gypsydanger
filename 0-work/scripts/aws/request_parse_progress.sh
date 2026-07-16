#!/usr/bin/env bash
# Request a parse progress email — immediately or via S3 trigger (watcher picks up).
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

BUCKET="${GYPSY_S3_BUCKET:?set GYPSY_S3_BUCKET}"

if [[ "${1:-}" == "--now" ]]; then
  exec "$ROOT/0-work/scripts/aws/parse_progress_email.sh" "on-demand"
fi

echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) request" | aws s3 cp - \
  "s3://${BUCKET}/manifests/request_parse_progress.trigger" --no-cli-pager
echo "Parse progress email requested — expect it within ~5 minutes."
echo "For instant email: $0 --now"
echo "Or read manifest:  aws s3 cp s3://${BUCKET}/manifests/parse_progress.json -"
