"""Load a case's original DICOM files into the task-owned Orthanc and verify DICOMweb.

Idempotent: if DICOMweb already lists exactly the case's SOP instances, nothing is
uploaded; otherwise every file is POSTed to ``/instances`` (Orthanc reports
``AlreadyStored`` for duplicates).
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from covmap.paths import case_dir

SOP_INSTANCE_UID = "00080018"
SERIES_INSTANCE_UID = "0020000E"


def _request(url: str, data: bytes | None = None, headers: dict | None = None,
             timeout: float = 60.0) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=data, headers=headers or {},
                                 method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - local Orthanc
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _get_json(url: str):
    status, body = _request(url, headers={"Accept": "application/dicom+json, application/json"})
    if status == 404 or (status == 200 and not body.strip()):
        return []
    if status != 200:
        raise RuntimeError(f"GET {url} -> HTTP {status}: {body[:200]!r}")
    return json.loads(body)


def dicomweb_sops(url: str, study_uid: str, series_uid: str) -> list[str]:
    rows = _get_json(f"{url}/dicom-web/studies/{study_uid}/series/{series_uid}/instances")
    return [r[SOP_INSTANCE_UID]["Value"][0] for r in rows]


def _upload(url: str, path: Path) -> str:
    status, body = _request(f"{url}/instances", data=path.read_bytes(),
                            headers={"Content-Type": "application/dicom"}, timeout=120)
    if status != 200:
        raise RuntimeError(f"upload {path.name} -> HTTP {status}: {body[:200]!r}")
    return json.loads(body).get("Status", "unknown")


def load_case(case_id: str, url: str = "http://127.0.0.1:8042", workers: int = 8) -> dict:
    t0 = time.perf_counter()
    url = url.rstrip("/")
    cdir = case_dir(case_id)
    volume = json.loads((cdir / "volume.json").read_text())
    files = sorted((cdir / "source").glob("*.dcm"))
    if len(files) != volume["n_slices"]:
        raise RuntimeError(f"{len(files)} files in source/ but volume.json has {volume['n_slices']} slices")
    status, body = _request(f"{url}/system")
    if status != 200:
        raise RuntimeError(f"Orthanc not reachable at {url} (HTTP {status})")
    study, series = volume["study_uid"], volume["series_uid"]
    expected = set(volume["sop_uids"])

    before = set(dicomweb_sops(url, study, series))
    statuses: dict[str, int] = {}
    t = time.perf_counter()
    if before != expected:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for s in pool.map(lambda p: _upload(url, p), files):
                statuses[s] = statuses.get(s, 0) + 1
    upload_s = time.perf_counter() - t

    t = time.perf_counter()
    qido = _get_json(f"{url}/dicom-web/series?" + urllib.parse.urlencode({"SeriesInstanceUID": series}))
    found = [r for r in qido if r.get(SERIES_INSTANCE_UID, {}).get("Value", [None])[0] == series]
    if len(found) != 1:
        raise RuntimeError(f"series {series} not queryable over DICOMweb ({len(qido)} results)")
    after = dicomweb_sops(url, study, series)
    verify_s = time.perf_counter() - t
    if set(after) != expected or len(after) != len(expected):
        missing = expected - set(after)
        raise RuntimeError(f"DICOMweb lists {len(after)} instances; {len(missing)} expected SOPs missing")

    return {
        "case": case_id,
        "orthanc": url,
        "study_uid": study,
        "series_uid": series,
        "files": len(files),
        "uploaded": statuses or None,
        "skipped_upload": before == expected,
        "instances_before": len(before),
        "dicomweb_instances": len(after),
        "dicomweb_series_query": f"{url}/dicom-web/series?SeriesInstanceUID={series}",
        "timings_s": {"upload_s": round(upload_s, 3), "verify_s": round(verify_s, 3),
                      "total_s": round(time.perf_counter() - t0, 3)},
    }
