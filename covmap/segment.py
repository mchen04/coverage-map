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


CONV3D_ENV = "COVMAP_SEG_CONV3D"  # overrides the conv3d implementation (benchmarks)
CONV3D_BUDGET_ENV = "COVMAP_SEG_CONV3D_BUDGET_MB"
CONV3D_DEFAULT = "chunk3d"  # CPU; S2 round 2: faster than sum2d on both cases, lower peak RSS
# Per-device defaults. CPU without MKLDNN: torch's slow_conv3d needs ~14.5 GB buffers.
# MPS (torch 2.14): native 3x3x3 Conv3d returns wrong values; conv2d is correct.
CONV3D_DEFAULTS = {"cpu": CONV3D_DEFAULT, "mps": "sum2d_t"}
CONV3D_MPS_MODES = ("sum2d", "sum2d_t", "fold2d")  # chunk3d/native would call MPS conv3d
CONV3D_BUDGET_MB_DEFAULT = 256
# TotalSegmentator worker counts (resampling / nnU-Net preprocessing, NIfTI export pool).
TS_WORKER_ENV = {"nr_thr_resamp": "COVMAP_SEG_NR_THR_RESAMP", "nr_thr_saving": "COVMAP_SEG_NR_THR_SAVING"}
CONV3D_MODES = {
    "sum2d": "conv3d as a sum over kernel depth of batched conv2d",
    "sum2d_t": "conv3d as a sum over kernel depth of batched conv2d on a depth-major input copy",
    "fold2d": "conv3d as one batched conv2d with kernel depth folded into input channels",
    "chunk3d": "native conv3d on overlapping depth chunks (bounded vol2col buffer)",
    "native": "native torch conv3d (no patch)",
}


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


def _depth_major(x, pd: int):
    """``[n, c, d, h, w]`` -> zero-padded (along depth) contiguous ``[n, d + 2*pd, c, h, w]``."""
    n, c, d, h, w = x.shape
    xt = x.new_zeros((n, d + 2 * pd, c, h, w))
    xt[:, pd:pd + d] = x.transpose(1, 2)
    return xt


def conv3d_sum2d_t(x, weight, bias, stride, padding, budget_bytes: int = 256 << 20):
    """Same arithmetic as :func:`conv3d_by_conv2d` (identical conv2d calls and summation
    order), but the input is copied once into depth-major layout so each depth tap is a
    contiguous view, and the output is written in place instead of concatenated."""
    import torch.nn.functional as F

    (sd, sh, sw), (pd, ph, pw) = stride, padding
    o, c, kd, kh, kw = weight.shape
    xt = _depth_major(x, pd)
    n, d, _, h, w = xt.shape
    d_out = (d - kd) // sd + 1
    # Keep the strided weight views of sum2d: for C_in == 1 a contiguous tap would make
    # torch pick a different conv2d backend (Winograd3x3Depthwise instead of NNPACK).
    taps = [weight[:, :, i] for i in range(kd)]
    chunk = max(1, budget_bytes // max(1, n * c * kh * kw * h * w * x.element_size()))
    out = None
    for z0 in range(0, d_out, chunk):
        z1 = min(d_out, z0 + chunk)
        m = z1 - z0
        acc = None
        for i in range(kd):
            sl = xt[:, z0 * sd + i:(z1 - 1) * sd + i + 1:sd].reshape(n * m, c, h, w)
            y = F.conv2d(sl, taps[i], None, (sh, sw), (ph, pw))
            acc = y if acc is None else acc.add_(y)
        if out is None:
            out = x.new_empty((n, o, d_out, *acc.shape[2:]))
        out[:, :, z0:z1] = acc.reshape(n, m, *acc.shape[1:]).transpose(1, 2)
    if bias is not None:
        out.add_(bias.view(1, -1, 1, 1, 1))
    return out


def conv3d_fold2d(x, weight, bias, stride, padding, budget_bytes: int = 256 << 20):
    """3D convolution as one 2D convolution per depth chunk: the ``kd`` input slices of
    each output slice are stacked as ``kd*C_in`` channels (exact up to float summation order)."""
    import torch.nn.functional as F

    (sd, sh, sw), (pd, ph, pw) = stride, padding
    o, c, kd, kh, kw = weight.shape
    xt = _depth_major(x, pd)
    n, d, _, h, w = xt.shape
    d_out = (d - kd) // sd + 1
    wf = weight.transpose(1, 2).reshape(o, kd * c, kh, kw)  # channel index = tap * C_in + c
    win = xt.unfold(1, kd, sd)  # [n, d_out, c, h, w, kd]
    chunk = max(1, budget_bytes // max(1, n * kd * c * kh * kw * h * w * x.element_size()))
    out = None
    for z0 in range(0, d_out, chunk):
        z1 = min(d_out, z0 + chunk)
        m = z1 - z0
        xs = win[:, z0:z1].permute(0, 1, 5, 2, 3, 4).reshape(n * m, kd * c, h, w)
        y = F.conv2d(xs, wf, None, (sh, sw), (ph, pw))
        if out is None:
            out = x.new_empty((n, o, d_out, *y.shape[2:]))
        out[:, :, z0:z1] = y.reshape(n, m, *y.shape[1:]).transpose(1, 2)
    if bias is not None:
        out.add_(bias.view(1, -1, 1, 1, 1))
    return out


def conv3d_chunk3d(x, weight, bias, stride, padding, budget_bytes: int = 256 << 20):
    """Native ``F.conv3d`` on overlapping depth chunks so its vol2col buffer stays bounded."""
    import torch.nn.functional as F

    (sd, sh, sw), (pd, ph, pw) = stride, padding
    o, c, kd, kh, kw = weight.shape
    x = F.pad(x, (0, 0, 0, 0, pd, pd))
    n, _, d, h, w = x.shape
    d_out = (d - kd) // sd + 1
    chunk = max(1, budget_bytes // max(1, n * c * kd * kh * kw * h * w * x.element_size()))
    if chunk >= d_out:
        return F.conv3d(x, weight, bias, stride, (0, ph, pw))
    out = None
    for z0 in range(0, d_out, chunk):
        z1 = min(d_out, z0 + chunk)
        y = F.conv3d(x[:, :, z0 * sd:(z1 - 1) * sd + kd], weight, bias, stride, (0, ph, pw))
        if out is None:
            out = x.new_empty((n, o, d_out, *y.shape[3:]))
        out[:, :, z0:z1] = y
    return out


CONV3D_FUNCS = {"sum2d": conv3d_by_conv2d, "sum2d_t": conv3d_sum2d_t, "fold2d": conv3d_fold2d,
                "chunk3d": conv3d_chunk3d}


def conv3d_config(device: str = "cpu", mode: str | None = None, budget_mb: int | None = None) -> dict:
    """The conv3d implementation for ``device``: arguments, else environment, else defaults.

    ``native`` on a CPU torch build with MKLDNN (fast native conv3d there).
    """
    import torch

    source = "argument" if mode else "environment" if os.environ.get(CONV3D_ENV) else "default"
    mode = mode or os.environ.get(CONV3D_ENV)
    if not mode:
        if device == "cpu" and torch.backends.mkldnn.is_available():
            mode = "native"
        else:
            mode = CONV3D_DEFAULTS.get(device, CONV3D_DEFAULT)
    if mode not in CONV3D_MODES:
        raise ValueError(f"{CONV3D_ENV}={mode!r}: expected one of {sorted(CONV3D_MODES)}")
    if device == "mps" and mode not in CONV3D_MPS_MODES:
        raise ValueError(f"conv3d mode {mode!r} would run MPS Conv3d, which is wrong in this torch; "
                         f"use one of {CONV3D_MPS_MODES}")
    budget_mb = int(budget_mb or os.environ.get(CONV3D_BUDGET_ENV) or CONV3D_BUDGET_MB_DEFAULT)
    if budget_mb < 1:
        raise ValueError(f"{CONV3D_BUDGET_ENV} must be >= 1")
    return {"mode": mode, "budget_mb": budget_mb, "source": source}


def _conv3d_self_check(fn, budget_bytes: int, device: str = "cpu") -> float:
    """Max relative error of ``fn`` vs CPU ``F.conv3d`` on a small strided/padded case."""
    import torch
    import torch.nn.functional as F

    g = torch.Generator().manual_seed(0)
    x, w, b = (torch.randn(*s, generator=g) for s in ((1, 8, 9, 10, 11), (6, 8, 3, 3, 3), (6,)))
    with torch.no_grad():
        ref = F.conv3d(x, w, b, (2, 1, 1), 1)
        out = fn(x.to(device), w.to(device), b.to(device), (2, 1, 1), (1, 1, 1), budget_bytes).cpu()
    return float((out - ref).abs().max() / ref.abs().max())


@contextlib.contextmanager
def decomposed_conv3d(mode: str = CONV3D_DEFAULT, budget_mb: int = CONV3D_BUDGET_MB_DEFAULT,
                      device: str = "cpu"):
    """Route eligible ``nn.Conv3d`` calls on ``device`` through the ``mode`` implementation.

    Yields a record (mode, self-check error, call counts) that is filled in while active.
    """
    import torch

    fn, budget = CONV3D_FUNCS[mode], budget_mb << 20
    original = torch.nn.Conv3d._conv_forward
    calls = {"decomposed": 0, "native": 0}

    def forward(self, input, weight, bias):
        if (input.device.type == device and self.groups == 1 and tuple(self.dilation) == (1, 1, 1)
                and self.padding_mode == "zeros" and not isinstance(self.padding, str)):
            calls["decomposed"] += 1
            if "torch_threads_at_first_call" not in record:  # threads in effect during inference
                record["torch_threads_at_first_call"] = torch.get_num_threads()
            return fn(input, weight, bias, tuple(self.stride), tuple(self.padding), budget)
        if input.device.type == "mps":  # native MPS Conv3d is numerically wrong (see above)
            raise RuntimeError(
                f"Conv3d (groups={self.groups}, dilation={tuple(self.dilation)}, padding={self.padding!r}, "
                f"padding_mode={self.padding_mode!r}) cannot use the decomposed path on MPS; "
                "rerun with --device cpu")
        calls["native"] += 1
        return original(self, input, weight, bias)

    err = _conv3d_self_check(fn, budget, device)
    if err > 1e-4:
        raise RuntimeError(f"conv3d {mode} self-check on {device} failed (rel err {err:g})")
    record = {"mode": mode, "description": CONV3D_MODES[mode], "device": device, "budget_mb": budget_mb,
              "self_check_rel_err": err, "calls": calls}
    torch.nn.Conv3d._conv_forward = forward
    try:
        yield record
    finally:
        torch.nn.Conv3d._conv_forward = original


def lowmem_cpu_conv3d():
    """The default CPU patch for torch builds without MKLDNN."""
    return decomposed_conv3d(CONV3D_DEFAULTS["cpu"], CONV3D_BUDGET_MB_DEFAULT, "cpu")


def mps_self_test(conv: dict) -> dict:
    """An nnU-Net-like block (strided conv, instance norm, transposed conv, 1x1x1 head) on
    MPS with the decomposed conv3d vs native CPU; also records what native MPS Conv3d gives."""
    import copy

    import torch
    from torch import nn

    with torch.random.fork_rng(devices=[]):
        torch.manual_seed(0)
        net = nn.Sequential(
            nn.Conv3d(2, 8, 3, padding=1), nn.InstanceNorm3d(8, affine=True), nn.LeakyReLU(0.01),
            nn.Conv3d(8, 16, 3, stride=2, padding=1), nn.InstanceNorm3d(16, affine=True), nn.LeakyReLU(0.01),
            nn.ConvTranspose3d(16, 8, 2, 2), nn.Conv3d(8, 4, 1)).eval()
        for m in net:
            if isinstance(m, nn.InstanceNorm3d):
                nn.init.normal_(m.weight)
                nn.init.normal_(m.bias)
        x = torch.randn(1, 2, 24, 24, 24)

    def rel(out, ref):
        return float((out - ref).abs().max() / ref.abs().max())

    with torch.no_grad():
        ref = net(x)
        on_mps = copy.deepcopy(net).to("mps")
        native = on_mps(x.to("mps")).cpu()
        with decomposed_conv3d(conv["mode"], conv["budget_mb"], "mps") as rec:
            out = on_mps(x.to("mps")).cpu()
    return {"conv3d_mode": conv["mode"], "rel_err": rel(out, ref),
            "native_mps_conv3d_rel_err": rel(native, ref), "decomposed_calls": rec["calls"]["decomposed"]}


def check_device(device: str, conv: dict | None = None) -> dict:
    """Validate the requested torch device; on MPS the decomposed network path must match CPU."""
    import torch

    if device == "cpu":
        return {"device": "cpu", "torch_threads_before": torch.get_num_threads()}
    if device != "mps":
        raise ValueError(f"device must be 'cpu' or 'mps', got {device!r}")
    if not torch.backends.mps.is_available():
        raise RuntimeError(f"MPS requested but unavailable (built={torch.backends.mps.is_built()}); "
                           "rerun with --device cpu")
    conv = conv or conv3d_config("mps")
    try:
        test = mps_self_test(conv)
    except Exception as e:  # noqa: BLE001 - report the device failure verbatim
        raise RuntimeError(f"MPS self-test raised {type(e).__name__}: {e}") from e
    if not test["rel_err"] <= 1e-4:
        raise RuntimeError(f"MPS self-test differs from CPU: max rel err {test['rel_err']:.3g}; "
                           "TotalSegmentator would produce wrong labels on this device. Use --device cpu.")
    return {"device": "mps", "torch_threads_before": torch.get_num_threads(), "self_test": test}


def select_device(device: str) -> tuple[str, dict, dict]:
    """Resolve ``auto`` (MPS when available and its self-test passes, else CPU) and validate.

    Returns ``(device, conv3d config, device info)``.
    """
    import torch

    if device != "auto":
        conv = conv3d_config(device)
        return device, conv, check_device(device, conv)
    if torch.backends.mps.is_available():
        try:
            conv = conv3d_config("mps")
            return "mps", conv, {**check_device("mps", conv), "requested": "auto"}
        except (RuntimeError, ValueError) as e:
            fallback = f"{type(e).__name__}: {e}"
    else:
        fallback = "MPS unavailable"
    conv = conv3d_config("cpu")
    return "cpu", conv, {**check_device("cpu", conv), "requested": "auto", "auto_fallback": fallback}


def run_totalsegmentator(ct_path: Path, out_path: Path, fast: bool, device: str,
                         roi_classes: list[str] | None, threads: int | None, conv: dict | None = None) -> dict:
    """Run the Python API into a temp dir and move the multilabel file into place."""
    import torch
    from totalsegmentator.python_api import totalsegmentator

    conv = conv or conv3d_config(device)
    tmp = out_path.parent / ".totalseg_tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    kwargs = dict(input=str(ct_path), output=str(tmp / "totalseg.nii.gz"), ml=True, fast=fast,
                  device=device, roi_subset=roi_classes, task="total", quiet=False)
    for key, env in TS_WORKER_ENV.items():  # TotalSegmentator's own defaults unless overridden
        if os.environ.get(env):
            kwargs[key] = int(os.environ[env])
    # On CPU TotalSegmentator sets torch threads to multiprocessing.cpu_count() and nnU-Net then
    # caps them at nnUNet_def_n_proc (8) while predicting; on MPS they stay at torch's default
    # (they drive the CPU-side logit accumulation). --threads sets the starting count on both.
    cpu_patch = (mock.patch("multiprocessing.cpu_count", return_value=threads)
                 if threads else contextlib.nullcontext())
    if threads:
        torch.set_num_threads(threads)
    conv_patch = (decomposed_conv3d(conv["mode"], conv["budget_mb"], device)
                  if conv["mode"] != "native" else contextlib.nullcontext())
    t = time.perf_counter()
    with stdout_to_stderr(), cpu_patch, conv_patch as conv_record:
        totalsegmentator(**kwargs)
    wall = time.perf_counter() - t
    (tmp / "totalseg.nii.gz").replace(out_path)
    shutil.rmtree(tmp, ignore_errors=True)
    shown = {k: v for k, v in kwargs.items() if k not in ("input", "output")}
    import resource

    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss  # bytes on macOS, KiB on Linux
    rss_gb = rss / 1e9 if sys.platform == "darwin" else rss * 1024 / 1e9
    out = {"call": "totalsegmentator.python_api.totalsegmentator", "kwargs": shown,
           "threads": threads,
           # TotalSegmentator restores the thread count on return; this is the value during inference
           "torch_threads": (conv_record or {}).get("torch_threads_at_first_call"),
           "conv3d": {**(conv_record or {"mode": "native", "device": device}), "source": conv["source"]},
           "wall_s": wall, "process_max_rss_gb": round(rss_gb, 2)}
    if device == "mps":
        out["mps_driver_allocated_gb"] = round(torch.mps.driver_allocated_memory() / 1e9, 2)
    return out


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


class SegmentError(RuntimeError):
    """A segmentation request that cannot be honoured as asked (e.g. an unsafe ``--reuse``)."""


def _label_source(ts_version: str | None, fast: bool, roi_subset: bool) -> str:
    return (f"TotalSegmentator {ts_version} total" + (" (fast 3 mm)" if fast else "")
            + (" roi_subset" if roi_subset else ""))


def reuse_provenance(previous: dict, record_path: Path, ct_sha: str, fast: bool, roi_subset: bool) -> dict:
    """Check that an existing ``totalseg.nii.gz`` may be reused for this request.

    It must come from the current ``ct.nii.gz`` and the same ``fast``/``roi_subset`` flags
    (per the record of the run that produced it). Returns that run's provenance (options,
    versions, input, label_source), which a reuse run carries forward unchanged.
    """
    if not previous:
        raise SegmentError(f"{record_path} missing: no record of the run that produced totalseg.nii.gz; "
                           "rerun without --reuse")
    prev_input = previous.get("input") or {}
    if prev_input.get("ct_nii_sha256") != ct_sha:
        raise SegmentError(f"--reuse refused: ct.nii.gz (sha256 {ct_sha}) is not the CT that produced "
                           f"totalseg.nii.gz (sha256 {prev_input.get('ct_nii_sha256')}); rerun without --reuse")
    opts = previous.get("options") or {}
    ts = previous.get("totalsegmentator") or {}
    while ts.get("reused"):
        ts = ts.get("previous") or {}
    kwargs = ts.get("kwargs") or {}
    for key, want in (("fast", fast), ("roi_subset", roi_subset)):
        produced = {bool(opts[key])} if key in opts else set()
        if key in kwargs:  # what TotalSegmentator was actually called with
            produced.add(bool(kwargs[key]))
        if produced != {bool(want)}:
            raise SegmentError(f"--reuse refused: requested {key}={bool(want)} but totalseg.nii.gz was produced "
                               f"with {key}={'/'.join(map(str, sorted(produced))) or 'unknown'}; "
                               "rerun without --reuse or with the producing flags")
    versions = previous.get("versions") or {}
    return {"options": opts, "versions": versions, "input": prev_input,
            "label_source": previous.get("label_source")
            or _label_source(versions.get("TotalSegmentator"), bool(fast), bool(roi_subset))}


def segment_case(case_id: str, fast: bool = False, device: str = "auto", roi_subset: bool = False,
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
    requested = device
    record_path = out / "segment.json"
    previous = json.loads(record_path.read_text()) if record_path.exists() else {}
    ct_sha = _sha256(ct_path)
    conv = device_info = produced = None
    # fail before touching outputs
    if run_ts:
        device, conv, device_info = select_device(device)
    else:
        produced = reuse_provenance(previous, record_path, ct_sha, fast, roi_subset)
    out.mkdir(parents=True, exist_ok=True)

    options = {"task": "total", "fast": fast, "device": device, "device_requested": requested,
               "roi_subset": roi_subset, "threads": threads, "conv3d": conv, "reuse": reuse,
               "out_name": out_name, "min_voxels": min_voxels}
    host_start = _host()
    timings: dict[str, float] = {}
    roi_classes = organs.catalog_classes() if roi_subset else None

    if produced is not None:
        original = previous.get("totalsegmentator")
        while original and original.get("reused"):  # keep the record of the run that made the file
            original = original.get("previous")
        ts_run = {"reused": True, "previous": original}
        device_info = previous.get("device_info")
        device = previous.get("device", device)  # the device that produced the reused file
        weights = previous.get("weights")
    else:
        t = time.perf_counter()
        weights = ensure_weights(weight_task_ids(fast, roi_subset))
        timings["weights_check_s"] = time.perf_counter() - t  # excluded from the TS wall time
        ts_run = run_totalsegmentator(ct_path, ts_path, fast, device, roi_classes, threads, conv)
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
    label_source = (produced["label_source"] if produced is not None
                    else _label_source(_versions()["TotalSegmentator"], fast, roi_subset))
    organs_doc = {
        "case": case_id,
        "label_source": label_source,
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
        # on reuse: options, versions, input and label_source of the run that produced totalseg.nii.gz
        "options": produced["options"] if produced is not None else options,
        "device": device,
        "device_info": device_info,
        "versions": produced["versions"] if produced is not None else _versions(),
        "weights": weights,
        "totalsegmentator": ts_run,
        "input": produced["input"] if produced is not None else {"ct_nii": str(ct_path), "ct_nii_sha256": ct_sha},
        "label_source": label_source,
        "reuse": ({"reused_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "reused_by_versions": _versions(),
                   "request": options} if produced is not None else None),
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
        "totalsegmentator": ({"reused": True} if ts_run.get("reused") else {
            k: ts_run.get(k) for k in ("wall_s", "threads", "torch_threads", "conv3d", "process_max_rss_gb",
                                       "mps_driver_allocated_gb", "loadavg_after") if k in ts_run}),
        "loadavg_start": host_start["loadavg"],
        "hashes_sha256": hashes,
        "paths": {"labels": str(out / "labels.npy"), "organs": str(out / "organs.json"),
                  "meshes_bin": str(out / "meshes.bin"), "meshes_json": str(out / "meshes.json"),
                  "record": str(record_path)},
    }
