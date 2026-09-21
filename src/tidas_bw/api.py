"""Public Python API."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from .brightway import (
    install_payload,
    load_database,
    load_parameter_counts,
    load_referenced_nodes,
    load_tidas_method_states,
)
from .mapping import TidasMapper
from .models import MigrationReport
from .package import TidasPackage, read_package, write_package
from .precision import PrecisionPolicy
from .reverse import BrightwayExporter
from .validation import validate_package


def validate_tidas(
    source: str | Path,
    *,
    require_open: bool = True,
    strict_references: bool = False,
    max_json_mib: int = 128,
) -> MigrationReport:
    """Check package format, licences, and reference closure only.

    A passing report does not guarantee that the package can be imported:
    mapping-level restrictions (amount precision, uncertainty representation,
    provider resolution) are checked separately. Use :func:`preflight_import`
    to run every import check without touching Brightway.
    """
    package = read_package(source, max_json_mib=max_json_mib)
    return validate_package(
        package,
        require_open=require_open,
        strict_references=strict_references,
    )


def _read_and_check_import_basics(
    source: str | Path,
    *,
    strict_references: bool,
    max_json_mib: int,
) -> tuple[TidasPackage, MigrationReport]:
    package = read_package(source, max_json_mib=max_json_mib)
    validation = validate_package(
        package,
        require_open=True,
        strict_references=strict_references,
    )
    if not package.by_category("processes"):
        validation.add(
            "error",
            "no_process_datasets",
            "A Brightway import requires at least one TIDAS process dataset",
        )
    return package, validation


def preflight_import(
    source: str | Path,
    *,
    database: str,
    biosphere_database: str | None = None,
    strict_references: bool = False,
    max_json_mib: int = 128,
    allow_rounding: bool = False,
    absolute_tolerance: str = "0",
    relative_tolerance: str = "0",
) -> MigrationReport:
    """Run every import check without creating or modifying Brightway data.

    The report distinguishes package-level findings (schema, licence,
    reference closure) from mapping-level findings (amount precision,
    uncertainty representation, provider resolution), so a package that
    validates cleanly but cannot be mapped is reported as such.
    """
    precision = PrecisionPolicy(allow_rounding, absolute_tolerance, relative_tolerance)
    package, report = _read_and_check_import_basics(
        source,
        strict_references=strict_references,
        max_json_mib=max_json_mib,
    )
    target_biosphere = biosphere_database or f"{database}-biosphere"
    report.direction = "tidas-to-brightway"
    report.target = f"{database} (+ {target_biosphere}) [preflight; nothing written]"
    report.metadata["preflight"] = True
    report.metadata["database"] = database
    report.metadata["biosphere_database"] = target_biosphere
    if report.ok:
        report.add(
            "info",
            "package_checks_passed",
            "Schema, licence, and reference checks passed; mapping preflight follows",
        )
    report.metadata["package_ok"] = report.ok
    if report.ok:
        mapping = TidasMapper(
            package,
            database=database,
            biosphere_database=target_biosphere,
            precision=precision,
        ).build()
        report.issues.extend(mapping.report.issues)
        report.metadata.update(mapping.report.metadata)
        if report.ok:
            report.add(
                "info",
                "mapping_checks_passed",
                "All processes, exchanges, providers, amounts, and methods can be mapped "
                "to Brightway",
            )
        report.metadata["mapping_ok"] = report.ok
    else:
        report.metadata["mapping_ok"] = None
        report.add(
            "info", "mapping_checks_skipped", "Mapping was skipped because package checks failed"
        )
    report.add(
        "info",
        "preflight_no_write",
        "Preflight completed; no Brightway project or database was created or modified",
    )
    return report


def import_tidas(
    source: str | Path,
    *,
    project: str,
    database: str,
    biosphere_database: str | None = None,
    replace: bool = False,
    strict_references: bool = False,
    max_json_mib: int = 128,
    brightway_dir: str | Path | None = None,
    allow_rounding: bool = False,
    absolute_tolerance: str = "0",
    relative_tolerance: str = "0",
) -> MigrationReport:
    precision = PrecisionPolicy(allow_rounding, absolute_tolerance, relative_tolerance)
    package, validation = _read_and_check_import_basics(
        source,
        strict_references=strict_references,
        max_json_mib=max_json_mib,
    )
    target_biosphere = biosphere_database or f"{database}-biosphere"
    if not validation.ok:
        validation.direction = "tidas-to-brightway"
        validation.target = f"{project}/{database}"
        validation.add(
            "info",
            "write_skipped",
            "Brightway was not modified because validation failed",
        )
        return validation
    payload = TidasMapper(
        package,
        database=database,
        biosphere_database=target_biosphere,
        precision=precision,
    ).build()
    payload.report.issues = [*validation.issues, *payload.report.issues]
    payload.report.metadata["project"] = project
    payload.report.metadata["database"] = database
    payload.report.metadata["biosphere_database"] = target_biosphere
    if not payload.report.ok:
        payload.report.add(
            "info",
            "write_skipped",
            "Brightway was not modified because mapping preflight failed",
        )
        return payload.report
    install_payload(
        payload,
        project=project,
        database=database,
        biosphere_database=target_biosphere,
        replace=replace,
        brightway_dir=brightway_dir,
    )
    payload.report.add(
        "info",
        "installed",
        f"Installed Brightway databases {database} and {target_biosphere}",
    )
    return payload.report


def export_tidas(
    *,
    project: str,
    database: str,
    output: str | Path,
    overwrite: bool = False,
    strict_references: bool = False,
    brightway_dir: str | Path | None = None,
    license: str | None = None,
    owner: str | None = None,
    source: str | None = None,
) -> MigrationReport:
    database_data, database_metadata, bd = load_database(
        project=project,
        database=database,
        brightway_dir=brightway_dir,
    )
    marker = database_metadata.get("tidas_bw")
    companion_databases: list[str] = []
    if isinstance(marker, Mapping) and marker.get("biosphere_database"):
        companion_databases.append(str(marker["biosphere_database"]))
    referenced_nodes = load_referenced_nodes(
        bd,
        database_data,
        include_databases=companion_databases,
    )
    source_database_metadata = {
        name: dict(bd.databases[name])
        for name in {key[0] for key in referenced_nodes}
        if name in bd.databases
    }
    method_states = load_tidas_method_states(bd, database)
    parameter_counts = load_parameter_counts(database)
    records, manifest, report = BrightwayExporter(
        database_data,
        database_metadata,
        referenced_nodes,
        project=project,
        database=database,
        method_states=method_states,
        native_license=license,
        native_owner=owner,
        native_source=source,
        parameter_counts=parameter_counts,
        source_database_metadata=source_database_metadata,
    ).build()
    package = TidasPackage(records=records, source=f"{project}/{database}", manifest=manifest)
    validation = validate_package(
        package,
        require_open=True,
        strict_references=strict_references,
    )
    report.issues.extend(validation.issues)
    report.target = str(Path(output).expanduser().resolve())
    if not report.ok:
        report.add(
            "info",
            "write_skipped",
            "TIDAS output was not written because export validation failed",
        )
        return report
    manifest["issues"] = [
        {
            "severity": issue.severity,
            "code": issue.code,
            "message": issue.message,
            "dataset": issue.dataset,
            "path": issue.path,
        }
        for issue in report.issues
    ]
    written = write_package(records, output, manifest=manifest, overwrite=overwrite)
    report.target = str(written)
    report.add("info", "exported", f"Wrote TIDAS package {written}")
    return report
