from __future__ import annotations

import json
import warnings
import zipfile

import pytest

from tidas_bw.errors import PackageError
from tidas_bw.models import DatasetRecord
from tidas_bw.package import read_package, write_package

from ._fixtures import make_two_process_package


def test_zip_output_is_deterministic_and_readable(tmp_path) -> None:
    package = make_two_process_package()
    first = tmp_path / "first.zip"
    second = tmp_path / "second.zip"

    write_package(package.records, first, manifest={"format": "test"})
    write_package(package.records, second, manifest={"format": "test"})

    assert first.read_bytes() == second.read_bytes()
    loaded = read_package(first)
    assert loaded.counts() == package.counts()
    # "manifest.json" is the package-metadata name the official tidas tool
    # ignores; arbitrary root-level JSON names break its eILCD projection.
    assert loaded.ignored_json == []
    assert loaded.manifest == {"format": "test"}


def test_reader_accepts_platform_json_wrapper(tmp_path) -> None:
    package = make_two_process_package()
    process = next(record for record in package.records if record.category == "processes")
    source = tmp_path / "wrapped"
    source.mkdir()
    (source / "arbitrary-name.json").write_text(
        json.dumps({"json": process.document}, ensure_ascii=False),
        encoding="utf-8",
    )

    loaded = read_package(source)

    assert len(loaded.records) == 1
    assert loaded.records[0].uuid == process.uuid


@pytest.mark.parametrize("name", ("../escape.json", "folder\\escape.json"))
def test_reader_rejects_unsafe_zip_paths(tmp_path, name: str) -> None:
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr(name, "{}")

    with pytest.raises(PackageError, match="unsafe ZIP entry path"):
        read_package(archive)


def test_reader_rejects_duplicate_zip_member_names(tmp_path) -> None:
    archive = tmp_path / "duplicate.zip"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(archive, "w") as handle:
            handle.writestr("same.json", "{}")
            handle.writestr("same.json", "{}")

    with pytest.raises(PackageError, match="duplicate ZIP entry name"):
        read_package(archive)


def test_writer_rejects_unsafe_identity_path(tmp_path) -> None:
    record = DatasetRecord(
        category="processes",
        root_key="processDataSet",
        uuid="../escape",
        version="01.00.000",
        document={"processDataSet": {}},
        source_path="memory",
    )

    with pytest.raises(PackageError, match="unsafe TIDAS UUID"):
        write_package([record], tmp_path / "unsafe-output")


def test_reader_rejects_non_standard_json_numbers(tmp_path) -> None:
    source = tmp_path / "bad-json"
    source.mkdir()
    (source / "bad.json").write_text('{"processDataSet": NaN}', encoding="utf-8")

    with pytest.raises(PackageError, match="non-standard numeric constant"):
        read_package(source)


def test_writer_never_follows_output_symlink(tmp_path) -> None:
    package = make_two_process_package()
    real_target = tmp_path / "real.zip"
    real_target.write_bytes(b"keep this content")
    link = tmp_path / "linked.zip"
    link.symlink_to(real_target)

    with pytest.raises(PackageError, match="symbolic link"):
        write_package(package.records, link, overwrite=True)

    assert real_target.read_bytes() == b"keep this content"
