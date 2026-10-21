#!/usr/bin/env bash
# Mirror this checkout (code only) to the mini's task directory.
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
source "$here/scripts/env.sh"
ssh "$MINI_HOST" "mkdir -p '$MINI_REPO'"
rsync -a --delete \
  --exclude .git --exclude .forge --exclude data --exclude work --exclude node_modules \
  --exclude __pycache__ --exclude .pytest_cache --exclude 'evidence/raw' \
  "$here/" "$MINI_HOST:$MINI_REPO/"
