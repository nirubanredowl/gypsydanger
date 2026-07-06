#!/usr/bin/env bash
# Phase C: full loose annual-report fetch to S3 (20 workers, burn rotation, SNS on completion).
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
while [[ $# -gt 0 ]]; do
  case "$1" in
    --async) ASYNC=1; shift ;;
    -h|--help)
      cat <<EOF
Usage: run_phase_c_fetch.sh [--async]

Full Phase C fetch:
  - loose annual report filter (~22.6k PDFs across ~1,838 tickers)
  - 20 EC2 workers (ladder rung 4 fleet size)
  - S3 path entities/{TICKER}/annual_reports/{YYYY}_{documentKey}.pdf
  - burned EC2 replacement via local waiter
  - progress manifest at manifests/fetch_progress.json
  - SNS email on completion + milestone watcher on soak-01

Env:
  GYPSY_PHASE_C_WORKERS=20
  GYPSY_TOTAL_PDFS=22573
  GYPSY_FETCH_RATE_LIMIT_S=1.0
EOF
      exit 0
      ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

WORKERS="${GYPSY_PHASE_C_WORKERS:-20}"
BUCKET="${GYPSY_S3_BUCKET:?set GYPSY_S3_BUCKET in .env}"
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-fetch"
LAUNCH="$ROOT/0-work/scripts/aws/launch_phase_c_worker.sh"
WAITER="$ROOT/0-work/scripts/aws/phase_c_wait_and_notify.sh"
BUILD="$ROOT/0-work/scripts/12_build_phase_c_shards.py"

echo "==> Phase C fetch ${RUN_ID}"
echo "    Workers: ${WORKERS}"
echo "    Bucket:  s3://${BUCKET}"
echo "    Filter:  loose annual reports"

# Build balanced ticker shards
if [[ ! -f "$ROOT/data/phase_c/manifest.json" ]]; then
  echo "==> Building Phase C shards..."
  python3 "$BUILD" --workers "$WORKERS"
fi
TOTAL_PDFS="$(python3 -c "import json; print(json.load(open('$ROOT/data/phase_c/manifest.json'))['total_reports'])")"
export GYPSY_TOTAL_PDFS="${GYPSY_TOTAL_PDFS:-$TOTAL_PDFS}"
echo "    Targets: ${GYPSY_TOTAL_PDFS} loose annual reports"

# Upload scripts, shards, and initial progress manifest
aws s3 sync "$ROOT/data/phase_c/" "s3://${BUCKET}/phase_c/" --only-show-errors
aws s3 cp "$ROOT/0-work/scripts/00_asx_api.py" "s3://${BUCKET}/scripts/00_asx_api.py"
aws s3 cp "$ROOT/0-work/scripts/11_fetch_annual_reports_s3.py" "s3://${BUCKET}/scripts/11_fetch_annual_reports_s3.py"
aws s3 cp "$ROOT/0-work/scripts/12_fetch_phase_c_shard.py" "s3://${BUCKET}/scripts/12_fetch_phase_c_shard.py"
aws s3 cp "$ROOT/0-work/scripts/aws/launch_phase_c_worker.sh" "s3://${BUCKET}/scripts/launch_phase_c_worker.sh"
chmod +x "$LAUNCH" "$WAITER"

STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
python3 - <<PY | aws s3 cp - "s3://${BUCKET}/manifests/fetch_progress.json"
import json
print(json.dumps({
    "status": "running",
    "started_at": "${STARTED_AT}",
    "updated_at": "${STARTED_AT}",
    "run_id": "${RUN_ID}",
    "workers": int("${WORKERS}"),
    "pdfs_uploaded": 0,
    "pdfs_done": 0,
    "pdfs_total": int("${GYPSY_TOTAL_PDFS}"),
    "errors": 0,
    "annual_filter": "loose",
}, indent=2))
PY

echo "==> Launching ${WORKERS} fetch workers..."
LAUNCHED=()
for w in $(seq 0 $((WORKERS - 1))); do
  IID="$("$LAUNCH" "$RUN_ID" "$w" 0 0 0)"
  echo "  worker $(printf '%02d' "$w"): $IID"
  LAUNCHED+=("$IID")
done

echo "==> Starting local waiter (EC2 rotation + progress updates)..."
SESSION_NAME="phase-c-waiter-${RUN_ID}"
tmux -f /exec-daemon/tmux.portal.conf kill-session -t "$SESSION_NAME" 2>/dev/null || true
tmux -f /exec-daemon/tmux.portal.conf new-session -d -s "$SESSION_NAME" -c "$ROOT" -- "${SHELL:-zsh}" -l
tmux -f /exec-daemon/tmux.portal.conf send-keys -t "$SESSION_NAME:0.0" \
  "export PATH=\"/usr/local/bin:/home/ubuntu/.local/bin:\$PATH\"; set -a && source \"$ENV_FILE\" && set +a; export GYPSY_TOTAL_PDFS=${GYPSY_TOTAL_PDFS}; \"$WAITER\" \"$RUN_ID\" \"$WORKERS\" 2>&1 | tee -a \"$ROOT/0-work/scripts/phase-c-waiter.log\"" C-m

echo
echo "=== Phase C fetch started ==="
echo "Run ID:      ${RUN_ID}"
echo "Workers:     ${WORKERS} instances launched"
echo "Waiter:      local tmux session ${SESSION_NAME}"
echo "Progress:    s3://${BUCKET}/manifests/fetch_progress.json"
echo "Logs:        s3://${BUCKET}/logs/fetch/${RUN_ID}/"
echo "Waiter log:  0-work/scripts/phase-c-waiter.log"
echo "On-demand:   0-work/scripts/aws/progress_email.sh"
if [[ -n "${GYPSY_SNS_TOPIC_ARN:-}" ]]; then
  echo "Notify:      SNS on completion + milestone watcher on soak-01"
else
  echo "Notify:      set GYPSY_SNS_TOPIC_ARN for email"
fi

if [[ "$ASYNC" -eq 1 ]]; then
  echo
  echo "Async mode — safe to close. Monitor via progress_email.sh or SNS milestones."
  exit 0
fi

echo "Sync mode — tailing waiter log..."
for _ in $(seq 1 7200); do
  if grep -q "SNS notification sent" "$ROOT/0-work/scripts/phase-c-waiter.log" 2>/dev/null; then
    tail -40 "$ROOT/0-work/scripts/phase-c-waiter.log"
    break
  fi
  sleep 10
done
