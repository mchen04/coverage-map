# Shared locations. Source this file; it changes nothing by itself.
# MINI_WORK is the task-owned directory on the Mac mini that holds every
# dependency, dataset and generated artifact (never committed).
export MINI_HOST="${MINI_HOST:-mini}"
export MINI_WORK="${MINI_WORK:-/Users/michaelchen/coverage-map-work}"
export MINI_REPO="$MINI_WORK/repo"
export CAPACITY_GATE="${CAPACITY_GATE:-$HOME/.claude/skills/supervise/scripts/fleetcapacity.py}"
