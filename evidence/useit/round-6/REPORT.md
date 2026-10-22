# Coverage Map: use-it r6 report

- **Build/port:** frozen candidate c68665a at http://127.0.0.1:8772/ on the Mac mini. I used no other port and restarted nothing.
- **Browser:** agent-browser 0.25.4 (Chrome for Testing), session `covmap-useit-r6`, args `--use-angle=metal,--enable-gpu`. The session was closed at the end.
- **Viewports:** 1440x900 for the whole run, plus a check at 1280x800 (main screen, Finish dialog, Session menu).
- **Input:**
  - Real wheel, clicks and keys through Playwright over CDP, plus agent-browser clicks on menu items.
  - `eval` was used only to READ: `document.visibilityState`, `innerWidth`, and the active element's value to check a typed number.
  - Server state was read through the REST API (curl).
- **Evidence:** the screenshots cited for W2–W4 (and 18a, 19a) are here; every step screenshot (`NN*-*.png`) and the downloaded exports are on the mini under `coverage-map-work/evidence/useit-r6/`.

## Core loop (open, three window passes, Next gap, Finish, confirm)

| Case | Time | Keys | Clicks | Wheel | Result |
|---|---|---|---|---|---|
| pancreas-0051 (demo) | 228 s to confirmed Finish | 5 (2, 3, N, F, F)* | 2 (Go back*, Confirm) | ~505 notches (soft 184, lung 70, bone 114, plus 137 after N to close bone gaps) | 21/21 reviewed, 99.9% of voxels |
| lidc-0001 (chest) | ~101 s from open to Confirm | 4 (2, 3, N, F) | 4 (Session, Open another case, lidc-0001, Confirm) | ~396 notches | 25/25 reviewed |

\* These steps include the requested Finish → Go back → Finish detour. The minimum is F plus one Confirm click.

Scroll pacing (one image per notch) sets the time. The UI added no waits beyond loading the case.

## Findings

| Id | Severity | Finding | Suggested fix |
|---|---|---|---|
| W1 | IMPROVEMENT | Tab ownership flips the wrong way after a recent rules Apply. The setup: apply Research reading rules in tab A, keep scrolling, and within seconds open the same case in a new tab B. **Result:** B records and shows no banner. A, the tab being read in, gets "open in another tab — not recording here", and its scrolling is not logged until Record here is clicked. **Reproduced 3/3** (…b4ddd1, …ce9b2b, …c3775a; screens 30a–f, 33a/b). **Not reproduced** without the Apply (4 trials) or 40 s after it. The banner is visible, one click fixes it and nothing is over-credited, so this is not NEEDED. | A second tab must always defer to a live recording tab. Make sure Apply keeps or renews A's writer role. |
| W2 | IMPROVEMENT | Opening an earlier reading from Recent readings while the viewport is reoriented (MPR coronal) applies that reading's window but leaves the plane and slice as they were. The reader does not land where the reading left off; a reload then restores axial 126/185 (15a–c). | When a reading is opened, restore its last image as reload does, returning to the acquisition plane if needed. |
| W3 | IMPROVEMENT | N or a map click while heavily zoomed keeps the zoom. The jump lands on a blurry patch where the organ cannot be recognised, though the organ is credited (24a–c). | After a jump, zoom out to fit if the organ is not mostly in view. |
| W4 | NIT | Enter does not confirm the Finish dialog (focus stays on the body), so a mouse click is needed (25b/c). Esc does back out. | Focus Confirm when the dialog opens. |
| W5 | NIT | The rules form has no upper bound. Min dwell values of 8,020,000 ms and 200,000 ms were accepted, re-scoring the session to 0% without a warning. | Cap min dwell (for example at 10 s) or warn. |

**No NEEDED findings.**

### Reproduction for W1 (the most serious item)

1. Open the pancreas case at 1440x900 (tab A). Choose Session → New reading → Start new reading.
2. Choose Session → Research reading rules…, set Min dwell to 30000, then Apply.
3. Within about 2 s, press 2 and wheel 10–30 images in A.
4. At once, open the same URL in a new window (B) and wait about 9 s.
5. **Expected:** B shows the banner and A keeps recording. **Actual:** A shows the banner and B does not. In events.jsonl, wheeling 2 images in A adds 0 events, while wheeling 2 images in B adds 2.

## Server check: the first tab's display counts only until the takeover

Session s-20261022-023018-702199:
- A's last view was lung image 185/185 at t=60.2 s. B clicked Record here at t=89.6 s; the log shows `hidden *` followed by B's view.
- Re-scored at min dwell 45 s, image 185 was unseen, although A kept showing it for minutes. So A was not credited past the takeover.
- At 25 s, image 185 was seen. So A's ~29.5 s before the takeover counted.
- Rules were restored to the defaults afterwards. Closing B produced end, `hidden *` and A's view (auto takeover).

## What worked

- **Arriving cold:** a clear first-use hint, legend and research-only disclaimer (01).
- **Live updates:** the list and the 3D map update during each pass.
- **Scoring the misuse cases:**
  - A lung window over the abdomen gives soft organs 0% with "wrong window".
  - Scrolling at about 30 ms per image gives "too brief".
  - Zooming into air or panning an organ off-screen gives "out of region".
- **Jumps:** map clicks and N work while zoomed or panned at a moderate zoom, in MPR and in a reoriented viewport (13f, 14, 21).
- **Reader marks:** D, A, G with a note, O outline, and N/A on and off all work (17a–d, 29).
- **Rules form:** validation messages, whole-session re-scoring, Restore defaults and the "custom rules" chip (04, 23).
- **Exports:** JSON and CSV matched the screen right after Finish and after reading past it (flag, note, N/A and done shown; the finished record was kept, confirmed at 228414 ms).
- **Finish:**
  - Finish → Go back → Finish → Confirm works.
  - Reopening Finish and backing out with Go back or Esc keeps the confirmed finish, and the chip reads "Finished at X% · reading continued (now Y%)".
  - Recent readings shows ✓ finished, and a second confirm records a newer finish (16, 27).
- **Replay:** read-only, with recording paused (19a).
- **New readings:** Start from sample (confirm dialog, labelled as a scripted automated reading) and New reading with a confirm (20).
- **Resuming:**
  - Reload keeps the slice and window (26a/b).
  - Reset View keeps the preset window on stack, MPR and reoriented viewports (12, 13).
  - Opening the chest case and coming back resumes the reading (22).
  - An earlier reading can be found and opened from Session → Recent readings (15).
- **Two tabs:**
  - The banner and Record here work, and the second tab is not counted before it takes over.
  - Record here works while Replay is open.
  - When the recording tab closes, the other tab takes over automatically (18, 19, 36).
- **Hidden while loading:** hiding the tab while it loads or resumes logs `hidden` and over-credits nothing (11).
- **1280x800:** everything fits, including the Finish dialog and the Session menu (25).

Known limitations were not re-reported. That includes other jobs' sessions showing in Recent readings.
