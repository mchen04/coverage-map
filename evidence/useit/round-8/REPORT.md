# Coverage Map: use-it round 8

- **Build/port:** frozen candidate 17df1f5, http://127.0.0.1:8775/ on the mini. Cases pancreas-0051 (demo) and lidc-0001 (chest).
- **Evidence:** the Y1 screenshots are here; every step screenshot (`NN*-*.png`) and the downloaded exports are on the mini under `coverage-map-work/evidence/useit-r8/`.
- **Browser:** agent-browser 0.25.4 (Chrome for Testing 155, headless), session `covmap-useit-r8`, `--use-angle=metal,--enable-gpu`. The session is closed.
- **Viewports:** 1440x900 for the whole run, plus a 1280x800 check (MPR layout, Finish dialog with 25 organs, Session menu).
- **Input method:**
  - agent-browser clicks (snapshot refs), keys, downloads and screenshots.
  - Real wheel, drag (right = zoom, middle = pan), mouse and key input through Playwright attached over CDP. Wheel pace 100 to 150 ms per notch; the fast-scroll test used 8 ms per notch.
  - The second tab was a new window opened with CDP `Target.createTarget`. MPR loads were slowed with CDP network throttling (3 MB/s, cache off).
  - `eval` was used only to READ state: the localStorage session key, visibilityState and hasFocus, panel text and the image counter, and the export link hrefs.
  - Server checks used curl GETs of `/api/sessions/{id}/coverage` and `/events.jsonl` only.

## Core loop

Wall times include about 2 to 3 s of tool overhead per command, so a human would be faster.

| Case / run | Time | Keys | Clicks | Wheel | Result |
|---|---|---|---|---|---|
| Pancreas 1: fresh page, then soft, lung and bone passes, N (nothing left) and F (s-20261022-041209-12c9af) | 126 s | 4 (2, 3, N, F) | 1 (Confirm) | 553 notches | 21/21 reviewed, 100% |
| Pancreas 2: New reading, soft pass, then N-driven (s-20261022-041709-67f9f3) | 131 s | 6 (1, N×4, F) | 4 (Session, New reading, Start, Confirm) | 466 | 21/21, 99.9% |
| Chest: Session > Open another case > lidc-0001 (ready in about 4 s), then New reading, soft pass, then N-driven (s-20261022-043108-f96341) | 113 s | 4 (N×3, F) | 4, plus 3 to open the case | 507 | 25/25, 100% |

## Findings

| Id | Severity | Finding | Suggested fix |
|---|---|---|---|
| Y1 | IMPROVEMENT | **Reset View during an MPR load shows the wrong window.** If you press Reset View while an MPR volume is still loading, the active (axial) viewport shows the DICOM default lung window (about W1600/L-600) until the load finishes. Meanwhile its overlay still reads W:400 L:40 and Soft stays highlighted. On a 3 MB/s link this lasted about 20 s. Once slice 67 had loaded, its chest anatomy showed in a lung window labelled soft. The sagittal and coronal panes stayed soft, so the axial unloaded band was light gray while theirs was mid-gray. Nothing was credited: the only views were logged after the load, all at W400/L40. The soft preset came back when the load ended. Control run: without Reset View, the axial pane stayed soft throughout. (Y1-reset-view-during-mpr-load-axial-lung-vs-W400-overlay.png, Y1-anatomy-in-lung-window-labelled-W400-L40.png, Y1-after-load-soft-restored.png, Y1-control-no-reset-view-load.png, Y1-control-no-reset-view-loaded.png) | Re-apply the preset VOI straight after Reset View and on every progressive-load render, not only at load end. Or keep the VOI unchanged when Reset View is pressed during a load. |

Y1 repro:
1. Open lidc-0001 in the 1x1 stack with Soft (1) at image 67/133.
2. Throttle the network to about 3 MB/s with the cache disabled, then reload. The reading resumes.
3. Toolbar: Layout > MPR. Within 1 s, click Reset View.
4. Watch the axial pane: it shows the lung window while its overlay reads W:400 L:40.
5. When the load finishes, the pane switches to soft.

**No NEEDED findings.**

**Harness notes (not scored):**
- **How the tab was hidden.** In this headless browser, Esc on the Finish dialog opens chrome://settings/help in front of the page, which hides it. This was also seen in r5 and r7, and I used it as the "switch tab" hide. Bringing other tabs to the front over CDP did not change visibilityState here.
- **Input sent to a hidden page is recorded.** Key or wheel input sent over CDP while the page is hidden gets recorded: views about 170 ms apart, then `hidden *` about 180 ms after the last one (chest s-…f96341, seq 541 to 547). A real user cannot scroll a hidden tab, so this is not a finding.

## Two-tab server check (s-20261022-043424-00061f)

- **Second tab opens.** Tab A was recording. Tab B opened in a second window on the same reading and showed "This reading is open in another tab — not recording here until that tab closes", with Record here. B logged nothing.
- **A records.** A scrolled 40 images: events went from 31 to 71, and the unviewed ranges were [[0,83],[155,184]].
- **B takes over with Replay open.** B opened Replay, then clicked Record here. Only `hidden *` was logged (seq 71), and A showed the banner.
  - A then scrolled 84 more images. The event count stayed at 72, coverage stayed 0.281109 and the unviewed ranges did not change.
  - A's last image (k=84, about 25 s on screen) counted up to the takeover, and nothing A displayed afterwards counted.
- **B closes Replay.** B logged its view (seq 72), went back to its own image (61/185), and its scrolling recorded.
- **B's tab closes.** `end` was logged (seq 83). A took over by itself 2 ms later (`hidden *` seq 84, then a view at seq 85), and A's banner went away.

## What worked

- **Arriving cold:** the hint line, colour legend, per-organ S/L/B window badges, shortcut strip and research disclaimer make the task clear.
- **Scoring:**
  - Lungs in a soft window read "wrong window".
  - Fast scrolling (95 images at 8 ms each) reached 0.6%, with organs marked "too brief".
  - Zoomed into air for 60 images: coverage stayed at 0.0057.
  - With the image panned partly off-screen, organs read "off-view" and partial. At a corner zoom, only the tissue shown was credited.
- **Jumping to gaps:** N and map clicks open the organ in the right window and on screen:
  - yellow colon while zoomed and panned
  - gray spleen while panned off-screen (the view is re-panned)
  - gray right ribs from a soft window (switches to bone)
  - N in MPR
  - a Finish-list click on Liver

  With nothing left, N says "No gaps left".
- **Marks and flags:**
  - D, A and G toggle; O draws the outline.
  - A flag note saves on Enter.
  - Emptying the note removes it on screen and in the JSON and CSV; the flag stays.
  - Unflagging logs `clear`.
- **Rules:** 150% is refused with a message; 70% re-scores the session and shows a "custom rules" chip.
- **Exports match the screen:**
  - Right after Finish: provisional=false and the finish is confirmed. at_finish is 14/2/5 at 0.8407, matching "6 left · 84%" on screen.
  - After reading on: live 15/5/1 at 0.8753, matching the chip "Finished at 84% · reading continued (now 87%)". The finish is kept.
  - Chest, after Confirm then reopening and backing out: the finish stays confirmed and the screen shows "✓ Finished".
  - The CSV reader, flagged and note columns match.
- **Finish flow:**
  - Go back logs confirmed:false.
  - After confirming, reopening Finish and leaving with Go back or Esc keeps "✓ Finished", and the server's finish time does not change.
  - Confirming again records a newer finish (347.0 s → 448.0 s; remaining went from 6 to 5).
  - Recent readings shows "✓ finished".
- **Replay:** plays with statuses, scrubs, logs `hidden *`, records nothing and restores the view on close.
- **Sessions and cases:**
  - Start from sample works and shows the sample-events chip. New reading works.
  - A reload resumes at lung 122/185, W1500/L-600.
  - Going to the chest case and back resumes the pancreas reading.
  - An earlier finished reading opened from Recent readings resumes at its last image and window (bone, 1/185).
- **Reset View:** keeps lung in the stack, bone in MPR, and lung in a reoriented coronal viewport (once the volume is loaded).
- **MPR loading:** views are logged only after every frame has loaded, with or without Reset View. That took 0.3 s cached, and about 7 s and 22 s on the throttled chest.
- **Hidden during reload and resume:** while hidden, the panel reads "No session yet" and no views are logged. On return it resumed at 90/185, lung W1500/L-600, and logged one view.
- **1280x800:** the MPR layout, the 25-organ Finish dialog and the Session menu all fit.
