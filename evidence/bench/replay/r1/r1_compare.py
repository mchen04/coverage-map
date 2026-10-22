"""Compare the R1 bench with round 8 (fingerprints) and the C12 bench (timings)."""
import json
import sys

new = json.load(open(sys.argv[1]))
old = json.load(open(sys.argv[2]))
prev = json.load(open(sys.argv[3]))
ok = True
for case, sc in new["cases"].items():
    for name, r in sc.items():
        if not isinstance(r, dict) or "fingerprint" not in r:
            continue
        o, p = old["cases"][case][name], prev["cases"][case][name]
        same = ((r["fingerprint"], r["live_fingerprint"]) == (o["fingerprint"], o["live_fingerprint"])
                and r["live_final_matches_replay"])
        ok &= same
        print(case, name, same, "warm", p["warm_ms"]["median"], "->", r["warm_ms"]["median"],
              "live", p["live_post_ms"]["median"], "->", r["live_post_ms"]["median"])
for case, v in new["http"]["cases"].items():
    f, g = v["full_read"], prev["http"]["cases"][case]["full_read"]
    print("http", case, "rtt", g["rtt_ms"]["median"], "->", f["rtt_ms"]["median"],
          "score", g["server_score_ms"]["median"], "->", f["server_score_ms"]["median"],
          f["fingerprint_matches_replay"], f["fingerprint"] == old["cases"][case]["full_read"]["fingerprint"])
print("all fingerprints identical:", ok, "load", new["context"]["loadavg_1_5_15"],
      new["context_end"]["loadavg_1_5_15"])
