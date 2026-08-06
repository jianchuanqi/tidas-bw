"""Read and write TIDAS directory trees and deterministic ZIP packages."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import tempfile
import zipfile
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from .errors import PackageError
from .models import DatasetRecord
from .utils import deep_get, pick_text

ROOT_TO_CATEGORY = {
    "processDataSet": "processes",
    "flowDataSet": "flows",
    "flowPropertyDataSet": "flowproperties",
    "unitGroupDataSet": "unitgroups",
    "lifeCycleModelDataSet": "lifecyclemodels",
    "LCIAMethodDataSet": "lciamethods",
    "sourceDataSet": "sources",
    "contactDataSet": "contacts",
}

MAX_JSON_ENTRIES = 100_000

IDENTITY_PATHS = {
    "processes": (
        ("processDataSet", "processInformation", "dataSetInformation", "common:UUID"),
        (
            "processDataSet",
            "administrativeInformation",
            "publicationAndOwnership",
            "common:dataSetVersion",
        ),
        (
            "processDataSet",
            "administrativeInformation",
            "publicationAndOwnership",
            "common:licenseType",
        ),
    ),
    "flows": (
        ("flowDataSet", "flowInformation", "dataSetInformation", "common:UUID"),
        (
            "flowDataSet",
            "administrativeInformation",
            "publicationAndOwnership",
            "common:dataSetVersion",
        ),
        (
            "flowDataSet",
            "administrativeInformation",
            "publicationAndOwnership",
            "common:licenseType",
        ),
    ),
    "flowproperties": (
        (
            "flowPropertyDataSet",
            "flowPropertiesInformation",
            "dataSetInformation",
            "common:UUID",
        ),
        (
            "flowPropertyDataSet",
            "administrativeInformation",
            "publicationAndOwnership",
            "common:dataSetVersion",
        ),
        (
            "flowPropertyDataSet",
            "administrativeInformation",
            "publicationAndOwnership",
            "common:licenseType",
        ),
    ),
    "unitgroups": (
        ("unitGroupDataSet", "unitGroupInformation", "dataSetInformation", "common:UUID"),
        (
            "unitGroupDataSet",
            "administrativeInformation",
            "publicationAndOwnership",
            "common:dataSetVersion",
        ),
        (
            "unitGroupDataSet",
            "administrativeInformation",
            "publicationAndOwnership",
            "common:licenseType",
        ),
    ),
    "lifecyclemodels": (
        (
            "lifeCycleModelDataSet",
            "lifeCycleModelInformation",
            "dataSetInformation",
            "common:UUID",
        ),
        (
            "lifeCycleModelDataSet",
            "administrativeInformation",
            "publicationAndOwnership",
            "common:dataSetVersion",
        ),
        (
            "lifeCycleModelDataSet",
            "administrativeInformation",
            "publicationAndOwnership",
            "common:licenseType",
        ),
    ),
    "lciamethods": (
        ("LCIAMethodDataSet", "LCIAMethodInformation", "dataSetInformation", "common:UUID"),
        (
            "LCIAMethodDataSet",
            "administrativeInformation",
            "publicationAndOwnership",
            "common:dataSetVersion",
        ),
        (
            "LCIAMethodDataSet",
            "administrativeInformation",
            "publicationAndOwnership",
            "common:licenseType",
        ),
    ),
    "sources": (
        ("sourceDataSet", "sourceInformation", "dataSetInformation", "common:UUID"),
        (
            "sourceDataSet",
            "administrativeInformation",
            "publicationAndOwnership",
            "common:dataSetVersion",
        ),
        (
            "sourceDataSet",
            "administrativeInformation",
            "publicationAndOwnership",
            "common:licenseType",
        ),
    ),
    "contacts": (
        ("contactDataSet", "contactInformation", "dataSetInformation", "common:UUID"),
        (
            "contactDataSet",
            "administrativeInformation",
            "publicationAndOwnership",
            "common:dataSetVersion",
        ),
        (
            "contactDataSet",
            "administrativeInformation",
            "publicationAndOwnership",
            "common:licenseType",
        ),
    ),
}


@dataclass(slots=True)
class TidasPackage:
    records: list[DatasetRecord]
    source: str
    ignored_json: list[str] = field(default_factory=list)

    def by_category(self, category: str) -> list[DatasetRecord]:
        return [record for record in self.records if record.category == category]

    def index(self) -> dict[tuple[str, str, str], DatasetRecord]:
        return {
            (record.category, record.uuid.lower(), record.version): record
            for record in self.records
        }

    def uuid_index(self) -> dict[tuple[str, str], list[DatasetRecord]]:
        result: dict[tuple[str, str], list[DatasetRecord]] = defaultdict(list)
        for record in self.records:
            result[(record.category, record.uuid.lower())].append(record)
        return dict(result)

    def counts(self) -> dict[str, int]:
        result: dict[str, int] = defaultdict(int)
        for record in self.records:
            result[record.category] += 1
        return dict(sorted(result.items()))


def read_package(path: str | Path, *, max_json_mib: int = 128) -> TidasPackage:
    source = Path(path).expanduser().resolve()
    if not source.exists():
        raise PackageError(f"TIDAS input does not exist: {source}")
    limit = max_json_mib * 1024 * 1024
    entries: list[tuple[str, bytes]] = []
    if source.is_dir():
        for candidate in sorted(source.rglob("*.json")):
            if candidate.is_symlink():
                raise PackageError(f"symbolic links are not accepted: {candidate}")
            if candidate.stat().st_size > limit:
                raise PackageError(f"JSON entry exceeds {max_json_mib} MiB: {candidate}")
            entries.append((candidate.relative_to(source).as_posix(), candidate.read_bytes()))
    elif zipfile.is_zipfile(source):
        with zipfile.ZipFile(source) as archive:
            members = archive.infolist()
            if len(members) > MAX_JSON_ENTRIES:
                raise PackageError(f"ZIP contains more than {MAX_JSON_ENTRIES:,} entries: {source}")
            seen_names: set[str] = set()
            for info in sorted(members, key=lambda item: item.filename):
                if info.filename in seen_names:
                    raise PackageError(f"duplicate ZIP entry name: {info.filename}")
                seen_names.add(info.filename)
                mode = info.external_attr >> 16
                if mode and stat.S_ISLNK(mode):
                    raise PackageError(f"symbolic links are not accepted in ZIP: {info.filename}")
                if info.is_dir() or not info.filename.lower().endswith(".json"):
                    continue
                _validate_archive_name(info.filename)
                if info.file_size > limit:
                    raise PackageError(f"JSON entry exceeds {max_json_mib} MiB: {info.filename}")
                entries.append((info.filename, archive.read(info)))
    else:
        raise PackageError("TIDAS input must be a directory or ZIP package")

    records: list[DatasetRecord] = []
    ignored: list[str] = []
    seen: set[tuple[str, str, str]] = set()
    for name, raw in entries:
        try:
            payload = json.loads(raw, parse_constant=_reject_json_constant)
        except (UnicodeDecodeError, ValueError) as exc:
            raise PackageError(f"malformed JSON in {name}: {exc}") from exc
        if not isinstance(payload, Mapping):
            ignored.append(name)
            continue
        document = _unwrap_document(dict(payload))
        root_key = next((key for key in ROOT_TO_CATEGORY if key in document), None)
        if root_key is None:
            ignored.append(name)
            continue
        category = ROOT_TO_CATEGORY[root_key]
        uuid_path, version_path, license_path = IDENTITY_PATHS[category]
        uuid_value = deep_get(document, *uuid_path)
        version_value = deep_get(document, *version_path, default="00.00.000")
        license_value = _license_value(document, root_key, license_path)
        if not uuid_value:
            raise PackageError(f"dataset in {name} has no UUID")
        identity = (category, str(uuid_value).lower(), str(version_value))
        if identity in seen:
            raise PackageError(
                f"duplicate dataset {category}:{uuid_value}@{version_value} in {name}"
            )
        seen.add(identity)
        records.append(
            DatasetRecord(
                category=category,
                root_key=root_key,
                uuid=str(uuid_value),
                version=str(version_value),
                document=document,
                source_path=name,
                license_type=license_value,
            )
        )
    if not records:
        raise PackageError(f"no TIDAS datasets found in {source}")
    return TidasPackage(records=records, source=str(source), ignored_json=ignored)


def record_from_document(
    document: Mapping[str, Any], *, source_path: str = "generated"
) -> DatasetRecord:
    payload = dict(document)
    root_key = next((key for key in ROOT_TO_CATEGORY if key in payload), None)
    if root_key is None:
        raise PackageError(f"document has no recognised TIDAS root: {source_path}")
    category = ROOT_TO_CATEGORY[root_key]
    uuid_path, version_path, license_path = IDENTITY_PATHS[category]
    uuid_value = deep_get(payload, *uuid_path)
    if not uuid_value:
        raise PackageError(f"document has no UUID: {source_path}")
    version_value = deep_get(payload, *version_path, default="00.00.000")
    license_value = _license_value(payload, root_key, license_path)
    return DatasetRecord(
        category=category,
        root_key=root_key,
        uuid=str(uuid_value),
        version=str(version_value),
        document=payload,
        source_path=source_path,
        license_type=license_value,
    )


def write_package(
    records: Iterable[DatasetRecord],
    output: str | Path,
    *,
    manifest: Mapping[str, Any] | None = None,
    overwrite: bool = False,
) -> Path:
    requested = Path(output).expanduser()
    target = requested.parent.resolve() / requested.name
    target.parent.mkdir(parents=True, exist_ok=True)
    record_list = sorted(records, key=lambda item: (item.category, item.uuid, item.version))
    paths = [_record_path(record) for record in record_list]
    if len(paths) != len({path.casefold() for path in paths}):
        raise PackageError("cannot write duplicate TIDAS dataset identities")
    if target.is_symlink():
        raise PackageError(f"refusing to write through a symbolic link: {target}")
    if target.exists() and not overwrite:
        raise PackageError(f"output already exists; pass overwrite=True: {target}")
    if target.suffix.lower() == ".zip":
        return _write_zip(record_list, target, manifest=manifest, overwrite=overwrite)
    return _write_directory(record_list, target, manifest=manifest, overwrite=overwrite)


def _unwrap_document(payload: dict[str, Any]) -> dict[str, Any]:
    wrapped = payload.get("json")
    if isinstance(wrapped, Mapping) and any(key in wrapped for key in ROOT_TO_CATEGORY):
        return dict(wrapped)
    data = payload.get("data")
    if isinstance(data, Mapping) and any(key in data for key in ROOT_TO_CATEGORY):
        return dict(data)
    return payload


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard numeric constant {value}")


def _license_value(
    document: Mapping[str, Any],
    root_key: str,
    license_path: tuple[str, ...],
) -> str | None:
    declared = deep_get(document, *license_path)
    restrictions = deep_get(
        document,
        root_key,
        "administrativeInformation",
        "publicationAndOwnership",
        "common:accessRestrictions",
    )
    exclusive_access = deep_get(
        document,
        root_key,
        "administrativeInformation",
        "publicationAndOwnership",
        "common:referenceToEntitiesWithExclusiveAccess",
    )
    parts: list[str] = []
    if declared not in (None, ""):
        parts.append(str(declared))
    restriction_text = pick_text(restrictions)
    if restriction_text:
        parts.append(restriction_text)
    if exclusive_access is not None:
        parts.append("Exclusive access declared")
    return "; ".join(dict.fromkeys(parts)) or None


def _validate_archive_name(name: str) -> None:
    if "\\" in name or "\x00" in name:
        raise PackageError(f"unsafe ZIP entry path: {name}")
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "" in path.parts:
        raise PackageError(f"unsafe ZIP entry path: {name}")


def _record_path(record: DatasetRecord) -> str:
    if record.category not in ROOT_TO_CATEGORY.values():
        raise PackageError(f"unsupported TIDAS dataset category: {record.category}")
    for label, value in (("UUID", record.uuid), ("version", record.version)):
        if value in {".", ".."} or re.fullmatch(r"[A-Za-z0-9._-]+", value) is None:
            raise PackageError(f"unsafe TIDAS {label} for output path: {value!r}")
    return f"{record.category}/{record.uuid}_{record.version}.json"


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False) + "\n").encode("utf-8")


def _write_zip(
    records: list[DatasetRecord],
    target: Path,
    *,
    manifest: Mapping[str, Any] | None,
    overwrite: bool,
) -> Path:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for record in records:
                info = zipfile.ZipInfo(_record_path(record), date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                archive.writestr(info, _json_bytes(record.document))
            if manifest is not None:
                info = zipfile.ZipInfo("tidas-bw-manifest.json", date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                archive.writestr(info, _json_bytes(manifest))
        if target.exists() and not overwrite:
            raise PackageError(f"output already exists: {target}")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def _write_directory(
    records: list[DatasetRecord],
    target: Path,
    *,
    manifest: Mapping[str, Any] | None,
    overwrite: bool,
) -> Path:
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=target.parent))
    backup: Path | None = None
    try:
        for record in records:
            destination = staging / _record_path(record)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(_json_bytes(record.document))
        if manifest is not None:
            (staging / "tidas-bw-manifest.json").write_bytes(_json_bytes(manifest))
        if target.exists():
            if not overwrite:
                raise PackageError(f"output already exists: {target}")
            backup = target.with_name(f".{target.name}.backup")
            if backup.exists():
                raise PackageError(f"refusing to replace existing backup path: {backup}")
            target.rename(backup)
        staging.rename(target)
        if backup is not None:
            if backup.is_dir():
                shutil.rmtree(backup)
            else:
                backup.unlink()
    except Exception:
        if backup is not None and backup.exists() and not target.exists():
            backup.rename(target)
        raise
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return target
