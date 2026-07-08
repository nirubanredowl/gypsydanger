#!/usr/bin/env bash
# VM entrypoint: install deps, load AWS creds, parse sample corpus with LiteParse.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$ROOT/../../scripts/.env"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$ENV_FILE"
  set +a
fi

export AWS_PAGER=""
export AWS_CLI_PAGER=""
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-ap-southeast-2}"

cd "$ROOT"

if python3 -m venv --help >/dev/null 2>&1 && python3 -c "import ensurepip" 2>/dev/null; then
  if [[ ! -d .venv ]]; then
    python3 -m venv .venv
  fi
  # shellcheck source=/dev/null
  source .venv/bin/activate
else
  echo "note: python3-venv unavailable; using system Python" >&2
fi

pip install -q -U pip
pip install -q -r requirements.txt

echo "==> LiteParse sample corpus parse"
python3 parse_sample_corpus.py "$@"
