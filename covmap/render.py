"""3D organ-map PNG through the app's own vtk.js module in headless Chromium.

Opens ``/render.html?case=<id>`` (the standalone page bundled next to the OHIF build) on a
covmap server, applies per-organ statuses (default: all skipped, gray), waits for the page's
ready flag and screenshots the map canvas. Research use only.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


# Real GPU WebGL2 in headless Chromium on Apple silicon (default is SwiftShader).
CHROMIUM_ARGS = ["--use-angle=metal", "--enable-gpu"]
BACKGROUND_RGB = (7, 10, 20)  # organ-map default background [0.027, 0.039, 0.078]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _healthy(base: str) -> bool:
    try:
        with urllib.request.urlopen(f"{base}/api/health", timeout=2) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError):
        return False


@contextlib.contextmanager
def covmap_server(timeout_s: float = 60.0):
    """Start ``python -m covmap serve`` on an ephemeral port; yield its base URL."""
    port = _free_port()
    log = tempfile.NamedTemporaryFile(prefix="covmap-serve-", suffix=".log", delete=False)
    proc = subprocess.Popen([sys.executable, "-m", "covmap", "serve", "--port", str(port)],
                            stdout=log, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + timeout_s
        while not _healthy(base):
            if proc.poll() is not None or time.monotonic() > deadline:
                log.flush()
                tail = Path(log.name).read_text(errors="replace")[-1500:]
                raise RuntimeError(f"covmap serve did not become healthy (rc={proc.poll()}): {tail}")
            time.sleep(0.1)
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
        Path(log.name).unlink(missing_ok=True)


def _load_coverage(coverage) -> dict | None:
    if coverage is None:
        return None
    if isinstance(coverage, dict):
        return coverage
    return json.loads(Path(coverage).read_text())


def _nonblank(png: Path) -> dict:
    import numpy as np
    from PIL import Image

    img = np.asarray(Image.open(png).convert("RGB")).astype(np.int16)
    diff = np.abs(img - np.array(BACKGROUND_RGB, np.int16)).max(axis=2)
    fg = diff > 12
    colors = np.unique(img[fg].reshape(-1, 3), axis=0).shape[0] if fg.any() else 0
    return {"foreground_fraction": round(float(fg.mean()), 4), "foreground_colors": int(colors),
            "size": [int(img.shape[1]), int(img.shape[0])]}


def render(case_id: str, coverage=None, out="organ-map.png", width: int = 800, height: int = 800,
           server: str | None = None) -> dict:
    """Render the case's organ map to ``out`` (PNG). ``coverage`` is a coverage JSON (path or
    dict, e.g. from ``covmap replay``); without it every organ is drawn as skipped."""
    from playwright.sync_api import sync_playwright

    t0 = time.perf_counter()
    timings: dict[str, float] = {}
    cov = _load_coverage(coverage)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    init: dict = {"case": case_id, "width": width, "height": height}
    if cov is not None:
        init["coverage"] = {"organs": [{"key": o["key"], "status": o.get("status"), "reader": o.get("reader"),
                                        "flagged": bool(o.get("flagged"))} for o in cov.get("organs", [])]}

    with contextlib.ExitStack() as stack:
        if server is None:
            t = time.perf_counter()
            server = stack.enter_context(covmap_server())
            timings["server_start"] = (time.perf_counter() - t) * 1000
        server = server.rstrip("/")
        with sync_playwright() as p:
            t = time.perf_counter()
            browser = p.chromium.launch(headless=True, args=CHROMIUM_ARGS)
            timings["browser_start"] = (time.perf_counter() - t) * 1000
            try:
                page = browser.new_page(viewport={"width": width, "height": height}, device_scale_factor=1)
                console_errors: list[str] = []
                page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
                page.on("pageerror", lambda e: console_errors.append(f"pageerror: {e}"))
                page.add_init_script(f"window.__COVMAP_RENDER_INIT = {json.dumps(init)};")
                t = time.perf_counter()
                page.goto(f"{server}/render.html?case={case_id}", wait_until="load")
                timings["page_load"] = (time.perf_counter() - t) * 1000
                try:
                    page.wait_for_function(
                        "() => window.__render && (window.__render.ready || window.__render.error)",
                        timeout=30_000)
                except Exception as e:
                    state = page.evaluate("() => window.__render ?? null")
                    raise RuntimeError(f"render page not ready: {e}; state={state}; "
                                       f"console={console_errors[-5:]}") from None
                res = page.evaluate("() => window.__render")
                if res.get("error"):
                    raise RuntimeError(f"render page error: {res['error']}; console={console_errors[-5:]}")
                inner = res.get("timings", {})
                timings["meshes"] = float(inner.get("fetch", 0)) + float(inner.get("build", 0))
                timings["render"] = float(inner.get("render", 0))
                t = time.perf_counter()
                page.locator("#map canvas").screenshot(path=str(out))
                timings["screenshot"] = (time.perf_counter() - t) * 1000
            finally:
                browser.close()

    check = _nonblank(out)
    if check["foreground_fraction"] < 0.01 or check["foreground_colors"] < 8:
        raise RuntimeError(f"rendered PNG looks blank: {check}")
    if res.get("organsDrawn", 0) == 0:
        raise RuntimeError("no organ meshes were drawn")
    timings["total"] = (time.perf_counter() - t0) * 1000
    return {
        "case": case_id,
        "out": str(out),
        "sha256": hashlib.sha256(out.read_bytes()).hexdigest(),
        "width": check["size"][0],
        "height": check["size"][1],
        "organs_drawn": res["organsDrawn"],
        "organs_listed": res.get("organsListed"),
        "triangles": res.get("triangles"),
        "status_counts": res.get("statusCounts"),
        "coverage_applied": cov is not None,
        "foreground_fraction": check["foreground_fraction"],
        "webgl_renderer": res.get("renderer"),
        "console_errors": console_errors,
        "timings_ms": {k: round(v, 1) for k, v in timings.items()},
    }
