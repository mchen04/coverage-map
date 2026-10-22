"""Run one scripted reading through a real OHIF viewer and collect every tool's verdict.

    python e2e/run_reading.py --case lidc-0001 --scenario window_skip_lung \
        --base http://127.0.0.1:8765 --mode coverage --out $MINI_WORK/evidence/gauntlet/...

--mode coverage  : our build (/coverage route); the extension records real events.
--mode viewer    : the pristine stock OHIF build (/viewer route); we read its built-in
                   per-slice viewed tracker (cornerstone viewedDataService).
Prints one JSON object. Same plan (covmap.simulate.plan_scenario) in both modes.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.sync_api import sync_playwright  # noqa: E402

from covmap import paths  # noqa: E402
from e2e.ohif_driver import CHROMIUM_ARGS, NEUTRAL, OhifDriver  # noqa: E402


def _ranges(idx: list[int]) -> list[list[int]]:
    out: list[list[int]] = []
    for i in sorted(idx):
        if out and i == out[-1][1] + 1:
            out[-1][1] = i
        else:
            out.append([i, i])
    return out


def _get_json(url: str):
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.loads(r.read())


def run(case: str, scenario: str, base: str, mode: str, out: Path, headless: bool = True,
        plan: dict | None = None) -> dict:
    from covmap.simulate import plan_scenario

    vol = json.loads((paths.case_dir(case) / "volume.json").read_text())
    plan = plan or plan_scenario(case, scenario)
    case_sops = vol["sop_uids"]
    full_rect = [0, vol["rows"], 0, vol["cols"]]
    out.mkdir(parents=True, exist_ok=True)
    route = "coverage" if mode == "coverage" else "viewer"
    url = f"{base}/{route}?StudyInstanceUIDs={vol['study_uid']}"
    res: dict = {"case": case, "scenario": scenario, "mode": mode, "url": url,
                 "source": "browser-scripted", "truth": plan["truth"]}
    console_errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, args=CHROMIUM_ARGS)
        ctx = browser.new_context(viewport={"width": 1600, "height": 1000})
        page = ctx.new_page()
        page.on("console", lambda m: console_errors.append(m.text[:300]) if m.type == "error" else None)
        page.on("pageerror", lambda e: console_errors.append(f"pageerror: {e}"[:300]))
        drv = OhifDriver(page)
        res["open"] = drv.open(url)
        drv.set_window(*NEUTRAL)
        viewer_sops = drv.sops()
        if sorted(viewer_sops) != sorted(case_sops):
            raise RuntimeError("viewer image set differs from the case's SOP list")
        sop_to_index = {s: i for i, s in enumerate(viewer_sops)}
        res["viewer_order_equals_case_order"] = viewer_sops == case_sops
        if mode == "coverage":
            page.wait_for_function("() => window.__covmap && window.__covmap.sessionId", timeout=60000)
            # The coverage mode opens in a counting window; start the scored session only now
            # that the neutral window is set, so nothing before the plan's first step counts.
            if page.evaluate("() => typeof window.__covmap.newReading === 'function'"):
                res["pre_plan_session"] = page.evaluate("() => window.__covmap.sessionId")
                res["fresh_session"] = page.evaluate("() => window.__covmap.newReading()")
        res["run"] = drv.run_plan(plan, sop_to_index, case_sops, full_rect)
        page.screenshot(path=str(out / f"{case}.{scenario}.{mode}.png"))

        viewed = drv.stock_viewed()  # present in both builds: stock cornerstone extension
        unviewed_case_idx = sorted(case_sops.index(viewer_sops[i]) for i, v in enumerate(viewed) if not v)
        res["stock_viewed_tracker"] = {
            "source": "OHIF v3.13.13 cornerstone viewedDataService (built-in, default viewedDwellMs=0)",
            "images": len(viewed), "viewed": sum(viewed), "complete": all(viewed),
            "unviewed_ranges": _ranges(unviewed_case_idx)}

        if mode == "coverage":
            page.evaluate("() => window.__covmap.flush && window.__covmap.flush()")
            page.wait_for_timeout(800)
            sid = page.evaluate("() => window.__covmap.sessionId")
            res["session"] = sid
            res["coverage"] = _get_json(f"{base}/api/sessions/{sid}/coverage")
            with urllib.request.urlopen(f"{base}/api/sessions/{sid}/events.jsonl", timeout=60) as r:
                events = r.read()
            (out / f"{case}.{scenario}.events.jsonl").write_bytes(events)
            res["events_path"] = str(out / f"{case}.{scenario}.events.jsonl")
        res["console_errors"] = console_errors[:20]
        ctx.close()
        browser.close()
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", required=True)
    ap.add_argument("--scenario", required=True)
    ap.add_argument("--base", default="http://127.0.0.1:8765")
    ap.add_argument("--mode", choices=["coverage", "viewer"], default="coverage")
    ap.add_argument("--out", required=True)
    ap.add_argument("--headed", action="store_true")
    a = ap.parse_args()
    t0 = time.perf_counter()
    try:
        res = run(a.case, a.scenario, a.base, a.mode, Path(a.out), headless=not a.headed)
        res["ok"] = True
    except Exception as e:  # noqa: BLE001 - JSON on every exit path
        res = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    res["elapsed_s"] = round(time.perf_counter() - t0, 2)
    json.dump(res, sys.stdout, indent=1, default=str)
    print()
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
