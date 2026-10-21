# Coverage Map — interface spec

Research-only CT reading-coverage tool. Not a medical device; no diagnostic claims.
This file is the contract between the Python package (`covmap/`), the OHIF extension
(`ohif/`), the browser automation (`e2e/`, `bench/`) and the CLI. Change it before
changing an interface.

## Hosts and locations

- Code is edited on mbp-main in this checkout. Everything executes on the Mac mini in
  `$MINI_WORK=/Users/michaelchen/coverage-map-work` (task-owned):
  `venv/` (Python 3.12, `requirements-mini.txt`), `deps/node` (Node 24 + pnpm 12),
  `deps/orthanc` (Orthanc 1.13 + DicomWeb plugin), `ohif/` (OHIF v3.13.13 checkout),
  `ohif-dist/` (built viewer), `data/` (`$COVMAP_DATA`), `totalseg/` (weights).
- `scripts/mini.sh [--heavy] -- <cmd>` syncs the checkout to `$MINI_WORK/repo` and runs
  `<cmd>` there with `scripts/mini_env.sh` sourced. `--heavy` passes the capacity gate
  first (builds, segmentation, full suites, benchmarks).
- Large data never enters Git.

## Case directory: `$COVMAP_DATA/cases/<case_id>/`

| Path | Producer | Content |
|---|---|---|
| `source/*.dcm` | `covmap fetch` | Original DICOM files from TCIA, unmodified |
| `provenance.json` | `covmap fetch` | Source URL, collection, DOI, license, Patient/Study/Series UIDs, zip sha256, per-file sha256 manifest + manifest sha256, download time |
| `volume.json` | `covmap fetch` | Geometry (below) |
| `ct.nii.gz` | `covmap fetch` | CT in RAS NIfTI for TotalSegmentator, written from the sorted DICOM stack |
| `seg/totalseg.nii.gz` | `covmap segment` | Raw TotalSegmentator multilabel output |
| `seg/labels.npy` | `covmap segment` | `uint8 [n_slices, rows, cols]` organ ids in DICOM grid (0 = background) |
| `seg/organs.json` | `covmap segment` | Organ catalog for this case (below) |
| `seg/meshes.bin` + `seg/meshes.json` | `covmap segment` | Surface meshes for vtk.js (below) |
| `seg/segment.json` | `covmap segment` | Run record: command, options, device, versions, timings, hashes |

### `volume.json`

```json
{
  "case": "lidc-0001", "study_uid": "...", "series_uid": "...",
  "rows": 512, "cols": 512, "n_slices": 133,
  "sop_uids": ["... in slice-index order ..."],
  "origin_lps": [x, y, z],            // ImagePositionPatient of slice 0
  "row_dir": [..], "col_dir": [..],   // ImageOrientationPatient: first triple = direction of increasing column index, second = increasing row index
  "slice_dir": [..],                  // unit normal pointing from slice k to k+1
  "pixel_spacing": [row_mm, col_mm], "slice_spacing": mm,
  "affine_lps": [[4x4]],              // maps (c, r, k, 1) -> LPS mm   NOTE order c, r, k
  "default_window": {"wc": 40, "ww": 400} | null
}
```

Slice index `k` orders slices by position along `slice_dir` ascending (ImagePositionPatient
projected on the normal). Arrays are indexed `[k, r, c]`. `world = origin + c*col_mm*row_dir...`
— precisely: `world = origin + c * pixel_spacing[1] * row_dir + r * pixel_spacing[0] * col_dir + k * slice_spacing * slice_dir`
(DICOM: the first IOP triple is the direction along a row, i.e. increasing column).

### `seg/organs.json`

```json
{
  "case": "...", "label_source": "TotalSegmentator 2.x total",
  "organs": [
    {"id": 1, "key": "liver", "name": "Liver", "group": "abdomen",
     "windows": ["soft"], "color_rgb": [r,g,b],
     "voxels": 123456, "k_range": [k0, k1], "center_k": 80, "center_sop": "...",
     "bbox_rc": [r0, r1, c0, c1], "centroid_lps": [x,y,z],
     "totalseg_classes": ["liver"]}
  ]
}
```

Only organs with at least `min_voxels` (default 2000) in this case are listed. Ids are
stable across cases (from the catalog in `covmap/organs.py`), not renumbered.

### Meshes

`meshes.json`: `{"organs": [{"id", "key", "points_offset", "n_points", "tris_offset", "n_tris"}], "bounds_lps": [xmin,xmax,ymin,ymax,zmin,zmax]}`.
`meshes.bin`: little-endian; all organs' points as `float32 xyz` (LPS mm) first, then all
triangles as `uint32` index triples (indices local to the organ's own points). Offsets are
in elements (points / triangles), not bytes. Points block length = sum(n_points)*3 floats.

## Window classes (`covmap/windows.py`)

A displayed VOI `(wc, ww)` in HU classifies to at most one class:

| Class | ww range | wc range | Presets that land here |
|---|---|---|---|
| `lung` | 800–2500 | −900 to −300 | Lung 1500/−600 |
| `bone` | 1000–4500 | 150 to 1000 | Bone 2500/480 |
| `soft` | 100–700 | −50 to 150 | Soft tissue 400/40, Liver 150/90, Mediastinum 350/50 |

Anything else (e.g. Brain 80/40, full-range) is `other` and qualifies no organ.
Organ `windows` lists the classes that qualify it: lungs `["lung"]`, bones `["bone"]`,
soft organs `["soft"]`, trachea `["lung","soft"]`.

## Reading events (JSON Lines; also the POST body)

```json
{"v": 1, "session": "s-…", "case": "lidc-0001", "seq": 17, "t_ms": 1234.5,
 "kind": "view",
 "viewport": "default", "vp_type": "stack" | "volume",
 "series": "<SeriesInstanceUID>", "sop": "<SOPInstanceUID>" | null,
 "corners_lps": [[x,y,z],[x,y,z],[x,y,z],[x,y,z]],
 "normal": [nx, ny, nz],
 "voi": {"wc": 40, "ww": 400},
 "canvas_px": [w, h],
 "source": "ohif" | "simulated" | "browser-scripted"}
```

- `kind: "view"` starts a display state for that viewport; it lasts until the next event
  for the same viewport, a `kind: "hidden"` event (`viewport: "*"` ends all states, e.g.
  tab hidden), or `kind: "end"` (session end). Dwell = elapsed `t_ms`.
- `t_ms` is monotonic milliseconds since session start (`performance.now()` based).
- `corners_lps` are the four canvas corners (TL, TR, BR, BL) mapped to patient LPS mm via
  `viewport.canvasToWorld`. The scorer maps them to continuous voxel coordinates; the axis
  with (near) zero extent is the slice axis, the other two give the visible rectangle,
  clipped to the volume. Axial, coronal and sagittal planes are scored; oblique planes
  (normal not within 5° of a volume axis) are counted under `ignored.oblique`.
- For stack viewports `sop` is the displayed instance and fixes `k` (authoritative over
  corners for the slice axis). Unknown `sop`/`series` → `ignored.unknown_image`.
- `voi` is in modality units (HU after rescale).
- `source` labels provenance: `simulated` events are synthetic and must be labeled so in
  every report.

POST body to `/api/sessions/{id}/events`: `{"events": [...], "now_ms": 2345.6}`. `now_ms`
lets live scoring credit the still-open state of each viewport up to now (provisional,
not committed).

## Scoring (`covmap/scoring.py`)

Per organ voxel the scorer accumulates qualifying dwell: the sum over display states whose
plane contains the voxel, whose visible rectangle contains it, and whose window class is in
the organ's `windows`. A voxel is **seen** when qualifying dwell ≥ `min_dwell_ms`.

`coverage = seen voxels / organ voxels`. Status: `reviewed` (green) if coverage ≥
`reviewed_threshold`; `skipped` (gray) if coverage < `skipped_threshold`; else `partial`
(yellow).

Defaults: `min_dwell_ms = 80`, `reviewed_threshold = 0.90`, `skipped_threshold = 0.05`.

Unseen voxels are attributed to the first matching reason, in order:
`short_dwell` (some qualifying display, total below `min_dwell_ms`), `wrong_window`
(displayed in-region only under non-qualifying windows), `out_of_region` (its slice was
displayed but the voxel was never inside the visible rectangle), `never_displayed`.

### Coverage JSON (replay output, API response)

```json
{"case": "...", "session": "...", "provisional": false,
 "params": {"min_dwell_ms": 80, "reviewed_threshold": 0.9, "skipped_threshold": 0.05},
 "organs": [{"id": 1, "key": "liver", "name": "Liver", "windows": ["soft"],
             "coverage": 0.97, "status": "reviewed", "seen_voxels": 0, "voxels": 0,
             "unseen": {"short_dwell": 0.0, "wrong_window": 0.0, "out_of_region": 0.0, "never_displayed": 0.0}}],
 "summary": {"reviewed": 0, "partial": 0, "skipped": 0, "organs": 0, "voxel_coverage": 0.0},
 "baseline": {"model": "per-image-viewed", "images": 133, "viewed": 120,
              "unviewed_ranges": [[a, b]], "complete": false},
 "events": {"total": 0, "views": 0, "ignored": {"oblique": 0, "unknown_image": 0, "other": 0},
            "sources": {"ohif": 0}},
 "timing_ms": {"score": 0.0}}
```

`unseen` values are fractions of organ voxels. `baseline` is the per-image viewed/unviewed
model (`covmap/baseline.py`): an image counts as viewed when it was displayed in any
viewport for any duration under any window and zoom (≥ 1 view event naming it, or a volume
viewport state whose plane is that acquisition slice). This models the described
Synapse/eRAD per-image behaviour; it is not the proprietary product.

## HTTP API (`covmap serve`, aiohttp, default `127.0.0.1:8765`)

| Method/path | Result |
|---|---|
| `GET /api/health` | `{"ok": true, "orthanc": bool, "cases": n}` |
| `GET /api/cases` | `[{case, study_uid, series_uid, n_slices, organs}]` |
| `GET /api/lookup?series=…` or `?study=…` | `{case}` or 404 |
| `GET /api/cases/{case}/organs` | `seg/organs.json` |
| `GET /api/cases/{case}/volume` | `volume.json` |
| `GET /api/cases/{case}/meshes.json`, `/meshes.bin` | mesh files (bin as `application/octet-stream`) |
| `POST /api/sessions` `{case, label?}` | `{session, case}` |
| `POST /api/sessions/{id}/events` | coverage JSON (provisional at `now_ms`) |
| `GET /api/sessions/{id}/coverage` | committed coverage JSON |
| `GET /api/sessions/{id}/events.jsonl` | the session's event log |
| `GET /dicom-web/*`, `/wado*` | reverse proxy to Orthanc (`http://127.0.0.1:8042`) |
| `GET /render.html`, `/render/*` | standalone vtk.js organ-map page used by `covmap render` |
| `GET /*` | OHIF dist (`$COVMAP_OHIF_DIST`), SPA fallback to `index.html` |

Session logs persist to `$COVMAP_DATA/sessions/<id>.jsonl`.

## CLI (`python -m covmap <cmd>`; every command prints one JSON object to stdout)

| Command | Does |
|---|---|
| `fetch --case <id> [--series UID --collection C]` | download from TCIA NBIA v1, write source/, provenance, volume.json, ct.nii.gz |
| `orthanc-load --case <id>` | upload `source/` into Orthanc |
| `segment --case <id> [--fast] [--device cpu|mps] [--roi-subset]` | TotalSegmentator → labels.npy, organs.json, meshes |
| `simulate --case <id> --scenario <name> [--out events.jsonl]` | scripted reading events (`source: simulated`) + truth |
| `replay --case <id> --events <file>` | coverage JSON (+ baseline) |
| `report --case <id> --coverage <file> [--truth <file>]` | summary + truth comparison (TP/FP/FN per organ, baseline catches) |
| `render --case <id> [--coverage <file>] --out <png>` | 3D organ map PNG via the same vtk.js module in headless Chromium |
| `serve [--port 8765]` | app server |
| `gauntlet ...`, `bench ...` | evaluation drivers |

Errors: non-zero exit and `{"ok": false, "error": "..."}` on stdout.

### Scenario truth (`simulate` output, `<out>.truth.json`)

```json
{"case": "...", "scenario": "window_skip_lung", "source": "simulated",
 "intended_skips": ["lung_left", "lung_right"],
 "expected": {"lung_left": "skipped", "lung_right": "skipped", "*": "reviewed"},
 "baseline_expected_complete": true,
 "description": "..."}
```

`expected` gives the intended status per organ key; `"*"` covers every other organ in the
case. A scenario passes when every organ's status equals its expected status.
