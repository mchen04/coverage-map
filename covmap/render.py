"""3D organ-map PNG through the app's own vtk.js module in headless Chromium.

Opens ``/render.html?case=<id>`` (the standalone page bundled next to the OHIF build),
applies per-organ statuses (default: all skipped, gray), waits for the page's ready flag and
screenshots the map canvas. The page and the case files it reads are served from disk by a
small in-process server (or by a running covmap server, ``server=``). Research use only.
"""

from __future__ import annotations

import contextlib
import hashlib
import http.server
import json
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from covmap import paths


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


CASE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript", ".json": "application/json",
                 ".bin": "application/octet-stream"}


@contextlib.contextmanager
def page_server(case_id: str):
    """Serve exactly what ``/render.html`` reads, from disk, on an ephemeral 127.0.0.1 port in
    this process: the page and its bundle from the OHIF dist and the case's organs and meshes
    (same URLs and files as ``covmap serve``, without spawning it). Yields the base URL."""
    if not CASE_RE.match(case_id):
        raise ValueError(f"bad case id {case_id!r}")
    dist = paths.ohif_dist().resolve()
    seg = paths.case_dir(case_id) / "seg"
    files = {"/render.html": dist / "render.html",
             f"/api/cases/{case_id}/organs": seg / "organs.json",
             f"/api/cases/{case_id}/meshes.json": seg / "meshes.json",
             f"/api/cases/{case_id}/meshes.bin": seg / "meshes.bin"}
    if not files["/render.html"].is_file():
        raise RuntimeError(f"render page not built: {files['/render.html']} (run scripts/build_ohif.sh)")
    missing = [f.name for f in list(files.values())[1:] if not f.is_file()]
    if missing:
        raise RuntimeError(f"case {case_id!r} has no {', '.join(missing)} under {seg} (segment it first)")
    bundle = (dist / "render").resolve()

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            path = urllib.parse.urlsplit(self.path).path
            f = files.get(path)
            if f is None and path.startswith("/render/"):
                f = (bundle / path[len("/render/"):]).resolve()
                if bundle not in f.parents:
                    f = None
            if f is None or not f.is_file():
                self.send_error(404)
                return
            body = f.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPES.get(f.suffix, "application/octet-stream"))
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    httpd.block_on_close = False  # keep-alive handler threads end with the browser
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def _load_coverage(coverage) -> dict | None:
    if coverage is None:
        return None
    if isinstance(coverage, dict):
        return coverage
    return json.loads(Path(coverage).read_text())


def _nonblank(png: Path) -> dict:
    """Foreground = pixels whose largest channel difference from the background exceeds 12;
    returns its fraction and number of distinct colors. PIL only (no numpy import)."""
    from PIL import Image, ImageChops

    img = Image.open(png).convert("RGB")
    bg = Image.new("RGB", img.size, BACKGROUND_RGB)
    r, g, b = ImageChops.difference(img, bg).split()
    fg = ImageChops.lighter(ImageChops.lighter(r, g), b).point(lambda v: 255 if v > 12 else 0)
    total = img.size[0] * img.size[1]
    n_fg = fg.histogram()[255]
    # foreground pixels over the background color, which no foreground pixel can equal
    colors = len(Image.composite(img, bg, fg).getcolors(total)) - (n_fg < total) if n_fg else 0
    return {"foreground_fraction": round(n_fg / total, 4), "foreground_colors": colors, "size": list(img.size)}


def render(case_id: str, coverage=None, out="organ-map.png", width: int = 800, height: int = 800,
           server: str | None = None) -> dict:
    """Render the case's organ map to ``out`` (PNG). ``coverage`` is a coverage JSON (path or
    dict, e.g. from ``covmap replay``); without it every organ is drawn as skipped. ``server``
    is a running covmap server's base URL; without it the files are served in-process."""
    t0 = time.perf_counter()
    from playwright.sync_api import sync_playwright

    timings: dict[str, float] = {"import": (time.perf_counter() - t0) * 1000}
    cov = _load_coverage(coverage)
    if cov is not None:
        from covmap.casedata import check_case  # numpy: only when a coverage is drawn

        check_case(case_id, cov.get("case"), "coverage")
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    init: dict = {"case": case_id, "width": width, "height": height}
    if cov is not None:
        init["coverage"] = {"organs": [{"key": o["key"], "status": o.get("status"), "reader": o.get("reader"),
                                        "flagged": bool(o.get("flagged"))} for o in cov.get("organs", [])]}

    with contextlib.ExitStack() as stack:
        if server is None:
            t = time.perf_counter()
            server = stack.enter_context(page_server(case_id))
            timings["server_start"] = (time.perf_counter() - t) * 1000
        server = server.rstrip("/")
        t = time.perf_counter()
        with sync_playwright() as p:
            timings["playwright_start"] = (time.perf_counter() - t) * 1000
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
                t = time.perf_counter()
                try:
                    page.wait_for_function(
                        "() => window.__render && (window.__render.ready || window.__render.error)",
                        timeout=30_000)
                except Exception as e:
                    state = page.evaluate("() => window.__render ?? null")
                    raise RuntimeError(f"render page not ready: {e}; state={state}; "
                                       f"console={console_errors[-5:]}") from None
                res = page.evaluate("() => window.__render")
                timings["page_ready_wait"] = (time.perf_counter() - t) * 1000
                if res.get("error"):
                    raise RuntimeError(f"render page error: {res['error']}; console={console_errors[-5:]}")
                inner = res.get("timings", {})
                timings["meshes"] = float(inner.get("fetch", 0)) + float(inner.get("build", 0))
                timings["render"] = float(inner.get("render", 0))
                t = time.perf_counter()
                # a clipped page screenshot: same PNG bytes as an element screenshot, without its
                # scroll-into-view and stability waits (~45 ms)
                page.screenshot(path=str(out), clip=page.locator("#map canvas").bounding_box())
                timings["screenshot"] = (time.perf_counter() - t) * 1000
            finally:
                t = time.perf_counter()
                browser.close()
                timings["browser_close"] = (time.perf_counter() - t) * 1000
            t = time.perf_counter()
        timings["playwright_stop"] = (time.perf_counter() - t) * 1000
        t = time.perf_counter()
    timings["server_stop"] = (time.perf_counter() - t) * 1000

    t = time.perf_counter()
    check = _nonblank(out)
    if check["foreground_fraction"] < 0.01 or check["foreground_colors"] < 8:
        raise RuntimeError(f"rendered PNG looks blank: {check}")
    if res.get("organsDrawn", 0) == 0:
        raise RuntimeError("no organ meshes were drawn")
    timings["check"] = (time.perf_counter() - t) * 1000
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
