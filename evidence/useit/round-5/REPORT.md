# Coverage Map: use-it test r5 (independent)

**Build/port:** frozen candidate 2b213dc at http://127.0.0.1:8769/ on the mini (server cwd frozen-2b213dc/repo; COVMAP_DATA is shared with the 8765/8766/8768 servers).
**Browser:** agent-browser 0.25.4, session `covmap-useit-r5`, headless Chrome for Testing 155, `--use-angle=metal,--enable-gpu` (WebGL: ANGLE Metal, Apple M4). Session closed at the end.
**Viewports:** 1440x900 throughout; 1280x800 checks (main, Session menu, Finish with 0 and with 19 remaining plus 3 decisions).
**Input:** real clicks and keys via agent-browser; image scrolling, zoom (right-drag), pan (middle-drag) and W/L drag via CDP `Input.dispatchMouseEvent`; arrow keys also checked. `eval` was used only to read state (visibilityState, WebGL renderer, timing polls). Server checks used `/api/sessions/{id}/coverage` and `/events.jsonl`. Scripted wheel pace was about 6 to 7 images/s (120 to 150 ms per notch); the fast-scroll test used 8 ms per notch.
**Evidence:** finding screenshots `V*.png` here; step screenshots `NN-*.png` and the downloaded exports (`exports/`) on the mini under `coverage-map-work/evidence/useit-r5/`.

## Core loop (open -> read -> jump to gaps -> finish)

| Case | Path | Time | Clicks / keys |
|---|---|---|---|
| pancreas-0051, run A (first contact, with exploration) | soft pass of 185 images (30 s), `2` lung pass (20 s), `3` bone pass (25 s), N x4 + 1 map click + ~80-image bone pass, F, Confirm. Result: 21/21 reviewed, 99.9% of voxels | 5:19 wall clock including exploration (zoom/pan/O/G/export); about 2.5 min of reading | keys 2, 3, N x4, F; clicks: 1 map, 1 Confirm |
| pancreas-0051, run B (clean) | Session > New reading > Start new reading, presets 1/2/3, ~690 wheel notches, N x6 (each followed by short local scrolling), F, Confirm. Result: 20 reviewed, 1 left (left lung at 89%, shown in Finish) | 2:23 to confirmed finish | 4 clicks; 10 keys |
| lidc-0001 | Session > Open another case > lidc-0001; soft, lung and bone passes over 133 images (22 s each); F; Confirm. Result: 25/25 reviewed, no N needed | 1:56 to confirmed finish, including a first-load wait of up to 27 s (coarse polling) | 4 clicks; keys 2, 3, F |

A warm open or reload has the image overlay and panel ready in about 0.5 s; a warm case switch takes under 1 s. The time is spent almost entirely on scroll passes.

## Findings

No NEEDED findings.

| Id | Severity | Finding | Suggested fix |
|---|---|---|---|
| V1 | IMPROVEMENT | Session > Recent readings shows only the 9 newest readings for the case (the server returns 10, including the current one). The list is server-wide and has no owner, no coverage or finished % and no "older" link, and it includes empty 1- to 8-event readings that every fresh page load creates. Within about 30 minutes, other jobs on the shared data folder pushed all three of my earlier pancreas readings out of the list (12:26, finished at 99.9%; 12:31, finished at 57%; 12:37, from sample). They are still on the server (`/coverage` returns 200), but nothing in the UI reaches them. Anyone can also "continue" another person's reading and record into it. (V1-recent-readings-earlier.png, V1-recent-readings-flooded.png) | Hide or merge readings with no reading activity; add "Show older…"; show coverage %, finished-at % and last activity per row; optionally mark or filter "this browser's" readings |
| V2 | IMPROVEMENT | The organs CSV has only live statuses. After reading on past Finish, the screen says "Finished at 57% · reading continued (now 81%)", but the CSV has no finished flag or at-finish status/coverage, so a CSV-only analysis cannot reproduce the finished result (the JSON can). (V2-read-on-after-finish.png, exports/s2-after-finish.csv) | Add `finished`, `status_at_finish` and `coverage_at_finish` columns (or a header comment line) |
| V3 | NIT | Finish with nothing remaining shows a large empty box (about 450 px at 1440x900) between "Every organ was displayed enough…" and the decisions. (V3-finish-empty-space.png) | Collapse the list area when it is empty |
| V4 | NIT | Reset View from a slightly tuned lung window (W1628/L-500, labelled "counts as lung") lands on the DICOM default W2279/L116, which counts for nothing. The yellow note says so, but the reader loses a lung-qualifying window. (V4-*.png) | When resetting from a custom window that qualifies, return to the preset it qualifies as |
| V5 | NIT | Opening Finish again after confirming and backing out appends `finish` lines with `"confirmed":false` to events.jsonl after the confirmed one. The app, the JSON summary, Recent readings and the screen all correctly stay finished, but a naive JSONL consumer that takes the last finish line would see the reading as unconfirmed. | Log the back-out as a distinct kind (for example `finish_review_closed`) |
| V6 | NIT | The replay status line truncates the label: "Replay (read-only, recording paused): Your reading, star…". (V6-replay-label-truncated.png) | Shorten it to "Replay · read-only" and put the full label in a tooltip |

## Test caveats (not findings)

- **Escape hides the page in this harness.** In this headless agent-browser, a keyboard Escape whose default the page does not prevent makes the page `hidden`. A plain `data:` page did the same. This happened on Finish > Esc. The reading stayed finished, the app sent `hidden` and stopped recording, and it resumed when the tab was brought back. Later Finish exits used Go back.
- **Shared data folder.** The data folder is shared with other servers and with another job's Playwright runs. Their sessions appear in Recent readings, which surfaced V1.
- **Stray view in the two-tab test.** My first input helper attached Playwright to every page, and during the two-tab test it apparently woke the background tab for one stray soft view (seq 482). After that I used a raw single-page CDP helper.
- **Accidental near-empty finish.** I accidentally confirmed a finish on a nearly empty reading (s-20261022-010403-f2967c) after a menu click missed. I reused it to verify "confirm again records a newer finish".

## What worked

- **Arriving cold.** It is clear what to do: a one-line hint, a colour legend under the map, S/L/B window badges with section headers, a shortcut strip and a research-only disclaimer.
- **Scoring held up against attempts to fool it:**
  - A bone window over the abdomen gives "wrong window", 0%.
  - Very fast scrolling (about 35 ms per image) gives "too brief".
  - Zooming into air and scrolling 60 images adds nothing.
  - Panning the liver off-screen gives "off-view", 3%.
  - A hand-tuned window says what it counts as.
- **Live list and 3D map.** Both update while scrolling. Each organ row explains why it is unseen and gives "Still to read: images …".
- **N and map clicks.** They switch to the right window and keep the organ on screen when zoomed in (heart, esophagus, liver at heavy zoom), panned nearly off-screen, or zoomed out. When nothing is left, N says "every organ is reviewed, done or N/A".
- **Reader tools.**
  - D, A and G toggle on and off.
  - The flag note saves on Enter and reaches the CSV.
  - O draws the outline.
  - Letters and digits typed into the note or the rules fields do not trigger shortcuts.
  - Arrow keys scroll.
- **Finish.**
  - It lists what remains with reasons and "Your decisions", and clicking a row goes to that organ.
  - After a confirm, reopening Finish and backing out keeps it finished on screen, in Recent readings ("✓ finished") and on the server (`provisional:false`).
  - Confirming again records a newer finish (t 65.8 s -> 138.7 s; at-finish 1% -> 23%).
- **Exports.** JSON and CSV match the screen right after Finish (7/12/2, 12 left, 57%) and after reading on (9/12/0, 81%; finished block kept at 57%).
- **Replay.** It is read-only (D shows "Replay is read-only"), can be scrubbed by clicking, and Close returns to live recording.
- **Rules form.** It validates input (rejects 9099%), re-scores the session with a "custom rules" badge (400 ms/99% gives 0 reviewed), and Restore defaults works.
- **Sessions:**
  - Start from sample, with a confirm step and a "scripted automated reading (not a human)" badge.
  - New reading, with a confirm step.
  - Reload resumes on the same image and window.
  - Reset View keeps a preset window.
  - Switching to the chest case and back resumes both readings.
  - An earlier finished reading reopened from Recent readings (before V1 pushed it out).
- **Two tabs.**
  - The second tab shows a banner and Record here.
  - After the takeover, the first tab's bone coverage on the server was unchanged (0.671/0.599/0.602/0.658).
  - The passive tab posts nothing.
  - Closing the recording tab let the other take over by itself.
- **Hidden while loading or resuming.** Nothing is recorded while hidden, and on return the reader is on the same image and window.
- **1280x800.** The layout holds, including Finish with 19 remaining and 3 decisions.
