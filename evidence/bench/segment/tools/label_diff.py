"""Voxel-level label differences of bench segmentations vs production seg (per case)."""
import json, sys, os
sys.path.insert(0, os.getcwd())
import numpy as np
from covmap.paths import case_dir
from covmap import organs
out = {}
for case in ("lidc-0001", "pancreas-0051"):
    ref = np.load(case_dir(case) / "seg" / "labels.npy")
    for name in sys.argv[1:]:
        p = case_dir(case) / name / "labels.npy"
        if not p.exists():
            continue
        new = np.load(p)
        diff = ref != new
        n = int(diff.sum())
        pairs = {}
        if n:
            a, b = ref[diff], new[diff]
            u, c = np.unique(a.astype(np.int32) * 256 + b, return_counts=True)
            for k, v in sorted(zip(u.tolist(), c.tolist()), key=lambda t: -t[1])[:8]:
                ka, kb = divmod(k, 256)
                pairs[f"{organs.BY_ID[ka].key if ka else 'bg'}->{organs.BY_ID[kb].key if kb else 'bg'}"] = v
        out[f"{case}/{name}"] = {"diff_voxels": n, "labeled_voxels_ref": int((ref > 0).sum()),
                                 "frac_of_labeled": round(n / max(1, int((ref > 0).sum())), 7), "top_pairs": pairs}
print(json.dumps(out, indent=1))
