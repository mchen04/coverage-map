#!/usr/bin/env bash
set -euo pipefail
mkdir -p "$COVMAP_ORTHANC_DB" "$COVMAP_DATA/sessions"
Orthanc orthanc.json &
until curl -sf http://127.0.0.1:8042/system >/dev/null; do sleep 0.5; done
for dir in "$COVMAP_DATA"/cases/*/; do
  python -m covmap orthanc-load --case "$(basename "$dir")" >/dev/null
done
export COVMAP_ALLOWED_HOSTS="${SPACE_HOST:-${COVMAP_ALLOWED_HOSTS:-}}"
exec python -m covmap serve --host 0.0.0.0 --port 7860
