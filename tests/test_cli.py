from __future__ import annotations

import json

from tidas_bw.cli import cli
from tidas_bw.package import record_from_document, write_package

from ._fixtures import make_two_process_package, stable_uuid


def test_validate_cli_returns_machine_readable_success(tmp_path, capsys) -> None:
    source = tmp_path / "cli-input.zip"
    write_package(make_two_process_package().records, source)

    exit_code = cli(["validate", str(source), "--strict-references", "--json"])

    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert exit_code == 0
    assert result["ok"] is True
    assert result["direction"] == "validate"
    assert result["counts"]["processes"] == 2


def test_preflight_cli_reports_mappability_without_writing(tmp_path, capsys) -> None:
    source = tmp_path / "ok"
    write_package(make_two_process_package().records, source)

    exit_code = cli(["preflight", str(source), "--database", "probe", "--json"])

    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert exit_code == 0
    assert result["ok"] is True
    assert result["metadata"]["preflight"] is True
    assert result["target"].endswith("[preflight; nothing written]")
    codes = {issue["code"] for issue in result["issues"]}
    assert {"package_checks_passed", "mapping_checks_passed", "preflight_no_write"} <= codes


def test_preflight_cli_surfaces_mapping_failures_that_validation_misses(
    tmp_path, capsys
) -> None:
    package = make_two_process_package()
    consumer = next(
        record
        for record in package.records
        if record.category == "processes" and record.uuid == stable_uuid("test:process:consumer")
    )
    exchange = consumer.document["processDataSet"]["exchanges"]["exchange"][1]
    exchange["uncertaintyDistributionType"] = "triangular"
    exchange["minimumAmount"] = "0.5"
    exchange["maximumAmount"] = "1.5"
    package.records[package.records.index(consumer)] = record_from_document(
        consumer.document, source_path=consumer.source_path
    )
    source = tmp_path / "triangular"
    write_package(package.records, source)

    validate_exit = cli(["validate", str(source), "--json"])
    validate_result = json.loads(capsys.readouterr().out)
    assert validate_exit == 0
    assert validate_result["ok"] is True

    preflight_exit = cli(["preflight", str(source), "--database", "probe", "--json"])
    preflight_result = json.loads(capsys.readouterr().out)
    assert preflight_exit == 2
    assert preflight_result["ok"] is False
    assert preflight_result["metadata"]["preflight"] is True
    assert any(
        issue["code"] == "unmappable_exchange_uncertainty" for issue in preflight_result["issues"]
    )
