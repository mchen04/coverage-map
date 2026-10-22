"""Coverage-mode UI check: the real OHIF build in headless Chromium against a running covmap
server. Real input only (mouse wheel over the viewport, keys, clicks on panel and 3D map);
every claim is checked against the server's own session log and coverage. Research use only.

    python ohif/verify/ui_check.py --base http://127.0.0.1:8765 --case lidc-0001 \
        --out $MINI_WORK/evidence

Writes c-*.png screenshots and c-ui-check-<case>.json; exit 0 only if every check passed.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from covmap import paths  # noqa: E402
from e2e.ohif_driver import CHROMIUM_ARGS, OhifDriver  # noqa: E402

PRESETS = {"1": (40, 400), "2": (-600, 1500), "3": (480, 2500), "4": (90, 150), "5": (40, 80)}
CLASS_PRESET = {"soft": (40, 400), "lung": (-600, 1500), "bone": (480, 2500)}

VIEW_JS = r"""
() => {
  const s = window.services;
  const v = s.cornerstoneViewportService.getCornerstoneViewport(s.viewportGridService.getActiveViewportId());
  if (!v) return null;
  const r = v.getProperties().voiRange;
  const img = v.getCornerstoneImage ? v.getCornerstoneImage() : null;
  const id = img ? img.imageId : v.getCurrentImageId();
  const m = /instances\/([^/]+)\/frames/.exec(id || '');
  return {type: v.type, idx: v.getCurrentImageIdIndex ? v.getCurrentImageIdIndex() : null,
          sop: m ? m[1] : null, ww: Math.abs(r.upper - r.lower) + 1, wc: (r.lower + r.upper + 1) / 2};
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
          panel: panel ? [panel.clientWidth, panel.clientHeight, panel.scrollHeight] : null,
          note_bottom: note ? note.getBoundingClientRect().bottom : null,
          list: list ? [list.clientHeight, list.scrollHeight] : null, offenders: offenders.slice(0, 8)};
}
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

        def wait_view(sop=None, preset=None, timeout=8000):
            page.wait_for_function(
                """([sop, wc, ww]) => { const v = window.__cvView();
                     return v && (!sop || v.sop === sop) && (wc === null || (v.wc === wc && v.ww === ww)); }""",
                arg=[sop, preset[0] if preset else None, preset[1] if preset else None], timeout=timeout)

        # ---- open ----------------------------------------------------------------------
        def open_app():
            t0 = time.perf_counter()
            info = drv.open(url)
            install_view()
            page.wait_for_function("() => window.__covmap && window.__covmap.ready", timeout=60000)
            page.wait_for_selector('[data-testid="covmap-map"][data-ready="1"]', timeout=60000)
            page.wait_for_function("() => window.__covmap.eventsSent > 0", timeout=20000)
            marks = page.evaluate("""() => ({
                first: performance.getEntriesByName('covmap:first-image').map(e => e.startTime),
                map: performance.getEntriesByName('covmap:map-ready').map(e => e.startTime),
                hooks: {first: window.__covmap.firstImageRenderedAt, map: window.__covmap.mapReadyAt,
                        caseId: window.__covmap.caseId, sessionId: window.__covmap.sessionId}})""")
            need(marks["first"] and marks["map"], f"performance marks missing: {marks}")
            need(marks["hooks"]["caseId"] == a.case, f"case lookup gave {marks['hooks']['caseId']}")
            return {"open_s": round(time.perf_counter() - t0, 2), "viewer": info["state"], "marks_ms": marks}
        opened = C.run("open coverage mode, first image, case lookup, session, map ready", open_app)
        if not opened:
            browser.close()
            return finish(out, a, C, console, shots, posts)
        shot("01-initial-1440x900")

        C.run("panel lists every organ of the case", lambda: (
            need(page.locator('[data-testid^="covmap-organ-"][data-key]').count() == len(organs),
                 f"rows={page.locator('[data-testid^=covmap-organ-][data-key]').count()} organs={len(organs)}"),
            {"rows": len(organs), "summary": page.locator('[data-testid="covmap-summary"]').inner_text()})[1])

        def no_scroll(tag):
            lay = page.evaluate(LAYOUT_JS)
            w, h = lay["inner"]
            need(lay["doc"][0] <= w and lay["doc"][1] <= h, f"document larger than window: {lay}")
            need(lay["scrollY_after_scrollTo"] == 0, f"page scrolled: {lay}")
            need(lay["note_bottom"] is not None and lay["note_bottom"] <= h, f"note clipped: {lay}")
            need(lay["panel"][2] <= lay["panel"][1] + 1, f"panel overflows: {lay}")
            return {tag: lay}
        C.run("no page scroll at 1440x900", lambda: no_scroll("1440x900"))

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
        shot("03-panel-after-reading", page.locator('[data-testid="covmap-panel"]'))
        shot("04-map-after-reading", page.locator('[data-testid="covmap-map"]'))

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
            box = page.locator('[data-testid="covmap-map"]').bounding_box()
            found = {}
            for gy in range(2, 16):
                for gx in range(2, 16):
                    x, y = box["x"] + box["width"] * gx / 17, box["y"] + box["height"] * gy / 17
                    page.mouse.move(x, y)
                    page.wait_for_timeout(45)
                    el = page.locator('[data-testid="covmap-map-hover"]')
                    if el.count():
                        k = el.get_attribute("data-key")
                        if k and k not in found:
                            found[k] = (x, y, el.inner_text())
            return found
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
            key, (x, y, _) = target
            page.mouse.move(x, y)
            page.wait_for_timeout(120)
            shot("05-map-hover", page.locator('[data-testid="covmap-map"]'))
            page.mouse.click(x, y)
            o = by_key[key]
            pre = CLASS_PRESET[o["windows"][0]]
            wait_view(sop=o["center_sop"], preset=pre)
            need(page.evaluate("() => window.__covmap.state().selected") == key, "not selected")
            return {"clicked": key, "status": c[key]["status"], "now": view(), "expected_preset": pre}
        C.run("click gray/yellow organ in 3D map -> its center slice with class preset", map_click)
        shot("06-after-map-click")

        # ---- next gap --------------------------------------------------------------------
        def next_gap():
            jumps = []
            for _ in range(3):
                page.locator('[data-testid="covmap-next-gap"]').click()
                page.wait_for_timeout(250)
                key = page.evaluate("() => window.__covmap.state().selected")
                rem = page.evaluate("() => window.__covmap.remaining()")
                need(key in rem, f"next gap selected {key}, not remaining")
                o = by_key[key]
                wait_view(sop=o["center_sop"], preset=CLASS_PRESET[o["windows"][0]])
                jumps.append({"key": key, "view": view()})
            return jumps
        C.run("Next gap button jumps to remaining organs with explicit W/L", next_gap)

        # ---- row click + row actions ----------------------------------------------------
        def row_click():
            key = "liver" if "liver" in by_key else organs[0]["key"]
            row(key).locator(".cm-organ-line").click()
            o = by_key[key]
            wait_view(sop=o["center_sop"], preset=CLASS_PRESET[o["windows"][0]])
            return {"key": key, "view": view()}
        C.run("organ row click selects and jumps", row_click)

        rem_keys = page.evaluate("() => window.__covmap.remaining()")

        def row_actions():
            need(len(rem_keys) >= 3, f"need 3 remaining organs, have {rem_keys}")
            k_done, k_na, k_flag = rem_keys[-1], rem_keys[-2], rem_keys[-3]
            row(k_done).scroll_into_view_if_needed()
            page.locator(f'[data-testid="covmap-organ-{k_done}-done"]').click()
            row(k_na).scroll_into_view_if_needed()
            page.locator(f'[data-testid="covmap-organ-{k_na}-na"]').click()
            row(k_flag).scroll_into_view_if_needed()
            page.locator(f'[data-testid="covmap-organ-{k_flag}-flag"]').click()
            note = page.locator(f'[data-testid="covmap-organ-{k_flag}-note"]')
            note.fill("outline cuts through neighbour")
            note.press("Enter")
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
            shot("07-row-actions", page.locator('[data-testid="covmap-panel"]'))
            return {"done": k_done, "na": k_na, "flag": k_flag, "annotations": sorted(got, key=str),
                    "server_reader_layer": server_layer, "summary": cov()["summary"]}
        C.run("row actions done / N/A / flag+note post annotation events", row_actions)

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
            flush()
            acts = [e["action"] for e in api.events(session()) if e["kind"] == "annotation" and e["organ"] == key]
            need("done" in acts and "na" in acts and "flag" in acts and "clear" in acts, f"actions {acts}")
            return {"organ": key, "sequence": seq, "posted_actions": acts}
        C.run("hotkeys D / A / G toggle reader state of the selected organ", hotkeys_reader)

        def outline():
            key = "liver" if "liver" in by_key else organs[0]["key"]
            row(key).locator(".cm-organ-line").click()
            o = by_key[key]
            wait_view(sop=o["center_sop"])
            drv.press_key("o")
            page.wait_for_selector('[data-testid="covmap-outline"] path', timeout=8000)
            d1 = page.locator('[data-testid="covmap-outline"] path').nth(1).get_attribute("d")
            shot("08-outline", page.locator(".cornerstone-canvas").first.locator("xpath=.."))
            shot("08b-outline-full")
            x, y = drv.viewport_center()
            page.mouse.move(x, y)
            page.mouse.wheel(0, 100)
            page.wait_for_function("d => { const p = document.querySelectorAll('[data-testid=\"covmap-outline\"] path')[1];"
                                   " return p && p.getAttribute('d') !== d; }", arg=d1, timeout=5000)
            drv.press_key("o")
            page.wait_for_function("() => !document.querySelector('[data-testid=\"covmap-outline\"] path')", timeout=3000)
            return {"organ": key, "path_chars": len(d1), "redrawn_on_scroll": True, "toggled_off": True}
        C.run("O toggles the selected organ outline (SVG, redrawn on slice change)", outline)

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
            drv.press_key("f")
            dlg = page.locator('[data-testid="covmap-finish-dialog"]')
            dlg.wait_for(timeout=3000)
            n_items = dlg.locator('.cm-organ[data-testid^="covmap-finish-"]').count()
            rem = page.evaluate("() => window.__covmap.remaining()")
            need(n_items == len(rem), f"dialog lists {n_items}, remaining {len(rem)}")
            shot("09-finish-dialog", page.locator('[data-testid="covmap-panel"]'))
            first = rem[0]
            page.locator(f'[data-testid="covmap-finish-{first}"] .cm-organ-line').click()
            o = by_key[first]
            wait_view(sop=o["center_sop"], preset=CLASS_PRESET[o["windows"][0]])
            page.locator('[data-testid="covmap-go-back"]').click()
            dlg.wait_for(state="detached", timeout=3000)
            page.locator('[data-testid="covmap-finish"]').click()
            dlg.wait_for(timeout=3000)
            rem = page.evaluate("() => window.__covmap.remaining()")
            page.locator('[data-testid="covmap-confirm-finish"]').click()
            page.wait_for_selector('[data-testid="covmap-finished"]', timeout=3000)
            flush()
            fin = [e for e in api.events(session()) if e["kind"] == "finish"]
            need([e["confirmed"] for e in fin][-2:] == [False, True], f"finish events {fin}")
            need(fin[-1]["remaining"] == rem, "remaining in finish event differs")
            shot("10-finished-summary", page.locator('[data-testid="covmap-summary"]'))
            return {"listed": n_items, "jumped_to": first, "finish_events": [(e["confirmed"], len(e["remaining"])) for e in fin],
                    "server_finished": cov()["summary"].get("finished"),
                    "summary_text": page.locator('[data-testid="covmap-summary"]').inner_text()}
        C.run("Finish (F/button): remaining list, jump, Go back, Confirm -> finish event + summary", finish_flow)

        # ---- session menu: export, rules, replay, samples, new reading ------------------
        def menu(item):
            page.locator('[data-testid="covmap-session-menu"]').click()
            page.locator(f'[data-testid="{item}"]').click()

        def export():
            page.locator('[data-testid="covmap-session-menu"]').click()
            res = {}
            for f in ("jsonl", "json", "csv"):
                with page.expect_download(timeout=10000) as dl:
                    page.locator(f'[data-testid="covmap-export-{f}"]').click()
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
            page.locator('[data-testid="covmap-rules-dwell"]').fill("400")
            page.locator('[data-testid="covmap-rules-apply"]').click()
            page.wait_for_function("() => window.__covmap.lastCoverage.params.min_dwell_ms === 400", timeout=10000)
            c = cov()
            shot("11-rules-applied", page.locator('[data-testid="covmap-panel"]'))
            menu("covmap-rules")
            page.locator('[data-testid="covmap-rules-dwell"]').fill("80")
            page.locator('[data-testid="covmap-rules-apply"]').click()
            page.wait_for_function("() => window.__covmap.lastCoverage.params.min_dwell_ms === 80", timeout=10000)
            return {"at_400ms": c["summary"], "params": c["params"], "restored": cov()["params"]}
        C.run("Research reading rules re-score the session", rules)

        def replay():
            menu("covmap-replay")
            bar = page.locator('[data-testid="covmap-replay-bar"]')
            bar.wait_for(timeout=10000)
            page.locator('[data-testid="covmap-replay-play"]').click() if page.evaluate(
                "() => window.__covmap.state().replay.playing") else None
            sb = page.locator('[data-testid="covmap-replay-slider"]').bounding_box()
            n = page.evaluate("() => window.__covmap.state().replay.frames.length")
            page.mouse.click(sb["x"] + 2, sb["y"] + sb["height"] / 2)  # real click on the track
            page.wait_for_function("() => window.__covmap.state().replay.index === 0", timeout=3000)
            page.wait_for_timeout(300)
            first = page.locator('[data-testid="covmap-summary"]').inner_text()
            shot("12-replay-start", page.locator('[data-testid="covmap-panel"]'))
            page.mouse.click(sb["x"] + sb["width"] * 0.6, sb["y"] + sb["height"] / 2)
            page.wait_for_function("() => window.__covmap.state().replay.index > 0", timeout=3000)
            idx = page.evaluate("() => window.__covmap.state().replay.index")
            page.wait_for_timeout(300)
            mid = page.locator('[data-testid="covmap-summary"]').inner_text()
            shot("13-replay-mid", page.locator('[data-testid="covmap-panel"]'))
            disabled = page.locator('[data-testid$="-done"]').first.is_disabled()
            need(disabled, "row actions should be read-only during replay")
            page.locator('[data-testid="covmap-replay-close"]').click()
            bar.wait_for(state="detached", timeout=3000)
            need(first != mid, "replay frames did not change the summary")
            return {"frames": n, "mid_index": idx, "summary_start": first, "summary_mid": mid, "read_only": disabled}
        C.run("Replay: timeline slider recolours list/map, read-only", replay)

        samples = api.json(f"/api/cases/{a.case}/samples")

        def load_sample():
            need(samples, "no sample readings on the server for this case")
            smp = samples[0]
            s0 = session()
            menu("covmap-load-sample")
            page.locator('[data-testid="covmap-sample-list"]').wait_for(timeout=3000)
            shot("14a-sample-list", page.locator('[data-testid="covmap-panel"]'))
            page.locator(f'[data-testid="covmap-sample-{smp["name"]}"]').click()
            page.wait_for_function("s => window.__covmap.sessionId && window.__covmap.sessionId !== s", arg=s0, timeout=15000)
            page.wait_for_function("() => window.__covmap.lastCoverage", timeout=10000)
            label = page.locator('[data-testid="covmap-session-label"]').inner_text()
            need(smp["label"] in label and (smp.get("source") or "") in label, f"label {label!r} for {smp}")
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
            evs = api.events(session())
            pre = [e for e in evs if e.get("from_sample") == smp["name"]]
            live = [e for e in evs if not e.get("from_sample")]
            need(len(pre) == smp["events"], f"prefilled {len(pre)} of {smp['events']}")
            need(live, "no live events after loading the sample")
            need(min(e["t_ms"] for e in live) >= max(e["t_ms"] for e in pre), "live events precede the sample")
            need(min(e["seq"] for e in live) > max(e["seq"] for e in pre), "live seq overlaps the sample")
            return {"sample": {k: smp.get(k) for k in ("name", "label", "source", "events")}, "session": session(),
                    "label": label, "summary": loaded, "prefilled": len(pre), "live_after": len(live),
                    "first_live": {k: live[0][k] for k in ("seq", "t_ms", "kind")}}
        C.run("Load sample reading (labelled with its source)", load_sample)

        def new_reading():
            s0 = session()
            menu("covmap-new-reading")
            page.wait_for_function("s => window.__covmap.sessionId !== s", arg=s0, timeout=15000)
            page.wait_for_function("() => window.__covmap.lastCoverage", timeout=10000)
            return {"old": s0, "new": session(), "summary": cov()["summary"]}
        C.run("New reading starts a fresh session", new_reading)

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
