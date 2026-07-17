#!/usr/bin/env bash
# Stage 3B: Page split annual reports on EC2 workers (requires 3A LiteParse on S3).
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
Usage: run_split_pages.sh [--async]

Stage 3B page split (annual reports with 3A complete):
  - 20 EC2 workers (c6i.large default)
  - Input:  entities/{TICKER}/annual_reports/*.pdf + parsed/.../liteparse/
  - Output: parsed/{TICKER}/01_annual_reports/{doc}/pages/{NNNN}/page.pdf|png|liteparse.*
  - Progress: manifests/split_progress.json
  - SNS email on completion

Env:
  GYPSY_SPLIT_WORKERS=20
  GYPSY_SPLIT_TOTAL_DOCS=22570
  GYPSY_SPLIT_INSTANCE_TYPE=c6i.large
  GYPSY_SNS_TOPIC_ARN=...
EOF
      exit 0
      ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

WORKERS="${GYPSY_SPLIT_WORKERS:-20}"
BUCKET="${GYPSY_S3_BUCKET:?set GYPSY_S3_BUCKET in .env}"
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-split"
LAUNCH="$ROOT/0-work/scripts/aws/launch_split_worker.sh"
WAITER="$ROOT/0-work/scripts/aws/split_wait_and_notify.sh"
ENRICH_MANIFEST="$ROOT/data/manifests/parse_enrich_progress.json"

if [[ -f "$ENRICH_MANIFEST" ]]; then
  GYPSY_SPLIT_TOTAL_DOCS="${GYPSY_SPLIT_TOTAL_DOCS:-$(python3 -c "import json; print(json.load(open('$ENRICH_MANIFEST')).get('3b_total', 22570))")}"
else
  GYPSY_SPLIT_TOTAL_DOCS="${GYPSY_SPLIT_TOTAL_DOCS:-22570}"
fi
export GYPSY_SPLIT_TOTAL_DOCS

echo "==> Stage 3B Page Split ${RUN_ID}"
echo "    Workers: ${WORKERS}"
echo "    Bucket:  s3://${BUCKET}"
echo "    Targets: ${GYPSY_SPLIT_TOTAL_DOCS} documents (3A complete, no pages/ yet)"

aws s3 sync "$ROOT/data/parse_3a/" "s3://${BUCKET}/parse_3a/" --only-show-errors
aws s3 cp "$ROOT/0-work/scripts/requirements-parse.txt" "s3://${BUCKET}/scripts/requirements-parse.txt"
aws s3 cp "$ROOT/0-work/scripts/00_asx_api.py" "s3://${BUCKET}/scripts/00_asx_api.py"
aws s3 cp "$ROOT/0-work/scripts/11_fetch_annual_reports_s3.py" "s3://${BUCKET}/scripts/11_fetch_annual_reports_s3.py"
aws s3 cp "$ROOT/0-work/scripts/24_split_pdf_pages.py" "s3://${BUCKET}/scripts/24_split_pdf_pages.py"
aws s3 cp "$ROOT/0-work/scripts/25_split_shard.py" "s3://${BUCKET}/scripts/25_split_shard.py"
aws s3 cp "$ROOT/0-work/scripts/aws/launch_split_worker.sh" "s3://${BUCKET}/scripts/launch_split_worker.sh"
chmod +x "$LAUNCH" "$WAITER"

STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
python3 - <<PY | aws s3 cp - "s3://${BUCKET}/manifests/split_progress.json"
import json
print(json.dumps({
    "status": "running",
    "phase": "3b_split",
    "corpus_key": "annual_reports",
    "started_at": "${STARTED_AT}",
    "updated_at": "${STARTED_AT}",
    "run_id": "${RUN_ID}",
    "workers": int("${WORKERS}"),
    "documents_split": 0,
    "documents_done": 0,
    "documents_total": int("${GYPSY_SPLIT_TOTAL_DOCS}"),
    "pages_split": 0,
    "errors": 0,
}, indent=2))
PY

echo "==> Launching ${WORKERS} split workers..."
for w in $(seq 0 $((WORKERS - 1))); do
  IID="$("$LAUNCH" "$RUN_ID" "$w" 0 0 0)"
  echo "  worker $(printf '%02d' "$w"): $IID"
done

SESSION_NAME="split-waiter-${RUN_ID}"
tmux -f /exec-daemon/tmux.portal.conf kill-session -t "$SESSION_NAME" 2>/dev/null || true
tmux -f /exec-daemon/tmux.portal.conf new-session -d -s "$SESSION_NAME" -c "$ROOT" -- "${SHELL:-zsh}" -l
tmux -f /exec-daemon/tmux.portal.conf send-keys -t "$SESSION_NAME:0.0" \
  "export PATH=\"/usr/local/bin:/home/ubuntu/.local/bin:\$PATH\"; set -a && source \"$ENV_FILE\" && set +a; export GYPSY_SPLIT_TOTAL_DOCS=${GYPSY_SPLIT_TOTAL_DOCS}; \"$WAITER\" \"$RUN_ID\" \"$WORKERS\" 2>&1 | tee -a \"$ROOT/0-work/scripts/split-waiter.log\"" C-m

echo
echo "=== Stage 3B Page Split started ==="
echo "Run ID:      ${RUN_ID}"
echo "Workers:     ${WORKERS} instances launched"
echo "Waiter:      tmux session ${SESSION_NAME}"
echo "Progress:    s3://${BUCKET}/manifests/split_progress.json"
echo "Logs:        s3://${BUCKET}/logs/split/${RUN_ID}/"
echo "Waiter log:  0-work/scripts/split-waiter.log"

if [[ "$ASYNC" -eq 1 ]]; then
  echo
  echo "Async mode — safe to close."
  exit 0
fi

echo "Sync mode — tailing waiter log..."
for _ in $(seq 1 14400); do
  if grep -q "SNS notification sent" "$ROOT/0-work/scripts/split-waiter.log" 2>/dev/null; then
    tail -40 "$ROOT/0-work/scripts/split-waiter.log"
    break
  fi
  sleep 10
done
