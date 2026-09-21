from __future__ import annotations

import pytest

from tidas_bw import export_tidas, import_tidas, validate_tidas
from tidas_bw.package import read_package, write_package
from tidas_bw.templates import stable_uuid
from tidas_bw.utils import semantic_hash

from ._fixtures import make_two_process_package, with_lcia_method


def test_native_brightway_tidas_brightway_roundtrip(tmp_path, isolated_brightway_dir) -> None:
    brightway_dir = isolated_brightway_dir

    import bw2calc as bc
    import bw2data as bd

    bd.projects.set_current("source")
    biosphere = "sample-biosphere"
    database = "sample"
    bd.Database(biosphere).write(
        {
            (biosphere, "co2"): {
                "name": "Carbon dioxide, fossil",
                "unit": "kilogram",
                "categories": ("air",),
                "type": "emission",
            },
            (biosphere, "water"): {
                "name": "Water",
                "unit": "cubic meter",
                "categories": ("natural resource", "water"),
                "type": "natural resource",
            },
        }
    )
    bd.databases[biosphere].update(
        {
            "license": "CC BY 4.0",
            "owner": "Synthetic biosphere data owner",
            "source": "Synthetic biosphere integration fixture",
        }
    )
    bd.databases.flush()
    bd.Database(database).write(
        {
            (database, "supplier"): {
                "name": "electricity production",
                "reference product": "electricity",
                "unit": "kilowatt hour",
                "location": "CN",
                "exchanges": [
                    {"input": (database, "supplier"), "amount": 1, "type": "production"},
                    {"input": (biosphere, "co2"), "amount": 0.5, "type": "biosphere"},
                ],
            },
            (database, "consumer"): {
                "name": "widget production",
                "reference product": "widget",
                "unit": "kilogram",
                "location": "CN",
                "exchanges": [
                    {"input": (database, "consumer"), "amount": 1, "type": "production"},
                    {
                        "input": (database, "supplier"),
                        "amount": 2.5,
                        "type": "technosphere",
                    },
                    {"input": (biosphere, "water"), "amount": 0.01, "type": "biosphere"},
                ],
            },
        }
    )
    source_inventory = _inventory_by_flow(bd, bc, database, "widget production")

    refused_zip = tmp_path / "native-without-provenance-must-not-exist.zip"
    refused_report = export_tidas(
        project="source",
        database=database,
        output=refused_zip,
        brightway_dir=brightway_dir,
    )
    assert not refused_report.ok
    assert any(issue.code == "missing_native_open_license" for issue in refused_report.issues)
    assert not refused_zip.exists()

    first_zip = tmp_path / "native-export.zip"
    export_report = export_tidas(
        project="source",
        database=database,
        output=first_zip,
        brightway_dir=brightway_dir,
        license="CC BY 4.0",
        owner="Synthetic test data owner",
        source="Synthetic native Brightway integration fixture",
    )
    assert export_report.ok, export_report.to_dict()
    validation = validate_tidas(first_zip, strict_references=True)
    assert validation.ok, validation.to_dict()

    import_report = import_tidas(
        first_zip,
        project="target",
        database="roundtrip",
        biosphere_database="roundtrip-biosphere",
        brightway_dir=brightway_dir,
    )
    assert import_report.ok, import_report.to_dict()

    bd.projects.set_current("target")
    consumer = bd.get_node(database="roundtrip", name="widget production")
    assert consumer["reference product"] == "widget"
    assert consumer["unit"] == "kilogram"
    assert consumer["location"] == "CN"
    target_inventory = _inventory_by_flow(bd, bc, "roundtrip", "widget production")
    assert target_inventory == pytest.approx(source_inventory)
    assert target_inventory == pytest.approx(
        {("Carbon dioxide, fossil", "kilogram"): 1.25, ("Water", "cubic meter"): 0.01}
    )

    second_zip = tmp_path / "preserved-export.zip"
    second_export = export_tidas(
        project="target",
        database="roundtrip",
        output=second_zip,
        brightway_dir=brightway_dir,
    )
    assert second_export.ok, second_export.to_dict()
    assert _package_hashes(first_zip) == _package_hashes(second_zip)

    consumer = bd.get_node(database="roundtrip", name="widget production")
    next(iter(consumer.technosphere())).delete()
    changed_output = tmp_path / "changed-must-not-exist.zip"
    changed_report = export_tidas(
        project="target",
        database="roundtrip",
        output=changed_output,
        brightway_dir=brightway_dir,
    )
    assert not changed_report.ok
    assert any(issue.code == "removed_brightway_exchange" for issue in changed_report.issues)
    assert not changed_output.exists()


def test_native_export_rejects_additional_production(tmp_path, isolated_brightway_dir) -> None:
    brightway_dir = isolated_brightway_dir

    import bw2data as bd

    bd.projects.set_current("multi-output")
    database = "multi-output"
    bd.Database(database).write(
        {
            (database, "main"): {
                "name": "multi-output process",
                "reference product": "main product",
                "unit": "kilogram",
                "location": "GLO",
                "exchanges": [
                    {"input": (database, "main"), "amount": 1, "type": "production"},
                    {
                        "input": (database, "co-product"),
                        "amount": 0.2,
                        "type": "production",
                    },
                ],
            },
            (database, "co-product"): {
                "name": "co-product",
                "reference product": "co-product",
                "unit": "kilogram",
                "location": "GLO",
                "exchanges": [
                    {
                        "input": (database, "co-product"),
                        "amount": 1,
                        "type": "production",
                    }
                ],
            },
        }
    )
    output = tmp_path / "must-not-exist.zip"

    report = export_tidas(
        project="multi-output",
        database=database,
        output=output,
        brightway_dir=brightway_dir,
    )

    assert not report.ok
    assert any(issue.code == "unsupported_additional_production" for issue in report.issues)
    assert not output.exists()


def test_tidas_lcia_method_is_usable_by_brightway(tmp_path, isolated_brightway_dir) -> None:
    import bw2calc as bc
    import bw2data as bd

    package, method_name = with_lcia_method(make_two_process_package())
    source = tmp_path / "with-lcia.zip"
    write_package(package.records, source, manifest=package.manifest)

    report = import_tidas(
        source,
        project="lcia-import",
        database="lcia-database",
        biosphere_database="lcia-biosphere",
        brightway_dir=isolated_brightway_dir,
    )

    assert report.ok, report.to_dict()
    bd.projects.set_current("lcia-import")
    assert method_name in bd.methods
    assert bd.methods[method_name]["unit"] == "kilogram"
    activity = bd.get_node(database="lcia-database", name="widget production")
    calculation = bc.LCA({activity: 1}, method=method_name)
    calculation.lci()
    calculation.lcia()
    assert calculation.score == pytest.approx(3.125)

    preserved = tmp_path / "lcia-preserved.zip"
    preserved_report = export_tidas(
        project="lcia-import",
        database="lcia-database",
        output=preserved,
        brightway_dir=isolated_brightway_dir,
    )
    assert preserved_report.ok, preserved_report.to_dict()

    bd.Method(method_name).write([(("lcia-biosphere", stable_uuid("test:flow:carbon-dioxide")), 4)])
    changed = tmp_path / "lcia-changed-must-not-exist.zip"
    changed_report = export_tidas(
        project="lcia-import",
        database="lcia-database",
        output=changed,
        brightway_dir=isolated_brightway_dir,
    )
    assert not changed_report.ok
    assert any(issue.code == "changed_brightway_method" for issue in changed_report.issues)
    assert not changed.exists()


def test_parameterized_exchange_roundtrip_preserves_mean_and_blocks_unsafe_edit(
    tmp_path, isolated_brightway_dir
) -> None:
    import bw2data as bd

    package = make_two_process_package()
    consumer_record = next(
        record
        for record in package.records
        if record.category == "processes" and record.uuid == stable_uuid("test:process:consumer")
    )
    raw_exchange = consumer_record.document["processDataSet"]["exchanges"]["exchange"][1]
    raw_exchange["referenceToVariable"] = "electricity_multiplier"
    raw_exchange["meanAmount"] = "1.25"
    raw_exchange["resultingAmount"] = "2.5"
    source = tmp_path / "parameterized.zip"
    write_package(package.records, source, manifest=package.manifest)

    imported = import_tidas(
        source,
        project="parameterized-project",
        database="parameterized-db",
        brightway_dir=isolated_brightway_dir,
    )
    assert imported.ok, imported.to_dict()

    unchanged = tmp_path / "parameterized-unchanged.zip"
    unchanged_report = export_tidas(
        project="parameterized-project",
        database="parameterized-db",
        output=unchanged,
        brightway_dir=isolated_brightway_dir,
    )
    assert unchanged_report.ok, unchanged_report.to_dict()
    restored = next(
        record
        for record in read_package(unchanged).records
        if record.category == "processes" and record.uuid == stable_uuid("test:process:consumer")
    )
    restored_exchange = restored.document["processDataSet"]["exchanges"]["exchange"][1]
    assert restored_exchange["referenceToVariable"] == "electricity_multiplier"
    assert restored_exchange["meanAmount"] == "1.25"
    assert restored_exchange["resultingAmount"] == "2.5"

    bd.projects.set_current("parameterized-project")
    consumer = bd.get_node(database="parameterized-db", name="widget production")
    exchange = next(iter(consumer.technosphere()))
    exchange["amount"] = 3
    exchange.save()
    changed = tmp_path / "parameterized-changed-must-not-exist.zip"
    changed_report = export_tidas(
        project="parameterized-project",
        database="parameterized-db",
        output=changed,
        brightway_dir=isolated_brightway_dir,
    )
    assert not changed_report.ok
    assert any(issue.code == "changed_parameterized_exchange" for issue in changed_report.issues)
    assert not changed.exists()


def test_preserved_export_rejects_added_activity_metadata(tmp_path, isolated_brightway_dir) -> None:
    import bw2data as bd

    source = tmp_path / "metadata-source.zip"
    package = make_two_process_package()
    write_package(package.records, source, manifest=package.manifest)
    imported = import_tidas(
        source,
        project="metadata-project",
        database="metadata-db",
        brightway_dir=isolated_brightway_dir,
    )
    assert imported.ok, imported.to_dict()

    bd.projects.set_current("metadata-project")
    consumer = bd.get_node(database="metadata-db", name="widget production")
    consumer["reference year"] = 2035
    consumer["categories"] = ("manually edited",)
    consumer.save()

    output = tmp_path / "metadata-changed-must-not-exist.zip"
    report = export_tidas(
        project="metadata-project",
        database="metadata-db",
        output=output,
        brightway_dir=isolated_brightway_dir,
    )

    assert not report.ok
    assert any(issue.code == "unsupported_activity_metadata_change" for issue in report.issues)
    assert not output.exists()


def test_preserved_export_rejects_changed_unparameterized_amount(
    tmp_path, isolated_brightway_dir
) -> None:
    import bw2data as bd

    source = tmp_path / "amount-source.zip"
    package = make_two_process_package()
    write_package(package.records, source, manifest=package.manifest)
    imported = import_tidas(
        source,
        project="amount-project",
        database="amount-db",
        brightway_dir=isolated_brightway_dir,
    )
    assert imported.ok, imported.to_dict()

    bd.projects.set_current("amount-project")
    consumer = bd.get_node(database="amount-db", name="widget production")
    exchange = next(iter(consumer.technosphere()))
    exchange["amount"] = 3
    exchange.save()

    output = tmp_path / "amount-changed-must-not-exist.zip"
    report = export_tidas(
        project="amount-project",
        database="amount-db",
        output=output,
        brightway_dir=isolated_brightway_dir,
    )

    assert not report.ok
    assert any(issue.code == "changed_exchange_amount" for issue in report.issues)
    assert not output.exists()


def test_preserved_export_rejects_mixed_tidas_and_native_activities(
    tmp_path, isolated_brightway_dir
) -> None:
    import bw2data as bd

    source = tmp_path / "mixed-source.zip"
    package = make_two_process_package()
    write_package(package.records, source, manifest=package.manifest)
    imported = import_tidas(
        source,
        project="mixed-project",
        database="mixed-db",
        brightway_dir=isolated_brightway_dir,
    )
    assert imported.ok, imported.to_dict()

    bd.projects.set_current("mixed-project")
    data = bd.Database("mixed-db").load()
    data[("mixed-db", "manual-activity")] = {
        "name": "manually added activity",
        "reference product": "manual product",
        "unit": "kilogram",
        "location": "GLO",
        "exchanges": [
            {
                "input": ("mixed-db", "manual-activity"),
                "amount": 1,
                "type": "production",
            }
        ],
    }
    bd.Database("mixed-db").write(data)

    output = tmp_path / "mixed-must-not-exist.zip"
    report = export_tidas(
        project="mixed-project",
        database="mixed-db",
        output=output,
        brightway_dir=isolated_brightway_dir,
    )
    assert not report.ok
    assert any(issue.code == "mixed_or_incomplete_tidas_metadata" for issue in report.issues)
    assert not output.exists()


def test_preserved_export_detects_relink_to_same_code_in_other_database(
    tmp_path, isolated_brightway_dir
) -> None:
    import bw2data as bd

    source = tmp_path / "relink-source.zip"
    package = make_two_process_package()
    write_package(package.records, source, manifest=package.manifest)
    imported = import_tidas(
        source,
        project="relink-project",
        database="relink-db",
        brightway_dir=isolated_brightway_dir,
    )
    assert imported.ok, imported.to_dict()

    bd.projects.set_current("relink-project")
    supplier_code = stable_uuid("test:process:supplier").lower()
    bd.Database("other-db").write(
        {
            ("other-db", supplier_code): {
                "name": "different provider with reused code",
                "reference product": "electricity",
                "unit": "kilowatt hour",
                "location": "US",
                "exchanges": [
                    {
                        "input": ("other-db", supplier_code),
                        "amount": 1,
                        "type": "production",
                    }
                ],
            }
        }
    )
    consumer = bd.get_node(database="relink-db", name="widget production")
    exchange = next(iter(consumer.technosphere()))
    exchange["input"] = ("other-db", supplier_code)
    exchange.save()

    output = tmp_path / "relinked-must-not-exist.zip"
    report = export_tidas(
        project="relink-project",
        database="relink-db",
        output=output,
        brightway_dir=isolated_brightway_dir,
    )
    assert not report.ok
    assert any(issue.code == "changed_exchange_link" for issue in report.issues)
    assert not output.exists()


def test_empty_database_and_processless_package_are_rejected(
    tmp_path, isolated_brightway_dir
) -> None:
    import bw2data as bd

    bd.projects.set_current("empty-source")
    bd.Database("empty").register()
    empty_output = tmp_path / "empty.zip"
    export_report = export_tidas(
        project="empty-source",
        database="empty",
        output=empty_output,
        brightway_dir=isolated_brightway_dir,
    )
    assert not export_report.ok
    assert any(issue.code == "empty_brightway_database" for issue in export_report.issues)
    assert not empty_output.exists()

    package = make_two_process_package()
    auxiliary = [
        record
        for record in package.records
        if record.category not in {"processes", "lifecyclemodels"}
    ]
    processless = tmp_path / "processless.zip"
    write_package(auxiliary, processless)
    import_report = import_tidas(
        processless,
        project="processless-target",
        database="must-not-exist",
        brightway_dir=isolated_brightway_dir,
    )
    assert not import_report.ok
    assert any(issue.code == "no_process_datasets" for issue in import_report.issues)


def _inventory_by_flow(bd, bc, database: str, activity_name: str):
    activity = bd.get_node(database=database, name=activity_name)
    calculation = bc.LCA({activity: 1})
    calculation.lci()
    result = {}
    for row, node_id in calculation.dicts.biosphere.reversed.items():
        flow = bd.get_node(id=node_id)
        result[(flow["name"], flow["unit"])] = float(calculation.inventory[row, :].sum())
    return result


def _package_hashes(path):
    package = read_package(path)
    return {
        (record.category, record.uuid.lower(), record.version): semantic_hash(record.document)
        for record in package.records
    }
