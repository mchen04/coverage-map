# Worklog (resumable)

Stages are kept separate: S1 build, S2 performance hill climb, S3 gauntlet, S4 Michael-review, S5 UI cycles, S6 final verification.

## S1 build
- 2026-10-21 15:40 mini toolchain: venv py3.12 (TotalSegmentator 2.18.0, torch 2.14.1, MPS available), Node v24.22.1 + pnpm 12.8.1, Orthanc 1.13.0 (Orthanc-macOS-26.9.1.zip sha256 5bcc1450…4617) with DicomWeb plugin, OHIF v3.13.13 (commit 1fa151f) installed in 55 s.
- Stock OHIF build: 14 s (rspack), dist 216 MB -> $MINI_WORK/ohif-dist-stock, runtime config config/ohif-stock-app-config.js.
- Headless Chromium 156 on the mini: default WebGL2 = SwiftShader; `--use-angle=metal --enable-gpu` = ANGLE Metal Apple M4 (used for all browser work).
- Stock OHIF 3.13 has a built-in per-slice viewed tracker (cornerstone ViewedDataService, scrollbar "viewed fill", marks on slice index change, viewedDwellMs default 0) exposed via window.services -> gauntlet stock column reads it directly.
- Implementers dispatched (Opus): A data+segmentation, B scoring/simulate/report/server, C OHIF extension/mode/vtk.js map/render.
