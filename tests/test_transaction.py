from __future__ import annotations

import pytest

from tidas_bw.brightway import install_payload
from tidas_bw.errors import BrightwayError
from tidas_bw.models import BrightwayMethodPayload, BrightwayPayload, MigrationReport


def test_failed_initial_write_leaves_no_registered_targets(isolated_brightway_dir) -> None:
    import bw2data as bd

    payload = _payload(
        database="new-tech",
        biosphere="new-bio",
        method_name=("test", "new-failure"),
        biosphere_amount=0.8,
        factor="not-a-number",
    )

    with pytest.raises(BrightwayError, match="was rolled back"):
        install_payload(
            payload,
            project="transaction-new",
            database="new-tech",
            biosphere_database="new-bio",
            brightway_dir=isolated_brightway_dir,
        )

    bd.projects.set_current("transaction-new")
    assert "new-tech" not in bd.databases
    assert "new-bio" not in bd.databases
    assert ("test", "new-failure") not in bd.methods


def test_failed_replace_restores_database_metadata_method_and_score(
    isolated_brightway_dir,
) -> None:
    import bw2calc as bc
    import bw2data as bd

    project = "transaction-replace"
    database = "replace-tech"
    biosphere = "replace-bio"
    method_name = ("test", "replace-failure")
    bd.projects.set_current(project)
    bd.Database(biosphere).write(
        {
            (biosphere, "flow"): {
                "name": "test flow",
                "unit": "kilogram",
                "categories": ("air",),
                "type": "emission",
            }
        }
    )
    bd.Database(database).write(
        {
            (database, "activity"): {
                "name": "original activity",
                "reference product": "original product",
                "unit": "kilogram",
                "location": "GLO",
                "exchanges": [
                    {
                        "input": (database, "activity"),
                        "amount": 1,
                        "type": "production",
                    },
                    {"input": (biosphere, "flow"), "amount": 0.2, "type": "biosphere"},
                ],
            }
        }
    )
    bd.databases[database]["original_marker"] = "keep-me"
    bd.databases.flush()
    method = bd.Method(method_name)
    method.register(unit="kilogram equivalent", original_marker="keep-me")
    method.write([((biosphere, "flow"), 3.0)])
    assert _score(bd, bc, database, method_name) == pytest.approx(0.6)

    payload = _payload(
        database=database,
        biosphere=biosphere,
        method_name=method_name,
        biosphere_amount=0.8,
        factor="not-a-number",
    )
    with pytest.raises(BrightwayError, match="was rolled back"):
        install_payload(
            payload,
            project=project,
            database=database,
            biosphere_database=biosphere,
            replace=True,
            brightway_dir=isolated_brightway_dir,
        )

    bd.projects.set_current(project)
    assert bd.databases[database]["original_marker"] == "keep-me"
    assert bd.methods[method_name]["original_marker"] == "keep-me"
    activity = bd.get_node(database=database, code="activity")
    biosphere_exchange = next(exchange for exchange in activity.biosphere())
    assert biosphere_exchange["amount"] == pytest.approx(0.2)
    assert _score(bd, bc, database, method_name) == pytest.approx(0.6)


def test_successful_replace_rewires_unrelated_existing_method(
    isolated_brightway_dir,
) -> None:
    import bw2calc as bc
    import bw2data as bd

    project = "transaction-rewire"
    database = "rewire-tech"
    biosphere = "rewire-bio"
    method_name = ("test", "unrelated-existing-method")
    bd.projects.set_current(project)
    bd.Database(biosphere).write(
        {
            (biosphere, "flow"): {
                "name": "test flow",
                "unit": "kilogram",
                "categories": ("air",),
                "type": "emission",
            }
        }
    )
    bd.Database(database).write(
        {
            (database, "activity"): {
                "name": "original activity",
                "reference product": "original product",
                "unit": "kilogram",
                "location": "GLO",
                "exchanges": [
                    {
                        "input": (database, "activity"),
                        "amount": 1,
                        "type": "production",
                    },
                    {"input": (biosphere, "flow"), "amount": 0.2, "type": "biosphere"},
                ],
            }
        }
    )
    method = bd.Method(method_name)
    method.register(unit="kilogram equivalent")
    method.write([((biosphere, "flow"), 3.0)])
    assert _score(bd, bc, database, method_name) == pytest.approx(0.6)

    payload = _payload(
        database=database,
        biosphere=biosphere,
        method_name=("unused",),
        biosphere_amount=0.8,
        factor=1,
    )
    payload.methods = []
    install_payload(
        payload,
        project=project,
        database=database,
        biosphere_database=biosphere,
        replace=True,
        brightway_dir=isolated_brightway_dir,
    )

    bd.projects.set_current(project)
    assert method_name in bd.methods
    assert _score(bd, bc, database, method_name) == pytest.approx(2.4)


def test_replace_refuses_to_break_external_database_dependencies(
    isolated_brightway_dir,
) -> None:
    import bw2data as bd

    project = "transaction-dependent"
    database = "dependency-tech"
    biosphere = "dependency-bio"
    external = "external-consumer"
    bd.projects.set_current(project)
    bd.Database(biosphere).write(
        {
            (biosphere, "flow"): {
                "name": "test flow",
                "unit": "kilogram",
                "categories": ("air",),
                "type": "emission",
            }
        }
    )
    bd.Database(database).write(
        {
            (database, "activity"): {
                "name": "provider",
                "reference product": "product",
                "unit": "kilogram",
                "location": "GLO",
                "exchanges": [
                    {
                        "input": (database, "activity"),
                        "amount": 1,
                        "type": "production",
                    }
                ],
            }
        }
    )
    bd.Database(external).write(
        {
            (external, "consumer"): {
                "name": "consumer",
                "reference product": "service",
                "unit": "kilogram",
                "location": "GLO",
                "exchanges": [
                    {
                        "input": (external, "consumer"),
                        "amount": 1,
                        "type": "production",
                    },
                    {
                        "input": (database, "activity"),
                        "amount": 1,
                        "type": "technosphere",
                    },
                ],
            }
        }
    )
    payload = _payload(
        database=database,
        biosphere=biosphere,
        method_name=("unused",),
        biosphere_amount=0.8,
        factor=1,
    )
    payload.methods = []

    with pytest.raises(BrightwayError, match="used by other Brightway databases"):
        install_payload(
            payload,
            project=project,
            database=database,
            biosphere_database=biosphere,
            replace=True,
            brightway_dir=isolated_brightway_dir,
        )

    consumer = bd.get_node(database=external, code="consumer")
    exchange = next(iter(consumer.technosphere()))
    assert exchange.input["name"] == "provider"


def _payload(
    *,
    database: str,
    biosphere: str,
    method_name: tuple[str, ...],
    biosphere_amount: float,
    factor,
) -> BrightwayPayload:
    report = MigrationReport(direction="tidas-to-brightway", source="transaction-test")
    return BrightwayPayload(
        technosphere={
            (database, "activity"): {
                "name": "replacement activity",
                "reference product": "replacement product",
                "unit": "kilogram",
                "location": "GLO",
                "exchanges": [
                    {
                        "input": (database, "activity"),
                        "amount": 1,
                        "type": "production",
                    },
                    {
                        "input": (biosphere, "flow"),
                        "amount": biosphere_amount,
                        "type": "biosphere",
                    },
                ],
            }
        },
        biosphere={
            (biosphere, "flow"): {
                "name": "test flow",
                "unit": "kilogram",
                "categories": ("air",),
                "type": "emission",
            }
        },
        methods=[
            BrightwayMethodPayload(
                name=method_name,
                factors=[((biosphere, "flow"), factor)],
                metadata={"unit": "kilogram equivalent"},
            )
        ],
        database_metadata={"format": "tidas-bw-metadata-v1"},
        report=report,
    )


def _score(bd, bc, database: str, method_name: tuple[str, ...]) -> float:
    activity = bd.get_node(database=database, code="activity")
    calculation = bc.LCA({activity: 1}, method=method_name)
    calculation.lci()
    calculation.lcia()
    return float(calculation.score)
