#!/usr/bin/env bash
# Task-owned app services on the mini (run there):
#   Orthanc 127.0.0.1:8042, Coverage Map app 127.0.0.1:8765, stock OHIF baseline 127.0.0.1:8766.
#   scripts/services.sh start|stop|status
set -euo pipefail
MINI_WORK="${MINI_WORK:-/Users/michaelchen/coverage-map-work}"
here="$(cd "$(dirname "$0")/.." && pwd)"
source "$here/scripts/mini_env.sh"
mkdir -p "$MINI_WORK/logs" "$MINI_WORK/run"

start_one() {  # name port dist
  local name=$1 port=$2 dist=$3 pid="$MINI_WORK/run/$1.pid"
  if [[ -f $pid ]] && kill -0 "$(cat "$pid")" 2>/dev/null; then echo "$name already running (pid $(cat "$pid"))"; return; fi
  (cd "$here" && COVMAP_OHIF_DIST="$dist" nohup python -m covmap serve --port "$port" < /dev/null > "$MINI_WORK/logs/$name.log" 2>&1 & echo $! > "$pid")
  for _ in $(seq 1 100); do curl -sf "http://127.0.0.1:$port/api/health" >/dev/null && break; sleep 0.2; done
  echo "$name: $(curl -s "http://127.0.0.1:$port/api/health")"
}

stop_one() {
  local pid="$MINI_WORK/run/$1.pid"
  if [[ -f $pid ]]; then kill "$(cat "$pid")" 2>/dev/null || true; rm -f "$pid"; echo "$1 stopped"; fi
}

case "${1:-status}" in
  start)
    bash "$here/scripts/orthanc.sh" start
    start_one covmap 8765 "$MINI_WORK/ohif-dist"
    start_one stock 8766 "$MINI_WORK/ohif-dist-stock"
    ;;
  stop)
    stop_one covmap; stop_one stock
    [[ "${2:-}" == "--all" ]] && bash "$here/scripts/orthanc.sh" stop
    ;;
  restart)
    stop_one covmap; stop_one stock
    start_one covmap 8765 "$MINI_WORK/ohif-dist"
    start_one stock 8766 "$MINI_WORK/ohif-dist-stock"
    ;;
  status)
    bash "$here/scripts/orthanc.sh" status >/dev/null && echo "orthanc up" || echo "orthanc down"
    for p in 8765 8766; do echo "$p: $(curl -s -m 3 "http://127.0.0.1:$p/api/health" || echo down)"; done
    ;;
esac
