"""JSON command line: every command prints exactly one JSON object on stdout.

Commands map one-to-one onto the app's functions (see docs/SPEC.md). Modules are
imported lazily so each command only pays for what it uses.
"""

import argparse
import json
import sys
import time
import traceback


def _emit(obj) -> None:
    json.dump(obj, sys.stdout, indent=1, default=str)
    sys.stdout.write("\n")
    sys.stdout.flush()


def cmd_fetch(a):
    from covmap.fetch import fetch_case

    return fetch_case(a.case, series_uid=a.series, collection=a.collection, force=a.force)


def cmd_orthanc_load(a):
    from covmap.orthanc import load_case

    return load_case(a.case, url=a.orthanc)


def cmd_segment(a):
    from covmap.segment import segment_case

    return segment_case(a.case, fast=a.fast, device=a.device, roi_subset=a.roi_subset,
                        threads=a.threads, reuse=a.reuse, out_name=a.out_name)


def cmd_simulate(a):
    from covmap.simulate import simulate

    return simulate(a.case, a.scenario, out=a.out, seed=a.seed)


def cmd_replay(a):
    from covmap.scoring import replay_file

    return replay_file(a.case, a.events, out=a.out, min_dwell_ms=a.min_dwell_ms,
                       reviewed_threshold=a.reviewed_threshold, skipped_threshold=a.skipped_threshold)


def cmd_report(a):
    from covmap.report import report

    return report(a.case, a.coverage, truth=a.truth, out=a.out)


def cmd_render(a):
    from covmap.render import render

    return render(a.case, coverage=a.coverage, out=a.out, width=a.width, height=a.height,
                  server=a.server)


def cmd_serve(a):
    from covmap.server import serve

    return serve(host=a.host, port=a.port, orthanc=a.orthanc)


def cmd_scenarios(a):
    from covmap.simulate import list_scenarios

    return list_scenarios(a.case)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="covmap", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("fetch", help="download a public TCIA CT series")
    s.add_argument("--case", required=True)
    s.add_argument("--series", help="SeriesInstanceUID (defaults from covmap.fetch.KNOWN_CASES)")
    s.add_argument("--collection")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_fetch)

    s = sub.add_parser("orthanc-load", help="upload a case's DICOM into Orthanc")
    s.add_argument("--case", required=True)
    s.add_argument("--orthanc", default="http://127.0.0.1:8042")
    s.set_defaults(fn=cmd_orthanc_load)

    s = sub.add_parser("segment", help="TotalSegmentator organ labels, catalog and meshes")
    s.add_argument("--case", required=True)
    s.add_argument("--fast", action="store_true")
    s.add_argument("--device", default="cpu")
    s.add_argument("--roi-subset", action="store_true",
                   help="only segment the TotalSegmentator classes the organ catalog uses")
    s.add_argument("--threads", type=int, default=None)
    s.add_argument("--reuse", action="store_true",
                   help="skip TotalSegmentator if seg/totalseg.nii.gz exists; rebuild derived files")
    s.add_argument("--out-name", default="seg", help="output directory name inside the case")
    s.set_defaults(fn=cmd_segment)

    s = sub.add_parser("scenarios", help="list scripted reading scenarios for a case")
    s.add_argument("--case", required=True)
    s.set_defaults(fn=cmd_scenarios)

    s = sub.add_parser("simulate", help="generate a scripted reading (source: simulated)")
    s.add_argument("--case", required=True)
    s.add_argument("--scenario", required=True)
    s.add_argument("--out", required=True, help="events .jsonl; truth goes to <out>.truth.json")
    s.add_argument("--seed", type=int, default=0)
    s.set_defaults(fn=cmd_simulate)

    s = sub.add_parser("replay", help="score an event log")
    s.add_argument("--case", required=True)
    s.add_argument("--events", required=True)
    s.add_argument("--out")
    s.add_argument("--min-dwell-ms", type=float, default=None)
    s.add_argument("--reviewed-threshold", type=float, default=None)
    s.add_argument("--skipped-threshold", type=float, default=None)
    s.set_defaults(fn=cmd_replay)

    s = sub.add_parser("report", help="summarize coverage, optionally against scenario truth")
    s.add_argument("--case", required=True)
    s.add_argument("--coverage", required=True)
    s.add_argument("--truth")
    s.add_argument("--out")
    s.set_defaults(fn=cmd_report)

    s = sub.add_parser("render", help="3D organ map PNG via the app's vtk.js module")
    s.add_argument("--case", required=True)
    s.add_argument("--coverage")
    s.add_argument("--out", required=True)
    s.add_argument("--width", type=int, default=800)
    s.add_argument("--height", type=int, default=800)
    s.add_argument("--server", help="use a running covmap server instead of starting one")
    s.set_defaults(fn=cmd_render)

    s = sub.add_parser("serve", help="run the app server")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--orthanc", default="http://127.0.0.1:8042")
    s.set_defaults(fn=cmd_serve)
    return p


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    t0 = time.perf_counter()
    try:
        out = a.fn(a)
    except Exception as e:  # noqa: BLE001 - the CLI contract is JSON on every exit path
        _emit({"ok": False, "cmd": a.cmd, "error": f"{type(e).__name__}: {e}",
               "trace": traceback.format_exc(limit=5)})
        return 1
    if a.cmd == "serve":
        return 0
    if isinstance(out, dict):
        out.setdefault("ok", True)
        out.setdefault("cmd", a.cmd)
        out.setdefault("elapsed_s", round(time.perf_counter() - t0, 3))
        ok = out["ok"]
    else:
        out, ok = {"ok": True, "cmd": a.cmd, "result": out}, True
    _emit(out)
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
