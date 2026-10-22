# Use-it round 1 (independent Opus tester, 2026-10-21)

Build: frozen copy of b64d795 served on the mini (port 8769). Task-owned agent-browser session
`covmap-useit-r1`, headless Chrome for Testing with ANGLE Metal, 1440x900 and 1280x800. Real
input (wheel via CDP at image coordinates, because agent-browser 0.25.4 `mouse wheel` fires at
(0,0); keys; clicks). `eval` used only to read state. Session closed afterwards.

Core loop (open → read → gap jump → finish): chest 3.2 min wall (≈1.5 min scrolling);
pancreas 14.6 min wall, ≈3–4 min of input (rest was map experiments and wheel workaround).

| Id | Severity | Finding | Disposition |
|---|---|---|---|
| U1 | NEEDED | Next gap / map click / row click jump to the organ centre, often an already-read slice | server `gap` endpoint + client jump (round 2) |
| U2 | NEEDED | "Slices X–Y" uses case/instance numbering, not the viewer's n/N | client mapping (round 2) |
| U3 | NEEDED | Rotating the 3D map drifts/clips/blanks it; R/L labels stay fixed | client (round 2) |
| U4 | NEEDED | Loaded sample silently absorbs the user's reading, still labelled as the sample | session_info + labels (round 2) |
| U5 | NEEDED | Scrolling during read-only replay is recorded | pause recording; replay follows the image (round 2) |
| U6 | IMPROVEMENT | Pancreas demo shows posterior at top (col_dir [0,-1,0]) | radiological display in coverage mode |
| U7 | IMPROVEMENT | Opens in a window that counts for nothing, no hint | open in Soft, active preset, hint |
| U8 | IMPROVEMENT | Finish sheet omits reader decisions, hides disclaimer, overstates | reader-decision section, wording |
| U9 | IMPROVEMENT | Flag note input hidden below the list | scroll into view + focus |
| U10 | IMPROVEMENT | Reload/New reading/Load sample discard the reading silently | confirm + resume last session |
| U11 | IMPROVEMENT | Partial rows say "unseen" next to 66% | "34% unseen · reason" |
| U12 | IMPROVEMENT | No marker for custom rules, no restore defaults | marker + restore |
| U13 | IMPROVEMENT | N/A gray ≈ skipped gray on the map | distinct style |
| U14 | IMPROVEMENT | Replay does not move the image; controls lack labels | follow-the-image + aria labels |
| U15 | IMPROVEMENT | No in-app case switch; "<" opens the stock study list | case picker |
| U16 | IMPROVEMENT | A tiny (0.25x zoom) image still counts | documented limitation (zoom not scored) |
| U17 | IMPROVEMENT | Row buttons 22x18 px, adjacent | ≥ 24x24 |
| U18–U25 | NIT | banner over marker, sheet edge, 90% rounding, sample jargon, Escape, legend overlap, list scroll, cryptic header/title | cheap fixes |

What worked: immediate score updates with precise reason tooltips; window keys; row/map jumps
for visible organs; instant re-score on rule change; D/A/G toggles reflected in header, row,
map and CSV; shortcuts suppressed while typing; outline follows scrolling; zooming into air
gives no credit; the page never scrolls at 1440x900 or 1280x800; honest disclaimer.
