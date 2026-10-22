# Gauntlet, final (frozen build 711c0a9)

Command (on the mini): `python e2e/gauntlet.py --cases lidc-0001 pancreas-0051 --coverage-base http://127.0.0.1:8768 --stock-base http://127.0.0.1:8766 --out $MINI_WORK/evidence/gauntlet/final`, exit 0.
Each scripted reading (source `simulated`, known skip truth) is scored three ways: by Coverage Map from the simulated log (`sim`), by Coverage Map in a real browser driving OHIF with wheel/keys/camera (`browser`), and by two slice-level tools: stock OHIF 3.13's built-in `viewedDataService` (real browser, stock build) and the per-image viewed/unviewed model in `covmap/baseline.py` (our documented model of the per-image tracking described for Synapse/eRAD; not the proprietary product, never tested against it). Slice-level tools are judged by the organ coverage their viewed images imply, at the same thresholds (reviewed >= 0.90, skipped < 0.05).

Cells: caught / missed / false alarms over the scenario's intended skips (organs expected not `reviewed`).

| Case | Scenario | Truth skips | Coverage Map sim | Coverage Map browser | Stock OHIF | Per-image model | Browser run |
|---|---|---|---|---|---|---|---|
| lidc-0001 | full_read | 0 | 0/0/0 exact | 0/0/0 exact | 0/0/0 | 0/0/0 | 80.28 s, 396 wheel ticks |
| lidc-0001 | window_skip_lung | 2 | 2/0/0 exact | 2/0/0 exact | 0/2/0 | 0/2/0 | 54.99 s, 264 wheel ticks |
| lidc-0001 | window_skip_bone | 8 | 8/0/0 exact | 8/0/0 exact | 0/8/0 | 0/8/0 | 53.65 s, 264 wheel ticks |
| lidc-0001 | region_skip_left_lung | 1 | 1/0/0 exact | 1/0/0 exact | 0/1/0 | 0/1/0 | 102.06 s, 396 wheel ticks |
| lidc-0001 | dwell_skip_lung | 2 | 2/0/0 exact | 2/0/0 exact | 0/2/0 | 0/2/0 | 61.15 s, 396 wheel ticks |
| lidc-0001 | slice_skip_caudal | 20 | 20/0/0 exact | 20/0/0 exact | 20/0/0 | 20/0/0 | 44.75 s, 216 wheel ticks |
| lidc-0001 | partial_lung | 2 | 2/0/0 exact | 2/0/0 exact | 0/2/0 | 0/2/0 | 64.64 s, 315 wheel ticks |
| lidc-0001 | organ_skip_spleen | 1 | 1/0/0 exact | 1/0/0 exact | 0/1/0 | 0/1/0 | 105.23 s, 396 wheel ticks |
| lidc-0001 | organ_skip_kidney_left | 1 | 1/0/0 exact | 1/0/0 exact | 0/1/0 | 0/1/0 | 109.36 s, 396 wheel ticks |
| pancreas-0051 | full_read | 0 | 0/0/0 exact | 0/0/0 exact | 0/0/0 | 0/0/0 | 112.2 s, 552 wheel ticks |
| pancreas-0051 | window_skip_lung | 2 | 2/0/0 exact | 2/0/0 exact | 0/2/0 | 0/2/0 | 74.34 s, 368 wheel ticks |
| pancreas-0051 | window_skip_bone | 5 | 5/0/0 exact | 5/0/0 exact | 0/5/0 | 0/5/0 | 74.67 s, 368 wheel ticks |
| pancreas-0051 | region_skip_left_lung | 1 | 1/0/0 exact | 1/0/0 exact | 0/1/0 | 0/1/0 | 141.88 s, 552 wheel ticks |
| pancreas-0051 | dwell_skip_lung | 2 | 2/0/0 exact | 2/0/0 exact | 0/2/0 | 0/2/0 | 86.75 s, 552 wheel ticks |
| pancreas-0051 | slice_skip_caudal | 8 | 8/0/0 exact | 8/0/0 exact | 8/0/0 | 8/0/0 | 88.8 s, 423 wheel ticks |
| pancreas-0051 | partial_lung | 2 | 2/0/0 exact | 2/0/0 exact | 0/2/0 | 0/2/0 | 83.18 s, 403 wheel ticks |
| pancreas-0051 | organ_skip_spleen | 1 | 1/0/0 exact | 1/0/0 exact | 0/1/0 | 0/1/0 | 2037.57 s, 552 wheel ticks |
| pancreas-0051 | organ_skip_kidney_left | 1 | 1/0/0 exact | 1/0/0 exact | 0/1/0 | 0/1/0 | 238.18 s, 552 wheel ticks |

| Totals | caught | missed | false alarms | exact rows |
|---|---|---|---|---|
| Coverage Map, sim | 59 | 0 | 0 | 18 |
| Coverage Map, browser | 59 | 0 | 0 | 18 |
| Stock OHIF viewedDataService | 28 | 31 | 0 | — |
| Per-image model (browser log) | 28 | 31 | 0 | — |

What the slice-level tools miss: skips inside images that were displayed — the wrong window (lung or bone organs read only in soft tissue), a region panned or zoomed off-screen, a flick shorter than the dwell, and a single organ left unread on slices that were shown for other organs. They catch slice ranges never displayed (`slice_skip_caudal`), where both they and Coverage Map flag every organ in the range. No scenario had a slice-level catch that Coverage Map missed (`baseline_catches_missed_by_coverage_map`: []).

Limitations: the readings are scripted (labelled `simulated`), not human reads; two public CTs; stock OHIF's tracker counts an image viewed on any display (default `viewedDwellMs` 0), which is its design, not a defect; the per-image model is an implementation of the described behaviour, not the vendor product. The pancreas `organ_skip_spleen` browser run took 2038 s because its plan tiles the reading into 12,426 zoomed frames; the result is still exact. Raw logs, per-row coverage, viewer screenshots and maps stay on the mini under `evidence/gauntlet/final/`.
