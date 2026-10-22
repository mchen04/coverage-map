#!/usr/bin/env bash
# Launch Coverage Map for use on this Mac. Services run on the Mac mini (task-owned);
# an SSH tunnel exposes them locally.
#   scripts/launch.sh          start services + tunnel, print URLs
#   scripts/launch.sh stop     close the tunnel and stop the app servers (Orthanc stays up)
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
source "$here/scripts/env.sh"
tunnel_pid="$here/.launch-tunnel.pid"
case "${1:-start}" in
  start)
    "$here/scripts/mini.sh" -- bash scripts/services.sh start
    if [[ -f $tunnel_pid ]] && kill -0 "$(cat "$tunnel_pid")" 2>/dev/null; then
      echo "tunnel already open (pid $(cat "$tunnel_pid"))"
    else
      ssh -N -o ExitOnForwardFailure=yes -L 8765:127.0.0.1:8765 -L 8766:127.0.0.1:8766 "$MINI_HOST" &
      echo $! > "$tunnel_pid"
      sleep 2
    fi
    curl -sf http://127.0.0.1:8765/api/health >/dev/null && echo "tunnel ok"
    echo
    echo "Coverage Map (research use only):"
    echo "  demo reading     http://127.0.0.1:8765/"
    "$here/scripts/mini.sh" -- python -c '
import json, pathlib
from covmap import paths
for d in sorted((paths.data_dir() / "cases").glob("*/volume.json")):
    v = json.loads(d.read_text())
    print(f"  {v[\"case\"]:15s} http://127.0.0.1:8765/coverage?StudyInstanceUIDs={v[\"study_uid\"]}")
print("Stock OHIF (baseline build, unmodified):")
for d in sorted((paths.data_dir() / "cases").glob("*/volume.json")):
    v = json.loads(d.read_text())
    print(f"  {v[\"case\"]:15s} http://127.0.0.1:8766/viewer?StudyInstanceUIDs={v[\"study_uid\"]}")
'
    ;;
  stop)
    if [[ -f $tunnel_pid ]]; then kill "$(cat "$tunnel_pid")" 2>/dev/null || true; rm -f "$tunnel_pid"; echo "tunnel closed"; fi
    "$here/scripts/mini.sh" -- bash scripts/services.sh stop
    ;;
esac
