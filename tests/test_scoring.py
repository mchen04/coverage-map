"""Scoring rules (docs/SPEC.md "Scoring") on synthetic cases; expectations by direct counting."""

import json
import math

import numpy as np
import pytest

from covmap.casedata import Geometry, load_case
from covmap.events import EventError
from covmap.scoring import Scorer, replay_file, resolve_view, timeline
from synth import (annotation, build_case, end, finish, hidden, read_pass, stack_view, view_krc,
                   world)


@pytest.fixture(scope="module", params=["standard", "flipped"])
def case(request, tmp_path_factory):
    shape = (40, 96, 96) if request.param == "standard" else (36, 80, 96)
    d = build_case(tmp_path_factory.mktemp("cases"), f"synth-{request.param}", shape=shape,
                   orientation=request.param)
    return load_case(d)


@pytest.fixture
def vol(case):
    return case.volume


def organ_count(case, key, sl=np.s_[:]):
    return int(np.count_nonzero(case.labels[sl] == case.organ(key)["id"]))


def by_key(cov):
    return {o["key"]: o for o in cov["organs"]}


def busiest_slice(case, key):
    return int(np.argmax((case.labels == case.organ(key)["id"]).sum(axis=(1, 2))))


# ---- geometry -----------------------------------------------------------------------
@pytest.mark.parametrize("orientation", ["standard", "flipped", "mirrored"])
def test_world_voxel_roundtrip_matches_spec_and_affine(tmp_path, orientation):
    d = build_case(tmp_path, "g", shape=(12, 20, 24), orientation=orientation, organs=["liver"])
    v = json.loads((d / "volume.json").read_text())
    g = Geometry.from_volume(v)
    rng = np.random.default_rng(1)
    krc = rng.uniform(-1, 20, size=(50, 3))
    w_spec = np.array([world(v, *p) for p in krc])
    aff = np.asarray(v["affine_lps"])
    w_aff = (aff @ np.c_[krc[:, 2], krc[:, 1], krc[:, 0], np.ones(50)].T).T[:, :3]
    assert np.allclose(g.voxel_to_world(krc), w_spec)
    assert np.allclose(w_spec, w_aff)
    assert np.allclose(g.world_to_voxel(w_spec), krc)


# ---- basic rules --------------------------------------------------------------------
def test_full_soft_read_reviews_soft_organs_only(case, vol):
    K = case.shape[0]
    cov = Scorer(case).add(read_pass(vol, range(K), 0, 0, "soft") + [end(vol, K * 150, K)])
    o = by_key(cov)
    for key, org in o.items():
        windows = case.organ(key)["windows"]
        if "soft" in windows:
            assert org["status"] == "reviewed" and org["coverage"] == 1.0, key
        else:
            assert org["status"] == "skipped" and org["unseen"]["wrong_window"] == 1.0, key
    assert o["trachea"]["status"] == "reviewed"  # trachea qualifies with soft or lung
    assert cov["baseline"]["complete"] and cov["baseline"]["unviewed_ranges"] == []
    assert cov["events"]["views"] == K and cov["events"]["sources"] == {"simulated": K + 1}
    assert cov["provisional"] is False and cov["summary"]["organs"] == len(case.organs)


def test_three_window_read_reviews_everything(case, vol):
    K = case.shape[0]
    evs = []
    for i, w in enumerate(("soft", "lung", "bone")):
        evs += read_pass(vol, range(K), i * K * 150, i * K, w)
    evs.append(end(vol, 3 * K * 150, 3 * K))
    cov = Scorer(case).add(evs)
    assert cov["summary"]["reviewed"] == len(case.organs)
    assert cov["summary"]["voxel_coverage"] == 1.0


def test_other_window_qualifies_nothing(case, vol):
    K = case.shape[0]
    cov = Scorer(case).add(read_pass(vol, range(K), 0, 0, "brain") + [end(vol, K * 150, K)])
    for o in cov["organs"]:
        assert o["status"] == "skipped" and o["unseen"]["wrong_window"] == 1.0
    assert cov["baseline"]["complete"]  # the per-image model counts any window


def test_wrong_window_for_lungs(case, vol):
    k = busiest_slice(case, "lung_left")
    cov = Scorer(case).add([stack_view(vol, k, 0, 0, "bone"), end(vol, 500, 1)])
    lung = by_key(cov)["lung_left"]
    frac = organ_count(case, "lung_left", np.s_[k]) / organ_count(case, "lung_left")
    assert lung["seen_voxels"] == 0
    assert lung["unseen"]["wrong_window"] == pytest.approx(frac, abs=1e-6)
    assert lung["unseen"]["never_displayed"] == pytest.approx(1 - frac, abs=1e-6)


@pytest.mark.parametrize("dwells,seen", [((50,), False), ((79.9,), False), ((80,), True),
                                         ((40, 40), True), ((40, 39), False), ((30, 30, 30), True)])
def test_min_dwell_cumulative_across_passes(case, vol, dwells, seen):
    k = busiest_slice(case, "liver")
    evs, t, seq = [], 0.0, 0
    for d in dwells:  # separate passes: the slice is revisited between other slices
        evs += [stack_view(vol, k, t, seq), stack_view(vol, (k + 5) % case.shape[0], t + d, seq + 1, "brain")]
        t, seq = t + d + 500, seq + 2
    evs.append(end(vol, t, seq))
    liver = by_key(Scorer(case).add(evs))["liver"]
    n_k = organ_count(case, "liver", np.s_[k])
    assert liver["seen_voxels"] == (n_k if seen else 0)
    if not seen:
        assert liver["unseen"]["short_dwell"] == pytest.approx(n_k / liver["voxels"], abs=1e-6)


def test_min_dwell_param(case, vol):
    k = busiest_slice(case, "liver")
    evs = [stack_view(vol, k, 0, 0), end(vol, 50, 1)]
    assert by_key(Scorer(case).add(evs))["liver"]["seen_voxels"] == 0
    assert by_key(Scorer(case, min_dwell_ms=50).add(evs))["liver"]["seen_voxels"] > 0


def test_zoomed_rectangle_partial_region(case, vol):
    K, R, C = case.shape
    rect = (10, 10 + R // 2, 5, 5 + R // 2)  # square in pixels (isotropic spacing)
    evs = read_pass(vol, range(K), 0, 0, "soft", rect=rect) + [end(vol, K * 150, K)]
    cov = by_key(Scorer(case).add(evs))
    for key in ("liver", "heart", "spleen"):
        inside = organ_count(case, key, np.s_[:, rect[0]:rect[1], rect[2]:rect[3]])
        total = organ_count(case, key)
        assert cov[key]["seen_voxels"] == inside, key
        assert cov[key]["unseen"]["out_of_region"] == pytest.approx((total - inside) / total, abs=1e-6)


def test_panned_view_beyond_image_is_clipped(case, vol):
    K, R, C = case.shape
    rect = (0, R, 30 - R, 30)  # square view panned so only columns 0..29 are on screen
    cov = by_key(Scorer(case).add(read_pass(vol, range(K), 0, 0, "soft", rect=rect) + [end(vol, K * 150, K)]))
    for key in ("liver", "spleen"):
        assert cov[key]["seen_voxels"] == organ_count(case, key, np.s_[:, :, :30])


def test_rotated_in_plane_view_uses_exact_quad(case, vol):
    k = busiest_slice(case, "liver")
    rc, cc, d = 47.3, 41.7, 30.1  # diamond: |r - rc| + |c - cc| <= d
    corners = [(k, rc - d, cc), (k, rc, cc + d), (k, rc + d, cc), (k, rc, cc - d)]
    ev = view_krc(vol, corners, 0, 0, "soft", vp_type="stack", sop=vol["sop_uids"][k])
    liver = by_key(Scorer(case).add([ev, end(vol, 200, 1)]))["liver"]
    r, c = np.nonzero(case.labels[k] == case.organ("liver")["id"])
    assert liver["seen_voxels"] == int(np.count_nonzero(np.abs(r - rc) + np.abs(c - cc) <= d))


# ---- volume viewports ---------------------------------------------------------------
def test_coronal_plane(case, vol):
    K, R, C = case.shape
    r0 = 50
    corners = [(-0.5, r0, -0.5), (-0.5, r0, C - 0.5), (K - 0.5, r0, C - 0.5), (K - 0.5, r0, -0.5)]
    sc = Scorer(case)
    cov = by_key(sc.add([view_krc(vol, corners, 0, 0, "soft"), end(vol, 100, 1)]))
    for key in ("liver", "aorta", "heart"):
        assert cov[key]["seen_voxels"] == organ_count(case, key, np.s_[:, r0, :]), key
    assert sc.baseline.summary()["viewed"] == 0  # a coronal plane is no acquisition image


def test_sagittal_plane_partial_region(case, vol):
    K, R, C = case.shape
    c0, (k0, k1), (ra, rb) = 48, (5, 25), (20, 70)
    corners = [(k0 - 0.5, ra - 0.5, c0), (k0 - 0.5, rb - 0.5, c0), (k1 - 0.5, rb - 0.5, c0), (k1 - 0.5, ra - 0.5, c0)]
    cov = by_key(Scorer(case).add([view_krc(vol, corners, 0, 0, "bone"), end(vol, 100, 1)]))
    for key in ("thoracic_spine", "lumbar_spine", "sternum"):
        assert cov[key]["seen_voxels"] == organ_count(case, key, np.s_[k0:k1, ra:rb, c0]), key


def test_coronal_sweep_reviews_like_axial(case, vol):
    K, R, C = case.shape
    evs = [view_krc(vol, [(-0.5, r, -0.5), (-0.5, r, C - 0.5), (K - 0.5, r, C - 0.5), (K - 0.5, r, -0.5)],
                    r * 100, r, "soft") for r in range(R)]
    cov = Scorer(case).add(evs + [end(vol, R * 100, R)])
    for o in cov["organs"]:
        assert o["status"] == ("reviewed" if "soft" in o["windows"] else "skipped"), o["key"]


def test_volume_axial_plane_counts_for_baseline(case, vol):
    K, R, C = case.shape
    k = 7
    corners = [(k, -0.5, -0.5), (k, -0.5, C - 0.5), (k, R - 0.5, C - 0.5), (k, R - 0.5, -0.5)]
    sc = Scorer(case)
    sc.add([view_krc(vol, corners, 0, 0, "soft"), end(vol, 100, 1)])
    assert sc.baseline.viewed[k] and sc.baseline.viewed.sum() == 1


def _tilted_axial(case, vol, k, degrees):
    """Corners of an axial plane through slice k rotated about the volume's column axis."""
    K, R, C = case.shape
    g = case.geom
    center = g.voxel_to_world([k, (R - 1) / 2, (C - 1) / 2])
    corners = g.voxel_to_world([(k, -0.5, -0.5), (k, -0.5, C - 0.5), (k, R - 0.5, C - 0.5), (k, R - 0.5, -0.5)])
    axis = np.asarray(vol["row_dir"], float)
    a = math.radians(degrees)
    def rot(p):
        p = p - center
        return center + p * math.cos(a) + np.cross(axis, p) * math.sin(a) + axis * (axis @ p) * (1 - math.cos(a))
    return [rot(p).tolist() for p in corners]


def test_oblique_ignored_and_small_tilt_accepted(case, vol):
    k = busiest_slice(case, "liver")
    base = {"v": 1, "case": vol["case"], "kind": "view", "viewport": "v", "vp_type": "volume",
            "series": vol["series_uid"], "sop": None, "voi": {"wc": 40, "ww": 400}, "source": "ohif"}
    sc = Scorer(case)
    cov = sc.add([{**base, "seq": 0, "t_ms": 0.0, "corners_lps": _tilted_axial(case, vol, k, 10)},
                  end(vol, 200, 1)])
    assert cov["events"]["ignored"]["oblique"] == 1 and by_key(cov)["liver"]["seen_voxels"] == 0
    cov = Scorer(case).add([{**base, "seq": 0, "t_ms": 0.0, "corners_lps": _tilted_axial(case, vol, k, 3)},
                            end(vol, 200, 1)])
    assert cov["events"]["ignored"]["oblique"] == 0
    assert by_key(cov)["liver"]["seen_voxels"] == organ_count(case, "liver", np.s_[k])


# ---- display-state lifecycle --------------------------------------------------------
def test_hidden_and_end_close_states(case, vol):
    k = busiest_slice(case, "liver")
    n_k = organ_count(case, "liver", np.s_[k])
    # hidden after 50 ms: the 10 s the tab stayed hidden must not count
    evs = [stack_view(vol, k, 0, 0), hidden(vol, 50, 1), stack_view(vol, k, 10_000, 2), end(vol, 10_040, 3)]
    assert by_key(Scorer(case).add(evs))["liver"]["seen_voxels"] == n_k  # 50 + 40 = 90 >= 80
    evs = [stack_view(vol, k, 0, 0), hidden(vol, 50, 1), stack_view(vol, k, 10_000, 2), end(vol, 10_020, 3)]
    assert by_key(Scorer(case).add(evs))["liver"]["seen_voxels"] == 0  # 70 < 80
    # a hidden event for another viewport leaves this one open
    evs = [stack_view(vol, k, 0, 0), hidden(vol, 50, 1, viewport="other"), end(vol, 100, 2)]
    assert by_key(Scorer(case).add(evs))["liver"]["seen_voxels"] == n_k


def test_two_viewports_are_independent(case, vol):
    k1 = busiest_slice(case, "liver")
    k2 = busiest_slice(case, "lung_left")
    evs = [stack_view(vol, k1, 0, 0, "soft", viewport="a"), stack_view(vol, k2, 10, 1, "lung", viewport="b"),
           stack_view(vol, k1, 60, 2, "soft", viewport="b"), end(vol, 100, 3)]
    cov = by_key(Scorer(case).add(evs))
    assert cov["liver"]["seen_voxels"] == organ_count(case, "liver", np.s_[k1])  # a: 100 ms
    assert cov["lung_left"]["seen_voxels"] == 0  # b showed it for 50 ms only


def test_open_state_not_credited_without_now(case, vol):
    k = busiest_slice(case, "liver")
    cov = Scorer(case).add([stack_view(vol, k, 0, 0)])
    assert by_key(cov)["liver"]["seen_voxels"] == 0 and cov["events"]["open"] == 1


def test_provisional_now_does_not_mutate(case, vol):
    k = busiest_slice(case, "liver")
    n_k = organ_count(case, "liver", np.s_[k])
    sc = Scorer(case)
    prov = sc.add([stack_view(vol, k, 1000, 0)], now_ms=1100)
    assert prov["provisional"] is True and by_key(prov)["liver"]["seen_voxels"] == n_k
    assert float(sc.qdwell.sum()) == 0.0 and sc.flags.max() == 0
    committed = sc.coverage()
    assert committed["provisional"] is False and by_key(committed)["liver"]["seen_voxels"] == 0
    assert by_key(sc.add([], now_ms=1050))["liver"]["seen_voxels"] == 0  # 50 ms so far
    final = sc.add([end(vol, 1100, 1)])
    assert by_key(final)["liver"]["seen_voxels"] == n_k
    assert by_key(final)["liver"]["unseen"] == by_key(prov)["liver"]["unseen"]


def test_incremental_batches_equal_single_replay(case, vol):
    K = case.shape[0]
    evs = read_pass(vol, range(K), 0, 0, "soft", dwell=60) + read_pass(vol, range(0, K, 2), K * 60, K, "lung", dwell=90)
    evs.append(end(vol, K * 60 + (K // 2 + 1) * 90, 2 * K))
    one = Scorer(case).add(evs)
    sc = Scorer(case)
    for i in range(0, len(evs), 7):
        sc.add(evs[i:i + 7], now_ms=evs[min(i + 7, len(evs) - 1)]["t_ms"])
    inc = sc.coverage()
    assert [o["seen_voxels"] for o in one["organs"]] == [o["seen_voxels"] for o in inc["organs"]]
    assert [o["unseen"] for o in one["organs"]] == [o["unseen"] for o in inc["organs"]]


def test_unknown_images_ignored(case, vol):
    k = busiest_slice(case, "liver")
    evs = [stack_view(vol, k, 0, 0, sop="1.2.3.unknown"), stack_view(vol, k, 100, 1, series="9.9.9"),
           end(vol, 300, 2)]
    sc = Scorer(case)
    cov = sc.add(evs)
    assert cov["events"]["ignored"]["unknown_image"] == 2
    assert by_key(cov)["liver"]["seen_voxels"] == 0 and sc.baseline.viewed.sum() == 0


def test_volume_plane_outside_volume_is_other(case, vol):
    K, R, C = case.shape
    k = K + 3
    corners = [(k, -0.5, -0.5), (k, -0.5, C - 0.5), (k, R - 0.5, C - 0.5), (k, R - 0.5, -0.5)]
    cov = Scorer(case).add([view_krc(vol, corners, 0, 0), end(vol, 100, 1)])
    assert cov["events"]["ignored"]["other"] == 1


def test_duplicate_seq_ignored(case, vol):
    k = busiest_slice(case, "liver")
    ev = stack_view(vol, k, 0, 0)
    sc = Scorer(case)
    sc.add([ev])
    cov = sc.add([ev, end(vol, 100, 1)])
    assert cov["events"]["total"] == 3 and cov["events"]["views"] == 1
    assert cov["events"]["ignored"]["other"] == 1


def test_events_sorted_by_time_then_seq(case, vol):
    k = busiest_slice(case, "liver")
    evs = [end(vol, 100, 2), stack_view(vol, k, 0, 0), stack_view(vol, k + 1, 0, 1)]
    cov = by_key(Scorer(case).add(evs))
    # seq 0 (k) lasts 0 ms, seq 1 (k+1) lasts 100 ms
    assert cov["liver"]["seen_voxels"] == organ_count(case, "liver", np.s_[k + 1])


@pytest.mark.parametrize("bad,msg", [
    ({"kind": "view"}, "version"),
    ({"v": 1, "kind": "glance", "seq": 0, "t_ms": 0, "source": "x"}, "kind"),
    ({"v": 1, "kind": "end", "seq": -1, "t_ms": 0, "source": "x"}, "seq"),
    ({"v": 1, "kind": "end", "seq": 0, "t_ms": float("nan"), "source": "x"}, "t_ms"),
    ({"v": 1, "kind": "end", "seq": 0, "t_ms": 0}, "source"),
    ({"v": 1, "kind": "hidden", "seq": 0, "t_ms": 0, "source": "x"}, "viewport"),
    ({"v": 1, "kind": "annotation", "seq": 0, "t_ms": 0, "source": "x", "organ": "liver", "action": "maybe"}, "action"),
    ({"v": 1, "kind": "finish", "seq": 0, "t_ms": 0, "source": "x"}, "confirmed"),
])
def test_malformed_events_rejected(case, bad, msg):
    with pytest.raises(EventError, match=msg):
        Scorer(case).add([bad])


def test_malformed_view_fields_rejected_without_mutation(case, vol):
    good = stack_view(vol, 3, 0, 0)
    sc = Scorer(case)
    for field, value, msg in [("corners_lps", [[0, 0, 0]] * 3, "corners_lps"), ("voi", {"wc": 40}, "ww"),
                              ("vp_type", "3d", "vp_type"), ("sop", None, "sop")]:
        with pytest.raises(EventError, match=msg):
            sc.add([good, {**good, "seq": 1, field: value}])
    with pytest.raises(EventError, match="case"):
        sc.add([{**good, "case": "another-case"}])
    assert sc.n_total == 0 and not sc.segmenter.open


# ---- unseen reasons -----------------------------------------------------------------
def test_unseen_reason_precedence(case, vol):
    K, R, C = case.shape
    ks = np.flatnonzero((case.labels == case.organ("liver")["id"]).any(axis=(1, 2)))
    k_short, k_wrong, k_region, k_never = (int(x) for x in ks[[0, 1, 2, 3]])
    rect = (0, 4, 0, 4)  # a corner that holds no liver
    evs = [stack_view(vol, k_short, 0, 0, "soft"), stack_view(vol, k_short, 30, 1, "lung"),  # 30 ms soft
           stack_view(vol, k_wrong, 1000, 2, "bone"),
           stack_view(vol, k_region, 2000, 3, "soft", rect=rect), end(vol, 3000, 4)]
    liver = by_key(Scorer(case).add(evs))["liver"]
    n = liver["voxels"]
    expect = {"short_dwell": organ_count(case, "liver", np.s_[k_short]) / n,
              "wrong_window": organ_count(case, "liver", np.s_[k_wrong]) / n,
              "out_of_region": organ_count(case, "liver", np.s_[k_region]) / n}
    expect["never_displayed"] = 1 - sum(expect.values())
    assert liver["seen_voxels"] == 0
    for r, x in expect.items():
        assert liver["unseen"][r] == pytest.approx(x, abs=2e-6), r
    assert k_never not in (k_short, k_wrong, k_region)


# ---- reader layer -------------------------------------------------------------------
def test_annotations_and_finish(case, vol, tmp_path):
    K = case.shape[0]
    evs = read_pass(vol, range(K), 0, 0, "soft") + [
        annotation(vol, K * 150 + 1, K, "lung_left", "done", note="read on prior"),
        annotation(vol, K * 150 + 2, K + 1, "sternum", "na"),
        annotation(vol, K * 150 + 3, K + 2, "liver", "flag", note="outline off"),
        annotation(vol, K * 150 + 4, K + 3, "spleen", "done"),  # already reviewed: not attested
        annotation(vol, K * 150 + 5, K + 4, "lung_right", "flag"),
        annotation(vol, K * 150 + 6, K + 5, "lung_right", "clear"),
        annotation(vol, K * 150 + 7, K + 6, "no_such_organ", "done"),
        finish(vol, K * 150 + 8, K + 7, confirmed=True, remaining=["lung_right"]),
        end(vol, K * 150 + 9, K + 8)]
    cov = Scorer(case).add(evs)
    o = by_key(cov)
    assert o["lung_left"]["status"] == "skipped" and o["lung_left"]["reader"] == "done"
    assert o["lung_left"]["note"] == "read on prior"
    assert o["sternum"]["reader"] == "na" and o["liver"]["flagged"] and o["liver"]["reader"] is None
    assert o["lung_right"] == {**o["lung_right"], "reader": None, "flagged": False, "note": None}
    s = cov["summary"]
    assert s["attested"] == 1 and s["not_applicable"] == 1 and s["flagged"] == 1
    non_reviewed = [k for k, x in o.items() if x["status"] != "reviewed"]
    assert s["remaining"] == [k for k in non_reviewed if k not in ("lung_left", "sternum")]
    assert s["finished"] == {"t_ms": K * 150 + 8, "confirmed": True, "remaining": ["lung_right"]}
    assert cov["events"]["ignored"]["other"] == 1
    # replay reproduces the reader layer
    p = tmp_path / "ev.jsonl"
    p.write_text("".join(json.dumps(e) + "\n" for e in evs))
    rep = replay_file(case.dir, p)
    assert rep["summary"] == cov["summary"]
    assert [(x["reader"], x["flagged"], x["note"]) for x in rep["organs"]] == \
        [(x["reader"], x["flagged"], x["note"]) for x in cov["organs"]]


# ---- snapshots, timeline, replay ----------------------------------------------------
def test_snapshot_and_timeline_agree_with_coverage(case, vol):
    K = case.shape[0]
    evs = read_pass(vol, range(K), 0, 0, "soft", dwell=100) + read_pass(vol, range(K // 2), K * 100, K, "lung", dwell=100)
    evs.append(end(vol, K * 100 + (K // 2) * 100, K + K // 2))
    sc = Scorer(case)
    cov = sc.add(evs)
    snap = sc.snapshot()
    assert snap["statuses"] == {o["key"]: o["status"] for o in cov["organs"]}
    assert sc.seen_counts.tolist() == [o["seen_voxels"] for o in cov["organs"]]
    tl = timeline(case, evs, frames=12)
    assert len(tl["frames"]) == 12
    assert tl["frames"][-1]["statuses"] == snap["statuses"]
    assert tl["frames"][0]["summary"]["reviewed"] == 0
    reviewed = [f["summary"]["reviewed"] for f in tl["frames"]]
    assert reviewed == sorted(reviewed)
    # a mid-session frame equals a provisional score at that time
    mid = tl["frames"][5]
    sc2 = Scorer(case)
    prov = sc2.add([e for e in evs if e["t_ms"] <= mid["t_ms"]], now_ms=mid["t_ms"])
    assert mid["statuses"] == {o["key"]: o["status"] for o in prov["organs"]}


def test_replay_file_and_params(case, vol, tmp_path):
    K = case.shape[0]
    p = tmp_path / "ev.jsonl"
    evs = read_pass(vol, range(K), 0, 0, "soft", dwell=60) + [end(vol, K * 60, K)]
    p.write_text("".join(json.dumps(e) + "\n" for e in evs) + "\n")
    out = tmp_path / "cov.json"
    cov = replay_file(case.dir, p, out=out)
    assert json.loads(out.read_text())["organs"] == cov["organs"]
    assert by_key(cov)["liver"]["status"] == "skipped"  # 60 ms < 80
    cov = replay_file(case.dir, p, min_dwell_ms=50, reviewed_threshold=0.5, skipped_threshold=0.01)
    assert by_key(cov)["liver"]["status"] == "reviewed"
    assert cov["params"] == {"min_dwell_ms": 50.0, "reviewed_threshold": 0.5, "skipped_threshold": 0.01}
    assert set(cov["timing_ms"]) >= {"score", "load", "parse", "total"}
    bad = tmp_path / "bad.jsonl"
    bad.write_text(json.dumps(evs[0]) + "\n{not json\n")
    with pytest.raises(EventError, match="bad.jsonl:2"):
        replay_file(case.dir, bad)


def test_resolve_view_stack_uses_sop_for_k(case, vol):
    ev = stack_view(vol, 4, 0, 0)
    ev["corners_lps"] = stack_view(vol, 9, 0, 0)["corners_lps"]  # corners of another slice
    plane = resolve_view(case, ev)
    assert (plane.axis, plane.index, plane.region, plane.cls) == (0, 4, ("all",), "soft")


def test_seen_counts_match_full_recount(case, vol):
    rng = np.random.default_rng(0)
    K, R, C = case.shape
    evs, plan, t = [], [], 0.0
    for i in range(300):
        k, side = int(rng.integers(K)), int(rng.integers(8, R))
        r0, c0 = int(rng.integers(-10, R - 4)), int(rng.integers(-10, C - 4))
        w = ["soft", "lung", "bone", "brain"][int(rng.integers(4))]
        evs.append(stack_view(vol, k, t, i, w, rect=(r0, r0 + side, c0, c0 + side)))
        plan.append((k, w, (r0, r0 + side, c0, c0 + side)))
        t += float(rng.integers(5, 120))
    evs.append(end(vol, t, 300))
    sc = Scorer(case)
    cov = sc.add(evs)
    assert sc.seen_counts.tolist() == [o["seen_voxels"] for o in cov["organs"]]
    # independent dense recount of qualifying dwell from the plan
    dwell = {w: np.zeros(case.shape) for w in ("soft", "lung", "bone", "brain")}
    for (k, w, (r0, r1, c0, c1)), a, b in zip(plan, evs[:-1], evs[1:]):
        dwell[w][k, max(r0, 0):r1, max(c0, 0):c1] += b["t_ms"] - a["t_ms"]
    for o in cov["organs"]:
        mask = case.labels == o["id"]
        q = sum(dwell[w] for w in o["windows"])
        assert o["seen_voxels"] == int(np.count_nonzero(q[mask] >= 80)), o["key"]


def _random_mixed_events(case, vol, n, seed):
    """Random axial stack views (panned/zoomed) on viewport "a" and coronal/sagittal volume
    views (partial boxes) on viewport "b", with hidden events, at irregular times."""
    rng = np.random.default_rng(seed)
    K, R, C = case.shape
    evs, t = [], 0.0
    for i in range(n):
        w = ["soft", "lung", "bone", "brain"][int(rng.integers(4))]
        roll = rng.random()
        if roll < 0.55:
            k, side = int(rng.integers(K)), int(rng.integers(8, R))
            r0, c0 = int(rng.integers(-10, R - 4)), int(rng.integers(-10, C - 4))
            evs.append(stack_view(vol, k, t, i, w, rect=(r0, r0 + side, c0, c0 + side), viewport="a"))
        elif roll < 0.9:
            k0, k1 = sorted(rng.uniform(-0.5, K - 0.5, 2))
            a0, a1 = sorted(rng.uniform(-0.5, C - 0.5, 2))
            if rng.random() < 0.5:  # coronal r = const
                r = int(rng.integers(R))
                corners = [(k0, r, a0), (k0, r, a1), (k1, r, a1), (k1, r, a0)]
            else:  # sagittal c = const
                c = int(rng.integers(C))
                corners = [(k0, a0, c), (k0, a1, c), (k1, a1, c), (k1, a0, c)]
            evs.append(view_krc(vol, corners, t, i, w, viewport="b"))
        else:
            evs.append(hidden(vol, t, i, viewport="ab"[int(rng.integers(2))]))
        t += float(rng.integers(5, 120))
    return evs, t


@pytest.mark.parametrize("seed", [1, 2])
def test_incremental_table_equals_recount_and_provisional_equals_closing(case, vol, seed):
    """Live path: after every batch the incrementally kept state table equals a full
    recount, and the provisional coverage at now equals committing the open states at now."""
    evs, t_end = _random_mixed_events(case, vol, 240, seed)
    sc = Scorer(case)
    for i in range(0, len(evs), 6):
        batch = evs[i:i + 6]
        now = batch[-1]["t_ms"] + 37.0
        prov = sc.add(batch, now_ms=now)
        assert np.array_equal(sc.table, sc._full_table()), i
        if i % 30 == 0:
            closed = Scorer(case).add(evs[:i + 6] + [end(vol, now, 10_000)])
            assert [(o["key"], o["seen_voxels"], o["unseen"]) for o in prov["organs"]] == \
                [(o["key"], o["seen_voxels"], o["unseen"]) for o in closed["organs"]], i
            snap = sc.snapshot(now)
            assert snap["statuses"] == {o["key"]: o["status"] for o in prov["organs"]}
    final = sc.add([end(vol, t_end, 10_001)])
    one = Scorer(case).add(evs + [end(vol, t_end, 10_001)])
    assert final["organs"] == one["organs"]
    assert sc.seen_counts.tolist() == [o["seen_voxels"] for o in final["organs"]]


@pytest.mark.parametrize("shape", [(9, 64, 32), (7, 50, 44)])
def test_case_voxel_index_matches_nonzero(tmp_path, shape):
    from covmap.casedata import Case

    d = build_case(tmp_path, f"idx-{shape[1]}", shape=shape)
    lab = np.load(d / "seg" / "labels.npy")
    lab[0, :3, :3] = 200  # a label that is not in the organ catalog: ignored
    np.save(d / "seg" / "labels.npy", lab)
    c = Case(d)
    ids = [o["id"] for o in c.organs]
    kk, rr, cc = np.nonzero(np.isin(lab, ids))
    assert np.array_equal(c.vk, kk) and np.array_equal(c.vr, rr) and np.array_equal(c.vc, cc)
    assert np.array_equal(c.organ_ids[c.vord], lab[kk, rr, cc])
    assert c.organ_voxels.tolist() == [int(np.count_nonzero(lab == i)) for i in ids]
    assert c.organ_slice_counts.tolist() == [[int(np.count_nonzero(lab[k] == i)) for k in range(shape[0])] for i in ids]
    for k in (0, shape[0] // 2):
        sel, u, v = c.plane(0, k)
        assert np.array_equal(u, rr[kk == k]) and np.array_equal(v, cc[kk == k])
    # coronal / sagittal planes: every voxel of the plane, in (k, c) / (k, r) order
    flat_ids = np.arange(kk.size)
    for axis, coord, uu, vv in ((1, rr, kk, cc), (2, cc, kk, rr)):
        for i in range(shape[axis]):
            sel, u, v = c.plane(axis, i)
            m = coord == i
            assert np.array_equal(sel, flat_ids[m]), (axis, i)
            assert np.array_equal(u, uu[m]) and np.array_equal(v, vv[m]), (axis, i)


def test_index_disk_cache(tmp_path, monkeypatch):
    import os

    from covmap.casedata import INDEX_FILE, Case

    d = build_case(tmp_path, "cache-1", shape=(9, 64, 32))
    first = Case(d)
    assert first.index_source == "built" and (d / INDEX_FILE).is_file()
    hit = Case(d)
    assert hit.index_source == "cache"
    for name in ("vord", "vr", "vc", "organ_slice_counts", "organ_voxels"):
        assert np.array_equal(getattr(hit, name), getattr(first, name)), name
    assert np.array_equal(hit._offsets[0], first._offsets[0])
    assert np.array_equal(hit.plane(1, 20)[0], first.plane(1, 20)[0])
    # labels rewritten -> stale key -> rebuilt from the new labels
    lab = np.load(d / "seg" / "labels.npy")
    lab[4] = 0
    np.save(d / "seg" / "labels.npy", lab)
    fresh = Case(d)
    assert fresh.index_source == "built" and fresh.organ_slice_counts[:, 4].sum() == 0
    assert Case(d).index_source == "cache"
    # a different organ catalog (ids / order) -> rebuilt
    org = json.loads((d / "seg" / "organs.json").read_text())
    org["organs"] = org["organs"][::-1]
    (d / "seg" / "organs.json").write_text(json.dumps(org))
    rev = Case(d)
    assert rev.index_source == "built" and rev.organ_keys == [o["key"] for o in org["organs"]]
    assert rev.organ_voxels.tolist() == fresh.organ_voxels[::-1].tolist()
    # corrupt cache file -> rebuilt and rewritten
    (d / INDEX_FILE).write_bytes(b"not a zip")
    assert Case(d).index_source == "built" and Case(d).index_source == "cache"
    # disabled
    monkeypatch.setenv("COVMAP_INDEX_CACHE", "0")
    assert Case(d).index_source == "built"
    monkeypatch.delenv("COVMAP_INDEX_CACHE")
    # read-only cache dir: works, just no cache
    d2 = build_case(tmp_path, "cache-ro", shape=(5, 32, 32))
    (d2 / INDEX_FILE.parent).mkdir()
    os.chmod(d2 / INDEX_FILE.parent, 0o500)
    try:
        c = Case(d2)
        assert c.index_source == "built" and not (d2 / INDEX_FILE).exists()
        assert not list((d2 / INDEX_FILE.parent).iterdir())
    finally:
        os.chmod(d2 / INDEX_FILE.parent, 0o700)
    # labels.npy replaced (atomically) while the index is being built: that index is used
    # for this Case but not cached; the next Case builds from the new labels and caches
    import covmap.casedata as cd

    d3 = build_case(tmp_path, "cache-race", shape=(9, 64, 32))
    real = cd._build_index

    def racing(labels, organ_ids):
        out = real(labels, organ_ids)
        new = np.array(labels)
        new[int(np.argmax(np.count_nonzero(new.reshape(new.shape[0], -1), axis=1)))] = 0
        np.save(d3 / "seg" / "labels.new.npy", new)
        os.replace(d3 / "seg" / "labels.new.npy", d3 / "seg" / "labels.npy")
        return out

    monkeypatch.setattr(cd, "_build_index", racing)
    raced = Case(d3)
    assert raced.index_source == "built" and not (d3 / INDEX_FILE).exists()
    monkeypatch.setattr(cd, "_build_index", real)
    after = Case(d3)
    assert after.index_source == "built" and after.n_vox < raced.n_vox
    assert Case(d3).index_source == "cache"
