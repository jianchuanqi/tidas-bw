"""Thin Brightway 2.5 boundary; imports are deliberately lazy."""

from __future__ import annotations

import os
from collections.abc import Sequence
from copy import deepcopy
from pathlib import Path
from typing import Any

from .errors import BrightwayError, ValidationFailure
from .models import BrightwayPayload


def configure_brightway_dir(path: str | Path | None) -> None:
    """Set the Brightway data directory before bw2data is imported."""
    if path is None:
        return
    directory = Path(path).expanduser().resolve()
    if "bw2data" in __import__("sys").modules:
        current = os.environ.get("BRIGHTWAY2_DIR")
        if current and Path(current).expanduser().resolve() == directory:
            return
        raise BrightwayError(
            "--brightway-dir must be applied before bw2data is imported in this process"
        )
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BrightwayError(f"cannot create Brightway data directory {directory}: {exc}") from exc
    os.environ["BRIGHTWAY2_DIR"] = str(directory)


def install_payload(
    payload: BrightwayPayload,
    *,
    project: str,
    database: str,
    biosphere_database: str,
    replace: bool = False,
    brightway_dir: str | Path | None = None,
) -> None:
    if not payload.report.ok:
        raise ValidationFailure("migration preflight failed; Brightway was not modified")
    if not database or not biosphere_database:
        raise BrightwayError("Brightway database names cannot be empty")
    if database == biosphere_database:
        raise BrightwayError("technosphere and biosphere database names must be different")
    configure_brightway_dir(brightway_dir)
    import bw2data as bd

    bd.projects.set_current(project)
    requested = (biosphere_database, database)
    existing = [name for name in requested if name in bd.databases]
    method_names = [method.name for method in payload.methods]
    existing_methods = [name for name in method_names if name in bd.methods]
    if (existing or existing_methods) and not replace:
        detail = ", ".join([*existing, *(" / ".join(name) for name in existing_methods)])
        raise BrightwayError(f"refusing to overwrite existing Brightway data: {detail}")
    if replace and existing:
        targets = set(existing)
        dependents = [
            name
            for name in bd.databases
            if name not in targets and targets.intersection(bd.databases[name].get("depends", []))
        ]
        if dependents:
            raise BrightwayError(
                "refusing to replace databases used by other Brightway databases: "
                + ", ".join(sorted(dependents))
            )

    database_backups: dict[str, tuple[dict[Any, Any], dict[str, Any]]] = {}
    method_backups: dict[tuple[str, ...], tuple[list[Any], dict[str, Any]]] = {}
    if replace:
        for name in existing:
            database_backups[name] = (
                deepcopy(bd.Database(name).load()),
                deepcopy(dict(bd.databases[name])),
            )
        replaced_databases = set(existing)
        for name in list(bd.methods):
            stable_data = _stable_method_data(bd, deepcopy(bd.Method(name).load()))
            references_replaced_database = any(
                row[0][0] in replaced_databases for row in stable_data
            )
            if name in existing_methods or references_replaced_database:
                method_backups[name] = (
                    stable_data,
                    deepcopy(dict(bd.methods[name])),
                )

    touched_databases: list[str] = []
    touched_methods: list[tuple[str, ...]] = []
    try:
        touched_databases.append(biosphere_database)
        _write_database(bd, biosphere_database, payload.biosphere)
        touched_databases.append(database)
        _write_database(bd, database, payload.technosphere)

        metadata = deepcopy(payload.database_metadata)
        metadata.update(
            {
                "technosphere_database": database,
                "biosphere_database": biosphere_database,
                "project": project,
            }
        )
        bd.databases[database]["tidas_bw"] = metadata
        bd.databases[biosphere_database]["tidas_bw"] = {
            "format": "tidas-bw-metadata-v1",
            "owner_database": database,
        }
        bd.databases.flush()

        payload_method_names = set(method_names)
        for name, (data, metadata) in method_backups.items():
            if name in payload_method_names:
                continue
            touched_methods.append(name)
            _delete_method(bd, name)
            method = bd.Method(name)
            method.register(**deepcopy(metadata))
            method.write(data)

        for method_payload in payload.methods:
            touched_methods.append(method_payload.name)
            _delete_method(bd, method_payload.name)
            method = bd.Method(method_payload.name)
            method.register(**deepcopy(method_payload.metadata))
            method.write(method_payload.factors)
    except BaseException as exc:
        try:
            _rollback(
                bd,
                touched_databases=touched_databases,
                touched_methods=touched_methods,
                database_backups=database_backups,
                method_backups=method_backups,
            )
        except Exception as rollback_exc:
            raise BrightwayError(
                "Brightway write failed and rollback also failed; inspect the target project: "
                f"write={exc}; rollback={rollback_exc}"
            ) from rollback_exc
        if isinstance(exc, Exception):
            raise BrightwayError(f"Brightway write failed and was rolled back: {exc}") from exc
        raise


def load_database(
    *,
    project: str,
    database: str,
    brightway_dir: str | Path | None = None,
) -> tuple[dict[Any, Any], dict[str, Any], Any]:
    configure_brightway_dir(brightway_dir)
    import bw2data as bd

    bd.projects.set_current(project)
    if database not in bd.databases:
        raise BrightwayError(f"Brightway database does not exist: {database}")
    return bd.Database(database).load(), deepcopy(dict(bd.databases[database])), bd


def load_referenced_nodes(
    bd: Any,
    database_data: dict[Any, Any],
    *,
    include_databases: Sequence[str] = (),
) -> dict[tuple[str, str], dict[str, Any]]:
    result: dict[tuple[str, str], dict[str, Any]] = {}
    keys: set[tuple[str, str]] = set()
    for dataset in database_data.values():
        for exchange in dataset.get("exchanges", []):
            input_key = exchange.get("input")
            if isinstance(input_key, tuple | list) and len(input_key) == 2:
                keys.add((str(input_key[0]), str(input_key[1])))
    for key in keys:
        try:
            node = bd.get_node(database=key[0], code=key[1])
        except Exception as exc:
            raise BrightwayError(f"referenced Brightway node is missing: {key}") from exc
        result[key] = dict(node)
    for database in include_databases:
        if database not in bd.databases:
            raise BrightwayError(f"companion Brightway database is missing: {database}")
        for key, node in bd.Database(database).load().items():
            if not isinstance(key, tuple | list) or len(key) != 2:
                continue
            result[(str(key[0]), str(key[1]))] = deepcopy(dict(node))
    return result


def load_tidas_method_states(bd: Any, database: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for name in list(bd.methods):
        metadata = deepcopy(dict(bd.methods[name]))
        tidas = metadata.get("tidas")
        if metadata.get("tidas_database") != database or not isinstance(tidas, dict):
            continue
        uuid = tidas.get("uuid")
        if not uuid:
            continue
        result[str(uuid).lower()] = {
            "name": tuple(name),
            "data": _stable_method_data(bd, deepcopy(bd.Method(name).load())),
            "metadata": metadata,
        }
    return result


def load_parameter_counts(database: str) -> dict[str, int]:
    from bw2data.parameters import ActivityParameter, DatabaseParameter, ProjectParameter

    return {
        "activity": ActivityParameter.select()
        .where(ActivityParameter.database == database)
        .count(),
        "database": DatabaseParameter.select()
        .where(DatabaseParameter.database == database)
        .count(),
        "project": ProjectParameter.select().count(),
    }


def _write_database(bd: Any, name: str, data: dict[Any, Any]) -> None:
    database = bd.Database(name)
    if name not in bd.databases:
        database.register(write_empty=False, format="TIDAS")
    database.write(data)


def _delete_database(bd: Any, name: str) -> None:
    if name in bd.databases:
        database = bd.Database(name)
        database.delete(warn=False)
        database.deregister()


def _delete_method(bd: Any, name: tuple[str, ...]) -> None:
    if name in bd.methods:
        method = bd.Method(name)
        processed = Path(method.filepath_processed())
        intermediate = (
            Path(bd.projects.dir) / str(method._intermediate_dir) / f"{method.filename}.pickle"
        )
        method.deregister()
        processed.unlink(missing_ok=True)
        intermediate.unlink(missing_ok=True)


def _stable_method_data(bd: Any, rows: list[Any]) -> list[Any]:
    result: list[Any] = []
    for row in rows:
        if not isinstance(row, tuple | list) or len(row) < 2:
            raise BrightwayError(f"cannot back up malformed Brightway method row: {row!r}")
        flow = row[0]
        if isinstance(flow, int):
            node = bd.get_node(id=flow)
            key = (str(node["database"]), str(node["code"]))
        elif isinstance(flow, tuple | list) and len(flow) == 2:
            key = (str(flow[0]), str(flow[1]))
        else:
            raise BrightwayError(f"cannot resolve Brightway method flow: {flow!r}")
        result.append((key, *row[1:]))
    return result


def _rollback(
    bd: Any,
    *,
    touched_databases: list[str],
    touched_methods: list[tuple[str, ...]],
    database_backups: dict[str, tuple[dict[Any, Any], dict[str, Any]]],
    method_backups: dict[tuple[str, ...], tuple[list[Any], dict[str, Any]]],
) -> None:
    for name in reversed(touched_methods):
        _delete_method(bd, name)
    for name in reversed(touched_databases):
        _delete_database(bd, name)
    for name, (data, metadata) in database_backups.items():
        _write_database(bd, name, data)
        bd.databases[name].update(metadata)
    bd.databases.flush()
    for name, (data, metadata) in method_backups.items():
        _delete_method(bd, name)
        method = bd.Method(name)
        method.register(**metadata)
        method.write(data)
