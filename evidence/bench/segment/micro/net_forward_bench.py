"""S2 micro-benchmark: one TotalSegmentator (task 291) 3d_fullres forward on a real 128^3 CT patch.

Times the network forward per conv3d implementation / budget / thread count / device and
compares logits with a reference (bitwise, max abs diff, argmax disagreement).
Run in a fresh process per thread count (NNPACK's thread pool is sized on first use).
"""
import argparse, json, os, sys, time
sys.path.insert(0, os.getcwd())
import numpy as np
import torch

ap = argparse.ArgumentParser()
ap.add_argument("--threads", type=int, required=True)
ap.add_argument("--device", default="cpu")
ap.add_argument("--configs", default="sum2d:256,sum2d_t:256,fold2d:256,chunk3d:256")
ap.add_argument("--reps", type=int, default=2)
ap.add_argument("--ref", required=True)
ap.add_argument("--case", default="lidc-0001")
ap.add_argument("--out", required=True)
a = ap.parse_args()
torch.set_num_threads(a.threads)

from covmap.paths import case_dir
from covmap.segment import decomposed_conv3d, prepare_totalseg
prepare_totalseg()
from nnunetv2.inference.predict_from_raw_data import nnUNetPredictor
from totalsegmentator.config import get_weights_dir
import nibabel as nib
from scipy import ndimage

folder = next(get_weights_dir().glob("Dataset291_*")) / "nnUNetTrainerNoMirroring__nnUNetPlans__3d_fullres"
plans = json.loads((folder / "plans.json").read_text())
props = plans["foreground_intensity_properties_per_channel"]["0"]
dev = torch.device(a.device)
pred = nnUNetPredictor(tile_step_size=0.5, use_gaussian=True, use_mirroring=False,
                       perform_everything_on_device=True, device=dev, verbose=False, allow_tqdm=False)
pred.initialize_from_trained_model_folder(str(folder), use_folds=[0], checkpoint_name="checkpoint_final.pth")
net = pred.network.to(dev).eval()

img = nib.as_closest_canonical(nib.load(str(case_dir(a.case) / "ct.nii.gz")))
arr = np.asarray(img.dataobj, dtype=np.float32).transpose(2, 1, 0)  # (z, y, x) like SimpleITK
sp = np.array(img.header.get_zooms()[:3])[::-1]
arr = ndimage.zoom(arr, sp / 1.5, order=1)
c = [s // 2 for s in arr.shape]
patch = np.full((128, 128, 128), arr.min(), np.float32)
sl = tuple(slice(max(0, ci - 64), max(0, ci - 64) + min(128, s)) for ci, s in zip(c, arr.shape))
src = arr[sl]
patch[:src.shape[0], :src.shape[1], :src.shape[2]] = src
patch = (np.clip(patch, props["percentile_00_5"], props["percentile_99_5"]) - props["mean"]) / max(props["std"], 1e-8)
x = torch.from_numpy(patch)[None, None].to(dev)

def sync():
    if dev.type == "mps":
        torch.mps.synchronize()

results = []
ref = np.load(a.ref) if os.path.exists(a.ref) else None
for cfg in a.configs.split(","):
    mode, budget = cfg.split(":")
    budget = int(budget)
    times, rss = [], None
    ctx = decomposed_conv3d(mode, budget, a.device) if mode != "native" else None
    with torch.no_grad(), (ctx if ctx is not None else torch.no_grad()) as rec:
        for r in range(a.reps + 1):
            sync(); t = time.perf_counter()
            y = net(x)
            sync(); dt = time.perf_counter() - t
            if r:
                times.append(round(dt, 3))
        logits = y.float().cpu().numpy()[0]
    import resource
    res = {"config": cfg, "device": a.device, "threads": a.threads, "times_s": times,
           "median_s": float(np.median(times)), "warmup_excluded": 1,
           "calls": dict(rec["calls"]) if isinstance(rec, dict) else None,
           "loadavg": [round(v, 2) for v in os.getloadavg()],
           "max_rss_gb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e9, 2)}
    if ref is None:
        np.save(a.ref, logits)
        ref = logits
        res["reference"] = "this run (written to --ref)"
    else:
        am, bm = logits.argmax(0), ref.argmax(0)
        res["bitwise_equal_logits"] = bool(np.array_equal(logits, ref))
        res["max_abs_diff_logits"] = float(np.abs(logits - ref).max())
        res["argmax_disagree_voxels"] = int((am != bm).sum())
        res["argmax_voxels"] = int(am.size)
    print(json.dumps(res), flush=True)
    results.append(res)
os.makedirs(os.path.dirname(a.out), exist_ok=True)
json.dump({"script": "net_forward_bench.py", "case": a.case, "patch": "center 128^3 at 1.5 mm, task 291 normalization",
           "torch": torch.__version__, "results": results}, open(a.out, "w"), indent=1)
