#!/usr/bin/env bash
# Build the pristine OHIF checkout (stock baseline) and install the runtime config.
# Run on the mini. The OHIF source in $MINI_WORK/ohif is never modified.
set -euo pipefail
MINI_WORK="${MINI_WORK:-/Users/michaelchen/coverage-map-work}"
here="$(cd "$(dirname "$0")/.." && pwd)"
export PATH="$MINI_WORK/deps/node/bin:$PATH"
cd "$MINI_WORK/ohif"
if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  echo "refusing: $MINI_WORK/ohif has local modifications (must stay pristine)" >&2; exit 1
fi
if [[ "${1:-}" == "--build" || ! -f "$MINI_WORK/ohif-dist-stock/index.html" ]]; then
  t0=$(date +%s)
  APP_CONFIG=config/default.js pnpm run build
  rm -rf "$MINI_WORK/ohif-dist-stock" && cp -R platform/app/dist "$MINI_WORK/ohif-dist-stock"
  echo "stock build seconds: $(( $(date +%s) - t0 ))"
fi
cp "$here/config/ohif-stock-app-config.js" "$MINI_WORK/ohif-dist-stock/app-config.js"
echo "stock OHIF $(git describe --tags) commit $(git rev-parse --short HEAD) -> $MINI_WORK/ohif-dist-stock"
