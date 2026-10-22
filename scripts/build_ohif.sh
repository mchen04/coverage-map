#!/usr/bin/env bash
# Build the Coverage Map OHIF app on the mini (idempotent):
#   scripts/mini.sh --heavy -- bash scripts/build_ohif.sh
# Syncs ohif/ packages into the task-owned working copy $MINI_WORK/ohif-cov (the pristine
# $MINI_WORK/ohif is never touched), registers them as OHIF plugins, installs, builds with
# our app config, bundles the standalone render page, and installs the result into
# $MINI_WORK/ohif-dist. Prints timings and the dist size.
set -euo pipefail
MINI_WORK="${MINI_WORK:-/Users/michaelchen/coverage-map-work}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$MINI_WORK/ohif"
COV="$MINI_WORK/ohif-cov"
DIST="$MINI_WORK/ohif-dist"
export PATH="$MINI_WORK/deps/node/bin:$PATH"

now() { python3 -c 'import time; print(f"{time.time():.3f}")'; }
since() { python3 -c "print(f'{$(now) - $1:.1f}')"; }
T0=$(now)

# 1. working copy (a local clone of the pristine checkout; no network)
if [[ ! -d "$COV/.git" ]]; then
  git clone --quiet "$SRC" "$COV"
fi

# 2. our packages -> workspace packages
t=$(now)
sync_pkg() { mkdir -p "$2" && rsync -a --delete --exclude node_modules --exclude dist "$1/" "$2/"; }
sync_pkg "$REPO/ohif/extension-coverage" "$COV/extensions/covmap-coverage"
sync_pkg "$REPO/ohif/organ-map" "$COV/extensions/covmap-organ-map"
sync_pkg "$REPO/ohif/mode-coverage" "$COV/modes/covmap-coverage"
cp "$REPO/ohif/config/coverage-map.js" "$COV/platform/app/public/config/coverage-map.js"
python3 - "$COV/platform/app/pluginConfig.json" <<'EOF'
import json, sys
path = sys.argv[1]
cfg = json.load(open(path))
changed = False
for section, name in (("extensions", "@covmap/extension-coverage"), ("modes", "@covmap/mode-coverage")):
    if not any(e.get("packageName") == name for e in cfg[section]):
        cfg[section].append({"packageName": name, "version": "0.1.0"})
        changed = True
if changed:
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
print(f"pluginConfig: {'updated' if changed else 'already registered'}")
EOF
T_SYNC=$(since "$t")

# 3. install (lockfile gains the three workspace packages; deps already pinned upstream)
t=$(now)
cd "$COV"
pnpm install --no-frozen-lockfile --reporter=append-only 2>&1 | tail -n 5
for p in extension-coverage organ-map mode-coverage; do
  [[ -e "node_modules/@covmap/$p/package.json" ]] || { echo "missing workspace link @covmap/$p" >&2; exit 1; }
done
T_INSTALL=$(since "$t")

# 4. OHIF build with our app config
t=$(now)
APP_CONFIG=config/coverage-map.js pnpm run build 2>&1 | tail -n 25
[[ -f platform/app/dist/index.html ]] || { echo "OHIF build produced no index.html" >&2; exit 1; }
T_BUILD=$(since "$t")

# 5. standalone render page (after the OHIF build: its output.clean empties dist)
t=$(now)
RENDER_OUT="$COV/platform/app/dist/render" \
ORGAN_MAP_SRC="$COV/extensions/covmap-organ-map/src/index.ts" \
OHIF_NODE_MODULES="$COV/node_modules" \
  node_modules/.bin/rspack build --config "$REPO/web/render/rspack.config.js" 2>&1 | tail -n 6
cp "$REPO/web/render/render.html" platform/app/dist/render.html
[[ -f platform/app/dist/render/render.js ]] || { echo "render page bundle missing" >&2; exit 1; }
T_RENDER=$(since "$t")

# 6. install the dist
t=$(now)
rsync -a --delete platform/app/dist/ "$DIST/"
T_COPY=$(since "$t")

SIZE=$(du -sh "$DIST" | cut -f1)
FILES=$(find "$DIST" -type f | wc -l | tr -d ' ')
python3 - <<EOF
import json
print(json.dumps({"ok": True, "dist": "$DIST", "dist_size": "$SIZE", "dist_files": $FILES,
                  "seconds": {"sync": $T_SYNC, "install": $T_INSTALL, "build": $T_BUILD,
                              "render_page": $T_RENDER, "copy": $T_COPY, "total": $(since "$T0")},
                  "ohif": "$(git -C "$COV" describe --tags 2>/dev/null || echo unknown)"}))
EOF
