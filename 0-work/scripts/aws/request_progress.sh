#!/usr/bin/env bash
# Request a progress email — either immediately or via S3 trigger (watcher picks up).
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
  exec "$ROOT/0-work/scripts/aws/progress_email.sh" "on-demand"
fi

# Drop a trigger file; progress_watcher on soak-01 sends email within ~5 min
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) request" | aws s3 cp - \
  "s3://${BUCKET}/manifests/request_progress.trigger" --no-cli-pager
echo "Progress email requested — expect it within ~5 minutes."
echo "For instant email: $0 --now"
