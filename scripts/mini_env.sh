# Sourced on the mini before every command: task-local toolchain on PATH.
export MINI_WORK="${MINI_WORK:-/Users/michaelchen/coverage-map-work}"
export PATH="$MINI_WORK/deps/node/bin:$MINI_WORK/venv/bin:$MINI_WORK/deps/orthanc:$PATH"
export TOTALSEG_HOME_DIR="$MINI_WORK/totalseg"
export COVMAP_DATA="${COVMAP_DATA:-$MINI_WORK/data}"
export COVMAP_OHIF_DIST="${COVMAP_OHIF_DIST:-$MINI_WORK/ohif-dist}"
export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-$HOME/Library/Caches/ms-playwright}"
