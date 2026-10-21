#!/usr/bin/env bash
# Run a command in the synced repo on the mini.
#   scripts/mini.sh [--heavy[=GiB]] -- <command...>
# --heavy first passes the fleet capacity gate (exit 3 = wait, never bypass); --heavy=8 asks
# the gate for an 8 GiB job (full-resolution segmentation peaks at ~7 GB).
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
source "$here/scripts/env.sh"
heavy=0
job_gib=4
case "${1:-}" in
  --heavy) heavy=1; shift ;;
  --heavy=*) heavy=1; job_gib="${1#--heavy=}"; shift ;;
esac
[[ "${1:-}" == "--" ]] && shift
if [[ $heavy == 1 ]]; then
  for attempt in 1 2 3 4 5 6 7 8 9 10; do
    rc=0; python3 "$CAPACITY_GATE" --need-mini --job-gib "$job_gib" >/dev/null || rc=$?
    [[ $rc == 0 ]] && break
    if [[ $rc != 3 ]]; then echo "capacity gate failed rc=$rc" >&2; exit $rc; fi
    echo "mini busy (gate exit 3); waiting 60s (attempt $attempt)" >&2
    sleep 60
    [[ $attempt == 10 ]] && { echo "mini still busy; giving up" >&2; exit 3; }
  done
fi
"$here/scripts/sync_mini.sh"
cmd="$(printf '%q ' "$@")"
ssh "$MINI_HOST" "cd '$MINI_REPO' && source scripts/mini_env.sh && $cmd"
