#!/usr/bin/env bash
# Stage 3A: LiteParse annual reports on EC2 workers → S3 parsed/ prefix.
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
FORCE_SHARDS=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --async) ASYNC=1; shift ;;
    --rebuild-shards) FORCE_SHARDS=1; shift ;;
    -h|--help)
      cat <<EOF
Usage: run_liteparse_parse.sh [--async] [--rebuild-shards]

Stage 3A LiteParse (annual reports):
  - 20 EC2 workers (c6i.large default) — CPU-bound local parse
  - Input:  entities/{TICKER}/annual_reports/*.pdf
  - Output: parsed/{TICKER}/01_annual_reports/{documentKey}/liteparse/...
  - Progress manifest at manifests/parse_progress.json
  - SNS email on completion + milestone watcher (26_parse_progress_watcher.py)
  - On-demand status: aws/request_parse_progress.sh

Env:
  GYPSY_PARSE_WORKERS=20
  GYPSY_PARSE_TOTAL_DOCS=22573
  GYPSY_PARSE_INSTANCE_TYPE=c6i.large
  GYPSY_SNS_TOPIC_ARN=...
EOF
      exit 0
      ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

WORKERS="${GYPSY_PARSE_WORKERS:-20}"
BUCKET="${GYPSY_S3_BUCKET:?set GYPSY_S3_BUCKET in .env}"
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-parse"
LAUNCH="$ROOT/0-work/scripts/aws/launch_liteparse_worker.sh"
WAITER="$ROOT/0-work/scripts/aws/liteparse_wait_and_notify.sh"
BUILD="$ROOT/0-work/scripts/25_build_parse_shards.py"

echo "==> Stage 3A LiteParse ${RUN_ID}"
echo "    Workers: ${WORKERS}"
echo "    Bucket:  s3://${BUCKET}"
echo "    Corpus:  annual_reports (loose filter)"

if [[ "$FORCE_SHARDS" -eq 1 ]] || [[ ! -f "$ROOT/data/parse_3a/manifest.json" ]]; then
  echo "==> Building parse shards..."
  python3 "$BUILD" --workers "$WORKERS"
fi
TOTAL_DOCS="$(python3 -c "import json; print(json.load(open('$ROOT/data/parse_3a/manifest.json'))['documents_total'])")"
export GYPSY_PARSE_TOTAL_DOCS="${GYPSY_PARSE_TOTAL_DOCS:-$TOTAL_DOCS}"
echo "    Targets: ${GYPSY_PARSE_TOTAL_DOCS} annual reports"

aws s3 sync "$ROOT/data/parse_3a/" "s3://${BUCKET}/parse_3a/" --only-show-errors
aws s3 cp "$ROOT/data/catalog/corpus_registry.csv" "s3://${BUCKET}/catalog/corpus_registry.csv" 2>/dev/null || true
aws s3 cp "$ROOT/0-work/scripts/requirements-parse.txt" "s3://${BUCKET}/scripts/requirements-parse.txt"
aws s3 cp "$ROOT/0-work/scripts/00_asx_api.py" "s3://${BUCKET}/scripts/00_asx_api.py"
aws s3 cp "$ROOT/0-work/scripts/11_fetch_annual_reports_s3.py" "s3://${BUCKET}/scripts/11_fetch_annual_reports_s3.py"
aws s3 cp "$ROOT/0-work/scripts/22_liteparse_document.py" "s3://${BUCKET}/scripts/22_liteparse_document.py"
aws s3 cp "$ROOT/0-work/scripts/23_liteparse_shard.py" "s3://${BUCKET}/scripts/23_liteparse_shard.py"
aws s3 cp "$ROOT/0-work/scripts/aws/launch_liteparse_worker.sh" "s3://${BUCKET}/scripts/launch_liteparse_worker.sh"
chmod +x "$LAUNCH" "$WAITER"

STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
python3 - <<PY | aws s3 cp - "s3://${BUCKET}/manifests/parse_progress.json"
import json
print(json.dumps({
    "status": "running",
    "phase": "3a_liteparse",
    "corpus_key": "annual_reports",
    "started_at": "${STARTED_AT}",
    "updated_at": "${STARTED_AT}",
    "run_id": "${RUN_ID}",
    "workers": int("${WORKERS}"),
    "documents_parsed": 0,
    "documents_done": 0,
    "documents_total": int("${GYPSY_PARSE_TOTAL_DOCS}"),
    "pages_parsed": 0,
    "errors": 0,
}, indent=2))
PY

echo "==> Launching ${WORKERS} LiteParse workers..."
for w in $(seq 0 $((WORKERS - 1))); do
  IID="$("$LAUNCH" "$RUN_ID" "$w" 0 0 0)"
  echo "  worker $(printf '%02d' "$w"): $IID"
done

SESSION_NAME="liteparse-waiter-${RUN_ID}"
tmux -f /exec-daemon/tmux.portal.conf kill-session -t "$SESSION_NAME" 2>/dev/null || true
tmux -f /exec-daemon/tmux.portal.conf new-session -d -s "$SESSION_NAME" -c "$ROOT" -- "${SHELL:-zsh}" -l
tmux -f /exec-daemon/tmux.portal.conf send-keys -t "$SESSION_NAME:0.0" \
  "export PATH=\"/usr/local/bin:/home/ubuntu/.local/bin:\$PATH\"; set -a && source \"$ENV_FILE\" && set +a; export GYPSY_PARSE_TOTAL_DOCS=${GYPSY_PARSE_TOTAL_DOCS}; \"$WAITER\" \"$RUN_ID\" \"$WORKERS\" 2>&1 | tee -a \"$ROOT/0-work/scripts/liteparse-waiter.log\"" C-m

echo
echo "=== Stage 3A LiteParse started ==="
echo "Run ID:      ${RUN_ID}"
echo "Workers:     ${WORKERS} instances launched"
echo "Waiter:      tmux session ${SESSION_NAME}"
echo "Progress:    s3://${BUCKET}/manifests/parse_progress.json"
echo "Logs:        s3://${BUCKET}/logs/parse/${RUN_ID}/"
echo "Waiter log:  0-work/scripts/liteparse-waiter.log"
echo "On-demand:   0-work/scripts/aws/request_parse_progress.sh"
if [[ -n "${GYPSY_SNS_TOPIC_ARN:-}" ]]; then
  echo "Notify:      SNS on completion + parse progress watcher"
else
  echo "Notify:      set GYPSY_SNS_TOPIC_ARN for email"
fi

if [[ "$ASYNC" -eq 1 ]]; then
  echo
  echo "Async mode — safe to close. Monitor via request_parse_progress.sh or SNS."
  exit 0
fi

echo "Sync mode — tailing waiter log..."
for _ in $(seq 1 14400); do
  if grep -q "SNS notification sent" "$ROOT/0-work/scripts/liteparse-waiter.log" 2>/dev/null; then
    tail -40 "$ROOT/0-work/scripts/liteparse-waiter.log"
    break
  fi
  sleep 10
done
