#!/usr/bin/env bash
set -euo pipefail
here="$(cd "$(dirname "$0")/../.." && pwd)"
source "$here/scripts/env.sh"
out="${1:?usage: deploy/huggingface/build_space.sh <out_dir>}"
src="$MINI_HOST:$MINI_WORK"
mkdir -p "$out/data/cases"
rsync -a --delete --exclude __pycache__ "$here/covmap" "$out/"
cp "$here/pyproject.toml" "$here"/deploy/huggingface/{Dockerfile,README.md,start.sh,orthanc.json} "$out/"
rsync -a --delete --exclude '*.map' "$src/ohif-dist/" "$out/ohif-dist/"
rsync -a --delete "$src/data/samples/" "$out/data/samples/"
for case in lidc-0001 pancreas-0051; do
  dir="$out/data/cases/$case"
  mkdir -p "$dir/seg"
  rsync -a --delete "$src/data/cases/$case/source/" "$dir/source/"
  rsync -a "$src/data/cases/$case/"{volume.json,provenance.json} "$dir/"
  rsync -a "$src/data/cases/$case/seg/"{labels.npy,meshes.bin,meshes.json,organs.json,segment.json} "$dir/seg/"
done
du -sh "$out"
