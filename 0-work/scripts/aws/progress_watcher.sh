#!/usr/bin/env bash
# Run progress event watcher once (intended for cron every 5 min on soak-01).
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
python3 "$ROOT/0-work/scripts/09_progress_watcher.py"
