"""Report which WebGL2 renderer headless Chromium gets on this host."""

import json
import sys

from playwright.sync_api import sync_playwright

ARGS = {
    "default": [],
    "angle-metal": ["--use-angle=metal", "--enable-gpu"],
    "swiftshader": ["--use-angle=swiftshader", "--enable-unsafe-swiftshader"],
}

PROBE = """() => {
  const c = document.createElement('canvas');
  const gl = c.getContext('webgl2');
  if (!gl) return {webgl2: false};
  const d = gl.getExtension('WEBGL_debug_renderer_info');
  return {webgl2: true, renderer: d ? gl.getParameter(d.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER),
          maxTexture: gl.getParameter(gl.MAX_TEXTURE_SIZE), max3d: gl.getParameter(gl.MAX_3D_TEXTURE_SIZE)};
}"""

out = {}
with sync_playwright() as p:
    for name, args in ARGS.items():
        try:
            b = p.chromium.launch(headless=True, args=args)
            page = b.new_page()
            page.set_content("<html><body></body></html>")
            out[name] = page.evaluate(PROBE)
            out[name]["version"] = b.version
            b.close()
        except Exception as e:  # report every mode, even failing ones
            out[name] = {"error": str(e)[:300]}
json.dump(out, sys.stdout, indent=1)
print()
