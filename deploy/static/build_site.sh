#!/usr/bin/env bash
set -euo pipefail
here="$(cd "$(dirname "$0")/../.." && pwd)"
source "$here/scripts/env.sh"
out="${1:?usage: deploy/static/build_site.sh <out_dir>}"
scans="${COVMAP_SCANS_URL:-https://huggingface.co/datasets/mchen04/coverage-map-data/resolve/main}"
demo="${COVMAP_DEMO_CASE:-pancreas-0051}"
cases=(lidc-0001 pancreas-0051)
src="$MINI_HOST:$MINI_WORK"

"$here/scripts/mini.sh" -- python -c "
from covmap.browser import write_outlines
from covmap.paths import case_dir
for c in '${cases[*]}'.split():
    print(write_outlines(case_dir(c)))
"
scp -q "$here/deploy/static/dicom-json.mjs" "$MINI_HOST:$MINI_WORK/dicom-json.mjs"
ssh "$MINI_HOST" "export PATH=$MINI_WORK/deps/node/bin:\$PATH; mkdir -p $MINI_WORK/static-dicom && cd $MINI_WORK/ohif-cov && \
  for c in ${cases[*]}; do node $MINI_WORK/dicom-json.mjs $MINI_WORK/data/cases/\$c/source $scans/\$c $MINI_WORK/static-dicom/\$c.json 2>/dev/null; done"

mkdir -p "$out"
find "$out" -mindepth 1 -maxdepth 1 ! -name .vercel -exec rm -rf {} +
mkdir -p "$out/data/dicom" "$out/py"
rsync -a --exclude '*.map' --exclude ort/ --exclude dicom-microscopy-viewer/ --exclude _redirects --exclude _headers \
  "$src/ohif-dist/" "$out/"
cp "$here/ohif/config/coverage-map.static.js" "$out/app-config.js"
cp "$here/web/inbrowser/server-worker.js" "$out/covmap-server-worker.js"
(cd "$here" && zip -qr "$out/py/covmap.zip" covmap -i '*.py')

files=()
for c in "${cases[@]}"; do
  dir="$out/data/cases/$c"
  mkdir -p "$dir/seg"
  rsync -a "$src/data/cases/$c/volume.json" "$dir/"
  rsync -a "$src/data/cases/$c/seg/"{organs.json,meshes.json,meshes.bin,labels.npy,outlines.json} "$dir/seg/"
  gzip -9 -f "$dir/seg/labels.npy" "$dir/seg/outlines.json"
  rsync -a "$src/static-dicom/$c.json" "$out/data/dicom/"
  files+=("cases/$c/volume.json" "cases/$c/seg/organs.json" "cases/$c/seg/labels.npy.gz" "cases/$c/seg/outlines.json.gz")
done
rsync -a "$src/data/samples/" "$out/data/samples/"
while IFS= read -r f; do files+=("${f#"$out/data/"}"); done < <(find "$out/data/samples" -type f | sort)
python3 - "$out" "$demo" "${files[@]}" <<'PY'
import json, sys
from pathlib import Path
from urllib.parse import quote
out, demo, files = Path(sys.argv[1]), sys.argv[2], sys.argv[3:]
(out / "data" / "files.json").write_text(json.dumps({"files": files}, indent=1) + "\n")
study = json.loads((out / "data" / "cases" / demo / "volume.json").read_text())["study_uid"]
start = f"/coverage?url={quote(f'/data/dicom/{demo}.json', safe='')}&StudyInstanceUIDs={study}"
(out / "vercel.json").write_text(json.dumps({
    "redirects": [{"source": "/", "destination": start, "permanent": False}],
    "rewrites": [{"source": "/((?!data/|py/).*)", "destination": "/index.html"}],
}, indent=1) + "\n")
PY
du -sh "$out"
