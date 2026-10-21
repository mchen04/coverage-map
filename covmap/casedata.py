"""Case data for scoring: geometry, organ catalog, labels and per-plane voxel indices.

Labeled organ voxels (those whose label is listed in ``seg/organs.json``) are flattened
once into parallel arrays ``vk, vr, vc`` (int16 voxel indices) and ``vord`` (organ
ordinal), in C order of ``labels[k, r, c]`` so every axial slice is a contiguous range.
Coronal (fixed ``r``) and sagittal (fixed ``c``) planes use lazily built permutations
grouped by that coordinate. Loaded cases are cached and shared (treat as read-only).
"""

from __future__ import annotations

import json
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from covmap import paths

NO_ORGAN = 255


def resolve_case_dir(case) -> Path:
    """A case id resolves under ``$COVMAP_DATA/cases``; a path (or str with a slash) is used as is."""
    if isinstance(case, Path):
        return case
    s = str(case)
    return Path(s) if "/" in s else paths.case_dir(s)


def read_json(path: Path):
    with open(path) as f:
        return json.load(f)


@dataclass(frozen=True)
class Geometry:
    """Voxel (k, r, c) <-> LPS world mm, per the SPEC formula."""

    origin: np.ndarray       # (3,)
    axes: np.ndarray         # (3, 3) columns = world step per +1 in k, r, c
    shape: tuple[int, int, int]

    @classmethod
    def from_volume(cls, vol: dict) -> "Geometry":
        ps_row, ps_col = (float(x) for x in vol["pixel_spacing"])
        dk = np.asarray(vol["slice_dir"], float) * float(vol["slice_spacing"])
        dr = np.asarray(vol["col_dir"], float) * ps_row
        dc = np.asarray(vol["row_dir"], float) * ps_col
        return cls(np.asarray(vol["origin_lps"], float), np.stack([dk, dr, dc], axis=1),
                   (int(vol["n_slices"]), int(vol["rows"]), int(vol["cols"])))

    def voxel_to_world(self, krc) -> np.ndarray:
        krc = np.asarray(krc, float)
        return krc @ self.axes.T + self.origin

    def world_to_voxel(self, pts) -> np.ndarray:
        """Continuous (k, r, c) of LPS points (any leading shape, last dim 3)."""
        pts = np.asarray(pts, float)
        return np.linalg.solve(self.axes, (pts - self.origin).reshape(-1, 3).T).T.reshape(pts.shape)

    @property
    def plane_normals(self) -> np.ndarray:
        """Unit normals (rows) of the planes k = const, r = const, c = const."""
        dk, dr, dc = self.axes.T
        n = np.stack([np.cross(dr, dc), np.cross(dk, dc), np.cross(dk, dr)])
        return n / np.linalg.norm(n, axis=1, keepdims=True)


class Case:
    def __init__(self, case_dir: Path):
        self.dir = Path(case_dir)
        self.volume = read_json(self.dir / "volume.json")
        catalog = read_json(self.dir / "seg" / "organs.json")
        labels = np.load(self.dir / "seg" / "labels.npy")
        v = self.volume
        self.case_id: str = v["case"]
        self.study_uid: str | None = v.get("study_uid")
        self.series_uid: str | None = v.get("series_uid")
        self.shape = (int(v["n_slices"]), int(v["rows"]), int(v["cols"]))
        if labels.shape != self.shape or labels.dtype != np.uint8:
            raise ValueError(f"{self.dir}: labels.npy {labels.dtype}{labels.shape} does not match "
                             f"volume.json uint8{self.shape}")
        self.labels = labels
        self.geom = Geometry.from_volume(v)
        self.sop_uids: list[str] = list(v["sop_uids"])
        self.sop_index = {s: i for i, s in enumerate(self.sop_uids)}

        self.organs: list[dict] = list(catalog["organs"])
        if len(self.organs) >= NO_ORGAN:
            raise ValueError("too many organs")
        self.organ_ids = np.array([o["id"] for o in self.organs], np.int64)
        self.organ_keys = [o["key"] for o in self.organs]
        self.key_index = {k: i for i, k in enumerate(self.organ_keys)}

        lut = np.full(256, NO_ORGAN, np.uint8)
        lut[self.organ_ids] = np.arange(len(self.organs), dtype=np.uint8)
        ordvol = lut[labels].ravel()
        flat = np.flatnonzero(ordvol != NO_ORGAN)
        self.vord = ordvol[flat]
        del ordvol
        K, R, C = self.shape
        self.vk = (flat // (R * C)).astype(np.int16)
        rem = flat % (R * C)
        self.vr = (rem // C).astype(np.int16)
        self.vc = (rem % C).astype(np.int16)
        del flat, rem
        self.n_vox = int(self.vord.size)
        n_org = len(self.organs)
        self.organ_voxels = np.bincount(self.vord, minlength=n_org).astype(np.int64)
        # organ x slice voxel counts (baseline-implied coverage, reports)
        self.organ_slice_counts = np.bincount(
            self.vord.astype(np.int64) * K + self.vk, minlength=n_org * K).reshape(n_org, K)
        self._offsets = {0: self._group_offsets(self.vk, K)}
        self._perm: dict[int, tuple] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _group_offsets(sorted_coord: np.ndarray, n: int) -> np.ndarray:
        return np.concatenate([[0], np.cumsum(np.bincount(sorted_coord, minlength=n))])

    def _axis_index(self, axis: int):
        with self._lock:
            if axis not in self._perm:
                coord = self.vr if axis == 1 else self.vc
                perm = np.argsort(coord, kind="stable").astype(np.int32)
                other = self.vc if axis == 1 else self.vr
                self._perm[axis] = (perm, self.vk[perm], other[perm])
                self._offsets[axis] = self._group_offsets(coord[perm], self.shape[axis])
            return self._perm[axis], self._offsets[axis]

    def plane(self, axis: int, index: int):
        """Labeled voxels on a plane: ``(sel, u, v)``.

        ``sel`` selects them in the flat voxel arrays (a slice for axial planes, an index
        array otherwise); ``u, v`` are their in-plane voxel coordinates, in the order of the
        remaining axes (axial: r, c; coronal: k, c; sagittal: k, r).
        """
        if axis == 0:
            off = self._offsets[0]
            sl = slice(int(off[index]), int(off[index + 1]))
            return sl, self.vr[sl], self.vc[sl]
        (perm, u, v), off = self._axis_index(axis)
        lo, hi = int(off[index]), int(off[index + 1])
        return perm[lo:hi], u[lo:hi], v[lo:hi]

    def organ(self, key: str) -> dict:
        return self.organs[self.key_index[key]]


_cache: OrderedDict = OrderedDict()
_cache_lock = threading.Lock()
_load_locks: dict = {}
CACHE_SIZE = 4


def _stamp(d: Path):
    files = [d / "volume.json", d / "seg" / "organs.json", d / "seg" / "labels.npy"]
    missing = [str(f) for f in files if not f.exists()]
    if missing:
        raise FileNotFoundError(f"case not ready for scoring, missing: {', '.join(missing)}")
    return tuple(f.stat().st_mtime_ns for f in files)


def load_case(case) -> Case:
    """Load (cached) a segmented case by id, directory path, or pass a Case through."""
    if isinstance(case, Case):
        return case
    d = resolve_case_dir(case).resolve()
    key = (str(d), _stamp(d))
    with _cache_lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
        lock = _load_locks.setdefault(str(d), threading.Lock())
    with lock:  # one loader per case directory; others wait and reuse
        with _cache_lock:
            if key in _cache:
                return _cache[key]
        c = Case(d)
        with _cache_lock:
            for old in [k for k in _cache if k[0] == str(d)]:
                del _cache[old]
            _cache[key] = c
            while len(_cache) > CACHE_SIZE:
                _cache.popitem(last=False)
        return c


def load_volume(case) -> dict:
    return read_json(resolve_case_dir(case) / "volume.json")


_mmaps: dict = {}


def _labels_mmap(case_dir: Path) -> np.ndarray:
    f = case_dir / "seg" / "labels.npy"
    key = (str(f), f.stat().st_mtime_ns)
    with _cache_lock:
        if key not in _mmaps:
            for old in [k for k in _mmaps if k[0] == key[0]]:
                del _mmaps[old]
            _mmaps[key] = np.load(f, mmap_mode="r")
        return _mmaps[key]


def organ_outline(case, key: str, k: int, tolerance: float = 0.5) -> list[list[list[float]]]:
    """Contours of organ ``key`` on axial slice ``k`` in image-pixel (r, c) coordinates
    (marching squares at the 0.5 iso-level, simplified to ``tolerance`` px)."""
    from skimage.measure import approximate_polygon, find_contours

    d = resolve_case_dir(case).resolve()
    organ = next((o for o in read_json(d / "seg" / "organs.json")["organs"] if o["key"] == key), None)
    if organ is None:
        raise KeyError(f"unknown organ {key!r}")
    labels = _labels_mmap(d)
    if not 0 <= k < labels.shape[0]:
        raise IndexError(f"slice {k} outside 0..{labels.shape[0] - 1}")
    mask = np.pad(np.asarray(labels[k]) == organ["id"], 1)
    if not mask.any():
        return []
    out = []
    for cnt in find_contours(mask.astype(np.uint8), 0.5):
        if tolerance:
            cnt = approximate_polygon(cnt, tolerance)
        out.append(np.round(cnt - 1.0, 2).tolist())
    return out
