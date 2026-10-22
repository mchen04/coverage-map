"""Coverage-mode UI check: the real OHIF build in headless Chromium against a running covmap
server. Real input only (mouse wheel over the viewport, keys, clicks on panel and 3D map);
every claim is checked against the server's own session log, coverage and gap target.
The exceptions are labelled in the check details: a scripted continuous scroll
(setImageIdIndex every 25 ms) that keeps running while the page switches sessions, the
setRadiological(false) test hook used to compare scoring with and without the radiological
flip, and the gauntlet driver's scripted camera calls (frameRect, resetCamera() + render) in
the camera-reset check, next to a real click on OHIF's Reset View. Research use only.

    python ohif/verify/ui_check.py --base http://127.0.0.1:8765 --case lidc-0001 \
        --out $MINI_WORK/evidence

Writes c-*.png screenshots and c-ui-check-<case>.json; exit 0 only if every check passed.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
import traceback
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from covmap import paths  # noqa: E402
from e2e.ohif_driver import CHROMIUM_ARGS, HELPERS, OhifDriver  # noqa: E402

PRESETS = {"1": (40, 400), "2": (-600, 1500), "3": (480, 2500), "4": (90, 150), "5": (40, 80)}
CLASS_PRESET = {"soft": (40, 400), "lung": (-600, 1500), "bone": (480, 2500)}
PRESET_BUTTON = {"1": "soft", "2": "lung", "3": "bone"}
SOURCE_TEXT = {"browser-scripted": "scripted automated reading (not a human)",
               "simulated": "simulated reading (not a human)", "ohif": "reader in this viewer"}
HINT_RE = re.compile(r"(\d+% unseen · )?(never shown|wrong window|off-view|too brief)")
MAP_BG = (7, 10, 20)  # organ-map background (0.027, 0.039, 0.078)

VIEW_JS = r"""
() => {
  const s = window.services;
  const v = s.cornerstoneViewportService.getCornerstoneViewport(s.viewportGridService.getActiveViewportId());
  if (!v) return null;
  const r = v.getProperties().voiRange;
  const img = v.getCornerstoneImage ? v.getCornerstoneImage() : null;
  const id = img ? img.imageId : v.getCurrentImageId();
  const m = /instances\/([^/]+)\/frames/.exec(id || '');
  const cam = v.getCamera();
  return {type: v.type, idx: v.getCurrentImageIdIndex ? v.getCurrentImageIdIndex() : null,
          sop: m ? m[1] : null, ww: Math.abs(r.upper - r.lower) + 1, wc: (r.lower + r.upper + 1) / 2,
          flipH: !!cam.flipHorizontal, flipV: !!cam.flipVertical};
}
"""

LAYOUT_JS = r"""
() => {
  const de = document.documentElement;
  const panel = document.querySelector('[data-testid="covmap-panel"]');
  const note = document.querySelector('[data-testid="covmap-note"]');
  const list = document.querySelector('[data-testid="covmap-organ-list"]');
  const offenders = [];
  for (const el of document.body.querySelectorAll('*')) {
    const r = el.getBoundingClientRect();
    if (r.bottom > innerHeight + 0.5 && r.height > 0 && !(el.parentElement && el.parentElement.getBoundingClientRect().bottom > innerHeight + 0.5)) {
      offenders.push({tag: el.tagName, cls: String(el.className).slice(0, 80), id: el.getAttribute('data-testid') || el.id,
                      top: Math.round(r.top), bottom: Math.round(r.bottom), parent: String(el.parentElement && el.parentElement.className).slice(0, 80)});
    }
  }
  window.scrollTo(0, 10000);
  const sy = window.scrollY;
  window.scrollTo(0, 0);
  return {inner: [innerWidth, innerHeight], doc: [de.scrollWidth, de.scrollHeight],
          body: [document.body.scrollWidth, document.body.scrollHeight], scrollY_after_scrollTo: sy,
          panel: panel ? [panel.clientWidth, panel.clientHeight, panel.scrollHeight, panel.scrollWidth, panel.scrollLeft] : null,
          header: [...document.querySelectorAll('.cm-summary, .cm-sub')].map(e => [e.scrollWidth, e.clientWidth]),
          note_bottom: note ? note.getBoundingClientRect().bottom : null,
          list: list ? [list.clientHeight, list.scrollHeight] : null, offenders: offenders.slice(0, 8)};
}
"""

# Scripted continuous scroll (labelled): the active stack alternates between two neighbouring
# images every 25 ms, so view events keep being produced while the page switches sessions.
SCROLL_START_JS = r"""
() => {
  const s = window.services;
  const v = s.cornerstoneViewportService.getCornerstoneViewport(s.viewportGridService.getActiveViewportId());
  const n = v.getImageIds().length, a = Math.max(0, Math.min(v.getCurrentImageIdIndex(), n - 2));
  let t = 0;
  window.__cvTicks = 0;
  window.__cvScroll = setInterval(() => {
    t ^= 1; window.__cvTicks++;
    Promise.resolve(v.setImageIdIndex(a + t)).catch(() => {});
  }, 25);
  return a;
}
"""
SCROLL_STOP_JS = "() => { clearInterval(window.__cvScroll); window.__cvScroll = null; return window.__cvTicks || 0; }"

ROWS_JS = r"""
() => [...document.querySelectorAll('[data-testid^="covmap-organ-"][data-key]')].map(r => ({
  key: r.dataset.key, status: r.dataset.status, reader: r.dataset.reader, flagged: r.dataset.flagged,
  pct: (r.querySelector('.cm-pct') || {}).textContent || null,
  hint: (r.querySelector('.cm-hint') || {}).textContent || null}))
"""


class Checks:
    def __init__(self):
        self.results: list[dict] = []
        self.current = "setup"

    def run(self, name: str, fn):
        self.current = name
        t0 = time.perf_counter()
        try:
            detail = fn()
            self.results.append({"check": name, "ok": True, "s": round(time.perf_counter() - t0, 2), "detail": detail})
            print(f"PASS {name}: {json.dumps(detail, default=str)[:300]}", flush=True)
            return detail
        except Exception as e:  # noqa: BLE001 - record and continue with the next check
            self.results.append({"check": name, "ok": False, "s": round(time.perf_counter() - t0, 2),
                                 "error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc(limit=3)})
            print(f"FAIL {name}: {type(e).__name__}: {e}", flush=True)
            return None


def need(cond, msg):
    if not cond:
        raise AssertionError(msg)


class Api:
    def __init__(self, base: str):
        self.base = base.rstrip("/")

    def raw(self, path: str):
        with urllib.request.urlopen(self.base + path, timeout=30) as r:
            return r.status, dict(r.headers), r.read()

    def json(self, path: str):
        return json.loads(self.raw(path)[2])

    def events(self, session: str) -> list[dict]:
        body = self.raw(f"/api/sessions/{session}/events.jsonl")[2].decode()
        return [json.loads(line) for line in body.splitlines() if line.strip()]

    def info(self, session: str) -> dict:
        return self.json(f"/api/sessions/{session}")

    def gap(self, session: str, organ: str) -> dict:
        return self.json(f"/api/sessions/{session}/gap?organ={organ}")

    def coverage(self, session: str) -> dict:
        return self.json(f"/api/sessions/{session}/coverage")


def ranges_text(ranges, mx=4) -> str:
    """Python twin of the panel's rangesText ("12–20, 25 +1 more")."""
    parts = [f"{a}" if a == b else f"{a}–{b}" for a, b in ranges[:mx]]
    return ", ".join(parts) + (f" +{len(ranges) - mx} more" if len(ranges) > mx else "")


def model_box(path: str) -> dict | None:
    """Where the 3D model is in a map screenshot: pixels that differ from the background,
    ignoring the toolbar strip at the top and the orientation letters at the sides."""
    import numpy as np
    from PIL import Image

    a = np.asarray(Image.open(path).convert("RGB")).astype(int)
    h, w, _ = a.shape
    m = np.abs(a - np.array(MAP_BG)).sum(-1) > 45
    m[:28, :] = False
    m[:, :18] = False
    m[:, w - 18:] = False
    ys, xs = np.nonzero(m)
    if not xs.size:
        return None
    return {"size": [w, h], "bbox": [int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())],
            "center": [round(float((xs.min() + xs.max()) / 2 / w), 3), round(float((ys.min() + ys.max()) / 2 / h), 3)],
            "fill": round(float(m.mean()), 4)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8765")
    ap.add_argument("--case", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--dwell-ms", type=float, default=130)
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    api = Api(a.base)
    vol = json.loads((paths.case_dir(a.case) / "volume.json").read_text())
    organs = json.loads((paths.case_dir(a.case) / "seg" / "organs.json").read_text())["organs"]
    by_key = {o["key"]: o for o in organs}
    case_sops = vol["sop_uids"]
    url = f"{a.base}/coverage?StudyInstanceUIDs={vol['study_uid']}"
    C = Checks()
    console: list[dict] = []
    posts: list[float] = []
    shots: list[str] = []
    marks: dict = {}

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=CHROMIUM_ARGS)
        ctx = browser.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=1,
                                  accept_downloads=True)
        page = ctx.new_page()
        page.on("console", lambda m: console.append({"type": m.type, "text": m.text[:400], "check": C.current,
                                                     "at": m.location}) if m.type in ("error", "warning") else None)
        page.on("pageerror", lambda e: console.append({"type": "pageerror", "text": str(e)[:400], "check": C.current}))
        page.on("request", lambda r: posts.append(time.perf_counter())
                if r.method == "POST" and "/events" in r.url else None)
        drv = OhifDriver(page)

        def shot(name, locator=None):
            path = out / f"c-{name}.png"
            (locator or page).screenshot(path=str(path))
            shots.append(str(path))
            return str(path)

        def install_view():
            page.evaluate("js => { window.__cvView = (new Function('return ' + js))(); }", VIEW_JS.strip())

        def view():
            return page.evaluate("() => window.__cvView()")

        def cov():
            return page.evaluate("() => window.__covmap.lastCoverage")

        def session():
            return page.evaluate("() => window.__covmap.sessionId")

        def flush():
            page.evaluate("() => window.__covmap.flush()")

        def row(key):
            return page.locator(f'[data-testid="covmap-organ-{key}"]')

        def tid(name):
            return page.locator(f'[data-testid="{name}"]')

        def wait_view(sop=None, preset=None, timeout=8000):
            page.wait_for_function(
                """([sop, wc, ww]) => { const v = window.__cvView();
                     return v && (!sop || v.sop === sop) && (wc === null || (v.wc === wc && v.ww === ww)); }""",
                arg=[sop, preset[0] if preset else None, preset[1] if preset else None], timeout=timeout)

        def reattach(case_id=None, timeout=90000):
            """After a navigation: the driver helpers and the view probe again, then ready."""
            page.wait_for_function("() => !!(window.services && window.commandsManager)", timeout=timeout)
            page.evaluate(HELPERS)
            page.wait_for_function("() => window.__e2e.ready()", timeout=timeout)
            install_view()
            page.wait_for_function("c => window.__covmap && window.__covmap.ready && (!c || window.__covmap.caseId === c)",
                                   arg=case_id, timeout=timeout)

        def neutral():
            """Brain window (counts for no organ): the coverage, and so the server's gap target,
            stays fixed until the next jump applies the organ's window."""
            drv.press_key("5")
            wait_view(preset=PRESETS["5"])
            flush()
            page.wait_for_timeout(120)

        def expect_jump(key):
            """Where a jump to `key` must land: the server's gap slice in the gap window (the
            organ's center slice when nothing is unseen)."""
            g = api.gap(session(), key)
            o = by_key[key]
            sop = g["sop"] if g.get("k") is not None else o["center_sop"]
            return {"key": key, "gap": g, "sop": sop, "preset": CLASS_PRESET[g.get("window") or o["windows"][0]]}

        def last_axial_view(evs):
            v = [e for e in evs if e["kind"] == "view" and e.get("vp_type") == "stack" and abs(e["normal"][2]) > 0.9]
            return v[-1] if v else None

        def radiological(e) -> bool:
            tl, tr, _, bl = e["corners_lps"]
            return tl[1] < bl[1] and tr[0] > tl[0]  # screen up = anterior (-y), screen right = patient left (+x)

        def start_scroll():
            x, y = drv.viewport_center()
            page.mouse.move(x, y)
            return page.evaluate(SCROLL_START_JS)

        def stop_scroll():
            return page.evaluate(SCROLL_STOP_JS)

        def fresh_session_events(s, info=None):
            """C6: a session opened while the viewer scrolls starts its own clock and numbering."""
            info = info or api.info(s)
            ds, dt = info.get("seq_offset", 0), info.get("t_offset_ms", 0.0)
            evs = [e for e in api.events(s) if not e.get("from_sample")]
            need(evs, f"no live events in {s}")
            seqs = [e["seq"] for e in evs]
            need(seqs == list(range(ds, ds + len(evs))), f"live seqs not {ds}..: {seqs[:12]}")
            ts = [e["t_ms"] - dt for e in evs]
            need(ts == sorted(ts), "t_ms not monotonic")
            need(0 <= ts[0] < 500, f"first live t_ms {ts[0]:.0f} ms after the session start (want near 0)")
            need(all(e["session"] == s for e in evs), "foreign session id in the log")
            return {"live_events": len(evs), "seq": [seqs[0], seqs[-1]], "first_t_ms": round(ts[0], 1),
                    "kinds": sorted({e["kind"] for e in evs})}

        def old_session_closed(s):
            evs = api.events(s)
            need(evs and max(evs, key=lambda e: e["seq"])["kind"] == "end", f"old session {s} does not end with 'end'")
            seqs = [e["seq"] for e in evs]
            need(len(set(seqs)) == len(seqs), "duplicate seqs in the old session")
            return {"old_events": len(evs), "old_last": evs[-1]["kind"]}

        # ---- open ----------------------------------------------------------------------
        def open_app():
            t0 = time.perf_counter()
            info = drv.open(url)
            install_view()
            page.wait_for_function("() => window.__covmap && window.__covmap.ready", timeout=60000)
            page.wait_for_selector('[data-testid="covmap-map"][data-ready="1"]', timeout=60000)
            page.wait_for_function("() => window.__covmap.eventsSent > 0", timeout=20000)
            marks_ = page.evaluate("""() => ({
                first: performance.getEntriesByName('covmap:first-image').map(e => e.startTime),
                map: performance.getEntriesByName('covmap:map-ready').map(e => e.startTime),
                hooks: {first: window.__covmap.firstImageRenderedAt, map: window.__covmap.mapReadyAt,
                        caseId: window.__covmap.caseId, sessionId: window.__covmap.sessionId}})""")
            need(marks_["first"] and marks_["map"], f"performance marks missing: {marks_}")
            need(marks_["hooks"]["caseId"] == a.case, f"case lookup gave {marks_['hooks']['caseId']}")
            # U7: opens in the soft-tissue window, its button pressed, no window hint
            wait_view(preset=CLASS_PRESET["soft"])
            need(tid("covmap-window-soft").get_attribute("aria-pressed") == "true", "soft preset not shown as active")
            need(tid("covmap-window-hint").count() == 0, "window hint shown in the soft window")
            # U25: tab title and panel label
            title = page.title()
            need(title == "Coverage Map", f"tab title {title!r}")
            need(page.evaluate("""() => [...document.body.querySelectorAll('*')].some(e => e.children.length === 0
                     && e.textContent.trim() === 'Coverage Map' && e.getBoundingClientRect().width > 0)"""),
                 "panel label 'Coverage Map' missing")
            need(tid("covmap-resumed").count() == 0, "first open shows 'resumed'")
            return {"open_s": round(time.perf_counter() - t0, 2), "viewer": info["state"], "marks_ms": marks_,
                    "voi": view(), "title": title}
        opened = C.run("open coverage mode: first image, case lookup, session, map ready, soft window, title", open_app)
        if not opened:
            browser.close()
            return finish(out, a, C, console, shots, posts)
        shot("01-initial-1440x900")

        C.run("panel lists every organ of the case", lambda: (
            need(page.locator('[data-testid^="covmap-organ-"][data-key]').count() == len(organs),
                 f"rows={page.locator('[data-testid^=covmap-organ-][data-key]').count()} organs={len(organs)}"),
            {"rows": len(organs), "summary": tid("covmap-summary").inner_text()})[1])

        def no_scroll(tag):
            lay = page.evaluate(LAYOUT_JS)
            w, h = lay["inner"]
            need(lay["doc"][0] <= w and lay["doc"][1] <= h, f"document larger than window: {lay}")
            need(lay["scrollY_after_scrollTo"] == 0, f"page scrolled: {lay}")
            need(lay["note_bottom"] is not None and lay["note_bottom"] <= h, f"note clipped: {lay}")
            need(lay["panel"][2] <= lay["panel"][1] + 1, f"panel overflows: {lay}")
            need(lay["panel"][3] <= lay["panel"][0] + 1 and lay["panel"][4] == 0, f"panel wider than its box: {lay}")
            need(all(sw <= cw + 1 for sw, cw in lay["header"]), f"header line overflows sideways: {lay['header']}")
            return {tag: lay}
        C.run("no page scroll at 1440x900", lambda: no_scroll("1440x900"))

        # ---- U6 radiological orientation -------------------------------------------------
        def orientation():
            flush()
            page.wait_for_timeout(300)
            e = last_axial_view(api.events(session()))
            need(e, "no axial stack view event")
            need(radiological(e), f"displayed corners not anterior-up / patient-left-on-screen-right: {e['corners_lps']}")
            v = view()
            shot("u06-radiological", page.locator(".cornerstone-canvas").first.locator("xpath=.."))
            return {"corners_lps": e["corners_lps"], "flipped": {"h": v["flipH"], "v": v["flipV"]}}
        C.run("U6 axial stack shown radiologically (anterior up, patient right on screen left), true corners recorded",
              orientation)

        # ---- heartbeat POSTs ----------------------------------------------------------
        def heartbeat():
            n0 = len(posts)
            page.wait_for_timeout(2100)
            gaps = [round((b - a_) * 1000) for a_, b in zip(posts[n0:], posts[n0 + 1:])]
            need(len(posts) - n0 >= 3, f"only {len(posts) - n0} POSTs in 2.1 s")
            return {"posts_in_2.1s": len(posts) - n0, "gaps_ms": gaps}
        C.run("events POST every ~400 ms (heartbeat when idle)", heartbeat)

        # ---- real reading: wheel through slices with explicit windows (hotkeys) ---------
        viewer_sops = drv.sops()
        sop_to_idx = {s: i for i, s in enumerate(viewer_sops)}

        def num_of_k(k):
            i = sop_to_idx.get(case_sops[k])
            return None if i is None else i + 1

        def viewer_ranges(ranges):
            """Python twin of CoverageService.viewerRanges: case k ranges as viewer image numbers."""
            runs = []
            for k0, k1 in ranges:
                nums = sorted(n for n in (num_of_k(k) for k in range(min(k0, k1), max(k0, k1) + 1)) if n is not None)
                if not nums:
                    continue
                start = prev = nums[0]
                for n in nums[1:]:
                    if n != prev + 1:
                        runs.append([start, prev])
                        start = n
                    prev = n
                runs.append([start, prev])
            runs.sort(key=lambda r: r[0])
            merged: list[list[int]] = []
            for r in runs:
                if merged and r[0] <= merged[-1][1] + 1:
                    merged[-1][1] = max(merged[-1][1], r[1])
                else:
                    merged.append(list(r))
            return merged

        def read_pass(key, ks):
            drv.press_key(key)
            wait_view(preset=PRESETS[key])
            drv.goto(sop_to_idx[case_sops[ks[0]]])
            for k in ks:
                drv.goto(sop_to_idx[case_sops[k]])
                drv.dwell(a.dwell_ms)

        def reading():
            n = vol["n_slices"]
            cranial_last = vol["slice_dir"][2] > 0
            soft = list(range(n * 3 // 10, n)) if cranial_last else list(range(0, n - n * 3 // 10))
            lung = list(range(n // 7, n - n // 12))
            t0 = time.perf_counter()
            read_pass("1", soft)
            read_pass("2", lung)
            drv.press_key("5")  # brain window: qualifies no body organ
            flush()
            page.wait_for_timeout(600)
            c = cov()
            st = {}
            for o in c["organs"]:
                st[o["status"]] = st.get(o["status"], 0) + 1
            need(len(st) >= 2, f"expected a mix of statuses, got {st}")
            return {"s": round(time.perf_counter() - t0, 1), "wheel_ticks": drv.log.wheel_ticks,
                    "api_jumps": drv.log.api_jumps, "statuses": st, "soft_k": [soft[0], soft[-1]],
                    "lung_k": [lung[0], lung[-1]]}
        C.run("wheel-scroll reading with soft (1) and lung (2) hotkey windows", reading)
        shot("02-after-reading")
        shot("03-panel-after-reading", tid("covmap-panel"))
        shot("04-map-after-reading", tid("covmap-map"))

        def window_hint():
            wait_view(preset=PRESETS["5"])
            hint = tid("covmap-window-hint")
            need(hint.is_visible(), "no window hint in the brain window")
            text = hint.inner_text()
            need("counts for no organ" in text and "1" in text and "2" in text and "3" in text, f"hint text {text!r}")
            pressed = [k for k in ("soft", "lung", "bone") if tid(f"covmap-window-{k}").get_attribute("aria-pressed") == "true"]
            need(not pressed, f"a preset shows as active in the brain window: {pressed}")
            shot("u07-window-hint", tid("covmap-panel"))
            got = {}
            for k_ in ("1", "2"):
                drv.press_key(k_)
                wait_view(preset=PRESETS[k_])
                page.wait_for_function("() => !document.querySelector('[data-testid=\"covmap-window-hint\"]')", timeout=3000)
                page.wait_for_function("id => document.querySelector(`[data-testid=\"covmap-window-${id}\"]`)"
                                       ".getAttribute('aria-pressed') === 'true'", arg=PRESET_BUTTON[k_], timeout=3000)
                others = [b for b in ("soft", "lung", "bone")
                          if b != PRESET_BUTTON[k_] and tid(f"covmap-window-{b}").get_attribute("aria-pressed") == "true"]
                need(not others, f"also active after key {k_}: {others}")
                got[k_] = PRESET_BUTTON[k_]
            drv.press_key("5")
            tid("covmap-window-hint").wait_for(timeout=3000)
            return {"hint": text, "active_after_key": got}
        C.run("U7 window hint in a non-counting window; active preset highlighted", window_hint)

        def pct_and_hints():
            neutral()
            page.wait_for_timeout(600)
            # rows and coverage from one moment: retry while a render is still catching up
            for _ in range(10):
                snap = page.evaluate(f"() => ({{rows: ({ROWS_JS.strip()})(), cov: window.__covmap.lastCoverage}})")
                c = {o["key"]: o for o in snap["cov"]["organs"]}
                if all(r["pct"] == f"{math.floor(c[r['key']]['coverage'] * 100 + 1e-9)}%" for r in snap["rows"]):
                    break
                page.wait_for_timeout(300)
            rows_ = snap["rows"]
            thr = snap["cov"]["params"]["reviewed_threshold"] * 100
            partial = []
            for r in rows_:
                o = c[r["key"]]
                shown = math.floor(o["coverage"] * 100 + 1e-9)
                need(r["pct"] == f"{shown}%", f"{r['key']}: shows {r['pct']}, coverage {o['coverage']}")
                if r["status"] != "reviewed":
                    need(shown < thr, f"{r['key']} {r['status']} shows {r['pct']} (>= reviewed threshold)")
                if r["status"] == "partial":
                    need(r["hint"] and HINT_RE.fullmatch(r["hint"]), f"{r['key']} hint {r['hint']!r}")
                    if shown > 0:
                        need(r["hint"].startswith(f"{100 - shown}% unseen · "), f"{r['key']} hint {r['hint']!r} vs {shown}%")
                    partial.append((r["key"], r["pct"], r["hint"]))
            need(partial, "no partial rows to check")
            return {"partial_rows": partial}
        C.run("U20/U11 rows floor the % (never shows the threshold early); hints '34% unseen · reason'", pct_and_hints)

        def events_logged():
            evs = api.events(session())
            views = [e for e in evs if e["kind"] == "view"]
            need(views, "no view events in events.jsonl")
            need(all(e["source"] == "ohif" for e in evs), "non-ohif source")
            need(all(len(e["corners_lps"]) == 4 and len(e["corners_lps"][0]) == 3 for e in views), "corners")
            need(all(e.get("sop") and e.get("series") == vol["series_uid"] for e in views), "sop/series")
            seqs = [e["seq"] for e in evs]
            need(seqs == sorted(seqs) and len(set(seqs)) == len(seqs), "seq not strictly increasing")
            ts = [e["t_ms"] for e in evs]
            need(ts == sorted(ts), "t_ms not monotonic")
            vois = sorted({(e["voi"]["wc"], e["voi"]["ww"]) for e in views})
            need((40, 400) in vois and (-600, 1500) in vois, f"exact presets missing from vois {vois}")
            sops_seen = {e["sop"] for e in views}
            dup = sum(1 for x, y in zip(views, views[1:]) if {**x, "seq": 0, "t_ms": 0} == {**y, "seq": 0, "t_ms": 0})
            need(dup == 0, f"{dup} identical consecutive view events (dedupe)")
            return {"events": len(evs), "views": len(views), "distinct_sops": len(sops_seen), "vois": vois,
                    "canvas_px": views[-1]["canvas_px"], "example": views[len(views) // 2]}
        C.run("events.jsonl: view events with sop/corners/normal/voi, deduped, ordered", events_logged)

        # ---- map hover / click ---------------------------------------------------------
        def scan_map():
            """Hover a 14x14 grid over the map: organ -> (first x, first y, label, mean x)."""
            box = tid("covmap-map").bounding_box()
            found: dict = {}
            xs: dict = {}
            for gy in range(2, 16):
                for gx in range(2, 16):
                    x, y = box["x"] + box["width"] * gx / 17, box["y"] + box["height"] * gy / 17
                    page.mouse.move(x, y)
                    page.wait_for_timeout(45)
                    el = tid("covmap-map-hover")
                    if el.count():
                        k = el.get_attribute("data-key")
                        if k:
                            xs.setdefault(k, []).append(x)
                            if k not in found:
                                found[k] = (x, y, el.inner_text())
            cx = box["x"] + box["width"] / 2
            return {k: (*v, sum(xs[k]) / len(xs[k]) - cx) for k, v in found.items()}
        hits: dict = {}

        def hover():
            hits.update(scan_map())
            need(len(hits) >= 5, f"hover found only {list(hits)}")
            return {"organs_hovered": len(hits), "examples": [v[2] for v in list(hits.values())[:4]]}
        C.run("3D map hover shows organ name and %", hover)

        def map_click():
            c = {o["key"]: o for o in cov()["organs"]}
            target = next(((k, v) for k, v in hits.items() if c[k]["status"] != "reviewed"), None)
            need(target, "no gray/yellow organ found under the map scan")
            key, (x, y, _, _) = target
            neutral()
            exp = expect_jump(key)
            page.mouse.move(x, y)
            page.wait_for_timeout(120)
            shot("05-map-hover", tid("covmap-map"))
            page.mouse.click(x, y)
            wait_view(sop=exp["sop"], preset=exp["preset"])
            need(page.evaluate("() => window.__covmap.state().selected") == key, "not selected")
            return {"clicked": key, "status": c[key]["status"], "gap": exp["gap"], "now": view(),
                    "center_k": by_key[key]["center_k"]}
        C.run("U1 click gray/yellow organ in 3D map -> the server's gap slice in the organ window", map_click)
        shot("06-after-map-click")

        # ---- next gap --------------------------------------------------------------------
        def next_gap():
            jumps = []
            for _ in range(3):
                neutral()
                rem = page.evaluate("() => window.__covmap.remaining()")
                pre = {k: expect_jump(k) for k in rem}
                prev = page.evaluate("() => window.__covmap.state().selected")
                tid("covmap-next-gap").click()
                if len(rem) > 1:
                    page.wait_for_function("p => { const s = window.__covmap.state().selected; return s && s !== p; }",
                                           arg=prev, timeout=3000)
                else:
                    page.wait_for_timeout(200)
                key = page.evaluate("() => window.__covmap.state().selected")
                need(key in pre, f"next gap selected {key}, not remaining")
                exp = pre[key]
                wait_view(sop=exp["sop"], preset=exp["preset"])
                jumps.append({"key": key, "gap_k": exp["gap"]["k"], "center_k": by_key[key]["center_k"],
                              "image": view()["idx"] + 1, "window": exp["preset"]})
            return jumps
        C.run("U1 Next gap jumps to remaining organs at the server's gap slice with the organ window", next_gap)

        # ---- row click + row actions ----------------------------------------------------
        def detail_texts(key):
            return page.evaluate("""k => { const s = window.__covmap.state();
                const q = id => { const el = document.querySelector(`[data-testid="${id}"]`); return el ? el.textContent.trim() : null; };
                return {gap: s.gaps[k] || null, extent: q(`covmap-organ-${k}-extent`), todo: q(`covmap-organ-${k}-gap`)}; }""", key)

        def want_todo(g):
            if not g:
                return None
            vr = viewer_ranges(g["ranges"])
            if g["unseen_voxels"] > 0 and vr:
                return f"Still to read: images {ranges_text(vr)}."
            if g["unseen_voxels"] > 0:
                return "Unseen parts are in view; window or dwell is missing."
            return "Nothing left unseen."

        def row_click():
            key = "liver" if "liver" in by_key else organs[0]["key"]
            neutral()
            exp = expect_jump(key)
            row(key).locator(".cm-organ-line").click()
            wait_view(sop=exp["sop"], preset=exp["preset"])
            here = view()["idx"] + 1
            g = exp["gap"]
            todo = viewer_ranges(g["ranges"])
            if g["k"] is not None:
                need(num_of_k(g["k"]) == here, f"landed on image {here}, gap k {g['k']} is image {num_of_k(g['k'])}")
                need(any(r0 <= here <= r1 for r0, r1 in todo), f"image {here} not in still-to-read {todo}")
            # U2: the detail line uses the viewer's image numbers (n of n/N), not case slice indices
            o = by_key[key]
            want_ext = f"On images {ranges_text(viewer_ranges([o['k_range']]), 2)} of {len(viewer_sops)}."
            deadline = time.perf_counter() + 4
            while True:
                t = detail_texts(key)
                if t["extent"] == want_ext and t["todo"] == want_todo(t["gap"]) and t["todo"]:
                    break
                need(time.perf_counter() < deadline, f"detail {t} != extent {want_ext!r} / todo {want_todo(t['gap'])!r}")
                page.wait_for_timeout(200)
            shot("u01-u02-row-detail", tid("covmap-panel"))
            return {"key": key, "gap": g, "image": here, "of": len(viewer_sops), "extent": t["extent"],
                    "still_to_read": t["todo"], "k_range": o["k_range"]}
        C.run("U1/U2 organ row click -> gap slice; detail shows extent and still-to-read in viewer image numbers",
              row_click)

        rem_keys = page.evaluate("() => window.__covmap.remaining()")

        def row_actions():
            need(len(rem_keys) >= 3, f"need 3 remaining organs, have {rem_keys}")
            k_done, k_na, k_flag = rem_keys[-1], rem_keys[-2], rem_keys[-3]
            marks.update(done=k_done, na=k_na, flag=k_flag)
            # U17: row buttons at least 24x24 with space between them
            boxes = [page.locator(f'[data-testid="covmap-organ-{k_done}-{b}"]').bounding_box() for b in ("done", "na", "flag")]
            need(all(b["width"] >= 24 and b["height"] >= 24 for b in boxes), f"row buttons {boxes}")
            gaps_px = [round(boxes[i + 1]["x"] - (boxes[i]["x"] + boxes[i]["width"]), 1) for i in range(2)]
            need(all(g_ >= 2 for g_ in gaps_px), f"row buttons touch: gaps {gaps_px}")
            row(k_done).scroll_into_view_if_needed()
            tid(f"covmap-organ-{k_done}-done").click()
            row(k_na).scroll_into_view_if_needed()
            tid(f"covmap-organ-{k_na}-na").click()
            row(k_flag).scroll_into_view_if_needed()
            tid(f"covmap-organ-{k_flag}-flag").click()
            note = tid(f"covmap-organ-{k_flag}-note")
            # U9: the note input takes focus and is scrolled into view
            page.wait_for_function("k => document.activeElement && document.activeElement.getAttribute('data-testid') === `covmap-organ-${k}-note`",
                                   arg=k_flag, timeout=3000)
            nb = note.bounding_box()
            lb = tid("covmap-organ-list").bounding_box()
            need(nb and nb["y"] >= lb["y"] - 1 and nb["y"] + nb["height"] <= lb["y"] + lb["height"] + 1,
                 f"note input not visible in the list: {nb} list {lb}")
            shot("u09-flag-note-focus", tid("covmap-panel"))
            page.keyboard.type("outline cuts through neighbour")
            page.keyboard.press("Enter")
            flush()
            page.wait_for_timeout(500)
            flush()
            need(row(k_done).get_attribute("data-reader") == "done", "done not shown")
            need(row(k_na).get_attribute("data-reader") == "na", "na not shown")
            need(row(k_flag).get_attribute("data-flagged") == "1", "flag not shown")
            rem = page.evaluate("() => window.__covmap.remaining()")
            need(k_done not in rem and k_na not in rem and k_flag in rem, f"remaining wrong: {rem}")
            ann = [e for e in api.events(session()) if e["kind"] == "annotation"]
            got = {(e["organ"], e["action"], e.get("note")) for e in ann}
            need((k_done, "done", None) in got and (k_na, "na", None) in got, f"annotations {got}")
            need((k_flag, "flag", "outline cuts through neighbour") in got, f"flag note missing {got}")
            c = {o["key"]: o for o in cov()["organs"]}
            server_layer = {k: (c[k].get("reader"), c[k].get("flagged"), c[k].get("note")) for k in (k_done, k_na, k_flag)}
            row(k_done).scroll_into_view_if_needed()
            shot("07-row-actions", tid("covmap-panel"))
            return {"done": k_done, "na": k_na, "flag": k_flag, "annotations": sorted(got, key=str),
                    "server_reader_layer": server_layer, "summary": cov()["summary"], "button_boxes": boxes,
                    "button_gaps_px": gaps_px, "note_box": nb}
        C.run("U9/U17 row actions done / N/A / flag (note focused, in view) post annotation events; 24px buttons",
              row_actions)

        # ---- hotkeys D A G O on the selected organ --------------------------------------
        def hotkeys_reader():
            key = next(k for k in page.evaluate("() => window.__covmap.remaining()") if k not in rem_keys[-3:])
            row(key).locator(".cm-organ-line").click()
            page.wait_for_timeout(300)
            seq = []
            for k_, attr, val in (("d", "data-reader", "done"), ("d", "data-reader", ""), ("a", "data-reader", "na"),
                                  ("a", "data-reader", ""), ("g", "data-flagged", "1"), ("g", "data-flagged", "0")):
                drv.press_key(k_)
                page.wait_for_function("([k, a, v]) => document.querySelector(`[data-testid=\"covmap-organ-${k}\"]`).getAttribute(a) === v",
                                       arg=[key, attr, val], timeout=3000)
                seq.append(f"{k_}->{attr}={val}")
                if k_ == "g" and val == "1":
                    # the note input takes focus after flagging; Esc leaves it so G works again
                    page.wait_for_function("k => document.activeElement && document.activeElement.getAttribute('data-testid') === `covmap-organ-${k}-note`",
                                           arg=key, timeout=3000)
                    page.keyboard.press("Escape")
                    page.wait_for_function("() => !(document.activeElement && document.activeElement.tagName === 'INPUT')", timeout=2000)
                    seq.append("note focused; Esc")
            flush()
            acts = [e["action"] for e in api.events(session()) if e["kind"] == "annotation" and e["organ"] == key]
            need("done" in acts and "na" in acts and "flag" in acts and "clear" in acts, f"actions {acts}")
            return {"organ": key, "sequence": seq, "posted_actions": acts}
        C.run("hotkeys D / A / G toggle reader state of the selected organ", hotkeys_reader)

        # ---- U3/U13 3D map views, rotation, picking after rotation, hide reviewed ---------
        def map_views():
            m = tid("covmap-map")
            canvas = page.locator('[data-testid="covmap-map"] .cm-map-canvas')
            res: dict = {}

            def away():
                x, y = drv.viewport_center()
                page.mouse.move(x, y)
                page.wait_for_timeout(150)

            want = {"back": "LR", "right": "PA", "left": "AP", "front": "RL"}
            for v, orient in want.items():
                tid(f"covmap-map-view-{v}").click()
                page.wait_for_function("([o, v]) => { const m = document.querySelector('[data-testid=\"covmap-map\"]');"
                                       " return m.dataset.orient === o && m.dataset.view === v; }", arg=[orient, v], timeout=3000)
                need(tid(f"covmap-map-view-{v}").get_attribute("aria-pressed") == "true", f"{v} not pressed")
                away()
                box = model_box(shot(f"u03-map-{v}", canvas))
                need(box and 0.3 < box["center"][0] < 0.7 and 0.3 < box["center"][1] < 0.7, f"{v} view: model {box}")
                res[v] = {"orient": orient, "model": box}
            labels = (tid("covmap-map-orient-left").inner_text(), tid("covmap-map-orient-right").inner_text())
            need(labels == ("R", "L"), f"front view side labels {labels}")
            # a 200 px drag rotates about the model's center: it stays in view and centred
            before = res["front"]["model"]
            v0 = view()
            cb = canvas.bounding_box()
            cx, cy = cb["x"] + cb["width"] / 2, cb["y"] + cb["height"] / 2
            page.mouse.move(cx - 100, cy)
            page.mouse.down()
            for i in range(1, 11):
                page.mouse.move(cx - 100 + 20 * i, cy, steps=2)
            page.mouse.up()
            page.wait_for_function("() => document.querySelector('[data-testid=\"covmap-map\"]').dataset.view === ''", timeout=2000)
            away()
            after = model_box(shot("u03-map-dragged", canvas))
            need(after and abs(after["center"][0] - 0.5) < 0.2 and abs(after["center"][1] - 0.5) < 0.2,
                 f"model left the centre after the drag: {after}")
            need(after["fill"] > 0.3 * before["fill"], f"model mostly out of view after the drag: {after} vs {before}")
            need(view()["sop"] == v0["sop"], "the drag jumped the viewer (taken as a click)")
            dragged_orient = m.get_attribute("data-orient")
            # picking after a rotation: in the back view the patient's left is on screen left
            tid("covmap-map-view-back").click()
            page.wait_for_function("() => document.querySelector('[data-testid=\"covmap-map\"]').dataset.orient === 'LR'", timeout=3000)
            back = scan_map()
            need(len(back) >= 3, f"hover after rotation found only {list(back)}")
            xs_lps = [o["centroid_lps"][0] for o in organs]
            mid = (min(xs_lps) + max(xs_lps)) / 2
            lateral = {k: by_key[k]["centroid_lps"][0] - mid for k in by_key if abs(by_key[k]["centroid_lps"][0] - mid) > 40}
            checks = []
            for name, found, sign in (("front", hits, 1), ("back", back, -1)):
                for k, v in found.items():
                    if k in lateral and abs(v[3]) > 8:
                        checks.append((name, k, (v[3] > 0) == (sign * lateral[k] > 0)))
            ok = [c_ for c_ in checks if c_[2]]
            need(len(checks) >= 2 and len(ok) >= 0.8 * len(checks), f"left/right picking: {checks}")
            # Hide reviewed: settled organs (reviewed, done, N/A) leave the map
            tid("covmap-map-view-front").click()
            rows_ = page.evaluate(ROWS_JS)
            unsettled = [r["key"] for r in rows_ if r["status"] != "reviewed" and r["reader"] not in ("done", "na")]
            drawn = int(m.get_attribute("data-organs"))
            need(int(m.get_attribute("data-visible")) == drawn, "organs hidden before the toggle")
            tid("covmap-map-gaps-only").click()
            if drawn == len(organs):  # every organ has a mesh: exactly the unsettled ones stay
                page.wait_for_function("n => document.querySelector('[data-testid=\"covmap-map\"]').dataset.visible === String(n)",
                                       arg=len(unsettled), timeout=3000)
            else:
                page.wait_for_timeout(500)
            visible = int(m.get_attribute("data-visible"))
            need(visible <= len(unsettled), f"visible {visible} > unsettled {len(unsettled)}")
            need(tid("covmap-map-gaps-only").get_attribute("aria-pressed") == "true", "toggle not pressed")
            away()
            shot("u03-map-hide-reviewed", m)
            tid("covmap-map-gaps-only").click()
            page.wait_for_function("n => document.querySelector('[data-testid=\"covmap-map\"]').dataset.visible === String(n)",
                                   arg=drawn, timeout=3000)
            # U13/U23: legend below the map, with the N/A entry
            lg = tid("covmap-map-legend").bounding_box()
            mb = m.bounding_box()
            need(lg["y"] >= mb["y"] + mb["height"] - 1, f"legend {lg} not below the map {mb}")
            need(page.locator('[data-testid="covmap-map-legend"] .cm-swatch--na').count() == 1, "no N/A legend swatch")
            shot("u13-map-na-legend", page.locator(".cm-map-wrap"))
            return {"views": res, "drag": {"before": before, "after": after, "orient": dragged_orient},
                    "back_hovered": sorted(back), "left_right_checks": checks, "unsettled": len(unsettled),
                    "visible_hidden_reviewed": visible, "drawn": drawn, "na_organ": marks.get("na")}
        C.run("U3/U13 map: A/P/R/L views + side labels, drag keeps model centred, picking after rotation, "
              "hide reviewed, N/A legend", map_views)

        def outline():
            key = "liver" if "liver" in by_key else organs[0]["key"]
            neutral()
            exp = expect_jump(key)
            row(key).locator(".cm-organ-line").click()
            wait_view(sop=exp["sop"], preset=exp["preset"])
            drv.press_key("o")
            page.wait_for_selector('[data-testid="covmap-outline"] path', timeout=8000)
            d1 = page.locator('[data-testid="covmap-outline"] path').nth(1).get_attribute("d")
            # U18: the outline banner sits below the viewport's top orientation marker
            label = page.locator('[data-testid="covmap-outline"] text').first.bounding_box()
            marker = page.evaluate("""() => { const c = document.querySelector('.cornerstone-canvas').getBoundingClientRect();
                const cx = c.left + c.width / 2;
                const els = [...document.querySelectorAll('.ViewportOrientationMarkers *, [class*="orientation-marker"], [class*="OrientationMarker"] *')]
                  .filter(e => e.children.length === 0 && e.textContent.trim().length && e.textContent.trim().length <= 3)
                  .map(e => e.getBoundingClientRect()).filter(r => r.width && Math.abs(r.left + r.width / 2 - cx) < 40 && r.top < c.top + 60);
                return els.length ? {top: els[0].top, bottom: els[0].bottom} : null; }""")
            if marker:
                need(label["y"] >= marker["bottom"] - 1, f"outline label {label} overlaps the orientation marker {marker}")
            shot("08-outline", page.locator(".cornerstone-canvas").first.locator("xpath=.."))
            shot("08b-outline-full")
            x, y = drv.viewport_center()
            page.mouse.move(x, y)
            page.mouse.wheel(0, 100)
            page.wait_for_function("d => { const p = document.querySelectorAll('[data-testid=\"covmap-outline\"] path')[1];"
                                   " return p && p.getAttribute('d') !== d; }", arg=d1, timeout=5000)
            drv.press_key("o")
            page.wait_for_function("() => !document.querySelector('[data-testid=\"covmap-outline\"] path')", timeout=3000)
            return {"organ": key, "path_chars": len(d1), "redrawn_on_scroll": True, "toggled_off": True,
                    "label_box": label, "top_marker": marker}
        C.run("O toggles the selected organ outline (SVG, redrawn on slice change, banner below the marker)", outline)

        def window_keys():
            got = {}
            for k_, pre in PRESETS.items():
                drv.press_key(k_)
                wait_view(preset=pre, timeout=4000)
                got[k_] = [view()["wc"], view()["ww"]]
            flush()
            vois = {(e["voi"]["wc"], e["voi"]["ww"]) for e in api.events(session()) if e["kind"] == "view"}
            missing = [p_ for p_ in PRESETS.values() if p_ not in vois]
            need(not missing, f"presets missing from events: {missing}")
            return got
        C.run("hotkeys 1-5 set exact W/L (soft, lung, bone, liver, brain)", window_keys)

        # ---- finish ---------------------------------------------------------------------
        def finish_flow():
            neutral()
            drv.press_key("f")
            dlg = tid("covmap-finish-dialog")
            dlg.wait_for(timeout=3000)
            n_items = dlg.locator('.cm-organ[data-testid^="covmap-finish-"]').count()
            rem = page.evaluate("() => window.__covmap.remaining()")
            need(n_items == len(rem), f"dialog lists {n_items}, remaining {len(rem)}")
            text = dlg.inner_text()
            need("not displayed enough" in text.lower(), "wording: 'displayed enough to count' missing")
            # U8: the reader's own decisions are listed
            dec = tid("covmap-finish-decisions")
            need(dec.is_visible(), "no decisions section")
            need(tid(f"covmap-decision-done-{marks['done']}").count() == 1, "done decision missing")
            need(by_key[marks["na"]]["name"] in tid("covmap-decision-na").inner_text(), "N/A decision missing")
            flag_txt = tid(f"covmap-decision-flag-{marks['flag']}").inner_text()
            need("outline cuts through neighbour" in flag_txt, f"flag decision {flag_txt!r}")
            # U19: the sheet leaves the header line and the research note visible
            lab = tid("covmap-session-label").bounding_box()
            top = page.evaluate("([x, y]) => { const e = document.elementFromPoint(x, y); return e && e.closest('[data-testid=\"covmap-session-label\"]') !== null; }",
                                [lab["x"] + min(20, lab["width"] / 2), lab["y"] + lab["height"] / 2])
            need(top, "session label covered by the finish sheet")
            nb = tid("covmap-note").bounding_box()
            need(tid("covmap-note").is_visible() and nb["y"] + nb["height"] <= page.viewport_size["height"],
                 f"research note not visible with the sheet open: {nb}")
            shot("09-finish-dialog", tid("covmap-panel"))
            first = rem[0]
            exp = expect_jump(first)
            page.locator(f'[data-testid="covmap-finish-{first}"] .cm-organ-line').click()
            wait_view(sop=exp["sop"], preset=exp["preset"])
            tid("covmap-go-back").click()
            dlg.wait_for(state="detached", timeout=3000)
            # U22: Escape closes the sheet (an open Session menu first, then the sheet)
            tid("covmap-finish").click()
            dlg.wait_for(timeout=3000)
            tid("covmap-session-menu").click()
            tid("covmap-session-popover").wait_for(timeout=2000)
            page.keyboard.press("Escape")
            tid("covmap-session-popover").wait_for(state="detached", timeout=2000)
            need(dlg.is_visible(), "one Escape closed both the Session menu and the sheet")
            page.keyboard.press("Escape")
            dlg.wait_for(state="detached", timeout=3000)
            drv.press_key("f")
            dlg.wait_for(timeout=3000)
            rem = page.evaluate("() => window.__covmap.remaining()")
            tid("covmap-confirm-finish").click()
            page.wait_for_selector('[data-testid="covmap-finished"]', timeout=3000)
            flush()
            fin = [e for e in api.events(session()) if e["kind"] == "finish"]
            need([e["confirmed"] for e in fin][-3:] == [False, False, True], f"finish events {fin}")
            need(fin[-1]["remaining"] == rem, "remaining in finish event differs")
            shot("10-finished-summary", tid("covmap-panel"))
            return {"listed": n_items, "jumped_to": first, "gap": exp["gap"],
                    "finish_events": [(e["confirmed"], len(e["remaining"])) for e in fin],
                    "decisions": dec.inner_text() if dec.count() else None,
                    "server_finished": cov()["summary"].get("finished"),
                    "summary_text": tid("covmap-summary").inner_text()}
        C.run("U8/U19/U22 Finish: list, decisions, jump, Go back, Esc, Confirm -> finish events + summary",
              finish_flow)

        def finish_follows_last():
            """C13: finished = the last finish event confirmed (SPEC). After a Confirm, F then Go
            back (or Esc) un-finishes: the chip goes at once, without a reload, and the server agrees."""
            s = session()
            need(tid("covmap-finished").count() == 1, "not finished before the check")
            steps = {}
            for how in ("go-back", "escape"):
                drv.press_key("f")
                dlg = tid("covmap-finish-dialog")
                dlg.wait_for(timeout=3000)
                if how == "go-back":
                    tid("covmap-go-back").click()
                else:
                    page.keyboard.press("Escape")
                dlg.wait_for(state="detached", timeout=3000)
                tid("covmap-finished").wait_for(state="detached", timeout=1500)  # at once, not after a reload
                need(tid("covmap-summary").get_attribute("data-finished") == "0", f"summary still finished after {how}")
                if how == "go-back":
                    shot("c13-after-go-back", tid("covmap-panel"))
                flush()
                page.wait_for_timeout(200)
                srv = api.coverage(s)["summary"]["finished"]
                need(srv and srv["confirmed"] is False, f"server finished after {how}: {srv}")
                need(cov()["summary"]["finished"]["confirmed"] is False, "client coverage still confirmed")
                need(tid("covmap-finished").count() == 0, f"chip back after the server reply ({how})")
                steps[how] = {"server": srv["confirmed"], "chip": tid("covmap-finished").count()}
                # Confirm again: the chip returns, and the server says finished
                drv.press_key("f")
                dlg.wait_for(timeout=3000)
                tid("covmap-confirm-finish").click()
                tid("covmap-finished").wait_for(timeout=1500)
                flush()
                page.wait_for_timeout(200)
                srv = api.coverage(s)["summary"]["finished"]
                need(srv and srv["confirmed"] is True, f"server not finished after Confirm: {srv}")
                need(tid("covmap-finished").count() == 1, "chip gone after the server confirmed")
                steps[how]["reconfirmed"] = srv["confirmed"]
            fin = [e["confirmed"] for e in api.events(s) if e["kind"] == "finish"]
            need(fin[-5:] == [True, False, True, False, True], f"finish events {fin}")
            return {"steps": steps, "finish_events": fin}
        C.run("C13 finished follows the last finish event: Confirm, then F + Go back / Esc un-finishes at once "
              "(chip, server), Confirm again", finish_follows_last)

        # ---- session menu: export, rules, replay, samples, new reading ------------------
        def menu(item):
            tid("covmap-session-menu").click()
            tid(item).click()

        def export():
            tid("covmap-session-menu").click()
            res = {}
            for f in ("jsonl", "json", "csv"):
                with page.expect_download(timeout=10000) as dl:
                    tid(f"covmap-export-{f}").click()
                d = dl.value
                target = out / f"c-export-{a.case}.{f}"
                d.save_as(str(target))
                res[f] = {"file": d.suggested_filename, "bytes": target.stat().st_size}
                need(target.stat().st_size > 0, f"empty {f} export")
            page.keyboard.press("Escape")
            return res
        C.run("Export JSONL / JSON / CSV downloads", export)

        def rules():
            menu("covmap-rules")
            dwell = tid("covmap-rules-dwell")
            need(dwell.get_attribute("min") == "0", f"min dwell min={dwell.get_attribute('min')!r}")
            need(tid("covmap-custom-rules").count() == 0, "custom-rules marker with default rules")
            need(tid("covmap-rules-restore").is_disabled(), "Restore defaults enabled with default rules")
            dwell.fill("")
            tid("covmap-rules-apply").click()
            tid("covmap-rules-error").wait_for(timeout=2000)
            err = tid("covmap-rules-error").inner_text()
            need(cov()["params"]["min_dwell_ms"] == 80, "empty dwell changed the rules")
            dwell.fill("400")
            tid("covmap-rules-apply").click()
            page.wait_for_function("() => window.__covmap.lastCoverage.params.min_dwell_ms === 400", timeout=10000)
            c = cov()
            tid("covmap-custom-rules").wait_for(timeout=3000)
            shot("11-rules-applied", tid("covmap-panel"))
            crowded = no_scroll("finished + custom rules + marks")  # the most crowded header lines
            tid("covmap-custom-rules").click()  # the marker opens the rules form
            tid("covmap-rules-restore").click()
            page.wait_for_function("""() => { const p = window.__covmap.lastCoverage.params;
                return p.min_dwell_ms === 80 && p.reviewed_threshold === 0.9 && p.skipped_threshold === 0.05; }""", timeout=10000)
            page.wait_for_function("() => !document.querySelector('[data-testid=\"covmap-custom-rules\"]')", timeout=3000)
            return {"empty_dwell_error": err, "at_400ms": c["summary"], "params": c["params"], "restored": cov()["params"],
                    "layout": crowded}
        C.run("U12 rules: min 0, empty rejected, custom marker, Restore defaults; re-scores the session", rules)

        def replay():
            s = session()
            flush()
            page.wait_for_timeout(300)
            seq0 = max(e["seq"] for e in api.events(s))
            before = view()
            menu("covmap-replay")
            bar = tid("covmap-replay-bar")
            bar.wait_for(timeout=10000)
            if page.evaluate("() => window.__covmap.state().replay.playing"):
                tid("covmap-replay-play").click()
            need(bar.get_attribute("role") == "group" and bar.get_attribute("aria-label"), "replay bar role/label")
            aria = {k: tid(f"covmap-replay-{k}").get_attribute("aria-label") for k in ("play", "slider", "close")}
            need(all(aria.values()), f"replay aria labels {aria}")
            need(tid("covmap-replay-slider").get_attribute("aria-valuetext"), "slider aria-valuetext")
            label = tid("covmap-session-label").inner_text()
            need("recording paused" in label, f"label during replay {label!r}")
            sb = tid("covmap-replay-slider").bounding_box()
            n = page.evaluate("() => window.__covmap.state().replay.frames.length")
            page.mouse.click(sb["x"] + 2, sb["y"] + sb["height"] / 2)  # real click on the track
            page.wait_for_function("() => window.__covmap.state().replay.index === 0", timeout=3000)
            page.wait_for_timeout(300)
            first = tid("covmap-summary").inner_text()
            shot("12-replay-start", tid("covmap-panel"))
            page.mouse.click(sb["x"] + sb["width"] * 0.6, sb["y"] + sb["height"] / 2)
            page.wait_for_function("() => window.__covmap.state().replay.index > 0", timeout=3000)
            idx = page.evaluate("() => window.__covmap.state().replay.index")
            fv = page.evaluate("i => window.__covmap.state().replay.frames[i].view || null", idx)
            # U5: the viewer follows the frame's image and window
            if fv:
                wait_view(sop=fv["sop"])
                vv = view()
                need(abs(vv["wc"] - fv["voi"]["wc"]) < 1 and abs(vv["ww"] - fv["voi"]["ww"]) < 1, f"replay window {vv} vs {fv}")
            page.wait_for_timeout(300)
            mid = tid("covmap-summary").inner_text()
            shot("13-replay-mid")
            disabled = page.locator('[data-testid$="-done"]').first.is_disabled()
            need(disabled, "row actions should be read-only during replay")
            # scrolling during the replay is not recorded
            x, y = drv.viewport_center()
            page.mouse.move(x, y)
            for _ in range(3):
                page.mouse.wheel(0, 100)
                page.wait_for_timeout(150)

            # each frame shows its own marks and counts: rows carry the reader marks at the
            # frame's time, the header the frame's summary ('left' counts N/A and done)
            def frame_view(i):
                page.evaluate("i => window.__covmap.service.setReplayIndex(i)", i)
                page.wait_for_function("i => window.__covmap.state().replay.index === i", arg=i, timeout=3000)
                page.wait_for_timeout(150)
                fs = page.evaluate("i => window.__covmap.state().replay.frames[i].summary", i)
                rows_ = page.evaluate(ROWS_JS)
                left = int(tid("covmap-remaining").inner_text().split()[0])
                got = {"left": left, "na": sorted(r["key"] for r in rows_ if r["reader"] == "na"),
                       "done": sorted(r["key"] for r in rows_ if r["reader"] == "done"),
                       "flagged": sorted(r["key"] for r in rows_ if r["flagged"] == "1"),
                       "attested": sum(r["reader"] == "done" and r["status"] != "reviewed" for r in rows_),
                       "row_left": sum(r["status"] != "reviewed" and r["reader"] not in ("done", "na") for r in rows_)}
                want = {"left": len(fs["remaining"]), "na": fs["not_applicable"], "flagged": fs["flagged"],
                        "attested": fs["attested"]}
                need(got["left"] == want["left"] == got["row_left"],
                     f"frame {i}: {got['left']} left shown, {got['row_left']} by its rows, summary {want['left']}")
                need(len(got["na"]) == want["na"] and len(got["flagged"]) == want["flagged"]
                     and got["attested"] == want["attested"], f"frame {i}: rows {got} vs summary {want}")
                return {"index": i, "t_ms": page.evaluate("i => window.__covmap.state().replay.frames[i].t_ms", i),
                        "shown": got, "summary": want}
            frames_checked = [frame_view(i) for i in sorted({0, idx, n - 1})]
            last = frames_checked[-1]["shown"]
            need(marks["na"] in last["na"] and marks["done"] in last["done"] and marks["flag"] in last["flagged"],
                 f"last frame lost the reader marks {marks}: {last}")
            panel_text = tid("covmap-panel").inner_text()
            need(f"{frames_checked[-1]['summary']['na']} N/A" in panel_text, "N/A count missing from the last frame header")
            live_left = len(api.coverage(s)["summary"]["remaining"])
            shot("replay-last-frame", tid("covmap-panel"))
            # Rules during replay: disabled with the reason, never a silent no-op
            tid("covmap-session-menu").click()
            rules_item = tid("covmap-rules")
            rules_item.wait_for(timeout=2000)
            need(rules_item.is_disabled(), "Rules menu item enabled during replay")
            rules_title = rules_item.get_attribute("title") or ""
            need("close replay" in rules_title.lower(), f"Rules item title {rules_title!r}")
            shot("replay-rules-disabled", tid("covmap-panel"))
            page.keyboard.press("Escape")
            tid("covmap-session-popover").wait_for(state="detached", timeout=2000)
            params0 = api.coverage(s)["params"]
            direct = page.evaluate("""async () => { try { await window.__covmap.service.applyRules({min_dwell_ms: 300});
                                       return 'applied'; } catch (e) { return 'refused: ' + e.message; } }""")
            need(direct.startswith("refused") and "close replay" in direct.lower(), f"applyRules during replay: {direct}")
            need(api.coverage(s)["params"] == params0, "rules changed during replay")
            tid("covmap-replay-close").click()
            bar.wait_for(state="detached", timeout=3000)
            need(first != mid, "replay frames did not change the summary")
            # closing restores the reader's image and window and resumes recording
            wait_view(sop=before["sop"], preset=(before["wc"], before["ww"]))
            page.wait_for_timeout(500)
            flush()
            new = [e for e in api.events(s) if e["seq"] > seq0]
            hid = next((i for i, e in enumerate(new) if e["kind"] == "hidden" and e["viewport"] == "*"), None)
            need(hid is not None, f"no hidden '*' at replay start: {[e['kind'] for e in new]}")
            after = [e for e in new[hid + 1:] if e["kind"] == "view"]
            need(after, "recording did not resume after replay")
            stray = [e["sop"] for e in after if e["sop"] != before["sop"]]
            need(not stray, f"{len(stray)} view events recorded during replay")
            return {"frames": n, "mid_index": idx, "frame_view": fv, "summary_start": first, "summary_mid": mid,
                    "read_only": disabled, "aria": aria, "restored": view(), "views_after_close": len(after),
                    "frame_marks": frames_checked, "live_left_at_close": live_left, "rules_item_title": rules_title,
                    "apply_rules_in_replay": direct}
        C.run("U5/U14 Replay: viewer follows frames, nothing recorded, view restored, recording resumes, aria; "
              "each frame shows its marks and counts (N/A, done, flag); Rules disabled during replay", replay)

        samples = api.json(f"/api/cases/{a.case}/samples")

        def load_sample():
            need(samples, "no sample readings on the server for this case")
            smp = samples[0]
            s0 = session()
            menu("covmap-load-sample")
            tid("covmap-sample-list").wait_for(timeout=3000)
            shot("14a-sample-list", tid("covmap-panel"))
            tid(f"covmap-sample-{smp['name']}").click()
            # U10: the current reading has content, so it asks first
            tid("covmap-confirm").wait_for(timeout=3000)
            shot("u10-confirm", tid("covmap-panel"))
            start_scroll()
            try:
                tid("covmap-confirm-ok").click()
                page.wait_for_function("s => window.__covmap.sessionId && window.__covmap.sessionId !== s", arg=s0, timeout=15000)
                page.wait_for_timeout(800)
            finally:
                ticks = stop_scroll()
            page.wait_for_function("() => window.__covmap.lastCoverage", timeout=10000)
            s1 = session()
            name = re.sub(r"^sample\s*:?\s*", "", smp["label"], flags=re.I)
            label = tid("covmap-session-label").inner_text()
            need(label == f"Your reading, started from sample: {name}", f"label {label!r}")
            origin = tid("covmap-origin").inner_text()
            need(SOURCE_TEXT.get(smp.get("source"), smp.get("source") or "") in origin, f"origin {origin!r} for {smp}")
            loaded = cov()["summary"]
            need(loaded["reviewed"] + loaded["partial"] > 0, f"sample coverage not shown: {loaded}")
            shot("14-sample-loaded")
            # the reader continues on top of the sample: live events follow it in time and seq
            x, y = drv.viewport_center()
            page.mouse.move(x, y)
            for _ in range(3):
                page.mouse.wheel(0, 100)
                page.wait_for_timeout(200)
            flush()
            evs = api.events(s1)
            pre = [e for e in evs if e.get("from_sample") == smp["name"]]
            live = [e for e in evs if not e.get("from_sample")]
            need(len(pre) == smp["events"], f"prefilled {len(pre)} of {smp['events']}")
            need(live, "no live events after loading the sample")
            need(min(e["t_ms"] for e in live) >= max(e["t_ms"] for e in pre), "live events precede the sample")
            need(min(e["seq"] for e in live) > max(e["seq"] for e in pre), "live seq overlaps the sample")
            c6 = fresh_session_events(s1)
            return {"sample": {k: smp.get(k) for k in ("name", "label", "source", "events")}, "session": s1,
                    "label": label, "origin": origin, "summary": loaded, "prefilled": len(pre), "live_after": len(live),
                    "first_live": {k: live[0][k] for k in ("seq", "t_ms", "kind")}, "scripted_scroll_ticks": ticks,
                    "c6": c6, **old_session_closed(s0)}
        C.run("U4/U10/C6 Start from sample while scrolling: confirm, labelled as your reading, source shown, "
              "clean seq/clock", load_sample)

        def new_reading():
            s0 = session()
            # U24: the list is scrolled down before the switch
            lb = tid("covmap-organ-list").bounding_box()
            page.mouse.move(lb["x"] + lb["width"] / 2, lb["y"] + lb["height"] / 2)
            page.mouse.wheel(0, 600)
            page.wait_for_timeout(200)
            scrolled = page.evaluate("() => document.querySelector('[data-testid=\"covmap-organ-list\"]').scrollTop")
            tid("covmap-session-menu").click()
            tid("covmap-new-reading").click()
            tid("covmap-confirm").wait_for(timeout=3000)
            start_scroll()
            try:
                tid("covmap-confirm-ok").click()
                page.wait_for_function("s => window.__covmap.sessionId && window.__covmap.sessionId !== s", arg=s0, timeout=15000)
                page.wait_for_timeout(800)
            finally:
                ticks = stop_scroll()
            page.wait_for_function("() => window.__covmap.lastCoverage", timeout=10000)
            s1 = session()
            flush()
            page.wait_for_timeout(300)
            top = page.evaluate("() => document.querySelector('[data-testid=\"covmap-organ-list\"]').scrollTop")
            need(top == 0, f"organ list scrollTop {top} after New reading")
            need(tid("covmap-session-label").inner_text().lower().startswith("live reading"), "label")
            need(tid("covmap-origin").count() == 0, "sample origin shown for a live reading")
            res = {"old": s0, "new": s1, "list_scroll_before": scrolled, "scripted_scroll_ticks": ticks,
                   "c6_menu": fresh_session_events(s1), **old_session_closed(s0)}
            # the test hook: same path without the confirm, resolves to the new id once attached
            start_scroll()
            try:
                s2 = page.evaluate("async () => await window.__covmap.newReading()")
                page.wait_for_timeout(800)
            finally:
                stop_scroll()
            need(isinstance(s2, str) and s2 not in (s0, s1) and session() == s2, f"hook returned {s2!r}, session {session()}")
            flush()
            page.wait_for_timeout(300)
            res.update(hook=s2, c6_hook=fresh_session_events(s2), hook_old_closed=old_session_closed(s1))
            return res
        C.run("U10/U24/C6 New reading (confirm, and the __covmap.newReading hook) while scrolling: seq 0, t~0, "
              "no leaks", new_reading)

        def resume():
            s0 = session()
            x, y = drv.viewport_center()
            page.mouse.move(x, y)
            page.mouse.wheel(0, 100)
            page.wait_for_timeout(300)
            flush()
            n_before = len(api.events(s0))
            drv.open(url)
            reattach(a.case)
            need(session() == s0, f"reload opened {session()} instead of resuming {s0}")
            tid("covmap-resumed").wait_for(timeout=3000)
            page.wait_for_selector('[data-testid="covmap-map"][data-ready="1"]', timeout=60000)
            x, y = drv.viewport_center()
            page.mouse.move(x, y)
            for _ in range(3):
                page.mouse.wheel(0, 100)
                page.wait_for_timeout(200)
            flush()
            page.wait_for_timeout(300)
            evs = api.events(s0)
            need(len(evs) > n_before, "no events after the resume")
            seqs = [e["seq"] for e in evs]
            need(seqs == sorted(seqs) and len(set(seqs)) == len(seqs), "seq not strictly increasing across the reload")
            ts = [e["t_ms"] for e in evs]
            need(ts == sorted(ts), "t_ms not monotonic across the reload")
            shot("u10-resumed", tid("covmap-panel"))
            # the visible New reading next to "resumed" asks first, then starts a fresh session
            tid("covmap-new-reading-quick").click()
            tid("covmap-confirm").wait_for(timeout=3000)
            tid("covmap-confirm-ok").click()
            page.wait_for_function("s => window.__covmap.sessionId && window.__covmap.sessionId !== s", arg=s0, timeout=15000)
            page.wait_for_function("() => !document.querySelector('[data-testid=\"covmap-resumed\"]')", timeout=3000)
            return {"session": s0, "events_before_reload": n_before, "events_after": len(evs),
                    "continued_from": evs[n_before]["seq"] if len(evs) > n_before else None, "new_after_quick": session()}
        C.run("U10 reload resumes the case's session (seq/t continue); 'New reading' next to 'resumed'", resume)

        # ---- C12 one writer per session: two tabs on the same case ---------------------------
        def seen_now(s):
            return {o["key"]: o["seen_voxels"] for o in api.coverage(s)["organs"]}

        def unread(sv, exclude=()):
            """An organ nothing of which counts yet (soft-tissue organs first, largest first)."""
            cand = [o for o in organs if sv.get(o["key"], 0) == 0 and o["key"] not in exclude]
            need(cand, "no unread organ left")
            cand.sort(key=lambda o: (o["windows"][0] != "soft", -o["voxels"]))
            return cand[0]["key"]

        def read_in(pg, drv_, key):
            """Jump to the organ's gap slice in its window, dwell, then the brain window (counts
            for nothing) so the display state closes, and send."""
            pg.evaluate("k => window.__covmap.jump(k)", key)
            pg.wait_for_timeout(900)
            drv_.press_key("5")
            pg.wait_for_timeout(300)
            pg.evaluate("() => window.__covmap.flush()")
            pg.wait_for_timeout(200)

        def two_tabs():
            s = session()
            need(page.evaluate("() => window.__covmap.writer") is True, "this tab does not record its session")
            neutral()
            x = unread(seen_now(s))
            read_in(page, drv, x)
            need(seen_now(s)[x] > 0, f"tab A's reading of {x} not counted")
            # tab B opens the same case: it resumes the same session but must not record it
            b_posts: list[str] = []
            pb = ctx.new_page()
            pb.on("console", lambda m: console.append({"type": m.type, "text": "[tab B] " + m.text[:400],
                                                       "check": C.current, "at": m.location})
                  if m.type in ("error", "warning") else None)
            pb.on("pageerror", lambda e: console.append({"type": "pageerror", "text": "[tab B] " + str(e)[:400],
                                                         "check": C.current}))
            pb.on("request", lambda r: b_posts.append(r.url) if r.method == "POST" and "/events" in r.url else None)
            drv_b = OhifDriver(pb)
            try:
                drv_b.open(url)
                pb.wait_for_function("() => window.__covmap && window.__covmap.ready && window.__covmap.sessionId",
                                     timeout=90000)
                banner_b = pb.locator('[data-testid="covmap-other-tab"]')
                banner_b.wait_for(timeout=10000)
                text = banner_b.inner_text()
                need("open in another tab" in text and "not recording here" in text, f"banner {text!r}")
                need(pb.evaluate("() => window.__covmap.sessionId") == s, "tab B opened another session")
                need(pb.evaluate("() => window.__covmap.writer") is False, "tab B records too")
                need(page.evaluate("() => window.__covmap.writer") is True and tid("covmap-other-tab").count() == 0,
                     "tab A stopped recording when B opened")
                pb.wait_for_selector('[data-testid="covmap-map"][data-ready="1"]', timeout=60000)
                # B shows the reading A records (the server's coverage of the session)
                pb.wait_for_function("k => ((window.__covmap.lastCoverage || {}).organs || []).some(o => o.key === k && o.seen_voxels > 0)",
                                     arg=x, timeout=8000)
                pb.wait_for_timeout(300)
                shot_b = str(out / "c-c12-tabB-not-recording.png")
                pb.locator('[data-testid="covmap-panel"]').screenshot(path=shot_b)
                shots.append(shot_b)
                # B reads while not recording: nothing posted, nothing counted, reader keys refused
                y = unread(seen_now(s), exclude=(x,))
                read_in(pb, drv_b, y)
                pb.evaluate("k => window.__covmap.service.toggleDone(k)", y)
                pb.wait_for_timeout(300)
                flush()
                need(not b_posts, f"tab B posted {len(b_posts)} event batches while not recording")
                need(seen_now(s)[y] == 0, f"tab B's reading of {y} counted while it was not recording")
                need(pb.evaluate("k => (window.__covmap.state().reader[k] || {}).reader || null", y) is None,
                     "tab B marked done while not recording")
                toast_b = pb.get_by_text('Press "Record here" to record in this tab').count()
                # switch to tab B and take over
                pb.bring_to_front()
                pb.locator('[data-testid="covmap-record-here"]').click()
                pb.wait_for_function("() => window.__covmap.writer === true", timeout=10000)
                banner_b.wait_for(state="detached", timeout=3000)
                page.wait_for_function("() => window.__covmap.writer === false", timeout=10000)
                tid("covmap-other-tab").wait_for(timeout=3000)
                shot("c12-tabA-after-takeover", tid("covmap-panel"))
                read_in(pb, drv_b, y)
                need(b_posts, "tab B posted nothing after Record here")
                need(seen_now(s)[y] > 0, f"tab B's reading of {y} not counted after it took over")
                # switch to A: not recording now, its scrolling posts nothing; it follows the server
                page.bring_to_front()
                n_a = len(posts)
                xx, yy = drv.viewport_center()
                page.mouse.move(xx, yy)
                for _ in range(3):
                    page.mouse.wheel(0, 100)
                    page.wait_for_timeout(200)
                page.wait_for_timeout(800)
                need(len(posts) == n_a, f"tab A posted {len(posts) - n_a} batches while B records")
                page.wait_for_function("k => ((window.__covmap.lastCoverage.organs || []).find(o => o.key === k) || {}).seen_voxels > 0",
                                       arg=y, timeout=10000)
                # A takes over again; B shows the banner
                tid("covmap-record-here").click()
                page.wait_for_function("() => window.__covmap.writer === true", timeout=10000)
                tid("covmap-other-tab").wait_for(state="detached", timeout=3000)
                pb.wait_for_function("() => window.__covmap.writer === false", timeout=10000)
                banner_b.wait_for(timeout=3000)
                z = unread(seen_now(s), exclude=(x, y))
                read_in(page, drv, z)
                n_b = len(b_posts)
                pb.wait_for_timeout(800)
                need(len(b_posts) == n_b, "tab B kept posting after A took over")
            finally:
                pb.close()
            c = api.coverage(s)
            sv = {o["key"]: o["seen_voxels"] for o in c["organs"]}
            ign = c["events"]["ignored"]
            need(ign.get("other", 0) == 0 and ign.get("conflict", 0) == 0, f"server ignored events {ign}")
            need(all(sv[k] > 0 for k in (x, y, z)), f"expected organs not all counted: {[(k, sv[k]) for k in (x, y, z)]}")
            evs = api.events(s)
            seqs = [e["seq"] for e in evs]
            need(len(set(seqs)) == len(seqs) and seqs == sorted(seqs), "seqs not unique and increasing in the log")
            ts = [e["t_ms"] for e in evs]
            need(ts == sorted(ts), "t_ms not monotonic across the takeovers")
            gaps_ = [(a_, b_) for a_, b_ in zip(seqs, seqs[1:]) if b_ != a_ + 1]
            return {"session": s, "read_in_A": x, "read_in_B": y, "read_in_A_again": z,
                    "seen_voxels": {k: sv[k] for k in (x, y, z)}, "ignored": ign, "events": len(evs),
                    "seq_gaps": gaps_, "tab_B_posts": len(b_posts), "tab_B_refusal_toast": toast_b,
                    "banner": text}
        C.run("C12 two tabs on one case: one records (Web Lock), the other shows 'open in another tab' and "
              "records nothing; Record here takes over both ways; server ignored.other == 0, organs counted",
              two_tabs)

        def seq_conflict():
            """C12 defence in depth: a writer whose counter reuses accepted seqs (as a second tab
            without Web Locks would) gets a 409, renumbers after the server's last event and
            sends once more; nothing is dropped or double counted."""
            s = session()
            neutral()
            n0 = len(api.events(s))
            n_warn = sum("seq conflict (409)" in c_["text"] for c_ in console)
            page.evaluate("() => { const svc = window.__covmap.service; svc.seq = Math.max(0, svc.seq - 4); }")
            xx, yy = drv.viewport_center()
            page.mouse.move(xx, yy)
            for _ in range(3):
                page.mouse.wheel(0, 100)
                page.wait_for_timeout(200)
            flush()
            page.wait_for_timeout(300)
            flush()
            evs = api.events(s)
            seqs = [e["seq"] for e in evs]
            need(len(evs) > n0, "the renumbered events did not land")
            need(len(set(seqs)) == len(seqs) and seqs == sorted(seqs), "duplicate or unordered seqs after the 409")
            ign = api.coverage(s)["events"]["ignored"]
            need(ign.get("other", 0) == 0 and ign.get("conflict", 0) == 0, f"server ignored events {ign}")
            warned = sum("seq conflict (409)" in c_["text"] for c_ in console) - n_warn
            need(warned == 1, f"expected one 409 resync, saw {warned}")
            need(page.evaluate("() => window.__covmap.writer") is True and tid("covmap-other-tab").count() == 0,
                 "a single conflict stopped recording")
            return {"events_before": n0, "events_after": len(evs), "resyncs": warned, "ignored": ign,
                    "last_seqs": seqs[-6:]}
        C.run("C12 409 on a reused seq: resync from the 409 body, renumber the queue, retry once; no loss, "
              "no duplicates", seq_conflict)

        # ---- U6 radiological display survives a camera reset --------------------------------
        def same_corners(c1, c2, ordered=True, tol=0.5):
            a_, b_ = (c1, c2) if ordered else (sorted(c1), sorted(c2))
            return all(abs(x - y) <= tol for p, q in zip(a_, b_) for x, y in zip(p, q))

        def views_since(s, n0):
            flush()
            page.wait_for_timeout(400)
            flush()
            evs = api.events(s)[n0:]
            return [e for e in evs if e["kind"] == "view" and e.get("vp_type") == "stack" and abs(e["normal"][2]) > 0.9]

        def reset_keeps_orientation():
            s = session()
            neutral()  # brain window: counts for no organ
            drv.goto(len(viewer_sops) // 2)
            page.wait_for_timeout(300)
            e0 = views_since(s, 0)[-1]
            v0 = view()
            need(radiological(e0), f"default view not radiological: {e0['corners_lps']}")
            rows, cols = vol["rows"], vol["cols"]
            rect = (rows // 4, rows // 2, cols // 2, cols * 3 // 4)  # off-centre: a mirrored pan shows another region
            out_ = {"default_corners": e0["corners_lps"], "flips": [v0["flipH"], v0["flipV"]], "rect": rect}

            def after_reset(how, do_reset):
                drv.frame_rect(rect)
                page.wait_for_timeout(300)
                framed = views_since(s, 0)[-1]
                need(not same_corners(framed["corners_lps"], e0["corners_lps"]), f"{how}: frame_rect did not zoom")
                n0 = len(api.events(s))
                do_reset()
                after = views_since(s, n0)
                v = view()
                need(after, f"{how}: no view event after the reset")
                need(all(radiological(e) for e in after),
                     f"{how}: a displayed view after the reset is not radiological: {[e['corners_lps'] for e in after]}")
                need(same_corners(after[-1]["corners_lps"], e0["corners_lps"]),
                     f"{how}: reset view {after[-1]['corners_lps']} != default {e0['corners_lps']}")
                need([v["flipH"], v["flipV"]] == [v0["flipH"], v0["flipV"]], f"{how}: flips {v} vs {v0}")
                need(len(after) <= 4, f"{how}: {len(after)} view events after one reset (re-flip loop?)")
                return {"views_after_reset": len(after), "all_radiological": True, "flips": [v["flipH"], v["flipV"]]}

            def ohif_reset():
                btn = page.locator('[data-cy="Reset-btn"]')
                need(btn.count() == 1 and btn.is_visible(), "OHIF Reset View button not in the toolbar")
                btn.click()

            out_["ohif_reset_view_button"] = after_reset("OHIF Reset View", ohif_reset)
            shot("u06-after-reset-view", page.locator(".cornerstone-canvas").first.locator("xpath=.."))
            neutral()  # Reset View also resets the window to the image default
            out_["driver_reset_camera"] = after_reset("viewport.resetCamera() + render", drv.reset_camera)

            # the gauntlet driver frames after a reset: same displayed region with and without the flip
            drv.frame_rect(rect)
            page.wait_for_timeout(300)
            fr = views_since(s, 0)[-1]
            need(radiological(fr), f"frame after reset not radiological: {fr['corners_lps']}")
            page.evaluate("() => window.__covmap.setRadiological(false)")
            drv.reset_camera()
            drv.frame_rect(rect)
            page.wait_for_timeout(300)
            fa = views_since(s, 0)[-1]
            v_acq = view()
            drv.reset_camera()
            page.evaluate("() => window.__covmap.setRadiological(true)")
            page.wait_for_timeout(300)
            need(same_corners(fr["corners_lps"], fa["corners_lps"], ordered=False),
                 f"framed region differs with the flip: {fr['corners_lps']} vs {fa['corners_lps']}")
            back = views_since(s, 0)[-1]
            need(radiological(back) and same_corners(back["corners_lps"], e0["corners_lps"]),
                 f"not back to the radiological default view: {back['corners_lps']}")
            out_["frame_after_reset"] = {"radiological": fr["corners_lps"], "acquisition": fa["corners_lps"],
                                         "acquisition_flips": [v_acq["flipH"], v_acq["flipV"]],
                                         "same_region": True}
            return out_
        C.run("U6 radiological display kept after OHIF Reset View and viewport.resetCamera(); framing after a reset "
              "shows the same region as without the flip", reset_keeps_orientation)

        # ---- U6 scoring unchanged by the radiological flip ---------------------------------
        def flip_equivalence():
            mid = len(viewer_sops) // 2
            idxs = list(range(max(0, mid - 20), min(len(viewer_sops), mid + 20)))
            got = {}
            for mode, radio in (("radiological", True), ("acquisition", False)):
                page.evaluate("on => window.__covmap.setRadiological(on)", radio)
                # OHIF Reset View before the reading; it also resets the window to the image's
                # own, so it comes before the neutral window and the new session
                page.locator('[data-cy="Reset-btn"]').click()
                page.wait_for_timeout(300)
                neutral()
                s = page.evaluate("async () => await window.__covmap.newReading()")
                drv.goto(idxs[0])
                drv.press_key("1")
                wait_view(preset=PRESETS["1"])
                for i in idxs:
                    drv.goto(i)
                    drv.dwell(200)
                drv.press_key("5")
                wait_view(preset=PRESETS["5"])
                page.wait_for_timeout(300)
                flush()
                page.wait_for_timeout(500)
                flush()
                c = api.coverage(s)
                axial = [x for x in api.events(s) if x["kind"] == "view" and x.get("vp_type") == "stack"
                         and abs(x["normal"][2]) > 0.9]
                e = axial[-1]
                v = view()
                got[mode] = {"session": s, "seen": {o["key"]: o["seen_voxels"] for o in c["organs"]},
                             "radiological_corners": radiological(e), "flip": [v["flipH"], v["flipV"]],
                             "axial_views": len(axial), "all_views_radiological": all(radiological(x) for x in axial)}
            page.evaluate("() => window.__covmap.setRadiological(true)")
            page.wait_for_timeout(300)
            r, q = got["radiological"], got["acquisition"]
            need(r["radiological_corners"], "radiological session not shown radiologically")
            need(r["all_views_radiological"], "a view after Reset View was recorded non-radiological")
            diff = {k: (r["seen"][k], q["seen"][k]) for k in r["seen"] if r["seen"][k] != q["seen"][k]}
            need(not diff, f"seen voxels differ with the flip: {diff}")
            need(sum(r["seen"].values()) > 0, "scripted reading saw nothing")
            return {"images": [idxs[0] + 1, idxs[-1] + 1], "flip_needed_for_this_case": r["flip"] != q["flip"],
                    "radiological": {k: v for k, v in r.items() if k != "seen"},
                    "acquisition": {k: v for k, v in q.items() if k != "seen"},
                    "seen_voxels_total": sum(r["seen"].values()), "organs_equal": len(r["seen"])}
        C.run("U6 scoring identical with and without the radiological flip (Reset View, then the same scripted "
              "reading; 2 sessions)", flip_equivalence)

        # ---- hidden / visibility ------------------------------------------------------------
        def hidden():
            page.evaluate("""() => { Object.defineProperty(document, 'visibilityState', {value: 'hidden', configurable: true});
                                    document.dispatchEvent(new Event('visibilitychange')); }""")
            page.wait_for_timeout(500)
            page.evaluate("""() => { Object.defineProperty(document, 'visibilityState', {value: 'visible', configurable: true});
                                    document.dispatchEvent(new Event('visibilitychange')); }""")
            page.wait_for_timeout(500)
            flush()
            evs = api.events(session())
            h = [e for e in evs if e["kind"] == "hidden"]
            need(h and h[-1]["viewport"] == "*", f"no hidden event: {h}")
            after = [e for e in evs if e["seq"] > h[-1]["seq"] and e["kind"] == "view"]
            need(after, "no view event re-emitted after becoming visible")
            return {"hidden": h[-1], "views_after": len(after)}
        C.run("visibilitychange hidden -> hidden event; visible -> state re-emitted", hidden)

        # ---- 1280x800 -------------------------------------------------------------------------
        def small():
            page.set_viewport_size({"width": 1280, "height": 800})
            page.wait_for_timeout(1200)
            r = no_scroll("1280x800")
            shot("15-1280x800")
            return r
        C.run("no page scroll at 1280x800", small)

        # ---- MPR (volume viewports) ------------------------------------------------------------
        def mpr():
            page.set_viewport_size({"width": 1440, "height": 900})
            s = session()
            flush()
            n0 = len(api.events(s))
            page.evaluate("() => window.commandsManager.runCommand('toggleHangingProtocol', {protocolId: 'mpr'})")
            page.wait_for_function("""() => { const vps = window.services.cornerstoneViewportService.getRenderingEngine().getViewports();
                                      return vps.length >= 3 && vps.every(v => v.type === 'orthographic'); }""", timeout=60000)
            page.wait_for_timeout(2500)
            box = page.locator(".cornerstone-canvas").nth(1).bounding_box()
            page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            for _ in range(5):
                page.mouse.wheel(0, 100)
                page.wait_for_timeout(150)
            flush()
            page.wait_for_timeout(400)
            evs = api.events(s)[n0:]
            vol_views = [e for e in evs if e["kind"] == "view" and e["vp_type"] == "volume"]
            need(vol_views, "no volume view events")
            normals = sorted({tuple(round(x) for x in e["normal"]) for e in vol_views})
            hidden_ = [e for e in evs if e["kind"] == "hidden"]
            shot("16-mpr")
            page.evaluate("() => window.commandsManager.runCommand('toggleHangingProtocol', {protocolId: 'mpr'})")
            page.wait_for_timeout(2000)
            return {"volume_views": len(vol_views), "normals": normals, "viewports": sorted({e["viewport"] for e in vol_views}),
                    "hidden_on_disable": len(hidden_), "example": vol_views[0]}
        C.run("MPR volume viewports are instrumented (vp_type volume, 3 normals)", mpr)

        # ---- U15 case picker ------------------------------------------------------------------
        def case_picker():
            s_here = session()
            flush()
            cases = api.json("/api/cases")
            menu("covmap-open-case")
            tid("covmap-case-list").wait_for(timeout=5000)
            page.wait_for_selector(f'[data-testid="covmap-case-{cases[0]["case"]}"]', timeout=5000)
            listed = [c["case"] for c in cases if tid(f"covmap-case-{c['case']}").count() == 1]
            need(len(listed) == len(cases), f"case list {listed} vs {cases}")
            need(tid(f"covmap-case-{a.case}").is_disabled(), "the open case is selectable")
            shot("u15-case-list", tid("covmap-panel"))
            other = next((c for c in cases if c["case"] != a.case and c["segmented"]), None)
            need(other, "no second case on the server")
            tid(f"covmap-case-{other['case']}").click()
            page.wait_for_url(lambda u: other["study_uid"] in u, timeout=30000)
            reattach(other["case"])
            s_other = session()
            need(s_other and s_other != s_here, "other case shares the session")
            shot("u15-other-case")
            menu("covmap-open-case")
            tid(f"covmap-case-{a.case}").click()
            page.wait_for_url(lambda u: vol["study_uid"] in u, timeout=30000)
            reattach(a.case)
            need(session() == s_here, f"returning opened {session()} instead of resuming {s_here}")
            tid("covmap-resumed").wait_for(timeout=3000)
            page.wait_for_selector('[data-testid="covmap-map"][data-ready="1"]', timeout=60000)
            return {"cases": listed, "went_to": other["case"], "other_session": s_other, "resumed": s_here}
        C.run("U15 case picker: lists cases, opens the other, returning resumes this case's reading", case_picker)

        def final_layout():
            out_ = {}
            for w, h in ((1280, 800), (1440, 900)):
                page.set_viewport_size({"width": w, "height": h})
                page.wait_for_timeout(1500)
                out_.update(no_scroll(f"{w}x{h} after all interactions"))
            shot("18-final-1440x900")
            return out_
        C.run("no page scroll after every interaction (1280x800, 1440x900)", final_layout)

        # ---- stock viewer mode unaffected, / redirect ------------------------------------------
        def stock():
            p2 = ctx.new_page()
            p2.goto(f"{a.base}/viewer?StudyInstanceUIDs={vol['study_uid']}", wait_until="domcontentloaded")
            p2.wait_for_function("() => window.services && window.services.cornerstoneViewportService.getRenderingEngine()"
                                 " && window.services.cornerstoneViewportService.getRenderingEngine().getViewports().length", timeout=60000)
            p2.wait_for_timeout(2500)
            panel = p2.locator('[data-testid="covmap-panel"]').count()
            hooks = p2.evaluate("() => typeof window.__covmap")
            mode_cls = p2.evaluate("() => document.documentElement.classList.contains('covmap-mode')")
            n_bind = p2.evaluate("() => (window.services.customizationService.getCustomization('ohif.hotkeyBindings') || [])"
                                 ".filter(b => (b.commandName || '').startsWith('covmap')).length")
            p2.screenshot(path=str(out / "c-17-stock-viewer.png"))
            shots.append(str(out / "c-17-stock-viewer.png"))
            p2.close()
            need(panel == 0 and hooks == "undefined" and n_bind == 0 and not mode_cls,
                 f"stock viewer touched: {panel} {hooks} {n_bind} {mode_cls}")
            return {"covmap_panel": panel, "window.__covmap": hooks, "covmap_hotkeys": n_bind, "covmap_mode_class": mode_cls}
        C.run("stock /viewer mode unchanged (no panel, no instrumentation, stock hotkeys)", stock)

        def root_redirect():
            p3 = ctx.new_page()
            p3.goto(f"{a.base}/", wait_until="domcontentloaded")
            p3.wait_for_selector('[data-testid="covmap-panel"]', timeout=60000)
            u = p3.url
            p3.close()
            need("/coverage?StudyInstanceUIDs=" in u, u)
            return {"landed": u}
        C.run("/ opens the demo case in coverage mode", root_redirect)

        browser.close()
    return finish(out, a, C, console, shots, posts)


def finish(out, a, C, console, shots, posts) -> int:
    errors = [c for c in console if c["type"] in ("error", "pageerror")]
    warnings: dict[str, int] = {}
    for c in console:
        if c["type"] == "warning":
            warnings[c["text"][:160]] = warnings.get(c["text"][:160], 0) + 1
    report = {"case": a.case, "base": a.base, "passed": sum(r["ok"] for r in C.results),
              "failed": sum(not r["ok"] for r in C.results), "checks": C.results,
              "console_errors": errors, "console_warnings": warnings,
              "event_posts": len(posts), "screenshots": shots}
    path = out / f"c-ui-check-{a.case}.json"
    path.write_text(json.dumps(report, indent=1, default=str))
    print(json.dumps({"ok": report["failed"] == 0, "report": str(path), "passed": report["passed"],
                      "failed": report["failed"], "console_errors": len(errors)}))
    return 0 if report["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
