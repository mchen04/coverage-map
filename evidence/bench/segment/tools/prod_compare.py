"""Compare production seg (regenerated) with the pre-S2 copy: hashes, voxel diffs, per-organ Dice."""
import json, sys, os
sys.path.insert(0, os.getcwd())
import numpy as np
from covmap.paths import case_dir
out = {}
for case in ("lidc-0001", "pancreas-0051"):
    old_dir, new_dir = case_dir(case) / sys.argv[1], case_dir(case) / "seg"
    old, new = np.load(old_dir / "labels.npy"), np.load(new_dir / "labels.npy")
    ho = json.loads((old_dir / "segment.json").read_text())["hashes_sha256"]
    hn = json.loads((new_dir / "segment.json").read_text())["hashes_sha256"]
    oo = json.loads((old_dir / "organs.json").read_text())
    on = json.loads((new_dir / "organs.json").read_text())
    ids = sorted({o["id"] for o in oo["organs"]} | {o["id"] for o in on["organs"]})
    key = {o["id"]: o["key"] for o in oo["organs"] + on["organs"]}
    dice = {}
    for i in ids:
        a, b = old == i, new == i
        s = int(a.sum()) + int(b.sum())
        dice[key[i]] = 1.0 if s == 0 else 2.0 * int((a & b).sum()) / s
    changed = {}
    for o_old in oo["organs"]:
        o_new = next((o for o in on["organs"] if o["id"] == o_old["id"]), None)
        if o_new != o_old:
            changed[o_old["key"]] = {k: [o_old[k], o_new[k] if o_new else None] for k in o_old
                                     if o_new is None or o_old[k] != o_new[k]}
    out[case] = {"hash_equal": {k: ho[k] == hn[k] for k in ho}, "old_hashes": ho, "new_hashes": hn,
                 "diff_voxels": int((old != new).sum()), "organ_set_equal": [o["key"] for o in oo["organs"]] == [o["key"] for o in on["organs"]],
                 "label_source": [oo["label_source"], on["label_source"]],
                 "dice_min": min(dice.values()), "dice_below_1": {k: round(v, 7) for k, v in dice.items() if v < 1},
                 "organs_json_changed_fields": changed}
print(json.dumps(out, indent=1))
