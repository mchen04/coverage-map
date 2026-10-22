# Coverage Map: use-it test, round 3 (independent, cold)

**Build:** frozen final build at http://127.0.0.1:8769/ on the mini, which was not restarted.
**Browser:** agent-browser 0.25.4, session `covmap-useit-r3`, Chrome for Testing 155, `--use-angle=metal,--enable-gpu`, headless.
**Viewports:** 1440x900 for the whole run, plus a check at 1280x800.
**Input:** real CDP input only: `Input.dispatchMouseEvent` for mouseWheel, left click, right-drag zoom and middle-drag pan, plus real key presses. I used `eval` only to read state, such as tooltips, the slice/W-L overlay and focus.
**Environment note:** once during the run, the automation Chrome process exited mid-scroll. There was no crash report, and I do not attribute it to the app. I reopened the app.

## Core loop: timing and input counts (from first input to confirmed finish)

| Case | Time | Wheel ticks (150 ms/tick) | Keys | Clicks | Notes |
|---|---|---|---|---|---|
| pancreas-0051, new reading | 116 s | 442 (soft 184, lung 75, bone 75+108) | N x4, F x1 | 1 to focus the image, 1 to confirm | Each N chose the right next window: lung at 185/185, bone at 110/185, then 0 left. |
| lidc-0001 (chest) | 107 s | 396 (3 x 132) | 2, 3, N, F | 3 to open the case, 1 to confirm | All 25 organs green after 3 straight passes. N said "No gaps left". |

The wheel input takes most of the time. The app adds about 6 inputs per case.

## Findings

| Id | Severity | Finding | Suggested fix |
|---|---|---|---|
| S1 | NEEDED | **Next gap (N), map clicks and finish-list clicks keep the current zoom and pan, so the target organ can be off-screen.** The jump sets the slice and window, but the reader lands on air or the wrong part of the body, and scrolling there scores nothing. | On a jump, if the target's outline on that slice is not fully in the viewport, pan and zoom to fit it. Keep the window preset. |
| S2 | IMPROVEMENT | After a confirmed Finish the reading keeps recording under the "✓ Finished" badge. Scrolling 5 slices moved the header from 0/12/9 to 0/13/8 and 10% to 11%. The exports then show `provisional:true` and a summary that no longer matches what was confirmed, because `finished` keeps only `remaining` and `t_ms`. | Store the confirmed snapshot (statuses and counts) in `finished`, label later events as post-finish, or ask "Reopen reading?" on the first input after Finish. |
| S3 | IMPROVEMENT | Two tabs: the newest tab records and the other shows a clear banner with "Record here", which works. But after I closed the recording tab, the remaining tab stayed on "not recording" for more than 65 s, and its scrolls were not recorded (the event count stayed at 403) until I clicked "Record here". | Take over recording automatically when the recording tab closes (BroadcastChannel/pagehide or a short lease). |
| S4 | IMPROVEMENT | OHIF's Reset View, the obvious way out of S1, also resets the window to the DICOM default (W2400/L176). That counts for no organ, and no preset button lights up. | Keep the active preset on Reset View, or show "custom window: nothing counts" in the panel. |
| S5 | IMPROVEMENT | With a fresh browser profile, the app silently started a new reading. My unfinished reading of the same case was on the server but was not offered. | Add "Recent readings for this case" to the Session menu. |
| S6 | NIT | After a reload, the chip "resumed New reading" reads as one phrase. | Write it as "Resumed · Start new reading…". |
| S7 | NIT | At 1280x800 the Finish panel's "Your decisions" line is half hidden behind the Go back/Confirm buttons. | Reserve space or make the line scroll with the list. |

### S1 reproduction
1. Session ▾ → New reading → Start new reading. Press 1, then wheel about 60 slices down.
2. Right-drag on the image to zoom in on the left kidney (about 4x). Middle-drag a little.
3. Press N. It lands on Heart, image 183/185, soft window, but the view shows lung air and a rib (`S1-N-keeps-zoom-target-offscreen.png`).
4. Wheel 8 slices. Heart stays at 0%, and its "outside the view" share rises from 10% to 28% (`S1-N-zoomed-scroll-heart-stays-0.png`).
5. A pan only (middle-drag 550 px left) gives the same result. Clicking Liver on the 3D map jumps to 126/185 soft with the liver off-screen (`S1-mapclick-after-pan-liver-offscreen.png`). N goes to Aorta, also off-screen (`S1-N-after-pan-aorta-offscreen.png`).

## What worked
- **Arrival:** the cold start was clear. There is a one-line hint, a colour legend under the map, an S/L/B window badge on every row, and a research-only footer. The hint can be dismissed.
- **Scoring:** it resisted every trick I tried.
  - Bone window over the abdomen counts as "shown only in a non-qualifying window" (`W-wrong-window.png`).
  - 60 slices in 0.6 s counts as "shown too briefly" (`W-fast-scroll-too-brief.png`).
  - Zooming into air scored nothing.
  - With the liver panned off-screen, the liver stayed at 1% while the spleen, stomach and kidney went up (`W-pan-liver-offscreen-not-counted.png`).
- **Live list and 3D map** updated within about a second. The detail line gives the reasons and "Still to read: images …". Map clicks and N chose the right window.
- **Reader tools:**
  - D, A and G work on the selected row. G opens a note field, typing the note does not trigger shortcuts, and Enter saves it.
  - O toggles the outline.
  - Rules form: Apply re-scores the session and Restore defaults works. Contradictory thresholds (reviewed 3, skipped 50) are rejected with a clear message.
- **Finish:** the Finish panel lists the remaining organs and the reader's own decisions, and clicking an organ jumps to it. Go back and Escape both close it, and Confirm works.
- **Exports right after Finish** matched the screen exactly:
  - complete session: 21/0/0, `provisional:false`, `finished.confirmed:true`
  - partial session: 0/12/9 with 19 remaining, done/na/flag and the note, matching across CSV and JSON
- **Replay:** recording pauses, the panel replays, scrubbing works, and closing the replay returns the reader to their slice.
- **Start from sample:** a confirm step warns that the current reading ends. The sample events are badged "scripted automated reading (not a human)".
- **Reload:** it resumed the exact slice and window (148/185, bone 2500/480).
- **Chest case:** opened lidc-0001 and came back to pancreas-0051. The pancreas reading resumed at its last view.
- **1280x800:** the layout is usable.

No data loss or wrong score was seen apart from S1.
