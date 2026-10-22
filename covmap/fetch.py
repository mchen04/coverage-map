"""Download public, deidentified TCIA CT series (NBIA v1 API, no credentials).

Writes ``source/*.dcm`` (unmodified), ``provenance.json``, ``volume.json`` and
``ct.nii.gz`` into the case directory (docs/SPEC.md). Idempotent: an existing
download is reused when every file still matches the recorded sha256 manifest.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import shutil
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

from covmap import __version__
from covmap.paths import case_dir

NBIA_V1 = "https://services.cancerimagingarchive.net/nbia-api/services/v1"
USER_AGENT = f"covmap/{__version__} (research; public TCIA download)"
MAX_UNCOMPRESSED = 4 * 1024**3

KNOWN_CASES = {
    "lidc-0001": {
        "collection": "LIDC-IDRI",
        "patient_id": "LIDC-IDRI-0001",
        "series_uid": "1.3.6.1.4.1.14519.5.2.1.6279.6001.179049373636438705059720603192",
        "doi": "https://doi.org/10.7937/K9/TCIA.2015.LO9QL9SX",
        "license": "CC BY 3.0",
    },
    "pancreas-0051": {
        "collection": "Pancreas-CT",
        "patient_id": "PANCREAS_0051",
        "series_uid": "1.2.826.0.1.3680043.2.1125.1.77419915248783746629465382576486048",
        "doi": "https://doi.org/10.7937/K9/TCIA.2016.tNB1kqBU",
        "license": "CC BY 3.0",
    },
}


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(chunk):
            h.update(block)
    return h.hexdigest()


def manifest_text(files: dict[str, str]) -> str:
    """Canonical manifest: ``sha256sum``-style lines sorted by file name."""
    return "".join(f"{files[name]}  {name}\n" for name in sorted(files))


def manifest_sha256(files: dict[str, str]) -> str:
    return hashlib.sha256(manifest_text(files).encode()).hexdigest()


def _get(url: str, timeout: float = 120.0):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    return urllib.request.urlopen(req, timeout=timeout)  # noqa: S310 - fixed https host


def _retry(fn, what: str, attempts: int = 3):
    for i in range(attempts):
        try:
            return fn()
        except (OSError, zipfile.BadZipFile) as e:
            if i == attempts - 1:
                raise RuntimeError(f"{what} failed after {attempts} attempts: {e}") from e
            time.sleep(2 * (i + 1))


def series_metadata(series_uid: str) -> tuple[dict, str]:
    url = f"{NBIA_V1}/getSeries?" + urllib.parse.urlencode({"SeriesInstanceUID": series_uid})

    def go():
        with _get(url, timeout=60) as r:
            return json.load(r)

    rows = _retry(go, f"GET {url}")
    if not rows:
        raise RuntimeError(f"TCIA has no public series {series_uid} ({url})")
    if len(rows) != 1:
        raise RuntimeError(f"TCIA returned {len(rows)} rows for series {series_uid}")
    return rows[0], url


def _download(url: str, dest: Path) -> tuple[str, int]:
    """Stream ``url`` to ``dest`` atomically; returns (sha256, bytes)."""
    part = dest.with_name(dest.name + ".part")

    def go():
        h, n = hashlib.sha256(), 0
        with _get(url, timeout=300) as r, open(part, "wb") as f:
            while block := r.read(1 << 20):
                h.update(block)
                f.write(block)
                n += len(block)
        if not zipfile.is_zipfile(part):
            raise zipfile.BadZipFile(f"{url} did not return a zip ({n} bytes)")
        return h.hexdigest(), n

    try:
        digest, n = _retry(go, f"download {url}")
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    part.replace(dest)
    return digest, n


def _is_dicom(head: bytes) -> bool:
    return len(head) >= 132 and head[128:132] == b"DICM"


def _extract(zip_path: Path, source: Path, extra: Path) -> list[dict]:
    """Extract DICOM members (flattened, safe names) into a fresh ``source`` dir.

    Non-DICOM members (e.g. a license text) go to ``extra``. Returns their records.
    """
    for d in (source, extra):
        tmp = d.with_name(d.name + ".tmp")
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
    tmp_source, tmp_extra = source.with_name(source.name + ".tmp"), extra.with_name(extra.name + ".tmp")
    others, seen = [], set()
    with zipfile.ZipFile(zip_path) as zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        total = sum(i.file_size for i in infos)
        if total > MAX_UNCOMPRESSED:
            raise RuntimeError(f"zip expands to {total} bytes; refusing (> {MAX_UNCOMPRESSED})")
        for info in infos:
            name = PurePosixPath(info.filename.replace("\\", "/")).name
            if not name or name.startswith(".") or name in (".", ".."):
                raise RuntimeError(f"unsafe zip member name {info.filename!r}")
            data = zf.read(info)
            if _is_dicom(data[:132]):
                if not name.lower().endswith(".dcm"):
                    name += ".dcm"
                target = tmp_source / name
            else:
                others.append({"member": info.filename, "bytes": len(data),
                               "sha256": hashlib.sha256(data).hexdigest()})
                target = tmp_extra / name
            if target in seen:
                raise RuntimeError(f"duplicate member name {name!r} in zip")
            seen.add(target)
            target.write_bytes(data)
    for d, tmp in ((source, tmp_source), (extra, tmp_extra)):
        shutil.rmtree(d, ignore_errors=True)
        tmp.replace(d)
    return others


def _verify_existing(cdir: Path, prov: dict) -> str | None:
    """None if ``source/`` and the zip match the recorded hashes, else the reason."""
    recorded = prov.get("files") or {}
    source = cdir / "source"
    if not recorded or not source.is_dir():
        return "no previous download"
    present = {p.name for p in source.glob("*.dcm")}
    if present != set(recorded):
        return f"file set differs ({len(present)} present vs {len(recorded)} recorded)"
    for name, digest in recorded.items():
        if sha256_file(source / name) != digest:
            return f"hash mismatch for {name}"
    if manifest_sha256(recorded) != prov.get("manifest_sha256"):
        return "manifest sha256 mismatch"
    zip_path = cdir / prov.get("zip", {}).get("file", "source.zip")
    if not zip_path.exists() or sha256_file(zip_path) != prov.get("zip", {}).get("sha256"):
        return "zip missing or hash mismatch"
    return None


def resolve_case(case_id: str, series_uid: str | None, collection: str | None) -> dict:
    known = dict(KNOWN_CASES.get(case_id, {}))
    if series_uid and known and series_uid != known["series_uid"]:
        raise ValueError(f"case {case_id!r} is pinned to series {known['series_uid']}; "
                         "use a different case id for another series")
    if not known:
        if not series_uid:
            raise ValueError(f"unknown case {case_id!r}: pass --series (known: {sorted(KNOWN_CASES)})")
        known = {"series_uid": series_uid, "collection": collection}
    if collection and known.get("collection") and collection != known["collection"]:
        raise ValueError(f"case {case_id!r} belongs to collection {known['collection']}, not {collection}")
    return known


def fetch_case(case_id: str, series_uid: str | None = None, collection: str | None = None,
               force: bool = False) -> dict:
    from covmap import dicom_io

    t_start = time.perf_counter()
    spec = resolve_case(case_id, series_uid, collection)
    uid = spec["series_uid"]
    cdir = case_dir(case_id)
    cdir.mkdir(parents=True, exist_ok=True)
    prov_path, vol_path, nii_path = cdir / "provenance.json", cdir / "volume.json", cdir / "ct.nii.gz"
    zip_path = cdir / "source.zip"
    timings: dict[str, float] = {}

    prov = json.loads(prov_path.read_text()) if prov_path.exists() else {}
    if prov and prov.get("series_uid") != uid:
        raise RuntimeError(f"{cdir} holds series {prov.get('series_uid')}, not {uid}")

    t = time.perf_counter()
    reason = "force" if force else _verify_existing(cdir, prov)
    timings["verify_existing_s"] = time.perf_counter() - t
    downloaded = reason is not None

    if downloaded:
        t = time.perf_counter()
        meta, series_url = series_metadata(uid)
        timings["metadata_s"] = time.perf_counter() - t
        if meta.get("Modality") != "CT":
            raise RuntimeError(f"series {uid} is {meta.get('Modality')}, not CT")
        for key, field in (("collection", "Collection"), ("patient_id", "PatientID")):
            if spec.get(key) and meta.get(field) != spec[key]:
                raise RuntimeError(f"TCIA {field} {meta.get(field)!r} != expected {spec[key]!r}")

        image_url = f"{NBIA_V1}/getImage?" + urllib.parse.urlencode({"SeriesInstanceUID": uid})
        t = time.perf_counter()
        when = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
        zip_sha, zip_bytes = _download(image_url, zip_path)
        timings["download_s"] = time.perf_counter() - t

        t = time.perf_counter()
        others = _extract(zip_path, cdir / "source", cdir / "source_extra")
        timings["extract_s"] = time.perf_counter() - t

        t = time.perf_counter()
        files = {p.name: sha256_file(p) for p in sorted((cdir / "source").glob("*.dcm"))}
        timings["hash_s"] = time.perf_counter() - t
        if meta.get("ImageCount") and int(meta["ImageCount"]) != len(files):
            raise RuntimeError(f"zip has {len(files)} DICOM files, TCIA reports ImageCount {meta['ImageCount']}")
        prov = {
            "case": case_id,
            "source": "The Cancer Imaging Archive (TCIA), public NBIA v1 REST API, no credentials",
            "image_url": image_url,
            "series_url": series_url,
            "collection": meta.get("Collection"),
            "collection_doi": spec.get("doi") or meta.get("CollectionURI"),
            "collection_uri": meta.get("CollectionURI"),
            "license": spec.get("license"),
            "license_name": meta.get("LicenseName"),
            "license_uri": meta.get("LicenseURI"),
            "patient_id": meta.get("PatientID"),
            "study_uid": meta.get("StudyInstanceUID"),
            "series_uid": uid,
            "series_metadata": meta,
            "download_time_utc": when,
            "download_s": round(timings["download_s"], 3),
            "zip": {"file": zip_path.name, "bytes": zip_bytes, "sha256": zip_sha},
            "files": files,
            "n_files": len(files),
            "manifest_format": "sha256sum lines '<sha256>  <name>\\n' sorted by name, UTF-8",
            "manifest_sha256": manifest_sha256(files),
            "non_dicom_members": others,
            "redownload_reason": reason,
            "tool": f"covmap {__version__}",
        }

    t = time.perf_counter()
    dcm_paths = sorted((cdir / "source").glob("*.dcm"))
    geometry, datasets, checks = dicom_io.read_series(dcm_paths, series_uid=uid)
    vol, hu_info = dicom_io.hu_volume(datasets)
    del datasets
    timings["geometry_hu_s"] = time.perf_counter() - t
    if prov.get("study_uid") and prov["study_uid"] != geometry["study_uid"]:
        raise RuntimeError(f"DICOM StudyInstanceUID {geometry['study_uid']} != TCIA {prov['study_uid']}")

    volume = {"case": case_id, **geometry}
    vol_text = json.dumps(volume, indent=1) + "\n"
    vol_sha = hashlib.sha256(vol_text.encode()).hexdigest()
    derived = prov.get("derived", {})
    t = time.perf_counter()
    if (downloaded or not nii_path.exists() or not vol_path.exists()
            or derived.get("volume_json_sha256") != vol_sha or sha256_file(vol_path) != vol_sha
            or sha256_file(nii_path) != derived.get("ct_nii_sha256")):
        vol_path.write_text(vol_text)
        tmp = nii_path.with_name("ct.tmp.nii.gz")
        dicom_io.write_nifti(vol, geometry, tmp)
        tmp.replace(nii_path)
        rebuilt = True
    else:
        rebuilt = False
    timings["derived_s"] = time.perf_counter() - t

    prov["derived"] = {
        "volume_json_sha256": vol_sha,
        "ct_nii_sha256": sha256_file(nii_path),
        "geometry_checks": checks,
        "hu": {"min": int(vol.min()), "max": int(vol.max()), **hu_info},
    }
    prov_path.write_text(json.dumps(prov, indent=1, default=str) + "\n")
    timings["total_s"] = time.perf_counter() - t_start

    return {
        "case": case_id,
        "case_dir": str(cdir),
        "download": "downloaded" if downloaded else "skipped (verified existing files)",
        "redownload_reason": reason,
        "derived_rebuilt": rebuilt,
        "collection": prov.get("collection"),
        "license": prov.get("license_name"),
        "series_uid": uid,
        "study_uid": geometry["study_uid"],
        "n_slices": geometry["n_slices"],
        "rows": geometry["rows"], "cols": geometry["cols"],
        "spacing": {"pixel_mm": geometry["pixel_spacing"], "slice_mm": geometry["slice_spacing"]},
        **checks,
        "hu_range": [int(vol.min()), int(vol.max())],
        "zip_sha256": prov["zip"]["sha256"],
        "manifest_sha256": prov["manifest_sha256"],
        "ct_nii_sha256": prov["derived"]["ct_nii_sha256"],
        "paths": {"source": str(cdir / "source"), "provenance": str(prov_path),
                  "volume": str(vol_path), "ct_nii": str(nii_path)},
        "timings_s": {k: round(v, 3) for k, v in timings.items()},
    }
