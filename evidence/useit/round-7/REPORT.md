# Coverage Map: use-it round 7

- **Build/port:** frozen candidate ae6a0f5, http://127.0.0.1:8775/ on the mini. Cases pancreas-0051 (demo) and lidc-0001.
- **Browser:** agent-browser 0.25.4 (Chromium), session `covmap-useit-r7`, `--use-angle=metal,--enable-gpu`. The session is now closed.
- **Evidence:** the screenshots cited for X1–X3, the unscored tab note and the MPR loading / Reset View steps (18b, 18e) are here; every step screenshot (`NN*-*.png`) and the downloaded exports are on the mini under `coverage-map-work/evidence/useit-r7/`.
- **Viewports:** 1440x900 for the whole run, plus a check at 1280x800 (main view, Finish dialog, Session menu).
- **Input method:**
  - agent-browser clicks (snapshot refs), tabs, downloads and screenshots.
  - Real wheel, drag, mouse and key input through Playwright attached over CDP.
  - `eval` was used only to read state: the localStorage session key, document.visibilityState and hasFocus, export link hrefs, button titles, panel innerText, and the replay slider's aria-valuetext.
  - Server checks used curl GETs only.

## Core loop

Wall times include about 2 to 3 s of tool overhead per command, so a human would be faster.

| Case / run | Time | Keys | Clicks | Wheel | Result |
|---|---|---|---|---|---|
| Pancreas 1: soft, lung and bone passes, then N and Finish | 123 s | 4 (2, 3, N, F) | 1 (Confirm) | 552 notches | 21/21 reviewed, 100% |
| Pancreas 2: New reading, soft pass, then N-driven | 162 s | 8 (1, N×6, F) | 4 (Session, New reading, Start, Confirm) | 483 | 21/21, 99.9% |
| Chest 1: Session, Open another case, lidc-0001, three passes | 92 s | 3 (2, 3, F) | 4 | 396 | 25/25, 100% |
| Chest 2: fast-scroll misuse, then N while zoomed or panned | 106 s | 4 (N×3, F) | 1 | ~409 notches + 2 drags | 25/25, 99.7% |

## Findings

| Id | Severity | Finding | Suggested fix |
|---|---|---|---|
| X1 | IMPROVEMENT | A reload while reading in an MPR layout does not resume where the reader was. Before the reload: MPR, axial 123/185, lung W1500/L-600. After it: the 1x1 stack at the last *stack* image and window (126/185, soft W400/L40), even though the chip says "Resumed". Coverage is intact. (X1-mpr-before-reload-axial-123-lung.png, X1-after-reload-stack-126-soft.png) | Save the last volume view (layout, axial position, window) as the resume point. At minimum, restore its window and the nearest axial image. |
| X2 | IMPROVEMENT | Replay plays a 5:14 session in about 6 s (40 coarse frames). There is no speed control and no frame step, so scrubbing is the only way to inspect it. (X2-replay-running.png, X2-replay-scrubbed-end.png) | Add speed options (1x, 4x, fit) and step-frame buttons or arrow keys. |
| X3 | NIT | The session label in the panel header truncates to "Your reading, starte…" once the Finished, Resumed and custom-rules chips show. (X3-label-truncated-finished.png, X3-label-truncated-mpr.png) | Let it wrap, or add a tooltip with the full label. |

**No NEEDED findings.**

**Seen once, not reproduced (note, not scored):**
- In s-20261022-035348-31b239 (lidc, MPR), tab A did New reading, then F, then Esc. A was then hidden by a chrome://settings/help tab that the automation browser opened by itself (`hidden *`, seq 2).
- About 60 s later I opened window B. B became the recorder while A showed the banner. B's D (seq 7) and Confirm Finish (seq 8) were recorded, and A took over by itself when B closed (seq 9 to 11).
- The banner was truthful and nothing was lost. Five retraces (ctl1, nr1, hid1, hid2, s-20261022-040031-201323) all kept the first tab recording. This may be W1-like. (NOTE-inversion-tabA-banner-while-B-recorded.png)

## Two-tab server check (s-20261022-034024-ca8c11, min dwell 3000 ms)

- **B takes over from A.** A showed lung image 139/185 (k=46) for 1.1 s, then B clicked Record here.
  - events.jsonl has A's view (seq 17), then `hidden *` (seq 18), then B's view.
  - A kept displaying k=46 for another 15 s, but gap/coverage still lists k=46 as unseen. A was not credited after the takeover.
- **A takes back from B.** B showed k=56 for 6.06 s, then A clicked Record here. The gap became [37,55],[57,79], so B's dwell before the takeover counted.
- **Record here while B had Replay open.** Only `hidden *` was logged (seq 92) and A's scrolling logged nothing. When B closed Replay, B's view was logged (seq 93).
- **B closed.** `end` was logged (94). A took over by itself with `hidden *` (95) and a view (96) about 2.7 s later.
- **Non-recording tab.** A tab showing the banner logs no views.

## What worked

- **Arriving cold:** the hint, legend, window buttons, shortcut strip and research disclaimer make the task clear.
- **Scoring:** lung shown in a soft window reads "wrong window"; 48 ms per image reads "too brief"; zooming into air and panning off-screen give no credit. Fast scrolling earns nothing.
- **Jumping to gaps:**
  - Map clicks work on yellow colon and on gray liver, even from a fully panned-off view.
  - N lands in the right window with the organ on screen, in the stack and in MPR.
- **Marks, flags, rules:**
  - D, A and G toggle on and off; O draws the outline.
  - A flag note saves. Clearing the note removes it on screen and in the export (the flag stays).
  - The rules form validates input, and a 70% rule re-scores and shows a "custom rules" chip.
- **Exports match the screen:**
  - After Finish: provisional=false, finish confirmed, at_finish 13/3/5, 0.8086.
  - After reading on: provisional=true, the finish is kept, and the chip reads "Finished at 82.6% · reading continued (now 82.8%)".
  - The CSV has the reader, flagged and note columns.
- **Finish flow:**
  - Go back logs confirmed:false.
  - After confirming, reopening Finish and backing out with Go back or Esc keeps "✓ Finished".
  - Confirming again records a newer finish.
  - Recent readings shows "✓ finished".
- **Replay:** shows the frame statuses, logs `hidden *`, records nothing and restores the view on close.
- **Sessions and cases:**
  - Starting from the sample, New reading, and opening an earlier reading from the Session menu all work.
  - A stack reload resumes the slice and window (lung, 40/185).
  - Opening the chest case and coming back resumes the pancreas reading.
- **Reset View:** keeps the preset in the stack, in MPR and in a reoriented coronal viewport.
- **MPR loading:** nothing is logged until the volume is ready, and coronal bone scrolling credits bones.
- **Hidden tab during resume, earlier-reading open, case open or sample start:** no views are logged while hidden, and the view is captured on return.
- **Busy tabs:** a mark or Finish during a reading switch is refused with a notice.
- **1280x800:** layout, Finish dialog and Session menu fit.
