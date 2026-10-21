"""Page-load timing for the Coverage Map build and the stock OHIF build on the same data.

Each run uses a fresh browser (cold HTTP cache). Measured from navigation start:
  first_image_ms : the active viewport has a displayed image (same probe for both builds)
  map_ready_ms   : coverage build only, the vtk.js organ map has drawn (performance mark)
  session_ms     : coverage build only, the reading session exists (events can be scored)
Usage: python bench/bench_pageload.py --case lidc-0001 --runs 5 --out <json>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.sync_api import sync_playwright  # noqa: E402

from bench.common import host_context, stats, write  # noqa: E402
from covmap import paths  # noqa: E402
from e2e.ohif_driver import CHROMIUM_ARGS  # noqa: E402

FIRST_IMAGE = """() => {
  const s = window.services && window.services.cornerstoneViewportService;
  const g = window.services && window.services.viewportGridService;
  if (!s || !g) return false;
  const v = s.getCornerstoneViewport(g.getActiveViewportId());
  return !!(v && v.getCurrentImageId && v.getCurrentImageId() && v.getImageData && v.getImageData());
}"""


def one(p, url: str, coverage: bool, timeout_ms: float = 120000) -> dict:
    browser = p.chromium.launch(headless=True, args=CHROMIUM_ARGS)
    ctx = browser.new_context(viewport={"width": 1600, "height": 1000})
    page = ctx.new_page()
    page.goto(url, wait_until="commit")
    page.wait_for_function(FIRST_IMAGE, polling=10, timeout=timeout_ms)
    r = {"first_image_ms": page.evaluate("() => performance.now()")}
    if coverage:
        page.wait_for_function("() => window.__covmap && window.__covmap.mapReadyAt", polling=10, timeout=timeout_ms)
        page.wait_for_function("() => window.__covmap && window.__covmap.sessionId", polling=10, timeout=timeout_ms)
        r["map_ready_ms"] = page.evaluate("() => window.__covmap.mapReadyAt")
        r["session_ms"] = page.evaluate("() => performance.now()")
        marks = page.evaluate("() => performance.getEntriesByType('mark').filter(m => m.name.startsWith('covmap:')).map(m => [m.name, m.startTime])")
        r["marks"] = dict(marks)
    nav = page.evaluate("() => { const n = performance.getEntriesByType('navigation')[0]; return n ? {dcl: n.domContentLoadedEventEnd, load: n.loadEventEnd, transfer_bytes: n.transferSize} : null; }")
    r["navigation"] = nav
    r["resources"] = page.evaluate("() => performance.getEntriesByType('resource').length")
    ctx.close()
    browser.close()
    return r


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", required=True)
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--coverage-base", default="http://127.0.0.1:8765")
    ap.add_argument("--stock-base", default="http://127.0.0.1:8766")
    ap.add_argument("--skip-stock", action="store_true")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    vol = json.loads((paths.case_dir(a.case) / "volume.json").read_text())
    q = f"StudyInstanceUIDs={vol['study_uid']}"
    res = {"case": a.case, "context": host_context(), "chromium_args": CHROMIUM_ARGS}
    with sync_playwright() as p:
        one(p, f"{a.coverage_base}/coverage?{q}", True)  # warm Orthanc/OS file cache, discarded
        cov = [one(p, f"{a.coverage_base}/coverage?{q}", True) for _ in range(a.runs)]
        res["coverage"] = {k: stats([r[k] for r in cov]) for k in ("first_image_ms", "map_ready_ms", "session_ms")}
        res["coverage"]["raw"] = cov
        if not a.skip_stock:
            one(p, f"{a.stock_base}/viewer?{q}", False)
            st = [one(p, f"{a.stock_base}/viewer?{q}", False) for _ in range(a.runs)]
            res["stock"] = {"first_image_ms": stats([r["first_image_ms"] for r in st]), "raw": st}
    res["context_end"] = host_context()
    write(Path(a.out), res)
    summary = {"ok": True, "case": a.case, "out": a.out,
               "coverage_first_image_ms": res["coverage"]["first_image_ms"]["median"],
               "coverage_map_ready_ms": res["coverage"]["map_ready_ms"]["median"],
               "stock_first_image_ms": res.get("stock", {}).get("first_image_ms", {}).get("median")}
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
