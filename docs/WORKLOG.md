# Worklog (resumable)

Stages are kept separate: S1 build, S2 performance hill climb, S3 gauntlet, S4 Michael-review, S5 UI cycles, S6 final verification.

## S1 build
- 2026-10-21 15:40 mini toolchain: venv py3.12 (TotalSegmentator 2.18.0, torch 2.14.1, MPS available), Node v24.22.1 + pnpm 12.8.1, Orthanc 1.13.0 (Orthanc-macOS-26.9.1.zip sha256 5bcc1450…4617) with DicomWeb plugin, OHIF v3.13.13 (commit 1fa151f) installed in 55 s.
- Stock OHIF build: 14 s (rspack), dist 216 MB -> $MINI_WORK/ohif-dist-stock, runtime config config/ohif-stock-app-config.js.
- Headless Chromium 156 on the mini: default WebGL2 = SwiftShader; `--use-angle=metal --enable-gpu` = ANGLE Metal Apple M4 (used for all browser work).
- Stock OHIF 3.13 has a built-in per-slice viewed tracker (cornerstone ViewedDataService, scrollbar "viewed fill", marks on slice index change, viewedDwellMs default 0) exposed via window.services -> gauntlet stock column reads it directly.
- Implementers dispatched (Opus): A data+segmentation, B scoring/simulate/report/server, C OHIF extension/mode/vtk.js map/render.
- 2026-10-21 15:58 session migrated mbp-main -> mbp-old (same conversation). Implementers had run ~5 min: only covmap/{fetch,dicom_io,organs}.py on disk; both cases fetched on the mini; ohif-cov pristine; no weights yet. All three resumed via SendMessage with the expanded brief.
- Card expanded (RULES sha 97a308e5): stop only when every Done item passes (no stagnation stop); demo CT + sample session preloaded; compact one-page UI; 3D click-to-jump; finish dialog; reader done/N-A/flag + outline; shortcuts; export/replay/rules; Opus use-it loop until a full run finds nothing; then cleanup -> prune tests -> docs -> full checks -> private GitHub push -> walkthrough video. SPEC.md extended accordingly.
- 16:26 A done. fetch: lidc-0001 133 sl 0.703 mm px 2.5 mm slices (zip sha256 90bb5f58…e83c, manifest f5e561ee…eca5); pancreas-0051 185 sl 0.703 mm px 1.0 mm slices (zip 3f62309e…72dd, manifest 0f3775d7…06e5); CC BY 3.0; NBIA v1 no credentials. Orthanc: 133 + 185 instances. TotalSegmentator weights Dataset291-295 from GitHub releases v2.0.0-weights; usage stats disabled.
- MPS Conv3d on this torch 2.14.1/macOS 26.2 returns zeros (self-test rel err 0.968) -> segment refuses mps. CPU torch lacks MKLDNN -> native conv3d 17 GB RSS, ~4.6 h; A's exact conv3d-as-sum-of-conv2d path (rel err 5e-7): lidc 451.6 s (7.22 GB RSS), pancreas 252.1 s (6.05 GB) functional runs under load ~12-17. 25 / 21 organs; meshes 221k / 199k tris. Alignment checks (lung <-500 HU 96%, liver median HU 73/106, L/R ordering) pass. 48 tests pass.
