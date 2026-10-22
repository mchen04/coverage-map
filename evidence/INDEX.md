# Evidence index

Concise pointers to stdout, exit statuses, timings, screenshots and limitations. Raw logs live on the mini under /Users/michaelchen/coverage-map-work/evidence (not in Git); small JSON summaries and PNGs are copied here.

| Item | Command | Exit | Result | File |
|---|---|---|---|---|
| Stock OHIF hotkeys | scratch probe (stock build, pancreas-0051) | 0 | key 3 "bone" applies Liver W150/L90; 4 applies Bone | gauntlet/stock_hotkeys.json |
| UI check (C), real samples | `python ohif/verify/ui_check.py --base http://127.0.0.1:8765 --case <case>` | 0 | 26/26 on both cases | mini: evidence/ui-check/<case>/ |
| Replay/scoring hill climb (B) | `python bench/bench_replay.py … --http` + interleaved A/B | 0 | live POST 36–59 → 1.4–1.7 ms; server RTT 60–87 → 6–7 ms; cold CLI 2.5–2.8x; warm 2x; fingerprints identical | bench/replay/final/ |
| Page-load + render hill climb (C) | `python bench/bench_pageload.py`, `bench_render.py`, interleaved A/B | 0 | map-ready after first image 141–152 → 92 ms; render 869–951 → 529–556 ms; first image 470–485 ms vs stock 492–499 ms | bench/pageload/final/, bench/render/final/ |
| Gauntlet driver re-check (partial) | `python e2e/gauntlet.py --scenarios region_skip_left_lung dwell_skip_lung organ_skip_spleen organ_skip_kidney_left --no-stock` | 1 (URLError: :8768 server stopped mid-run) | 6 of 8 rows recorded (lidc ×4, pancreas region + dwell), all sim and browser exact after the tiling/flick driver fix (round 1 exposed SC-4); pancreas spleen/kidney rows not recorded; no summary. Superseded by the final gauntlet | mini: evidence/gauntlet/check-driver/ |
| Use-it round 1 | independent Opus tester, agent-browser | — | 5 NEEDED, 12 IMPROVEMENT, 8 NIT | useit/round-1/REPORT.md |
| Review round 1 | michael-review ledger (hunters → refuters) | — | in progress: C1–C5, C7, C9 fixed; C6, C8, C10, C11 survived and being fixed | ledger outside repo |
