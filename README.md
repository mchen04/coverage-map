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
python -m covmap simulate --case lidc-0001 --scenario window_skip_lung --out work/r.jsonl
python -m covmap replay --case lidc-0001 --events work/r.jsonl --out work/cov.json
python -m covmap report --case lidc-0001 --coverage work/cov.json --truth work/r.jsonl.truth.json
python -m covmap render --case lidc-0001 --coverage work/cov.json --out work/map.png
python -m covmap serve --port 8765
```

Commands run on the Mac mini (`scripts/mini.sh -- python -m covmap …`), where the toolchain
and data live. Each `mini.sh` call re-syncs the checkout there and deletes other files, so
outputs go under `work/` (kept, like `data/`).

## Data

| Case | Source | Dataset DOI | License |
|---|---|---|---|
| `lidc-0001` | TCIA LIDC-IDRI, LIDC-IDRI-0001 (chest CT) | [10.7937/K9/TCIA.2015.LO9QL9SX](https://doi.org/10.7937/K9/TCIA.2015.LO9QL9SX) | CC BY 3.0 |
| `pancreas-0051` | TCIA Pancreas-CT, PANCREAS_0051 (abdominal CT) | [10.7937/K9/TCIA.2016.tNB1kqBU](https://doi.org/10.7937/K9/TCIA.2016.tNB1kqBU) | CC BY 3.0 |

Images shown in screenshots and the walkthrough come from these collections; cite the dataset
DOIs above (and TCIA, doi:10.1007/s10278-013-9622-7) when reusing them.

Both are public, deidentified research datasets downloaded without credentials; each case's
`provenance.json` records URLs, DOIs and file hashes.

## Setup

Everything runs in a task directory on the Mac mini, nothing is installed globally:

```bash
scripts/mini.sh -- bash scripts/setup_mini.sh all        # Python venv, Node, Orthanc, OHIF, Playwright Chromium
scripts/mini.sh --heavy -- bash scripts/build_ohif.sh    # Coverage Map OHIF build -> $MINI_WORK/ohif-dist
scripts/mini.sh --heavy -- bash scripts/stock_ohif.sh    # unmodified OHIF baseline -> $MINI_WORK/ohif-dist-stock
scripts/mini.sh -- bash scripts/services.sh start         # Orthanc, Coverage Map (8765), stock OHIF (8766)
scripts/mini.sh -- python -m covmap fetch --case pancreas-0051   # then orthanc-load and segment as above
scripts/mini.sh -- python e2e/make_sample.py --case pancreas-0051  # the sample reading, recorded in OHIF
scripts/mini.sh --heavy -- python -m pytest -q
```

Repeat the case steps for `lidc-0001`.

## Evaluation

- **Scripted readings with known skips** ([gauntlet matrix](evidence/gauntlet/final/MATRIX.md)):
  18 readings over both CTs, scored from the log and in a real browser driving OHIF. Coverage Map
  flags exactly the intended skips in all 18 (59 caught, 0 missed, 0 false alarms). Stock OHIF's
  built-in viewed-image tracker and a per-image viewed/unviewed model each catch 28: they see
  slices never displayed, not a wrong window, a region off-screen, a too-brief look or one organ
  left unread. The per-image model implements the behaviour described for Synapse/eRAD; it is
  not the vendor product and was not tested against it.
- **Performance** ([docs/PERFORMANCE.md](docs/PERFORMANCE.md)): segmentation 377 → 105 s and
  189 → 60 s, live scoring 36–59 → 1.4–1.7 ms per update, render 869–951 → 529–556 ms. Scoring
  and render outputs are identical; the faster segmentation changes 4 / 16 voxels (per-organ
  Dice ≥ 0.99999).
- [evidence/INDEX.md](evidence/INDEX.md) lists every evaluation run with its command, exit
  status and result, including the review and hands-on use rounds.

The readings are scripted (labelled `simulated`), not clinical reads, on two public CTs.
