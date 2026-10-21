# Coverage Map

Research-only CT reading-coverage map. While you read a CT in OHIF, Coverage Map records what
the viewer actually displayed — slice, visible region, window preset and dwell — and scores
each organ: **reviewed** (green), **partly reviewed** (yellow) or **skipped** (gray), live on a
3D organ model.

> Research use only. Not a medical device. Coverage measures what was displayed, not what a
> reader perceived or concluded; a green organ does not prove it was clinically reviewed.

## Try it

```bash
scripts/launch.sh            # starts the services on the Mac mini and tunnels them here
open http://127.0.0.1:8765/  # opens the preloaded demo CT directly; no import needed
scripts/launch.sh stop
```

The demo opens a public, deidentified abdominal CT with a fresh reading session. Scroll
(wheel), switch windows (`1` soft, `2` lung, `3` bone) and watch organs turn green. `N` jumps to
the next gap with the right window, clicking a gray/yellow organ in the 3D map does the same,
and **Finish** (`F`) lists what is still gray/yellow before you confirm. The session menu loads
a recorded sample reading, replays a session, adjusts the research reading rules and exports
the log/coverage.

## How it works

| Piece | Implementation |
|---|---|
| Viewer | OHIF v3.13.13 + Cornerstone3D 5, custom `coverage` mode and extension (`ohif/`) |
| Instrumentation | Cornerstone render/camera/VOI events → one event per distinct displayed state (slice, canvas corners in patient mm, window, time) |
| Images | Orthanc 1.13 with the DICOMweb plugin, behind the app server's same-origin proxy |
| Organs | TotalSegmentator 2.18 (`total` task) → organ label map in the DICOM grid + surface meshes |
| Scoring | Python (`covmap/scoring.py`): a voxel counts when it was inside the visible region of a displayed slice, under a window that suits its organ (lung / bone / soft tissue), for at least the minimum dwell |
| 3D map | vtk.js, the same module in the OHIF panel and in `covmap render` |
| API | aiohttp (`covmap serve`): sessions, live scoring, meshes, samples, export, DICOMweb proxy |

The full contract is in [docs/SPEC.md](docs/SPEC.md).

## CLI

Every command prints one JSON object.

```bash
python -m covmap fetch --case lidc-0001            # public TCIA download + provenance + hashes
python -m covmap orthanc-load --case lidc-0001
python -m covmap segment --case lidc-0001          # TotalSegmentator → labels, organs, meshes
python -m covmap scenarios --case lidc-0001
python -m covmap simulate --case lidc-0001 --scenario window_skip_lung --out r.jsonl
python -m covmap replay --case lidc-0001 --events r.jsonl --out cov.json
python -m covmap report --case lidc-0001 --coverage cov.json --truth r.jsonl.truth.json
python -m covmap render --case lidc-0001 --coverage cov.json --out map.png
python -m covmap serve --port 8765
```

Commands run on the Mac mini (`scripts/mini.sh -- python -m covmap …`), where the toolchain
and data live.

## Data

| Case | Source | License |
|---|---|---|
| `lidc-0001` | TCIA LIDC-IDRI, LIDC-IDRI-0001 (chest CT) | CC BY 3.0 |
| `pancreas-0051` | TCIA Pancreas-CT, PANCREAS_0051 (abdominal CT) | CC BY 3.0 |

Both are public, deidentified research datasets downloaded without credentials; each case's
`provenance.json` records URLs, DOIs and file hashes.

## Evaluation

See [evidence/INDEX.md](evidence/INDEX.md): end-to-end CLI runs on both CTs with known skip
truth, performance baseline vs final, the gauntlet against stock OHIF's built-in viewed-slice
tracker and an implemented per-image viewed/unviewed model, review and UI evidence.
