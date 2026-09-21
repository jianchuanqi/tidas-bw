"""Schema, licence, and reference-closure validation."""

from __future__ import annotations

import warnings
from collections.abc import Mapping
from typing import Any

from tidas_sdk import (
    create_contact,
    create_flow,
    create_flow_property,
    create_lcia_method,
    create_life_cycle_model,
    create_process,
    create_source,
    create_unit_group,
)

from .licenses import is_open_license, is_restricted_license, normalise_license
from .models import DatasetRecord, MigrationReport
from .package import TidasPackage
from .utils import deep_get, pick_text

FACTORIES = {
    "processes": create_process,
    "flows": create_flow,
    "flowproperties": create_flow_property,
    "unitgroups": create_unit_group,
    "lifecyclemodels": create_life_cycle_model,
    "lciamethods": create_lcia_method,
    "sources": create_source,
    "contacts": create_contact,
}

REFERENCE_TYPES = {
    "process data set": "processes",
    "flow data set": "flows",
    "flow property data set": "flowproperties",
    "unit group data set": "unitgroups",
    "life cycle model data set": "lifecyclemodels",
    "lcia method data set": "lciamethods",
    "source data set": "sources",
    "contact data set": "contacts",
}

CORE_REFERENCE_CATEGORIES = {"processes", "flows", "flowproperties", "unitgroups"}

# The official TIDAS eILCD/XSD projection rejects the standard licence fields
# (common:copyright, common:licenseType, common:accessRestrictions) on these
# record categories, so generated documents never carry them there and their
# open licence follows the package's data-bearing datasets (issue #1).
# Processes, flows, sources, and lifecycle models accept the fields and must
# still prove an open licence individually.
STRUCTURAL_LICENCE_CATEGORIES = frozenset({"contacts", "flowproperties", "unitgroups"})


def validate_package(
    package: TidasPackage,
    *,
    require_open: bool = True,
    strict_references: bool = False,
) -> MigrationReport:
    report = MigrationReport(direction="validate", source=package.source)
    report.counts.update(package.counts())
    if package.ignored_json:
        report.add(
            "info",
            "ignored_json",
            f"Ignored {len(package.ignored_json)} non-dataset JSON files",
        )
    for record in package.records:
        _validate_schema(record, report)
        _validate_license(record, report, require_open=require_open)
    _validate_references(package, report, strict=strict_references)
    return report


def _validate_schema(record: DatasetRecord, report: MigrationReport) -> None:
    try:
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message="Pydantic serializer warnings:.*",
                category=UserWarning,
                module=r"pydantic\.main",
            )
            entity = FACTORIES[record.category](record.document)
            if entity.validate(mode="jsonschema"):
                return
            errors = entity.jsonschema_errors() or ["unknown JSON Schema error"]
    except Exception as exc:
        report.add(
            "error",
            "schema_validation",
            f"Schema validator could not read this document: {type(exc).__name__}: {exc}",
            dataset=record.identity,
            path=record.source_path,
        )
        return
    for message in errors[:20]:
        report.add(
            "error",
            "schema_validation",
            message,
            dataset=record.identity,
            path=record.source_path,
        )
    if len(errors) > 20:
        report.add(
            "error",
            "schema_validation_truncated",
            f"{len(errors) - 20} additional schema errors omitted",
            dataset=record.identity,
        )


def _validate_license(
    record: DatasetRecord,
    report: MigrationReport,
    *,
    require_open: bool,
) -> None:
    publication = deep_get(
        record.document,
        record.root_key,
        "administrativeInformation",
        "publicationAndOwnership",
        default={},
    )
    if not isinstance(publication, Mapping):
        publication = {}
    declared_value = publication.get("common:licenseType")
    declared = str(declared_value).strip() if declared_value not in (None, "") else None
    restrictions = pick_text(publication.get("common:accessRestrictions"))
    exclusive_access = publication.get("common:referenceToEntitiesWithExclusiveAccess")

    if exclusive_access not in (None, "", [], {}):
        report.add(
            "error",
            "restricted_license",
            "Dataset declares entities with exclusive access",
            dataset=record.identity,
        )
        return

    restricted_parts = [
        value for value in (declared, restrictions) if value and is_restricted_license(value)
    ]
    if restricted_parts:
        report.add(
            "error",
            "restricted_license",
            f"Dataset declares a restricted licence or access condition: {'; '.join(restricted_parts)}",
            dataset=record.identity,
        )
        return

    neutral_restriction = normalise_license(restrictions) == "none"
    if restrictions and not neutral_restriction and not is_open_license(restrictions):
        report.add(
            "error",
            "unrecognised_access_restriction",
            f"Access restrictions are not recognised as permitting open use: {restrictions}",
            dataset=record.identity,
        )
        return

    if record.category in STRUCTURAL_LICENCE_CATEGORIES:
        return

    open_proven = is_open_license(declared) or (
        not neutral_restriction and is_open_license(restrictions)
    )
    if require_open and not open_proven:
        report.add(
            "error",
            "open_license_not_proven",
            "An explicitly open licence is required for every dataset in the package",
            dataset=record.identity,
        )
    elif declared and not is_open_license(declared):
        report.add(
            "warning",
            "unrecognised_license",
            f"Licence declaration was not recognised as open: {declared}",
            dataset=record.identity,
        )


def _validate_references(package: TidasPackage, report: MigrationReport, *, strict: bool) -> None:
    exact = package.index()
    by_uuid = package.uuid_index()
    for record in package.records:
        for reference, pointer in _walk_references(record.document):
            category = REFERENCE_TYPES.get(str(reference.get("@type", "")).lower())
            target_uuid = reference.get("@refObjectId")
            target_version = reference.get("@version")
            if not category or not target_uuid:
                continue
            exact_key = (category, str(target_uuid).lower(), str(target_version or ""))
            exists = (
                exact_key in exact
                if target_version
                else bool(by_uuid.get((category, str(target_uuid).lower())))
            )
            if exists:
                continue
            severity = "error" if strict or category in CORE_REFERENCE_CATEGORIES else "warning"
            report.add(
                severity,
                "unresolved_reference",
                f"Missing {category} target {target_uuid}@{target_version or '*'}",
                dataset=record.identity,
                path=pointer,
            )


def _walk_references(value: Any, pointer: str = ""):
    if isinstance(value, Mapping):
        if "@refObjectId" in value and "@type" in value:
            yield value, pointer or "/"
        for key, item in value.items():
            escaped = str(key).replace("~", "~0").replace("/", "~1")
            yield from _walk_references(item, f"{pointer}/{escaped}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk_references(item, f"{pointer}/{index}")
