from __future__ import annotations

from copy import deepcopy

from tidas_bw.reverse import BrightwayExporter


def test_native_process_identity_changes_when_resolved_flow_identity_changes() -> None:
    database = "foreground"
    biosphere = "biosphere"
    data = {
        (database, "activity"): {
            "name": "test activity",
            "reference product": "test product",
            "unit": "kilogram",
            "location": "GLO",
            "exchanges": [
                {
                    "input": (database, "activity"),
                    "amount": 1,
                    "type": "production",
                },
                {"input": (biosphere, "co2"), "amount": 1, "type": "biosphere"},
            ],
        }
    }
    nodes = {
        (biosphere, "co2"): {
            "name": "Carbon dioxide, fossil",
            "unit": "kilogram",
            "type": "emission",
            "categories": ("air",),
        }
    }
    source_metadata = {
        biosphere: {
            "license": "CC BY 4.0",
            "owner": "Biosphere owner",
            "source": "Biosphere source",
        }
    }

    first = BrightwayExporter(
        data,
        {},
        nodes,
        project="identity-project",
        database=database,
        native_license="CC BY 4.0",
        native_owner="Foreground owner",
        native_source="Foreground source",
        source_database_metadata=source_metadata,
    )
    _, _, first_report = first.build()
    assert first_report.ok, first_report.to_dict()

    changed_nodes = deepcopy(nodes)
    changed_nodes[(biosphere, "co2")]["categories"] = ("air", "urban air")
    second = BrightwayExporter(
        data,
        {},
        changed_nodes,
        project="identity-project",
        database=database,
        native_license="CC BY 4.0",
        native_owner="Foreground owner",
        native_source="Foreground source",
        source_database_metadata=source_metadata,
    )
    _, _, second_report = second.build()
    assert second_report.ok, second_report.to_dict()

    key = (database, "activity")
    assert first.generated_process_ids[key] != second.generated_process_ids[key]
