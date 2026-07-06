#!/usr/bin/env bash
# Fetch CFO change announcement PDFs to S3 (headline filter, burn rotation, SNS).
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

ASYNC=0
INCLUDE_TIER_B=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --async) ASYNC=1; shift ;;
    --include-tier-b) INCLUDE_TIER_B=1; shift ;;
    -h|--help)
      cat <<EOF
Usage: run_cfo_changes_fetch.sh [--async] [--include-tier-b]

Fetch CFO change announcements to S3:
  - headline filter (tier A default; tier B with --include-tier-b)
  - S3 path entities/{TICKER}/cfo_changes/{YYYY-MM-DD}_{documentKey}.pdf
  - 10 EC2 workers (default), burn rotation, SNS on completion

Env:
  GYPSY_CFO_FETCH_WORKERS=10
  GYPSY_CFO_INCLUDE_TIER_B=1
  GYPSY_FETCH_RATE_LIMIT_S=1.0
EOF
      exit 0
      ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

WORKERS="${GYPSY_CFO_FETCH_WORKERS:-10}"
export GYPSY_CFO_INCLUDE_TIER_B="${GYPSY_CFO_INCLUDE_TIER_B:-$INCLUDE_TIER_B}"
BUCKET="${GYPSY_S3_BUCKET:?set GYPSY_S3_BUCKET in .env}"
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-cfo-fetch"
LAUNCH="$ROOT/0-work/scripts/aws/launch_cfo_changes_worker.sh"
WAITER="$ROOT/0-work/scripts/aws/cfo_changes_wait_and_notify.sh"
BUILD="$ROOT/0-work/scripts/17_build_cfo_change_shards.py"

TIER_B_FLAG=""
[[ "$GYPSY_CFO_INCLUDE_TIER_B" == "1" ]] && TIER_B_FLAG="--include-tier-b"

echo "==> CFO change fetch ${RUN_ID}"
echo "    Workers: ${WORKERS}"
echo "    Bucket:  s3://${BUCKET}"
echo "    Filter:  tier A${GYPSY_CFO_INCLUDE_TIER_B:+ + tier B}"
echo "    S3 path: entities/{TICKER}/cfo_changes/{cfo_change_date}_{documentKey}.pdf"

python3 "$BUILD" --workers "$WORKERS" $TIER_B_FLAG
TOTAL_DOCS="$(python3 -c "import json; print(json.load(open('$ROOT/data/cfo_changes/manifest.json'))['total_documents'])")"
export GYPSY_CFO_TOTAL_DOCS="${GYPSY_CFO_TOTAL_DOCS:-$TOTAL_DOCS}"
echo "    Targets: ${GYPSY_CFO_TOTAL_DOCS} CFO change PDFs"

aws s3 sync "$ROOT/data/cfo_changes/" "s3://${BUCKET}/cfo_changes/" --only-show-errors
aws s3 cp "$ROOT/0-work/scripts/00_asx_api.py" "s3://${BUCKET}/scripts/00_asx_api.py"
aws s3 cp "$ROOT/0-work/scripts/11_fetch_annual_reports_s3.py" "s3://${BUCKET}/scripts/11_fetch_annual_reports_s3.py"
aws s3 cp "$ROOT/0-work/scripts/16_fetch_cfo_changes_s3.py" "s3://${BUCKET}/scripts/16_fetch_cfo_changes_s3.py"
aws s3 cp "$ROOT/0-work/scripts/18_fetch_cfo_changes_shard.py" "s3://${BUCKET}/scripts/18_fetch_cfo_changes_shard.py"
aws s3 cp "$ROOT/0-work/scripts/aws/launch_cfo_changes_worker.sh" "s3://${BUCKET}/scripts/launch_cfo_changes_worker.sh"
chmod +x "$LAUNCH" "$WAITER"

STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
python3 - <<PY | aws s3 cp - "s3://${BUCKET}/manifests/cfo_fetch_progress.json"
import json
print(json.dumps({
    "status": "running",
    "started_at": "${STARTED_AT}",
    "updated_at": "${STARTED_AT}",
    "run_id": "${RUN_ID}",
    "workers": int("${WORKERS}"),
    "documents_uploaded": 0,
    "documents_done": 0,
    "documents_total": int("${GYPSY_CFO_TOTAL_DOCS}"),
    "errors": 0,
    "include_tier_b": bool(int("${GYPSY_CFO_INCLUDE_TIER_B}")),
}, indent=2))
PY

echo "==> Launching ${WORKERS} workers..."
for w in $(seq 0 $((WORKERS - 1))); do
  IID="$("$LAUNCH" "$RUN_ID" "$w" 0 0 0)"
  echo "  worker $(printf '%02d' "$w"): $IID"
done

SESSION_NAME="cfo-fetch-waiter-${RUN_ID}"
tmux -f /exec-daemon/tmux.portal.conf kill-session -t "$SESSION_NAME" 2>/dev/null || true
tmux -f /exec-daemon/tmux.portal.conf new-session -d -s "$SESSION_NAME" -c "$ROOT" -- "${SHELL:-zsh}" -l
tmux -f /exec-daemon/tmux.portal.conf send-keys -t "$SESSION_NAME:0.0" \
  "export PATH=\"/usr/local/bin:/home/ubuntu/.local/bin:\$PATH\"; set -a && source \"$ENV_FILE\" && set +a; export GYPSY_CFO_TOTAL_DOCS=${GYPSY_CFO_TOTAL_DOCS}; \"$WAITER\" \"$RUN_ID\" \"$WORKERS\" 2>&1 | tee -a \"$ROOT/0-work/scripts/cfo-fetch-waiter.log\"" C-m

echo
echo "=== CFO change fetch started ==="
echo "Run ID:   ${RUN_ID}"
echo "Waiter:   tmux session ${SESSION_NAME}"
echo "Progress: s3://${BUCKET}/manifests/cfo_fetch_progress.json"
echo "Logs:     s3://${BUCKET}/logs/cfo_fetch/${RUN_ID}/"

if [[ "$ASYNC" -eq 1 ]]; then
  echo "Async mode — safe to close."
  exit 0
fi

for _ in $(seq 1 720); do
  if grep -q "SNS notification sent" "$ROOT/0-work/scripts/cfo-fetch-waiter.log" 2>/dev/null; then
    tail -30 "$ROOT/0-work/scripts/cfo-fetch-waiter.log"
    break
  fi
  sleep 10
done
