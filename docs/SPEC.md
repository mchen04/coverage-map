# Coverage Map — interface spec

Research-only CT reading-coverage tool. Not a medical device; no diagnostic claims.
This file is the contract between the Python package (`covmap/`), the OHIF extension
(`ohif/`), the browser automation (`e2e/`, `bench/`) and the CLI. Change it before
changing an interface.

## Hosts and locations

- Code is edited on mbp-old in this checkout (/Users/michaelchen/Projects/Apps/coverage-map; nothing that can exceed 4 GB runs here). Everything executes on the Mac mini in
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

Anything else (e.g. Brain 80/40, full-range) is `other` and qualifies no organ. Coverage Map
targets body CT: head organs (`brain`, `skull`) exist only in the catalog, neither shipped case
contains them, and the Brain preset is deliberately neutral, so head CT is not supported.
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
 "source": "ohif" | "simulated"}
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
- Reader actions are events in the same log (so replay reproduces them):
  `{"kind": "annotation", "organ": "<key>", "action": "done" | "na" | "flag" | "clear", "note": "..."}`
  (`done` = the reader attests review of an organ the measurement does not show as reviewed;
  `na` = not applicable, e.g. absent/out of scope; `flag` = suspicious segmentation/outline;
  `clear` removes that organ's annotations) and
  `{"kind": "finish", "confirmed": true|false, "remaining": ["<key>", ...]}`.
  A confirmed `finish` also ends every open display state at its `t_ms`, exactly like
  `{"kind": "hidden", "viewport": "*"}`: the image on screen when the reader confirms is
  credited, so replaying a finished session's log equals what the reader saw at the
  confirm. View events after it start new states as usual; `confirmed: false` ends nothing.
- `source` labels provenance: `simulated` events are synthetic and must be labeled so in
  every report.

POST body to `/api/sessions/{id}/events`: `{"events": [...], "now_ms": 2345.6}`. `now_ms`
lets live scoring credit the still-open state of each viewport up to now (provisional,
not committed).

`seq` identifies an event within its session. An event whose `seq` was already applied is
not applied again: an identical resend (equal apart from the `session` / `from_sample`
stamps) counts under `events.ignored.other`, a different event with a used `seq` (e.g. two
tabs numbering from the same `seq`) under `events.ignored.conflict` (replay of a file, CLI;
live POSTs refuse it, below). The first event applied with a `seq` wins.

Live POST guards (HTTP only; `covmap replay`, `timeline` and the CLI score logs without
them). A batch failing a guard is refused whole and nothing is applied:
- 400: every event's `session`, when present, must be the session in the URL;
- 409 seq conflict: an event reuses a `seq` already accepted in the session, or used earlier
  in the same batch, for a different event (compared as above, in the log numbering after
  the sample shift). Body `{"ok": false, "error", "conflicts": [seq, ...], "last_seq",
  "last_t_ms"}`, all in the client's numbering (`last_*` as in session info), so the client
  can renumber after `last_seq` and resend. Identical resends are accepted as before;
- 400 clock: the scorer never runs time backwards, so events from an earlier client clock
  would freeze it. All times are checked in the client's clock (as POSTed, before the
  sample shift, so sample-started sessions post from `t_ms` 0). `elapsed` = server time
  since the session was created; the first accepted POST carrying `now_ms` pins the
  client's lead `max(0, now_ms - elapsed)` (the OHIF client may start its clock while the
  viewer loads, before the session exists; kept as `clock_lead_ms` in the session meta,
  0 until pinned). Refused: an event with `t_ms > min(elapsed + lead, now_ms) + tolerance`,
  or `now_ms > elapsed + lead + tolerance`. Tolerance 5000 ms; `COVMAP_CLOCK_TOLERANCE_MS`
  sets it, `off` disables the clock check (the session check stays).
- `from_sample` on posted events is dropped (only the server marks sample events).

## Scoring (`covmap/scoring.py`)

Per organ voxel the scorer accumulates qualifying dwell: the sum over display states whose
plane contains the voxel, whose visible rectangle contains it, and whose window class is in
the organ's `windows`. A voxel is **seen** when it had a qualifying display and qualifying dwell ≥ `min_dwell_ms`.

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
 "events": {"total": 0, "views": 0, "ignored": {"oblique": 0, "unknown_image": 0, "other": 0, "conflict": 0},
            "sources": {"ohif": 0}},
 "timing_ms": {"score": 0.0}}
```

API responses (`POST …/events`, `GET …/coverage`, `POST …/rules`) and the JSON export add
`"session_info"`, the object of `GET /api/sessions/{id}` (HTTP API); `covmap replay` output
does not have it.

`GET …/coverage` and the `json`/`csv` exports report the session as the reader last saw it:
while display states are open (no `end`, `hidden "*"` or confirmed `finish` since), the
coverage provisional at the latest POST's `now_ms`, equal to that POST's response
(`"provisional": true`, `"t_ms"` = that `now_ms`); otherwise, or when no `now_ms` is known
(no POST with one since the server started), the committed coverage. Replaying the log
(`covmap replay`) credits closed display states only, so it equals the export once the
session is closed (e.g. after a confirmed finish).

`unseen` values are fractions of organ voxels. Each organ also carries the reader layer
`"reader": null | "done" | "na"`, `"flagged": bool`, `"note": str|null`; the measured
`status` never changes because of annotations. `summary` adds `attested` (reader done on a
non-reviewed organ), `not_applicable`, `flagged`, and `remaining` (keys of organs that are not
reviewed and not marked done/na — the finish list), plus `finished` (last finish event or null). `baseline` is the per-image viewed/unviewed
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
| `POST /api/sessions` `{case, label?, from_sample?}` | `{session, case}`; `from_sample` pre-fills the session with that sample's events (marked in the log) |
| `GET /api/sessions/{id}` | session info (below), 404 if unknown |
| `POST /api/sessions/{id}/events` | coverage JSON (provisional at `now_ms`) + `session_info`; nothing applied on 400 (invalid events, foreign `session`, clock guard) or 409 (a `seq` already used for a different event: `{error, conflicts: [seq…], last_seq, last_t_ms}`, client numbering) — see "Reading events" |
| `GET /api/sessions/{id}/coverage` | coverage JSON as last shown (provisional at the latest POST's `now_ms` while views are open, else committed; "Coverage JSON") + `session_info` |
| `GET /api/sessions/{id}/gap?organ=<key>[&now_ms=]` | where the organ is still unseen (below); 400 without `organ`, 404 unknown organ |
| `GET /api/sessions/{id}/events.jsonl` | the session's event log |
| `POST /api/sessions/{id}/rules` `{min_dwell_ms?, reviewed_threshold?, skipped_threshold?}` | re-score the whole session under these research rules; returns coverage JSON |
| `GET /api/sessions/{id}/timeline?frames=40` | `{frames: [{t_ms, summary, statuses: {key: status}, coverage: {key: x}, view}]}` coverage snapshots for replay; `view` = the last stack image of the case's series displayed at or before `t_ms`, `{sop, k, voi: {wc, ww}, window}` (`window` = class of the voi), `null` before the first; volume views, hidden/end, unknown images and resent `seq` do not change it |
| `GET /api/sessions/{id}/export?format=jsonl|json|csv` | download: event log `<id>.jsonl`, coverage JSON `<id>.coverage.json` (with `session_info`; the same coverage as `GET …/coverage`), per-organ CSV `<id>.csv` (the same coverage), or `<id>-from-sample.csv` for a session started from a sample (attachment) |
| `GET /api/cases/{case}/samples` | preloaded sample reading sessions `[{name, label, source, description, events}]` |
| `GET /api/cases/{case}/outline?organ=<key>&k=<slice>` | organ contour(s) on slice k: `{contours: [[[r, c], ...], ...]}` image-pixel coords |
| `GET /api/demo` | `{case, study_uid, url}` the preloaded demo case; `GET /` redirects to it |
| `GET`/`HEAD` `/dicom-web/*`, `/wado` | read-only reverse proxy to Orthanc (`http://127.0.0.1:8042`); other methods 405, `.`/`..` segments 400; the server answers only loopback `Host` headers |
| `GET /render.html`, `/render/*` | standalone vtk.js organ-map page used by `covmap render` |
| `GET /*` | OHIF dist (`$COVMAP_OHIF_DIST`), SPA fallback to `index.html` |

Session logs persist to `$COVMAP_DATA/sessions/<id>.jsonl`.

Session info (`GET /api/sessions/{id}`, and `session_info` in coverage responses):
`{"session", "case", "label", "from_sample": name|null, "sample_events": n, "created",
"finished": bool, "events": n, "last_seq": int|null, "last_t_ms": float|null, "rules": {...},
"t_offset_ms": x, "seq_offset": n, "last_view": {sop, k, voi: {wc, ww}, window} | null}`.
`events` counts the whole log, `sample_events` the
prefilled sample events among them; `last_seq`/`last_t_ms` are the highest live (non-sample)
`seq`/`t_ms` in the client's own numbering (as POSTed, before the sample shift; null before
any), so a client can continue its numbering; `t_offset_ms`/`seq_offset` are that shift
(0 unless started from a sample). `finished` = the last `finish` event was confirmed;
`rules` = the session's rule overrides (`{}` = defaults; effective values are coverage
`params`). `created` is the server's local time stamp. `last_view` = the latest displayed
stack image of the case's series in the session (as timeline `view`; null before any), so a
resumed client can restore the slice and window.

Gap target (`GET /api/sessions/{id}/gap?organ=<key>`): `{"organ": key, "k": int|null,
"sop": str|null, "window": "soft"|"lung"|"bone", "unseen_voxels": n, "ranges": [[k0, k1], ...]}`
from the current state: committed, plus the open display states credited up to `now_ms`
(the query `now_ms`, client clock, shifted like POSTs; default the latest POST's `now_ms`
since the server started; committed only if neither). `unseen_voxels` = the organ's unseen voxels; `k` = the slice
holding most of them (ties: nearest to the organ's `center_k`, then the lower `k`), `sop`
its instance, both null when nothing is unseen. `ranges` = maximal runs of consecutive
slices (inclusive) whose unseen count is ≥ max(1, 2% of the organ's voxels on that slice);
slices under that (edge noise) are left out of the ranges but still count in
`unseen_voxels`, so `ranges` can be `[]` while `k` is set. `window` = the organ's first
qualifying window class (`windows[0]`).

## CLI (`python -m covmap <cmd>`; every command prints one JSON object to stdout)

| Command | Does |
|---|---|
| `fetch --case <id> [--series UID --collection C]` | download from TCIA NBIA v1, write source/, provenance, volume.json, ct.nii.gz |
| `orthanc-load --case <id>` | upload `source/` into Orthanc |
| `segment --case <id> [--fast] [--device auto|cpu|mps] [--roi-subset] [--reuse]` | TotalSegmentator → labels.npy, organs.json, meshes |
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

## Demo and sample sessions

- The default demo case is `pancreas-0051` (configurable `COVMAP_DEMO_CASE`); `GET /` opens
  `/coverage?StudyInstanceUIDs=<its study>` directly, so a visitor needs no import step.
- Sample sessions live in `$COVMAP_DATA/samples/<case>/<name>.jsonl` with `<name>.json`
  metadata `{label, source, description, created}`. They are real recordings of the OHIF
  instrumentation driven by the scripted browser reader (metadata `source: browser-scripted`)
  or labeled simulated logs (`source: simulated`); the UI shows the label and source. An event's
  own `source` names the instrument that recorded it, so recorded sample events say `ohif`; the
  server stamps every event copied from a sample with `from_sample: <name>` (clients cannot set
  it), and `session_info` carries `from_sample` and `sample_events`.

## UI contract (coverage mode, one page)

The page never scrolls; only the organ list (and dialogs' lists) scroll. Right panel, top to
bottom: summary line (reviewed/partial/skipped/remaining counts, overall %, session menu:
new reading, start from sample, open another case, replay, rules, export), the vtk.js 3D map
(front/back/right/left views, "Hide reviewed" toggle, legend with N/A), quick actions (Next gap,
Soft/Lung/Bone windows, Finish), the live organ list (status dot, floored %, window badge,
reader state; row actions done / N/A / flag; the selected organ's detail shows "On images a–b
of N", "Still to read: images …" in the viewer's numbering and "% unseen · reason" hints), and
a one-line research-use note.

- Jumps. Clicking a gray/yellow organ in the map, its list row, a finish-list entry or Next
  gap goes to the server's gap target (`GET /api/sessions/{id}/gap`, see "Gap target"): the
  organ's slice with the most unseen voxels, in the organ's window preset.
- Display. The viewer opens in Soft (the active preset is highlighted, with a window hint).
  Axial stacks are shown radiologically (anterior up, patient right on screen left) whatever the
  acquisition order; recorded corners stay in true patient coordinates, so scoring does not
  depend on the flip.
- Sessions. A live session is labeled "live reading (OHIF)". "Start from sample" creates a new
  session "Your reading, started from sample: <label>" holding the sample's events first; the
  sample itself is never modified. `localStorage` key `covmap:session:<caseId>` stores
  `{session, label, sample, saved}`; a reload resumes through `GET /api/sessions/{id}`,
  continuing at `last_seq + 1` and `last_t_ms + 1`. "New reading" asks before discarding.
  On a session switch, recording pauses, the old session's `end` is flushed and the event queue
  is cleared before the new session's clock starts at its first event. At `pagehide` recording
  stops, so `end` is the page's last event.
- One recording tab per session. A tab records only while it holds the Web Lock
  `covmap-session-<id>`; another tab on the same session shows "This reading is open in another
  tab — not recording here", follows the session's coverage, and can take over with Record here
  (steals the lock, continues at the server's `last_seq + 1`). Without Web Locks (non-secure
  origins) the server's 409 on a reused `seq` makes the client renumber its queue and resend once.
- Finished state follows the last `finish` event: Go back or Escape after a confirmed finish
  clears the "✓ Finished" chip at once, as on the server.
- Replay plays a session's timeline frames, moving the viewer to each frame's `view` and showing
  that frame's statuses, counts and done / N/A / flag marks; it sends `hidden '*'` when it starts,
  records nothing and disables the rules form until it closes, then restores the view.
- Finish opens a compact list of remaining gray/yellow organs with per-organ decisions
  (done / N/A / flag), Go back and Confirm finish; the research note stays visible.
- Test hooks: `window.__covmap` exposes `sessionId`, `flush()`, `newReading(): Promise<string|null>`
  (no confirm; resolves once the new session is attached) and `setRadiological(on)`.

Keyboard: `N` next gap, `D` mark selected organ done, `A` mark selected organ not applicable,
`G` flag selected organ's segmentation, `O` toggle selected organ outline on the image,
`F` finish, `1` soft, `2` lung, `3` bone, `4` liver, `5` brain (coverage mode only; stock
modes keep stock hotkeys). Every claim in the UI stays research-only: coverage measures what
was displayed (slice, region, window, dwell), not what was perceived or diagnosed.

Known limitation: magnification is not scored. A slice region counts as displayed at any zoom,
so an organ shown very small (for example at 0.25× zoom) still accrues coverage.
