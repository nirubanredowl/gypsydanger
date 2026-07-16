#!/usr/bin/env bash
# Send an immediate Stage 3A parse progress email via SNS.
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

REASON="${1:-manual check}"
REPORT="$(python3 "$ROOT/0-work/scripts/26_parse_progress.py")"
SUBJECT="Gypsy Danger parse progress — ${REASON}"

"$ROOT/0-work/scripts/aws/notify_sns.sh" "$SUBJECT" "$REPORT"
echo "Parse progress email sent (${REASON})."
