#!/usr/bin/env bash
# Run open-parse + LiteParse on full page-count sample and bundle PDFs + outputs.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
ENV_FILE="$ROOT/0-work/scripts/.env"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$ENV_FILE"
  set +a
fi

export AWS_PAGER=""
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-ap-southeast-2}"

OPEN="$ROOT/0-work/experiments/openparse-sample"
LITE="$ROOT/0-work/experiments/liteparse-sample"
BUNDLE="$ROOT/data/parse-sample-corpus"
LOG="$ROOT/0-work/scripts/parse-sample-bundle.log"

mkdir -p "$BUNDLE"

echo "==> parse sample bundle run $(date -u +%Y-%m-%dT%H:%M:%SZ)" | tee "$LOG"

echo "==> [1/3] open-parse (100 PDFs)" | tee -a "$LOG"
cd "$OPEN"
pip install -q -r requirements.txt
set +e
python3 parse_sample_corpus.py 2>&1 | tee -a "$LOG"
OPEN_EXIT=${PIPESTATUS[0]}
set -e

echo "==> [2/3] LiteParse (100 PDFs)" | tee -a "$LOG"
cd "$LITE"
pip install -q -r requirements.txt
set +e
python3 parse_sample_corpus.py 2>&1 | tee -a "$LOG"
LITE_EXIT=${PIPESTATUS[0]}
set -e

echo "==> [3/3] bundle to $BUNDLE" | tee -a "$LOG"
cd "$ROOT"
set +e
python3 0-work/scripts/21_bundle_parse_samples.py 2>&1 | tee -a "$LOG"
BUNDLE_EXIT=${PIPESTATUS[0]}
set -e

echo "==> done open=$OPEN_EXIT lite=$LITE_EXIT bundle=$BUNDLE_EXIT" | tee -a "$LOG"
exit $(( OPEN_EXIT || LITE_EXIT || BUNDLE_EXIT ))
