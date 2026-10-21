"""TotalSegmentator organ labels in the DICOM grid, per-case organ catalog and meshes.

Runs the TotalSegmentator v2 ``total`` task on ``ct.nii.gz``, maps its classes to the
stable catalog in :mod:`covmap.organs`, re-grids the result onto ``[k, r, c]`` (verified
from the affines, not assumed) and writes ``labels.npy``, ``organs.json``, meshes and a
``segment.json`` run record (docs/SPEC.md).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import platform
import shutil
import sys
import time
from pathlib import Path
from unittest import mock

import numpy as np

from covmap import __version__, organs
from covmap.paths import case_dir

WEIGHTS_RELEASE_BASE = "https://github.com/wasserth/TotalSegmentator/releases/download"
TOTAL_TASK_IDS = {"default": [291, 292, 293, 294, 295], "fast": [297], "roi_crop": [298]}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(1 << 20):
            h.update(block)
    return h.hexdigest()


@contextlib.contextmanager
def stdout_to_stderr():
    """Route everything (including child processes) written to fd 1 to stderr.

    The CLI contract reserves stdout for one JSON object; TotalSegmentator and
    nnU-Net print progress there.
    """
    sys.stdout.flush()
    saved = os.dup(1)
    try:
        os.dup2(2, 1)
        with contextlib.redirect_stdout(sys.stderr):
            yield
    finally:
        sys.stderr.flush()
        os.dup2(saved, 1)
        os.close(saved)


def _versions() -> dict:
    import importlib.metadata as md

    out = {"python": platform.python_version(), "covmap": __version__}
    for dist in ("TotalSegmentator", "nnunetv2", "torch", "numpy", "nibabel", "scikit-image", "scipy"):
        try:
            out[dist] = md.version(dist)
        except md.PackageNotFoundError:
            out[dist] = None
    return out


def _host() -> dict:
    return {"hostname": platform.node(), "machine": platform.machine(),
            "cpu_count": os.cpu_count(), "loadavg": [round(x, 2) for x in os.getloadavg()]}


def prepare_totalseg() -> dict:
    """Create the TotalSegmentator config under TOTALSEG_HOME_DIR with usage stats off."""
    from totalsegmentator.config import get_totalseg_dir, set_config_key, setup_nnunet, setup_totalseg

    setup_totalseg()
    set_config_key("send_usage_stats", False)
    set_config_key("statistics_disclaimer_shown", True)
    setup_nnunet()
    return {"home": str(get_totalseg_dir()), "send_usage_stats": False}


def weight_task_ids(fast: bool, roi_subset: bool) -> list[int]:
    ids = list(TOTAL_TASK_IDS["fast" if fast else "default"])
    if roi_subset:
        ids += TOTAL_TASK_IDS["roi_crop"]  # TotalSegmentator's 6 mm crop model for roi_subset
    return ids


def ensure_weights(task_ids: list[int], attempts: int = 3) -> list[dict]:
    """Download missing public weights (GitHub release assets); record what was used."""
    from totalsegmentator.config import get_weights_dir
    from totalsegmentator.libs import download_pretrained_weights
    from totalsegmentator.map_tasks_config import TASK_ID_WEIGHTS_CONFIGS

    prepare_totalseg()
    root = get_weights_dir()
    records = []
    for tid in task_ids:
        info = TASK_ID_WEIGHTS_CONFIGS[tid]
        folder = Path(info.get("rel_path") or "") / info["foldername"]
        path = root / folder
        existed = path.exists() and any(path.rglob("checkpoint_final.pth"))
        t = time.perf_counter()
        for attempt in range(1, attempts + 1):
            if path.exists() and not any(path.rglob("checkpoint_final.pth")):
                shutil.rmtree(path)  # incomplete extraction; TotalSegmentator would skip it
            try:
                with stdout_to_stderr():
                    download_pretrained_weights(tid)
                break
            except Exception as e:  # noqa: BLE001 - network errors from requests/zipfile
                print(f"weights task {tid} attempt {attempt}/{attempts} failed: {e}", file=sys.stderr)
                if attempt == attempts:
                    raise RuntimeError(f"could not download weights for task {tid}: {e}") from e
                time.sleep(10 * attempt)
        ckpts = sorted(path.rglob("checkpoint_final.pth"))
        if not ckpts:
            raise RuntimeError(f"weights for task {tid} missing after download: {path}")
        records.append({
            "task_id": tid,
            "folder": str(folder),
            "url": f"{WEIGHTS_RELEASE_BASE}/{info['version']}/{info['foldername']}.zip",
            "downloaded_now": not existed,
            "download_s": round(time.perf_counter() - t, 3) if not existed else 0.0,
            "checkpoints": [{"path": str(p.relative_to(root)), "bytes": p.stat().st_size,
                             "sha256": _sha256(p)} for p in ckpts],
        })
    return records


def conv3d_by_conv2d(x, weight, bias, stride, padding, budget_bytes: int = 256 << 20):
    """3D convolution as a sum over kernel depth of batched 2D convolutions.

    Mathematically identical to ``F.conv3d`` (groups=1, dilation=1, zero padding).
    torch builds without MKLDNN run CPU ``conv3d`` through ``slow_conv3d``, whose
    vol2col buffer is ``C_in*27*D*H*W`` floats (~14.5 GB for nnU-Net's 64-channel
    128^3 decoder layers); the 2D im2col buffers here are bounded by ``budget_bytes``.
    """
    import torch
    import torch.nn.functional as F

    (sd, sh, sw), (pd, ph, pw) = stride, padding
    kd = weight.shape[2]
    x = F.pad(x, (0, 0, 0, 0, pd, pd))
    n, c, d, h, w = x.shape
    d_out = (d - kd) // sd + 1
    per_slice = c * weight.shape[3] * weight.shape[4] * h * w * x.element_size()
    chunk = max(1, budget_bytes // max(1, n * per_slice))
    outs = []
    for z0 in range(0, d_out, chunk):
        z1 = min(d_out, z0 + chunk)
        acc = None
        for i in range(kd):
            sl = x[:, :, z0 * sd + i:(z1 - 1) * sd + i + 1:sd]
            m = sl.shape[2]
            y = F.conv2d(sl.transpose(1, 2).reshape(n * m, c, h, w), weight[:, :, i], None, (sh, sw), (ph, pw))
            acc = y if acc is None else acc.add_(y)
        outs.append(acc.reshape(n, z1 - z0, *acc.shape[1:]).transpose(1, 2))
    out = torch.cat(outs, 2)
    return out if bias is None else out + bias.view(1, -1, 1, 1, 1)


@contextlib.contextmanager
def lowmem_cpu_conv3d():
    """Route eligible CPU ``nn.Conv3d`` calls through :func:`conv3d_by_conv2d`; yields a record."""
    import torch
    import torch.nn.functional as F

    original = torch.nn.Conv3d._conv_forward
    calls = {"decomposed": 0, "native": 0}

    def forward(self, input, weight, bias):
        if (input.device.type == "cpu" and self.groups == 1 and tuple(self.dilation) == (1, 1, 1)
                and self.padding_mode == "zeros" and not isinstance(self.padding, str)):
            calls["decomposed"] += 1
            return conv3d_by_conv2d(input, weight, bias, tuple(self.stride), tuple(self.padding))
        calls["native"] += 1
        return original(self, input, weight, bias)

    g = torch.Generator().manual_seed(0)
    x, w, b = (torch.randn(*s, generator=g) for s in ((1, 8, 9, 10, 11), (6, 8, 3, 3, 3), (6,)))
    with torch.no_grad():
        ref = F.conv3d(x, w, b, (2, 1, 1), 1)
        err = float(((conv3d_by_conv2d(x, w, b, (2, 1, 1), (1, 1, 1)) - ref).abs().max() / ref.abs().max()))
    if err > 1e-4:
        raise RuntimeError(f"conv3d decomposition self-check failed (rel err {err:g})")
    record = {"mode": "conv3d as sum of conv2d over kernel depth (CPU, torch without MKLDNN)",
              "self_check_rel_err": err, "calls": calls}
    torch.nn.Conv3d._conv_forward = forward
    try:
        yield record
    finally:
        torch.nn.Conv3d._conv_forward = original


def check_device(device: str) -> dict:
    """Validate the requested torch device; MPS must actually run a 3D conv."""
    import torch

    if device == "cpu":
        return {"device": "cpu", "torch_threads": torch.get_num_threads()}
    if device != "mps":
        raise ValueError(f"device must be 'cpu' or 'mps', got {device!r}")
    if not torch.backends.mps.is_available():
        raise RuntimeError(f"MPS requested but unavailable (built={torch.backends.mps.is_built()}); "
                           "rerun with --device cpu")
    import copy

    try:
        g = torch.Generator().manual_seed(0)
        x = torch.randn(1, 2, 12, 12, 12, generator=g)
        conv = torch.nn.Conv3d(2, 4, 3, padding=1)
        with torch.no_grad():
            ref = conv(x)
            out = copy.deepcopy(conv).to("mps")(x.to("mps")).cpu()
        err = float((out - ref).abs().max() / ref.abs().max())
    except Exception as e:  # noqa: BLE001 - report the device failure verbatim
        raise RuntimeError(f"MPS Conv3d self-test raised {type(e).__name__}: {e}") from e
    if err > 1e-3:
        zeros = " (MPS output is all zeros)" if not bool(out.abs().max()) else ""
        raise RuntimeError(f"MPS Conv3d self-test differs from CPU: max rel err {err:.3g}{zeros}; "
                           "TotalSegmentator would produce wrong labels on this device. Use --device cpu.")
    return {"device": "mps", "conv3d_self_test_rel_err": err}


def run_totalsegmentator(ct_path: Path, out_path: Path, fast: bool, device: str,
                         roi_classes: list[str] | None, threads: int | None) -> dict:
    """Run the Python API into a temp dir and move the multilabel file into place."""
    from totalsegmentator.python_api import totalsegmentator

    tmp = out_path.parent / ".totalseg_tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    kwargs = dict(input=str(ct_path), output=str(tmp / "totalseg.nii.gz"), ml=True, fast=fast,
                  device=device, roi_subset=roi_classes, task="total", quiet=False)
    # On CPU TotalSegmentator sets torch threads to multiprocessing.cpu_count().
    cpu_patch = (mock.patch("multiprocessing.cpu_count", return_value=threads)
                 if threads else contextlib.nullcontext())
    import torch

    lowmem = device == "cpu" and not torch.backends.mkldnn.is_available()
    t = time.perf_counter()
    with stdout_to_stderr(), cpu_patch, (lowmem_cpu_conv3d() if lowmem else contextlib.nullcontext()) as conv:
        totalsegmentator(**kwargs)
    wall = time.perf_counter() - t
    (tmp / "totalseg.nii.gz").replace(out_path)
    shutil.rmtree(tmp, ignore_errors=True)
    shown = {k: v for k, v in kwargs.items() if k not in ("input", "output")}
    import resource

    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss  # bytes on macOS, KiB on Linux
    rss_gb = rss / 1e9 if sys.platform == "darwin" else rss * 1024 / 1e9
    return {"call": "totalsegmentator.python_api.totalsegmentator", "kwargs": shown,
            "threads": threads, "cpu_conv3d": conv or "native", "wall_s": wall,
            "process_max_rss_gb": round(rss_gb, 2)}


def _label_map(seg_img) -> tuple[dict[int, str], str]:
    """Label values -> class names, from the file's own header when present."""
    from totalsegmentator.map_to_binary import class_map

    try:
        from totalsegmentator.nifti_ext_header import load_multilabel_nifti

        _, lm = load_multilabel_nifti(seg_img)
        lm = {int(k): str(v) for k, v in lm.items()}
        expected = class_map["total"]
        bad = {k: v for k, v in lm.items() if expected.get(k) != v}
        if bad:
            raise RuntimeError(f"segmentation header label map disagrees with class_map['total']: {bad}")
        return lm, "nifti extension header (checked against class_map['total'])"
    except (IndexError, KeyError, ImportError):
        return dict(class_map["total"]), "class_map['total'] (no label header in file)"


def organ_stats(labels: np.ndarray, geometry: dict, min_voxels: int) -> tuple[list[dict], dict]:
    """Per-organ voxel counts, extents, median slice and centroid (catalog order).

    Returns ``(listed, below)``: organs with at least ``min_voxels`` and the voxel
    counts of catalog organs present but below that threshold.
    """
    from covmap.dicom_io import voxel_to_lps

    # hist_axis[i, label] = voxels of that label in plane i along the axis
    hists = [np.stack([np.bincount(np.take(labels, i, axis=ax).ravel(), minlength=256)
                       for i in range(labels.shape[ax])]) for ax in range(3)]
    counts = hists[0].sum(axis=0)
    listed, below = [], {}
    for o in organs.CATALOG:
        n = int(counts[o.id])
        if n < min_voxels:
            if n:
                below[o.key] = n
            continue
        ext, mean = [], []
        for h in hists:
            nz = np.flatnonzero(h[:, o.id])
            ext.append((int(nz[0]), int(nz[-1])))
            mean.append(float((h[:, o.id] * np.arange(len(h))).sum() / n))
        cum = np.cumsum(hists[0][:, o.id])
        center_k = int(np.searchsorted(cum, (n + 1) // 2))  # lower median slice
        centroid = voxel_to_lps(geometry, mean)
        listed.append({
            **o.as_dict(),
            "voxels": n,
            "k_range": list(ext[0]),
            "center_k": center_k,
            "center_sop": geometry["sop_uids"][center_k],
            "bbox_rc": [*ext[1], *ext[2]],
            "centroid_lps": [round(float(x), 3) for x in centroid],
        })
    return listed, below


def _ordered(organ: dict) -> dict:
    keys = ["id", "key", "name", "group", "windows", "color_rgb", "voxels", "k_range", "center_k",
            "center_sop", "bbox_rc", "centroid_lps", "totalseg_classes"]
    return {k: organ[k] for k in keys}


def segment_case(case_id: str, fast: bool = False, device: str = "cpu", roi_subset: bool = False,
                 threads: int | None = None, reuse: bool = False, out_name: str = "seg",
                 min_voxels: int = 2000) -> dict:
    import nibabel as nib

    from covmap import dicom_io, meshes

    t_all = time.perf_counter()
    cdir = case_dir(case_id)
    ct_path, vol_path = cdir / "ct.nii.gz", cdir / "volume.json"
    if not ct_path.exists() or not vol_path.exists():
        raise FileNotFoundError(f"{cdir}: run `covmap fetch --case {case_id}` first")
    if not out_name or "/" in out_name or out_name.startswith("."):
        raise ValueError(f"bad out_name {out_name!r}")
    geometry = json.loads(vol_path.read_text())
    out = cdir / out_name
    ts_path = out / "totalseg.nii.gz"
    run_ts = not (reuse and ts_path.exists())
    device_info = check_device(device) if run_ts else None  # fail before touching outputs
    out.mkdir(parents=True, exist_ok=True)
    record_path = out / "segment.json"
    previous = json.loads(record_path.read_text()) if record_path.exists() else {}

    options = {"task": "total", "fast": fast, "device": device, "roi_subset": roi_subset,
               "threads": threads, "reuse": reuse, "out_name": out_name, "min_voxels": min_voxels}
    host_start = _host()
    timings: dict[str, float] = {}
    roi_classes = organs.catalog_classes() if roi_subset else None

    if not run_ts:
        original = previous.get("totalsegmentator")
        while original and original.get("reused"):  # keep the record of the run that made the file
            original = original.get("previous")
        ts_run = {"reused": True, "previous": original}
        device_info = previous.get("device_info")
        weights = previous.get("weights")
    else:
        t = time.perf_counter()
        weights = ensure_weights(weight_task_ids(fast, roi_subset))
        timings["weights_check_s"] = time.perf_counter() - t  # excluded from the TS wall time
        ts_run = run_totalsegmentator(ct_path, ts_path, fast, device, roi_classes, threads)
        ts_run["loadavg_after"] = [round(x, 2) for x in os.getloadavg()]
        timings["totalsegmentator_s"] = ts_run["wall_s"]

    t = time.perf_counter()
    seg_img = nib.load(str(ts_path))
    ct_img = nib.load(str(ct_path))
    same_grid = (seg_img.shape == ct_img.shape and np.allclose(seg_img.affine, ct_img.affine, atol=1e-4))
    raw = np.asanyarray(seg_img.dataobj)
    if not np.issubdtype(raw.dtype, np.integer):
        raw = np.rint(raw)
    if raw.min() < 0 or raw.max() > 255:
        raise RuntimeError(f"unexpected label values {raw.min()}..{raw.max()} in {ts_path}")
    label_map, label_map_source = _label_map(seg_img)
    unknown = sorted(set(np.unique(raw).astype(int).tolist()) - set(label_map) - {0})
    if unknown:
        raise RuntimeError(f"label values {unknown} not in the TotalSegmentator label map")
    mapped = organs.label_lut(label_map)[raw.astype(np.uint8)]
    labels, regrid = dicom_io.regrid_to_dicom(mapped, seg_img.affine, geometry)
    expected_shape = (geometry["n_slices"], geometry["rows"], geometry["cols"])
    if labels.shape != expected_shape or labels.dtype != np.uint8:
        raise RuntimeError(f"labels {labels.shape} {labels.dtype} != {expected_shape} uint8")
    np.save(out / "labels.npy", labels)
    found, below = organ_stats(labels, geometry, min_voxels)
    organs_doc = {
        "case": case_id,
        "label_source": f"TotalSegmentator {_versions()['TotalSegmentator']} total"
                        + (" (fast 3 mm)" if fast else "") + (" roi_subset" if roi_subset else ""),
        "organs": [_ordered(o) for o in found],
    }
    (out / "organs.json").write_text(json.dumps(organs_doc, indent=1) + "\n")
    timings["postprocess_s"] = time.perf_counter() - t

    t = time.perf_counter()
    _, mesh_info = meshes.write_case_meshes(labels, geometry, [(o["id"], o["key"]) for o in found], out)
    timings["meshes_s"] = time.perf_counter() - t
    timings["total_s"] = time.perf_counter() - t_all

    hashes = {name: _sha256(out / name) for name in
              ("totalseg.nii.gz", "labels.npy", "organs.json", "meshes.bin", "meshes.json")}
    record = {
        "case": case_id,
        "command": " ".join(["python", "-m", "covmap", *sys.argv[1:]]) if sys.argv[0].endswith(
            ("covmap", "__main__.py", "cli.py")) else None,
        "options": options,
        "device": device,
        "device_info": device_info,
        "versions": _versions(),
        "weights": weights,
        "totalsegmentator": ts_run,
        "input": {"ct_nii": str(ct_path), "ct_nii_sha256": _sha256(ct_path)},
        "grid": {"totalseg_matches_ct_grid": bool(same_grid), "label_map_source": label_map_source,
                 "regrid": regrid},
        "organs_found": {o["key"]: o["voxels"] for o in found},
        "organs_below_min_voxels": below,
        "meshes": mesh_info,
        "timings_s": {k: round(v, 3) for k, v in timings.items()},
        "timing_note": "functional run on a shared host; not a benchmark",
        "host_start": host_start,
        "host_end": _host(),
        "hashes_sha256": hashes,
    }
    record_path.write_text(json.dumps(record, indent=1, default=str) + "\n")

    return {
        "case": case_id,
        "out_dir": str(out),
        "device": device,
        "reused_totalseg": bool(reuse and ts_run.get("reused")),
        "n_organs": len(found),
        "organs": {o["key"]: o["voxels"] for o in found},
        "organs_below_min_voxels": below,
        "regrid": regrid["mode"],
        "mesh_triangles": mesh_info["n_tris_total"],
        "weights_downloaded_now": [w["task_id"] for w in (weights or []) if w.get("downloaded_now")],
        "timings_s": record["timings_s"],
        "loadavg_start": host_start["loadavg"],
        "hashes_sha256": hashes,
        "paths": {"labels": str(out / "labels.npy"), "organs": str(out / "organs.json"),
                  "meshes_bin": str(out / "meshes.bin"), "meshes_json": str(out / "meshes.json"),
                  "record": str(record_path)},
    }
