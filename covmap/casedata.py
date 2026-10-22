"""Case data for scoring: geometry, organ catalog, labels and per-plane voxel indices.

Labeled organ voxels (those whose label is listed in ``seg/organs.json``) are flattened
once into parallel arrays ``vk, vr, vc`` (int16 voxel indices) and ``vord`` (organ
ordinal), in C order of ``labels[k, r, c]`` so every axial slice is a contiguous range.
Coronal (fixed ``r``) and sagittal (fixed ``c``) planes use lazily built permutations
grouped by that coordinate. Loaded cases are cached and shared (treat as read-only).

The flat index is also cached on disk in ``<case>/.covmap-cache/scoring-index.npz``
(about 5 bytes per labeled voxel), keyed by labels.npy (inode, size, mtime, sampled
content hash), the organ ids and the shape; a fresh process then loads it instead of
rebuilding it from the label volume. ``COVMAP_INDEX_CACHE=0`` disables the disk cache.
"""

from __future__ import annotations

import hashlib
import json
import mmap
import os
import threading
import zipfile
from collections import OrderedDict
from dataclasses import dataclass
from functools import cached_property
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

    @cached_property
    def plane_normals(self) -> np.ndarray:
        """Unit normals (rows) of the planes k = const, r = const, c = const."""
        dk, dr, dc = self.axes.T
        n = np.stack([np.cross(dr, dc), np.cross(dk, dc), np.cross(dk, dr)])
        return n / np.linalg.norm(n, axis=1, keepdims=True)


# ---- flat voxel index (built from labels.npy, cached on disk next to the case) -----------
INDEX_VERSION = 1
INDEX_FILE = Path(".covmap-cache") / "scoring-index.npz"
_SAMPLE_STRIDE = 1 << 16


def _build_index(labels: np.ndarray, organ_ids: np.ndarray):
    """(vord, vr, vc, slice offsets, organ x slice counts) of the labeled organ voxels.

    One pass per axial slice with small, cache-resident temporaries (a fresh process pays
    a page fault for every new large array). Flat voxel arrays are in C order, so every
    axial slice is a contiguous range [off[k], off[k+1])."""
    n_org = len(organ_ids)
    lut = np.full(256, NO_ORGAN, np.uint8)
    lut[organ_ids] = np.arange(n_org, dtype=np.uint8)
    K, R, C = labels.shape
    lab2 = labels.reshape(K, R * C)
    upper = int(np.count_nonzero(lab2))
    vord = np.empty(upper, np.uint8)
    vr = np.empty(upper, np.int16)
    vc = np.empty(upper, np.int16)
    osc = np.empty((n_org, K), np.int64)
    off = np.zeros(K + 1, np.int64)
    shift = C.bit_length() - 1 if C & (C - 1) == 0 else None  # 512: shifts, not division
    pos = 0
    for k in range(K):
        row = np.asarray(lab2[k])
        nz = np.flatnonzero(row)
        o = lut[row[nz]]
        cnt = np.bincount(o, minlength=n_org)
        if cnt.size > n_org:  # labels that are not in the organ catalog
            keep = o != NO_ORGAN
            nz, o, cnt = nz[keep], o[keep], cnt[:n_org]
        end = pos + o.size
        vord[pos:end] = o
        if shift is not None:
            vr[pos:end] = nz >> shift
            vc[pos:end] = nz & (C - 1)
        else:
            vr[pos:end], vc[pos:end] = np.divmod(nz, C)
        osc[:, k] = cnt
        pos = off[k + 1] = end
    if pos < upper:
        vord, vr, vc = vord[:pos].copy(), vr[:pos].copy(), vc[:pos].copy()
    return vord, vr, vc, off, osc


def _index_key(case_dir: Path, organ_ids: np.ndarray, shape) -> str:
    """What the index depends on: labels.npy (inode, size, mtime and a hash of every 64
    KiB-th byte), the organ ids in catalog order, the shape and the index format version."""
    lab = case_dir / "seg" / "labels.npy"
    st = lab.stat()
    h = hashlib.sha256(json.dumps({"v": INDEX_VERSION, "ino": st.st_ino, "size": st.st_size,
                                   "mtime_ns": st.st_mtime_ns, "organ_ids": [int(i) for i in organ_ids],
                                   "shape": list(shape)}).encode())
    if st.st_size:
        with open(lab, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
            h.update(m[::_SAMPLE_STRIDE])
    return h.hexdigest()


def _index_cache_enabled() -> bool:
    return os.environ.get("COVMAP_INDEX_CACHE", "1") != "0"


def _cached_index(case_dir: Path, key: str | None, n_org: int, shape):
    """The cached index if present, matching ``key`` and self-consistent, else None."""
    f = case_dir / INDEX_FILE
    if key is None or not f.is_file():
        return None
    try:
        with np.load(f, allow_pickle=False) as z:
            if str(z["key"]) != key:
                return None
            vord, vr, vc, off, osc = (z[k] for k in ("vord", "vr", "vc", "offsets", "organ_slice_counts"))
    except (OSError, ValueError, KeyError, EOFError, zipfile.BadZipFile):
        return None
    n, K = vord.size, shape[0]
    ok = (vord.dtype == np.uint8 and vr.dtype == np.int16 and vc.dtype == np.int16
          and off.dtype == np.int64 and osc.dtype == np.int64
          and vr.size == n and vc.size == n and off.shape == (K + 1,) and off[0] == 0 and off[-1] == n
          and osc.shape == (n_org, K) and int(osc.sum()) == n)
    return (vord, vr, vc, off, osc) if ok else None


def _write_index(case_dir: Path, key: str | None, organ_ids: np.ndarray, shape, index) -> None:
    """Best effort (atomic replace; a read-only data dir just means no cache). ``key`` was
    taken before the labels were read; nothing is written if labels.npy changed since."""
    if key is None:
        return
    f = case_dir / INDEX_FILE
    tmp = f.with_name(f".{f.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        if _index_key(case_dir, organ_ids, shape) != key:
            return
        f.parent.mkdir(exist_ok=True)
        vord, vr, vc, off, osc = index
        with open(tmp, "wb") as out:
            np.savez(out, key=np.array(key), vord=vord, vr=vr, vc=vc, offsets=off, organ_slice_counts=osc)
        os.replace(tmp, f)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


class Case:
    def __init__(self, case_dir: Path):
        self.dir = Path(case_dir)
        self.volume = read_json(self.dir / "volume.json")
        catalog = read_json(self.dir / "seg" / "organs.json")
        v = self.volume
        self.case_id: str = v["case"]
        self.study_uid: str | None = v.get("study_uid")
        self.series_uid: str | None = v.get("series_uid")
        self.shape = (int(v["n_slices"]), int(v["rows"]), int(v["cols"]))
        if max(self.shape) >= np.iinfo(np.int16).max:  # int16 voxel coordinates (and n + 1 keys)
            raise ValueError(f"{self.dir}: volume {self.shape} too large")
        self.geom = Geometry.from_volume(v)
        self.sop_uids: list[str] = list(v["sop_uids"])
        self.sop_index = {s: i for i, s in enumerate(self.sop_uids)}

        self.organs: list[dict] = list(catalog["organs"])
        if len(self.organs) >= NO_ORGAN:
            raise ValueError("too many organs")
        self.organ_ids = np.array([o["id"] for o in self.organs], np.int64)
        self.organ_keys = [o["key"] for o in self.organs]
        self.key_index = {k: i for i, k in enumerate(self.organ_keys)}
        if (self.organ_ids <= 0).any() or (self.organ_ids > 254).any():
            raise ValueError("organ label ids must be 1..254 (0 is background)")

        # cache key taken before labels.npy is opened (and re-checked before writing), so a
        # label volume replaced meanwhile can never be cached under its new key
        key = _index_key(self.dir, self.organ_ids, self.shape) if _index_cache_enabled() else None
        # memory-mapped: with a cached index the label volume is never read in full
        labels = np.load(self.dir / "seg" / "labels.npy", mmap_mode="r")
        if labels.shape != self.shape or labels.dtype != np.uint8:
            raise ValueError(f"{self.dir}: labels.npy {labels.dtype}{labels.shape} does not match "
                             f"volume.json uint8{self.shape}")
        self.labels = labels
        index = _cached_index(self.dir, key, len(self.organ_ids), self.shape)
        self.index_source = "cache"
        if index is None:
            index = _build_index(labels, self.organ_ids)
            self.index_source = "built"
            _write_index(self.dir, key, self.organ_ids, self.shape, index)
        self.vord, self.vr, self.vc, off, self.organ_slice_counts = index
        self.n_vox = int(self.vord.size)
        self.organ_voxels = self.organ_slice_counts.sum(axis=1)
        self._offsets = {0: off}
        self._vk = None
        self._perm: dict[int, tuple] = {}
        self._organ_idx: dict[int, np.ndarray] = {}
        self._lock = threading.Lock()

    def organ_index(self, o: int) -> np.ndarray:
        """Flat indices (sorted, int32) of organ ordinal ``o``'s voxels, built on first use;
        slice-major, so slice k's voxels are the k-th run of ``organ_slice_counts[o]``."""
        idx = self._organ_idx.get(o)
        if idx is None:  # idempotent: a concurrent first use just builds it twice
            idx = self._organ_idx[o] = np.flatnonzero(self.vord == o).astype(np.int32)
        return idx

    @property
    def vk(self) -> np.ndarray:
        """Slice index k of every flat voxel (built on first use)."""
        if self._vk is None:
            self._vk = np.repeat(np.arange(self.shape[0], dtype=np.int16), np.diff(self._offsets[0]))
        return self._vk

    def _axis_index(self, axis: int):
        """(perm, u, v), offsets for coronal (axis 1) or sagittal (axis 2) planes: voxels
        grouped by r (resp. c), each group in (k, c) (resp. (k, r)) order. Built once."""
        with self._lock:
            if axis not in self._perm:
                self._perm[axis] = self._build_coronal() if axis == 1 else self._build_sagittal()
            return self._perm[axis]

    def prepare_planes(self) -> None:
        """Build the coronal and sagittal indexes now (the server does this in the
        background so a session's first MPR view does not wait for it)."""
        self._axis_index(1)
        self._axis_index(2)

    def _build_coronal(self):
        # every axial slice is ordered by (r, c): its row-r voxels are one contiguous run, so
        # plane r is the concatenation over k of those runs (no sort needed)
        K, R, _ = self.shape
        off = self._offsets[0]
        q = np.arange(R + 1, dtype=self.vr.dtype)  # keys in the array dtype: no int64 copy per slice
        row_start = np.stack([np.searchsorted(self.vr[off[k]:off[k + 1]], q) for k in range(K)])  # (K, R+1)
        counts = np.diff(row_start, axis=1)  # (K, R)
        lens = counts.T.ravel()  # runs in (r, k) order
        starts = (off[:-1, None] + row_start[:, :-1]).T.ravel()
        first = np.zeros(lens.size + 1, np.int64)
        np.cumsum(lens, out=first[1:])
        perm = (np.arange(first[-1], dtype=np.int64) + np.repeat(starts - first[:-1], lens)).astype(np.int32)
        u = np.repeat(np.tile(np.arange(K, dtype=np.int16), R), lens)
        offsets = np.zeros(R + 1, np.int64)
        np.cumsum(counts.sum(axis=0), out=offsets[1:])
        return (perm, u, self.vc[perm]), offsets

    def _build_sagittal(self):
        C = self.shape[2]
        perm = np.argsort(self.vc, kind="stable").astype(np.int32)  # stable: (c, k, r) order
        offsets = np.searchsorted(self.vc[perm], np.arange(C + 1, dtype=self.vc.dtype))
        return (perm, self.vk[perm], self.vr[perm]), offsets

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
