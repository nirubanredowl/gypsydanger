#!/usr/bin/env bash
# End-to-end preflight: loose annual reports → S3, burn rotation, SNS email.
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
Usage: run_preflight_fetch.sh [--async]

Preflight validates before Phase C:
  - loose annual report filter
  - S3 layout entities/{TICKER}/annual_reports/{YYYY}_{documentKey}.pdf
  - burned EC2 replacement (worker 01 simulates burn after 2 uploads)
  - SNS email summary

Workers:
  w00 → CBA, 5 loose annual reports
  w01 → QGL, 5 loose annual reports (simulate burn → rotate → finish)
EOF
      exit 0
      ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

BUCKET="${GYPSY_S3_BUCKET:?set GYPSY_S3_BUCKET in .env}"
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-preflight"
WORKERS=2
LAUNCH="$ROOT/0-work/scripts/aws/launch_preflight_worker.sh"
WAITER="$ROOT/0-work/scripts/aws/preflight_wait_and_notify.sh"

echo "==> Preflight run ${RUN_ID}"
echo "    Bucket: s3://${BUCKET}"
echo "    PDF path: entities/{TICKER}/annual_reports/{YYYY}_{documentKey}.pdf"

# Upload scripts + ensure index CSVs for CBA and QGL exist on S3
aws s3 cp "$ROOT/0-work/scripts/00_asx_api.py" "s3://${BUCKET}/scripts/00_asx_api.py"
aws s3 cp "$ROOT/0-work/scripts/11_fetch_annual_reports_s3.py" "s3://${BUCKET}/scripts/11_fetch_annual_reports_s3.py"
aws s3 cp "$ROOT/0-work/scripts/aws/launch_preflight_worker.sh" "s3://${BUCKET}/scripts/launch_preflight_worker.sh"
for T in CBA QGL; do
  aws s3 cp "s3://${BUCKET}/entities/${T}/${T}_Announcements.csv" - >/dev/null 2>&1 || \
    aws s3 cp "$ROOT/data/entities/${T}/${T}_Announcements.csv" "s3://${BUCKET}/entities/${T}/${T}_Announcements.csv"
done

chmod +x "$LAUNCH" "$WAITER"

echo "==> Launching ${WORKERS} preflight workers..."
W0="$("$LAUNCH" "$RUN_ID" 0 CBA 5 0 0 0)"
echo "  worker 00 (CBA): $W0"
W1="$("$LAUNCH" "$RUN_ID" 1 QGL 5 0 0 2)"
echo "  worker 01 (QGL, simulate burn after 2): $W1"

echo "==> Starting local waiter (uses your AWS creds for EC2 rotation)..."
SESSION_NAME="preflight-waiter-${RUN_ID}"
tmux -f /exec-daemon/tmux.portal.conf kill-session -t "$SESSION_NAME" 2>/dev/null || true
tmux -f /exec-daemon/tmux.portal.conf new-session -d -s "$SESSION_NAME" -c "$ROOT" -- "${SHELL:-zsh}" -l
tmux -f /exec-daemon/tmux.portal.conf send-keys -t "$SESSION_NAME:0.0" \
  "export PATH=\"/usr/local/bin:/home/ubuntu/.local/bin:\$PATH\"; set -a && source \"$ENV_FILE\" && set +a; \"$WAITER\" \"$RUN_ID\" \"$WORKERS\" 2>&1 | tee -a \"$ROOT/0-work/scripts/preflight-waiter.log\"" C-m

echo
echo "=== Preflight started ==="
echo "Run ID:      ${RUN_ID}"
echo "Workers:     ${W0} ${W1}"
echo "Waiter:      local tmux session ${SESSION_NAME} (not soak-01 — needs EC2 perms)"
echo "Results:     s3://${BUCKET}/logs/preflight/${RUN_ID}/"
echo "Summary:     s3://${BUCKET}/manifests/preflight/${RUN_ID}/summary.json"
echo "Waiter log:  0-work/scripts/preflight-waiter.log"
if [[ -n "${GYPSY_SNS_TOPIC_ARN:-}" ]]; then
  echo "Notify:      SNS email when preflight completes"
else
  echo "Notify:      set GYPSY_SNS_TOPIC_ARN for email"
fi

if [[ "$ASYNC" -eq 1 ]]; then
  echo
  echo "Async mode — safe to close. Check email or S3 summary."
  exit 0
fi

echo "Sync mode — tailing waiter log..."
for _ in $(seq 1 360); do
  if grep -q "SNS notification sent" "$ROOT/0-work/scripts/preflight-waiter.log" 2>/dev/null; then
    tail -40 "$ROOT/0-work/scripts/preflight-waiter.log"
    break
  fi
  sleep 5
done
