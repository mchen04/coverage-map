#!/usr/bin/env bash
# Task-owned Orthanc on port 8042, local clients only (DICOMweb at /dicom-web/). Run on the mini.
#   scripts/orthanc.sh start|stop|status
set -euo pipefail
MINI_WORK="${MINI_WORK:-/Users/michaelchen/coverage-map-work}"
here="$(cd "$(dirname "$0")/.." && pwd)"
export COVMAP_ORTHANC_DB="${COVMAP_ORTHANC_DB:-$MINI_WORK/orthanc-db}"
export COVMAP_ORTHANC_PLUGINS="${COVMAP_ORTHANC_PLUGINS:-$MINI_WORK/deps/orthanc}"
pidfile="$MINI_WORK/orthanc.pid"
cfg="$MINI_WORK/orthanc.json"
case "${1:-status}" in
  start)
    if [[ -f $pidfile ]] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then echo "orthanc already running pid $(cat "$pidfile")"; exit 0; fi
    mkdir -p "$COVMAP_ORTHANC_DB" "$MINI_WORK/logs"
    # Orthanc expands ${VAR} in its config from the environment.
    cp "$here/config/orthanc.json" "$cfg"
    nohup "$MINI_WORK/deps/orthanc/Orthanc" "$cfg" < /dev/null > "$MINI_WORK/logs/orthanc.log" 2>&1 &
    echo $! > "$pidfile"
    for _ in $(seq 1 50); do curl -sf http://127.0.0.1:8042/system >/dev/null && break; sleep 0.2; done
    curl -sf http://127.0.0.1:8042/system | python3 -c 'import json,sys; d=json.load(sys.stdin); print("orthanc", d["Version"], "up; dicomweb plugin", "dicom-web" in json.dumps(d) or "?")'
    ;;
  stop)
    if [[ -f $pidfile ]]; then kill "$(cat "$pidfile")" 2>/dev/null || true; rm -f "$pidfile"; echo stopped; fi
    ;;
  status)
    if [[ -f $pidfile ]] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then echo "running pid $(cat "$pidfile")"; curl -s http://127.0.0.1:8042/statistics; else echo "not running"; exit 1; fi
    ;;
esac
