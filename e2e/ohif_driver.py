"""Drive a real OHIF viewer (stock or coverage mode) in Chromium with user-like input.

Scrolling uses real mouse-wheel events over the viewport (one wheel event = one slice
in OHIF's StackScroll binding); window presets and region framing go through OHIF's own
command and Cornerstone camera APIs (labeled browser-scripted). Nothing here writes
reading events itself: the coverage extension records whatever the viewer displayed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

CHROMIUM_ARGS = ["--use-angle=metal", "--enable-gpu"]
NEUTRAL = (40.0, 80.0)  # Brain W80/L40: window class "other", qualifies no organ

HELPERS = r"""
(() => {
  if (window.__e2e) return;
  const grid = () => window.services.viewportGridService;
  const vp = () => {
    const s = window.services && window.services.cornerstoneViewportService;
    if (!s) return null;
    return s.getCornerstoneViewport(grid().getActiveViewportId());
  };
  const sopOf = id => {
    const m = /instances\/([^/]+)\/frames/.exec(id || '');
    return m ? m[1] : null;
  };
  window.__e2e = {
    ready() {
      const v = vp();
      return !!(v && v.getImageIds && v.getImageIds().length && v.getCurrentImageId && v.getCurrentImageId());
    },
    state() {
      const v = vp();
      if (!v) return null;
      const ids = v.getImageIds();
      const p = v.getProperties ? v.getProperties() : {};
      const r = p.voiRange;
      const cam = v.getCamera();
      return {
        viewportId: v.id, type: v.type, n: ids.length,
        idx: v.getCurrentImageIdIndex(), sop: sopOf(v.getCurrentImageId()),
        // inverse of Cornerstone's toLowHighRange (lower = wc - 0.5 - (ww - 1) / 2)
        voi: r ? {wc: (r.upper + r.lower + 1) / 2, ww: r.upper - r.lower + 1} : null,
        canvas: [v.canvas.clientWidth, v.canvas.clientHeight],
        parallelScale: cam.parallelScale, focalPoint: cam.focalPoint,
      };
    },
    sops() { return vp().getImageIds().map(sopOf); },
    async setIndex(k) { await vp().setImageIdIndex(k); },
    setWindow(wc, ww) {
      const v = vp();
      window.commandsManager.runCommand('setViewportWindowLevel', {
        viewportId: v.id, windowWidth: ww, windowCenter: wc,
      });
    },
    // Frame an image-pixel rectangle [r0, r1) x [c0, c1) of the current slice.
    frameRect(r0, r1, c0, c1) {
      const v = vp();
      const img = v.getImageData();
      const [colMm, rowMm] = img.spacing;
      const mid = img.imageData.indexToWorld([(c0 + c1 - 1) / 2, (r0 + r1 - 1) / 2, 0]);
      const cam = v.getCamera();
      const [w, h] = [v.canvas.clientWidth, v.canvas.clientHeight];
      const halfH = Math.max((r1 - r0) * rowMm, (c1 - c0) * colMm * h / w) / 2;
      const off = [0, 1, 2].map(i => cam.position[i] - cam.focalPoint[i]);
      // keep the focal point on the current slice plane: move only in-plane
      const n = cam.viewPlaneNormal;
      const d = [0, 1, 2].map(i => mid[i] - cam.focalPoint[i]);
      const along = d[0] * n[0] + d[1] * n[1] + d[2] * n[2];
      const fp = [0, 1, 2].map(i => mid[i] - along * n[i]);
      v.setCamera({focalPoint: fp, position: [0, 1, 2].map(i => fp[i] + off[i]), parallelScale: halfH});
      v.render();
    },
    resetCamera() { const v = vp(); v.resetCamera(); v.render(); },
    stockViewed() {
      const v = vp();
      const svc = window.services.viewedDataService;
      return v.getImageIds().map(id => !!(svc && svc.isDataViewed(id)));
    },
  };
})();
"""


@dataclass
class StepLog:
    wheel_ticks: int = 0
    wheel_misses: int = 0
    api_jumps: int = 0
    frames: int = 0
    dwell_ms: list = field(default_factory=list)


class OhifDriver:
    def __init__(self, page, settle_ms: float = 15.0):
        self.page = page
        self.settle_ms = settle_ms
        self.log = StepLog()

    # ---- setup -------------------------------------------------------------------
    def open(self, url: str, timeout_s: float = 90.0) -> dict:
        t0 = time.perf_counter()
        self.page.goto(url, wait_until="domcontentloaded")
        self.page.wait_for_function("() => !!(window.services && window.commandsManager)", timeout=timeout_s * 1000)
        self.page.evaluate(HELPERS)
        self.page.wait_for_function("() => window.__e2e.ready()", timeout=timeout_s * 1000)
        # the image's own VOI lands after the first render and would overwrite an early setWindow
        self.page.wait_for_function("() => !!window.__e2e.state().voi", timeout=timeout_s * 1000)
        opened = round(time.perf_counter() - t0, 3)
        tour = self.dismiss_tour() if "/viewer" in url else False  # the tour is bound to /viewer
        return {"open_s": opened, "tour_dismissed": tour, "state": self.state()}

    def dismiss_tour(self, wait_ms: float = 2500) -> bool:
        """Stock /viewer starts OHIF's first-visit onboarding tour (Shepherd) in a fresh
        profile; its overlay swallows wheel input. Close it the way a user would."""
        tour = self.page.locator(".shepherd-element")
        try:
            tour.first.wait_for(state="visible", timeout=wait_ms)
        except Exception:  # noqa: BLE001 - no tour on this route/profile
            return False
        self.page.keyboard.press("Escape")
        try:
            tour.first.wait_for(state="hidden", timeout=2000)
        except Exception:  # noqa: BLE001
            self.page.get_by_role("button", name="Skip all").click()
            tour.first.wait_for(state="hidden", timeout=2000)
        return True

    def state(self) -> dict:
        return self.page.evaluate("() => window.__e2e.state()")

    def sops(self) -> list[str]:
        return self.page.evaluate("() => window.__e2e.sops()")

    def viewport_center(self) -> tuple[float, float]:
        box = self.page.locator(".cornerstone-canvas").first.bounding_box()
        return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2

    # ---- actions -----------------------------------------------------------------
    def goto(self, k: int) -> None:
        """Move to viewer index k: a real wheel event for adjacent slices; a direct jump
        otherwise, so slices the plan skips are never displayed in transit."""
        cur = self.state()["idx"]
        if cur == k:
            return
        if abs(k - cur) == 1:
            x, y = self.viewport_center()
            self.page.mouse.move(x, y)
            self.page.mouse.wheel(0, 100 * (k - cur))
            self.log.wheel_ticks += 1
            if self._wait_index(k):
                return
            self.log.wheel_misses += 1
        self.log.api_jumps += 1
        self.page.evaluate("k => window.__e2e.setIndex(k)", k)
        if not self._wait_index(k, 10000):
            raise RuntimeError(f"viewer did not reach index {k}: {self.state()}")

    def _wait_index(self, k: int, timeout_ms: float = 1500) -> bool:
        try:
            self.page.wait_for_function("k => window.__e2e.state().idx === k", arg=k, timeout=timeout_ms)
            return True
        except Exception:  # noqa: BLE001 - playwright TimeoutError
            return False

    def set_window(self, wc: float, ww: float) -> None:
        check = ("([wc, ww]) => { const v = window.__e2e.state().voi;"
                 " return v && Math.abs(v.wc - wc) < 1 && Math.abs(v.ww - ww) < 1; }")
        for attempt in (1, 2):
            self.page.evaluate("([wc, ww]) => window.__e2e.setWindow(wc, ww)", [wc, ww])
            try:
                self.page.wait_for_function(check, arg=[wc, ww], timeout=2500 * attempt)
                return
            except Exception:  # noqa: BLE001 - playwright TimeoutError; retry once
                if attempt == 2:
                    raise RuntimeError(f"window {wc}/{ww} not applied: {self.state()}") from None

    def press_key(self, key: str) -> None:
        x, y = self.viewport_center()
        self.page.mouse.move(x, y)
        self.page.keyboard.press(key)

    def frame_rect(self, rect) -> None:
        self.page.evaluate("r => window.__e2e.frameRect(r[0], r[1], r[2], r[3])", list(rect))
        self.log.frames += 1

    def reset_camera(self) -> None:
        self.page.evaluate("() => window.__e2e.resetCamera()")

    def dwell(self, ms: float) -> None:
        t0 = time.perf_counter()
        self.page.wait_for_timeout(ms)
        self.log.dwell_ms.append((time.perf_counter() - t0) * 1000)

    def stock_viewed(self) -> list[bool]:
        return self.page.evaluate("() => window.__e2e.stockViewed()")

    # ---- plan execution --------------------------------------------------------------
    def run_plan(self, plan: dict, sop_to_index: dict[str, int], case_sops: list[str], full_rect) -> dict:
        """Execute a covmap scenario plan (covmap.simulate.plan_scenario) in the viewer.

        Plan slice indices are case indices (volume.json order); they are mapped to the
        viewer's own image order through SOPInstanceUIDs, never assumed equal. Between
        passes the viewer sits in a neutral window (Brain 80/40, which qualifies no body
        organ) so transit frames add no qualifying dwell.
        """
        t0 = time.perf_counter()
        passes = []
        for p in plan["passes"]:
            tp = time.perf_counter()
            self.set_window(*NEUTRAL)
            self.reset_camera()
            steps = p["steps"]
            if not steps:
                continue
            self.goto(sop_to_index[case_sops[steps[0]["k"]]])
            self.set_window(p["voi"]["wc"], p["voi"]["ww"])
            for step in steps:
                self.goto(sop_to_index[case_sops[step["k"]]])
                for rect in step["rects"]:
                    if list(rect) == list(full_rect):
                        self.reset_camera()
                    else:
                        self.frame_rect(rect)
                    self.dwell(p["dwell_ms"])
            passes.append({"window": p["window"], "steps": len(steps), "s": round(time.perf_counter() - tp, 2)})
        self.set_window(*NEUTRAL)
        self.reset_camera()
        d = self.log.dwell_ms
        return {"run_s": round(time.perf_counter() - t0, 2), "passes": passes,
                "wheel_ticks": self.log.wheel_ticks, "wheel_misses": self.log.wheel_misses,
                "api_jumps": self.log.api_jumps, "frames": self.log.frames,
                "dwell_ms_requested_vs_observed": [p["dwell_ms"] for p in plan["passes"]],
                "observed_dwell_ms_median": sorted(d)[len(d) // 2] if d else None}
