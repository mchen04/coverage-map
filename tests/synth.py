"""Synthetic case directories (volume.json, seg/labels.npy, seg/organs.json per SPEC).

Organs are boxes, cylinders and ellipsoids placed in an anatomical frame (s: caudal ->
cranial, p: anterior -> posterior, l: patient right -> left) and mapped onto the voxel
grid through the chosen orientation, so scenario logic that reasons about cranial/caudal
and left/right is exercised against both the usual and a flipped (pancreas-0051-like)
DICOM geometry. Organ ids/keys follow covmap/organs.py.
"""

from __future__ import annotations

import json
import zlib
from pathlib import Path

import numpy as np

ORIENTATIONS = {
    # row_dir (increasing c), col_dir (increasing r), slice_dir (increasing k)
    "standard": ([1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]),
    "flipped": ([1.0, 0.0, 0.0], [0.0, -1.0, 0.0], [0.0, 0.0, -1.0]),
    "mirrored": ([-1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]),
}

# id, key, name, group, windows, shape spec in the 40 x 96 x 96 reference frame
ORGANS = [
    (1, "lung_left", "Left lung", "thorax", ["lung"], ("ellipsoid", (26, 44, 68), (12, 22, 15))),
    (2, "lung_right", "Right lung", "thorax", ["lung"], ("ellipsoid", (26, 44, 28), (12, 22, 15))),
    (4, "heart", "Heart", "thorax", ["soft"], ("ellipsoid", (19, 36, 54), (6, 11, 10))),
    (5, "aorta", "Aorta", "thorax", ["soft"], ("cylinder", (2, 35), (60, 54), 3)),
    (3, "trachea", "Trachea", "thorax", ["lung", "soft"], ("cylinder", (31, 40), (36, 48), 3)),
    (9, "liver", "Liver", "abdomen", ["soft"], ("ellipsoid", (9, 46, 30), (7, 20, 20))),
    (10, "spleen", "Spleen", "abdomen", ["soft"], ("ellipsoid", (11, 62, 78), (5, 9, 8))),
    (14, "kidney_left", "Left kidney", "abdomen", ["soft"], ("ellipsoid", (6, 70, 66), (5, 7, 5))),
    (15, "kidney_right", "Right kidney", "abdomen", ["soft"], ("ellipsoid", (5, 70, 30), (5, 7, 5))),
    (21, "thoracic_spine", "Thoracic spine", "skeleton", ["bone"], ("box", (14, 40), (74, 84), (44, 53))),
    (22, "lumbar_spine", "Lumbar spine", "skeleton", ["bone"], ("box", (0, 14), (74, 84), (44, 53))),
    (26, "sternum", "Sternum and costal cartilages", "skeleton", ["bone"], ("box", (16, 34), (12, 18), (45, 52))),
]
COLORS = {1: (138, 166, 204), 2: (110, 142, 186), 3: (120, 186, 196), 4: (186, 104, 104),
          5: (214, 84, 76), 9: (150, 86, 66), 10: (132, 76, 120), 14: (176, 96, 84),
          15: (160, 84, 100), 21: (222, 208, 172), 22: (210, 196, 158), 26: (214, 214, 196)}


def _mask(spec, S, P, L, scale):
    fs, fp, fl = scale
    kind = spec[0]
    if kind == "ellipsoid":
        (cs, cp, cl), (rs, rp, rl) = spec[1], spec[2]
        return ((S - cs * fs) / (rs * fs)) ** 2 + ((P - cp * fp) / (rp * fp)) ** 2 + \
            ((L - cl * fl) / (rl * fl)) ** 2 <= 1.0
    if kind == "cylinder":
        (s0, s1), (cp, cl), rad = spec[1], spec[2], spec[3]
        return (S >= s0 * fs) & (S < s1 * fs) & (((P - cp * fp) / fp) ** 2 + ((L - cl * fl) / fl) ** 2 <= rad ** 2)
    (s0, s1), (p0, p1), (l0, l1) = spec[1], spec[2], spec[3]
    return (S >= s0 * fs) & (S < s1 * fs) & (P >= p0 * fp) & (P < p1 * fp) & (L >= l0 * fl) & (L < l1 * fl)


def build_case(root, case_id: str = "synth-std", shape=(40, 96, 96), orientation: str = "standard",
               pixel_spacing=(0.8, 0.8), slice_spacing: float = 2.5, origin=(-38.0, -40.0, -60.0),
               organs=None) -> Path:
    """Write a synthetic case under ``root/case_id`` and return its directory."""
    K, R, C = shape
    row_dir, col_dir, slice_dir = (np.array(v) for v in ORIENTATIONS[orientation])
    k, r, c = np.ogrid[:K, :R, :C]
    # anatomical frame from the voxel grid: cranial-ness, posterior-ness, leftness
    S = k if slice_dir[2] > 0 else K - 1 - k
    P = r if col_dir[1] > 0 else R - 1 - r
    L = c if row_dir[0] > 0 else C - 1 - c
    scale = (K / 40, R / 96, C / 96)
    labels = np.zeros(shape, np.uint8)
    chosen = [o for o in ORGANS if organs is None or o[1] in organs]
    for oid, *_, spec in chosen:
        labels[np.broadcast_to(_mask(spec, S, P, L, scale), shape)] = oid

    d = Path(root) / case_id
    (d / "seg").mkdir(parents=True, exist_ok=True)
    ps_r, ps_c = pixel_spacing
    origin = np.asarray(origin, float)
    affine = np.eye(4)
    affine[:3, 0], affine[:3, 1], affine[:3, 2] = row_dir * ps_c, col_dir * ps_r, slice_dir * slice_spacing
    affine[:3, 3] = origin
    root_uid = f"1.2.826.0.1.3680043.8.498.{zlib.crc32(case_id.encode())}"
    sops = [f"{root_uid}.{i + 1}" for i in range(K)]
    volume = {
        "case": case_id, "study_uid": f"{root_uid}.0.1", "series_uid": f"{root_uid}.0.2",
        "rows": R, "cols": C, "n_slices": K, "sop_uids": sops, "origin_lps": origin.tolist(),
        "row_dir": row_dir.tolist(), "col_dir": col_dir.tolist(), "slice_dir": slice_dir.tolist(),
        "pixel_spacing": [ps_r, ps_c], "slice_spacing": slice_spacing, "affine_lps": affine.tolist(),
        "default_window": {"wc": 40, "ww": 400},
    }
    catalog = []
    for oid, key, name, group, windows, _ in chosen:
        kk, rr, cc = np.nonzero(labels == oid)
        if kk.size == 0:
            continue
        centroid = origin + cc.mean() * ps_c * row_dir + rr.mean() * ps_r * col_dir + kk.mean() * slice_spacing * slice_dir
        center_k = int(round(kk.mean()))
        catalog.append({"id": oid, "key": key, "name": name, "group": group, "windows": windows,
                        "color_rgb": list(COLORS[oid]), "voxels": int(kk.size),
                        "k_range": [int(kk.min()), int(kk.max())], "center_k": center_k,
                        "center_sop": sops[center_k],
                        "bbox_rc": [int(rr.min()), int(rr.max()), int(cc.min()), int(cc.max())],
                        "centroid_lps": centroid.round(3).tolist(), "totalseg_classes": []})
    (d / "volume.json").write_text(json.dumps(volume, indent=1))
    (d / "seg" / "organs.json").write_text(json.dumps(
        {"case": case_id, "label_source": "synthetic (tests/synth.py)", "organs": catalog}, indent=1))
    np.save(d / "seg" / "labels.npy", labels)
    return d


def world(volume: dict, k, r, c) -> list[float]:
    """SPEC formula, written out independently for tests."""
    o = np.asarray(volume["origin_lps"], float)
    ps_r, ps_c = volume["pixel_spacing"]
    return (o + c * ps_c * np.asarray(volume["row_dir"]) + r * ps_r * np.asarray(volume["col_dir"])
            + k * volume["slice_spacing"] * np.asarray(volume["slice_dir"])).tolist()


WINDOWS = {"soft": (40, 400), "lung": (-600, 1500), "bone": (480, 2500), "brain": (40, 80)}


def _event(volume, seq, t, kind, **kw) -> dict:
    return {"v": 1, "session": "s-test", "case": volume["case"], "seq": seq, "t_ms": float(t),
            "kind": kind, "source": kw.pop("source", "simulated"), **kw}


def view_krc(volume, corners_krc, t, seq, window="soft", vp_type="volume", sop=None,
             viewport="default", **kw) -> dict:
    """A view event whose canvas corners are given in continuous voxel (k, r, c)."""
    wc, ww = WINDOWS[window] if isinstance(window, str) else window
    corners = [world(volume, *p) for p in corners_krc]
    n = np.cross(np.subtract(corners[1], corners[0]), np.subtract(corners[3], corners[0]))
    kw.setdefault("series", volume["series_uid"])
    return _event(volume, seq, t, "view", viewport=viewport, vp_type=vp_type, sop=sop,
                  corners_lps=corners, normal=(n / np.linalg.norm(n)).tolist(),
                  voi={"wc": wc, "ww": ww}, canvas_px=[800, 800], **kw)


def stack_view(volume, k, t, seq, window="soft", rect=None, **kw) -> dict:
    """Stack view of slice k showing the half-open pixel rect [r0, r1) x [c0, c1)."""
    r0, r1, c0, c1 = rect or (0, volume["rows"], 0, volume["cols"])
    corners = [(k, r0 - 0.5, c0 - 0.5), (k, r0 - 0.5, c1 - 0.5), (k, r1 - 0.5, c1 - 0.5), (k, r1 - 0.5, c0 - 0.5)]
    sop = kw.pop("sop", volume["sop_uids"][k])
    return view_krc(volume, corners, t, seq, window, vp_type="stack", sop=sop, **kw)


def end(volume, t, seq, **kw) -> dict:
    return _event(volume, seq, t, "end", viewport="*", **kw)


def hidden(volume, t, seq, viewport="*", **kw) -> dict:
    return _event(volume, seq, t, "hidden", viewport=viewport, **kw)


def annotation(volume, t, seq, organ, action, note=None, **kw) -> dict:
    ev = _event(volume, seq, t, "annotation", organ=organ, action=action, **kw)
    if note is not None:
        ev["note"] = note
    return ev


def finish(volume, t, seq, confirmed=True, remaining=(), **kw) -> dict:
    return _event(volume, seq, t, "finish", confirmed=confirmed, remaining=list(remaining), **kw)


def read_pass(volume, ks, t0, seq0, window="soft", dwell=150.0, rect=None, **kw) -> list[dict]:
    return [stack_view(volume, k, t0 + i * dwell, seq0 + i, window, rect, **kw) for i, k in enumerate(ks)]
