"""Command-line interface for tidas-bw."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from contextlib import redirect_stdout

from . import __version__
from .api import export_tidas, import_tidas, preflight_import, validate_tidas
from .errors import TidasBwError
from .models import MigrationReport


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tidas-bw",
        description="Migrate open TIDAS data packages to and from Brightway.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="Validate a TIDAS directory or ZIP")
    validate.add_argument("source")
    _add_validation_options(validate)
    _add_output_options(validate)

    preflight = subparsers.add_parser(
        "preflight",
        help="Check importability of a TIDAS package without touching Brightway",
        description=(
            "Run every import check (package format, licences, references, mapping "
            "precision, uncertainty, providers) and never create or modify Brightway data."
        ),
    )
    preflight.add_argument("source")
    preflight.add_argument(
        "--database",
        required=True,
        help="Brightway database name the import would target",
    )
    preflight.add_argument("--biosphere-database")
    _add_validation_options(preflight)
    _add_output_options(preflight)

    import_command = subparsers.add_parser(
        "import", help="Install a TIDAS package in a Brightway project"
    )
    import_command.add_argument("source")
    import_command.add_argument("--project", required=True)
    import_command.add_argument("--database", required=True)
    import_command.add_argument("--biosphere-database")
    import_command.add_argument(
        "--replace",
        action="store_true",
        help="Explicitly replace databases and methods with the same names",
    )
    _add_validation_options(import_command)
    _add_brightway_options(import_command)
    _add_output_options(import_command)

    export = subparsers.add_parser(
        "export", help="Export one Brightway database as a TIDAS package"
    )
    export.add_argument("--project", required=True)
    export.add_argument("--database", required=True)
    export.add_argument("--output", required=True)
    export.add_argument(
        "--overwrite",
        action="store_true",
        help="Explicitly replace an existing TIDAS ZIP or directory",
    )
    export.add_argument(
        "--license",
        help="Open licence for a native Brightway database, e.g. 'CC BY 4.0'",
    )
    export.add_argument(
        "--owner",
        help="Data owner or publisher for a native Brightway database",
    )
    export.add_argument(
        "--source",
        help="Source citation or URL for a native Brightway database",
    )
    _add_validation_options(export, include_size=False)
    _add_brightway_options(export)
    _add_output_options(export)
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        if args.json:
            with redirect_stdout(sys.stderr):
                report = _dispatch(args)
        else:
            report = _dispatch(args)
    except (TidasBwError, OSError) as exc:
        if getattr(args, "json", False):
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        else:
            print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    _render_report(report, as_json=args.json)
    raise SystemExit(0 if report.ok else 2)


def _dispatch(args: argparse.Namespace) -> MigrationReport:
    common = {
        "strict_references": args.strict_references,
    }
    if args.command == "validate":
        return validate_tidas(args.source, max_json_mib=args.max_json_mib, **common)
    if args.command == "preflight":
        return preflight_import(
            args.source,
            database=args.database,
            biosphere_database=args.biosphere_database,
            max_json_mib=args.max_json_mib,
            **common,
        )
    if args.command == "import":
        return import_tidas(
            args.source,
            project=args.project,
            database=args.database,
            biosphere_database=args.biosphere_database,
            replace=args.replace,
            max_json_mib=args.max_json_mib,
            brightway_dir=args.brightway_dir,
            **common,
        )
    if args.command == "export":
        return export_tidas(
            project=args.project,
            database=args.database,
            output=args.output,
            overwrite=args.overwrite,
            brightway_dir=args.brightway_dir,
            license=args.license,
            owner=args.owner,
            source=args.source,
            **common,
        )
    raise AssertionError(f"unknown command: {args.command}")


def _add_validation_options(parser: argparse.ArgumentParser, *, include_size: bool = True) -> None:
    parser.add_argument(
        "--strict-references",
        action="store_true",
        help="Require documentation references as well as modelling references to close",
    )
    if include_size:
        parser.add_argument(
            "--max-json-mib",
            type=int,
            default=128,
            help="Maximum uncompressed size of each JSON document (default: 128 MiB)",
        )


def _add_brightway_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--brightway-dir",
        help="Alternative Brightway data directory; useful for isolated and reproducible runs",
    )


def _add_output_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="Print a machine-readable report")


def _render_report(report: MigrationReport, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        return
    status = "OK" if report.ok else "FAILED"
    print(f"{status}: {report.direction}")
    print(f"Source: {report.source}")
    if report.target:
        print(f"Target: {report.target}")
    if report.counts:
        counts = ", ".join(f"{key}={value}" for key, value in sorted(report.counts.items()))
        print(f"Counts: {counts}")
    for issue in report.issues:
        location = f" [{issue.dataset}]" if issue.dataset else ""
        print(f"{issue.severity.upper()} {issue.code}{location}: {issue.message}")


def cli(argv: Sequence[str] | None = None) -> int:
    """Test-friendly entry point which returns an exit code."""
    try:
        main(argv)
    except SystemExit as exc:
        return int(exc.code)
    return 0
