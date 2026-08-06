from __future__ import annotations

import json

from tidas_bw.cli import cli
from tidas_bw.package import write_package

from ._fixtures import make_two_process_package


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
