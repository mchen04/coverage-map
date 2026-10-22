# Use-it round 2 (independent Opus tester, 2026-10-21)

Build: frozen copy of f7413d3 served on the mini (port 8769); nothing restarted. Task-owned
agent-browser 0.25.4 session `covmap-useit-r2`, headless Chrome 155 with ANGLE Metal, 1440x900
(full run) and 1280x800 (check). Real clicks and keys; image scrolling by CDP `mouseWheel` at image
coordinates, one image per tick. `eval` used only to read panel text, tooltips, the slice/window
overlay, export links and `visibilityState`. Exports downloaded through the Session menu links.
Session closed afterwards. (Report text written by the orchestrator from the tester's reply; the
tester's harness blocked report-file writes.)

## Core loop (scripted at ~150 ms per image, so these times are a floor)

| Case | Clicks | Keys | Wheel ticks | Time |
|---|---|---|---|---|
| pancreas-0051 fresh (soft pass → N → lung → N → bone → F → Confirm) | 1 | 3 | ~523 | 155 s |
| pancreas-0051 from the sample (4 clicks to load, 3×N, F, Confirm) | 5 | 4 | ~345 | ~112 s |
| lidc-0001 (3 clicks to open; soft → N → lung → N → bone → F → Confirm) | 4 | 3 | ~396 | 119 s |

## Findings

| Id | Severity | Finding | Disposition |
|---|---|---|---|
| R1 | NEEDED | The export made at Finish disagrees with the screen: the panel credits the image on screen while it is shown, but the scored record does not credit it until another view arrives, and Finish does not close it. Gallbladder showed 92% reviewed and was not listed at Finish, yet exported partial 0.8987; JSON `summary.reviewed` 0 vs 1 on screen; after a reload the same session re-exported reviewed 0.9226. Also lumbar spine 100% on screen vs 0.990 exported, and a 4.8 s bone look at the final image exported with no bone credit. | fix: Finish closes the displayed views in the record; export uses the same as-of state as the screen |
| R2 | IMPROVEMENT | Resume restores coverage exactly, but the viewer reopens at image 1 in Soft, not at the last slice/window (also on case switch and back). | restore last view |
| R3 | NIT | Events counter counts only this page's events ("1 events" on a resumed 531-event session). | session total, singular |
| R4 | IMPROVEMENT | Organ names truncated ("Sto…", "Li…") whenever a row shows its unseen reason. | never truncate name first |
| R5 | NIT | Viewer shows "I:99 (87/185)"; in pancreas I: counts down while the bracketed index counts up; panel uses only the bracketed number. | say which numbering |
| R6 | NIT | Cold open is mostly clear, but nothing says what to do first; the sample reading is 4 clicks deep. | one-line first-use hint |

Not testable: a hidden tab (headless Chrome kept `visibilityState` visible). Risk judged small:
credit is per image shown ≥ 80 ms and did not grow while idling on one image (heart stayed 5% over ≥ 3 s).

## What worked

Scoring held against every attempt to fool it: abdomen read in the lung window gave 0% soft credit
("wrong window"); brain preset and a manual W:16 window gave 0; liver window counted as soft; a
full-stack flick (~33 ms/image) was "too brief" (0–9%); panning the body off-screen left organs at
~1%; a 2× zoom on the liver credited liver and gallbladder, not the spleen. N, map clicks and
Finish-list clicks jump to unread images in the right window ("No gaps left" at the end); D/A/G
need a selected organ and toggle; flag notes keep commas and are CSV-quoted; shortcuts ignored
while typing; Finish lists remaining organs with reasons and "Your decisions"; Go back and Confirm
work; the rules form re-scores, shows "custom rules", rejects inconsistent thresholds, restores
defaults; sample readings are labeled scripted; case switching keeps each reading; replay follows
frames without recording; layout holds at 1280x800.

Evidence (mini `evidence/useit-r2/`): `R1-a-screen-gallbladder-reviewed.png`, `R1-b-finish-dialog.png`,
`R1-c-after-confirm.png`, `R1-supp-*.png`, `R2-*.png`, `R3-*.png`, `R4-*.png`, `R5-*.png`, `R6-*.png`,
`exports/R1-gallbladder-*` (with `-after-reload-` versions).
