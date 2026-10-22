#!/usr/bin/env bash
# Idempotent, offline-reproducible toolchain setup in the mini's task directory.
# Everything lands under $MINI_WORK; nothing is installed globally.
# Run on the mini:  bash scripts/setup_mini.sh [venv|node|orthanc|ohif|all]
set -euo pipefail
MINI_WORK="${MINI_WORK:-/Users/michaelchen/coverage-map-work}"
NODE_VERSION="${NODE_VERSION:-v24.22.1}"
ORTHANC_VERSION="${ORTHANC_VERSION:-26.9.1}"
OHIF_TAG="${OHIF_TAG:-v3.13.13}"
PY_VERSION="${PY_VERSION:-3.12}"
mkdir -p "$MINI_WORK"/{deps,data,totalseg,downloads,logs}
cd "$MINI_WORK"
what="${1:-all}"

log() { echo "[setup $(date +%H:%M:%S)] $*"; }

if [[ $what == venv || $what == all ]]; then
  if [[ ! -x venv/bin/python ]]; then
    log "creating venv (python $PY_VERSION)"
    uv venv --python "$PY_VERSION" venv
  fi
  log "installing python requirements"
  VIRTUAL_ENV="$MINI_WORK/venv" uv pip install -r "$(dirname "$0")/../requirements-mini.txt"
  venv/bin/python -c "import totalsegmentator, torch, pydicom, nibabel, aiohttp; print('torch', torch.__version__, 'mps', torch.backends.mps.is_available())"
  log "installing the Playwright Chromium (render, e2e, benches)"
  venv/bin/python -m playwright install chromium
fi

if [[ $what == node || $what == all ]]; then
  if [[ ! -x deps/node/bin/node ]]; then
    log "downloading node $NODE_VERSION"
    tgz="downloads/node-$NODE_VERSION-darwin-arm64.tar.gz"
    curl -fsSL -o "$tgz" "https://nodejs.org/dist/$NODE_VERSION/node-$NODE_VERSION-darwin-arm64.tar.gz"
    curl -fsSL "https://nodejs.org/dist/$NODE_VERSION/SHASUMS256.txt" | grep "node-$NODE_VERSION-darwin-arm64.tar.gz" | (cd downloads && shasum -a 256 -c -)
    rm -rf deps/node && mkdir -p deps/node && tar -xzf "$tgz" -C deps/node --strip-components 1
  fi
  export PATH="$MINI_WORK/deps/node/bin:$PATH"
  node --version
  if ! command -v pnpm >/dev/null; then
    log "installing pnpm into task-local node"
    npm install -g --prefix "$MINI_WORK/deps/node" pnpm@12.8.1 >/dev/null
  fi
  pnpm --version
fi

if [[ $what == orthanc || $what == all ]]; then
  if [[ ! -x deps/orthanc/Orthanc ]]; then
    log "downloading Orthanc $ORTHANC_VERSION (universal macOS)"
    zip="downloads/Orthanc-macOS-$ORTHANC_VERSION.zip"
    curl -fsSL -o "$zip" "https://orthanc.uclouvain.be/downloads/macos/packages/universal/Orthanc-macOS-$ORTHANC_VERSION.zip"
    shasum -a 256 "$zip" | tee downloads/orthanc.sha256
    rm -rf deps/orthanc-unzip && mkdir -p deps/orthanc-unzip && unzip -q "$zip" -d deps/orthanc-unzip
    bin="$(find deps/orthanc-unzip -name Orthanc -type f -perm -u+x | head -1)"
    rm -rf deps/orthanc && mv "$(dirname "$bin")" deps/orthanc && rm -rf deps/orthanc-unzip
    xattr -dr com.apple.quarantine deps/orthanc 2>/dev/null || true
  fi
  deps/orthanc/Orthanc --version | head -1
fi

if [[ $what == ohif || $what == all ]]; then
  export PATH="$MINI_WORK/deps/node/bin:$PATH"
  if [[ ! -d ohif/.git ]]; then
    log "cloning OHIF $OHIF_TAG"
    git clone --depth 1 --branch "$OHIF_TAG" https://github.com/OHIF/Viewers.git ohif
  fi
  log "installing OHIF dependencies (frozen lockfile)"
  (cd ohif && pnpm install --frozen-lockfile)
fi
log "done: $what"
