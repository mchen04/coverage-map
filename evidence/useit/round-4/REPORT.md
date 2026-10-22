# Coverage Map: use-it test r4

**Setup:**
- **Build/port:** frozen 6603bbe at http://127.0.0.1:8769/ on mini.
- **Browser:** agent-browser 0.25.4, session covmap-useit-r4, headless Chrome for Testing 155, `--use-angle=metal,--enable-gpu`.
- **Viewports:** 1440x900, plus 1280x800.
- **Input:** real CDP `Input.dispatchMouseEvent` wheel, drag and click, plus agent-browser click, keys and mouse. I used `eval` and `curl` GETs only to read state.

**Caveats (not findings):**
- Another agent-browser instance and one unknown client created sessions on 8769 during my run. They appear in Recent readings.
- After the two-tab test, my headless tab stayed `hidden`, and case switches stalled 45–150 s. Brought to the front, a switch took 0.2 s.

## Core loop (open → read → N to gaps → finish)

| Case | Clicks | Keys | Wheel ticks | Wall time* | Result |
|---|---|---|---|---|---|
| pancreas-0051 (New reading) | 3 + 1 Confirm | 8 (1, N×6, F) | 454 | 157 s | 21/21 reviewed, 99% |
| lidc-0001 (Open another case, ready in ~1 s) | 3 + 1 Confirm | 7 (N×6, F) | 413 | 137 s | 25/25 reviewed, 99% |

*About 50 s of the wall time is scripted scrolling at 120 ms per tick. The rest is harness latency. N set the window, so no preset keys were needed after the soft pass.

## Findings

| Id | Severity | Finding | Suggested fix |
|---|---|---|---|
| T1 | NEEDED | Backing out of Finish (Go back or Esc) on a finished reading silently un-finishes it: the "✓ Finished" badge goes, the server says `finished=false`, and Recent readings loses "✓ finished". The coverage JSON's `summary.finished` becomes `{confirmed:false}`, with `at_finish` gone. | Take finished state and `at_finish` from the latest *confirmed* finish, and log a cancel as its own event. |
| T2 | IMPROVEMENT | N lands mid-gap, so the reader scrolls both ways. lidc Left lung: N went to image 46, but 8–45 and 47–101 were unread (38 ticks up, then 93 down). Pancreas bones did the same. | Land at the start (or the nearer end) of the first unread run, keeping the organ framed. |
| T3 | IMPROVEMENT | Recent readings shows only "Live reading", created time and event count. It has no %, last-activity time or "more", and lists 2–4-event empty sessions. My 434-event lidc reading fell off the 9-item list. | Show % left and last activity. Collapse empty sessions. Add "Show more". |
| T4 | IMPROVEMENT | After reading on past Finish, the organs CSV has only current status. Finish time and status-at-finish are only in the JSON. | Add `finished_at` and `status_at_finish` columns. |
| T5 | NIT | N during "Identifying the case…" toasts "No gaps left: every organ is reviewed, done or N/A." | Say "Still loading". |
| T6 | NIT | "Hide reviewed" with everything reviewed shows an empty black map with no message, and the button shows no pressed state. | Add an empty-state message, plus aria-pressed or a "Show reviewed" label. |
| T7 | NIT | On arrival, every organ is "skipped" (gray) before any reading. | Use "not yet shown" until the reader scrolls. |

### T1 reproduction (twice, once with the tab visible)
1. Open lidc-0001, read until 0 left, press F, then click **Confirm finish**. The header shows "✓ Finished", and the coverage JSON has `confirmed=true` and `at_finish` 25/0/0 at 99.7%.
2. Press F, then **Go back** (or Esc).
3. The header loses "✓ Finished". `GET /api/sessions/s-20261021-234446-84d093` returns `finished:false`. The coverage JSON has `finished:{confirmed:false}` and no `at_finish`.

Pancreas variant at 1280x800: "✓ Finished at 26% · reading continued (now 45%)" vanished after F then Esc. Only the events JSONL keeps the confirmed finish. Re-confirming replaces the original at-finish numbers.

**Evidence:**
- Screenshots: T1a-lidc-finished.png, T1b-lidc-after-F-goback.png, T1c-lidc-recent-not-finished.png, T1d/T1e-visible-*.png, T1f-pancreas-finished-badge-gone-1280.png.
- Exports: exports/T1-lidc-confirmed-coverage.json and exports/T1-lidc-after-goback-coverage.json.
- Other findings: T2–T7-*.png.

## What worked
- **Arrival:** the counts header, hint, legend, S/L/B badges and research-only footer make the page clear.
- **Window rules:**
  - A bone pass over the abdomen gave "wrong window".
  - A W/L nudge (420/50) still counted as soft, and the panel said so.
  - A custom window or the brain preset says "counts for no organ".
  - Scrolling at 10 ms per slice counted nothing ("too brief").
- **Fooling attempts:** 23× zoom into air and panning the liver off-screen credited nothing hidden ("off-view").
- **Jumps:** N and map clicks (gray and yellow, front and back views) reached the right slice and window. They reset zoom and pan so the organ was on screen, even from 23× zoom or a panned view.
- **D/A/G/O:** done (cyan), N/A, flag with a note, and outline all work. Finish lists them under "Your decisions".
- **Finish dialog:** shows the remaining organs with reasons and click-to-go. It fits at 1280x800.
- **Exports:** JSON, CSV and JSONL matched the screen after Finish and after reading on, with `at_finish` kept.
- **Rules:** the rules form re-scores, shows a "custom rules" badge, and resets.
- **Replay:** read-only, and recording resumes afterwards.
- **Reload:** restored slice 150 and the lung window.
- **Reset View:** kept the slice and preset.
- **Sessions:** the sample reading is labelled as scripted, and New reading confirms first. Each case resumes its own reading.
- **Two tabs:** the banner and Record here work in both directions. When the recording tab closed, the other took over by itself (verified on the server).

**One NEEDED finding (T1).**

---
In Git: this report, T1a/T1b/T1f screenshots and the two T1 coverage exports (in this folder rather than exports/). The other screenshots and exports stay on the mini under coverage-map-work/evidence/useit-r4/.
