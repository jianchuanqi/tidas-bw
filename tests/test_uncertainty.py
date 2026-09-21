"""Uncertainty conversion tests: spec formulas, seeded sampling, and roundtrip."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from tidas_bw.mapping import TidasMapper
from tidas_bw.package import record_from_document
from tidas_bw.uncertainty import convert_uncertainty
from tidas_bw.validation import validate_package

from ._fixtures import make_two_process_package, stable_uuid

SEED = 20260921
SAMPLES = 400_000


def test_normal_uses_specified_2sd_percentage() -> None:
    # Spec: the field states 2*SD as a percentage of the mean value.
    result = convert_uncertainty(
        distribution="normal", rsd95="20", minimum=None, maximum=None, amount=2.5
    )
    assert result.error is None
    assert result.fields["uncertainty type"] == 3
    assert result.fields["loc"] == pytest.approx(2.5)
    assert result.fields["scale"] == pytest.approx(2.5 * 20 / 200)


def test_normal_sigma_is_positive_for_negative_amounts() -> None:
    result = convert_uncertainty(
        distribution="normal", rsd95="10", minimum=None, maximum=None, amount=-3.0
    )
    assert result.error is None
    assert result.fields["scale"] == pytest.approx(3.0 * 10 / 200)
    assert result.fields["loc"] == pytest.approx(-3.0)


def test_lognormal_interprets_field_as_sdg_squared_percentage() -> None:
    # Spec: the field states SDg^2; SDg = sqrt(field/100), scale = ln(SDg).
    result = convert_uncertainty(
        distribution="log-normal", rsd95="125", minimum=None, maximum=None, amount=2.0
    )
    assert result.error is None
    assert result.fields["uncertainty type"] == 2
    assert result.fields["loc"] == pytest.approx(np.log(2.0))
    assert result.fields["scale"] == pytest.approx(0.5 * np.log(1.25))


def test_uniform_maps_min_max_and_requires_amount_inside() -> None:
    result = convert_uncertainty(
        distribution="uniform", rsd95=None, minimum="0.9", maximum="1.1", amount=1.0
    )
    assert result.error is None
    assert result.fields == {
        "uncertainty type": 4,
        "minimum": pytest.approx(0.9),
        "maximum": pytest.approx(1.1),
    }
    outside = convert_uncertainty(
        distribution="uniform", rsd95=None, minimum="1.5", maximum="2.0", amount=1.0
    )
    assert outside.error and "does not contain" in outside.error


def test_triangular_is_rejected_because_spec_defines_no_mode() -> None:
    result = convert_uncertainty(
        distribution="triangular", rsd95=None, minimum="0.5", maximum="1.5", amount=1.0
    )
    assert result.error and "no mode parameter" in result.error


def test_contradictory_and_incomplete_parameter_sets_are_rejected() -> None:
    cases = [
        ({"distribution": "normal", "rsd95": "20", "minimum": "1", "maximum": "2"}, "must remain empty"),
        ({"distribution": "log-normal", "rsd95": "50", "minimum": None, "maximum": None}, "SDg^2"),
        ({"distribution": "uniform", "rsd95": None, "minimum": "2", "maximum": "1"}, "greater than maximum"),
        ({"distribution": "uniform", "rsd95": None, "minimum": None, "maximum": "1"}, "require both"),
        ({"distribution": "beta", "rsd95": None, "minimum": None, "maximum": None}, "Unknown uncertaintyDistributionType"),
        ({"distribution": None, "rsd95": "20", "minimum": None, "maximum": None}, "cannot be determined"),
        ({"distribution": "log-normal", "rsd95": "125", "minimum": None, "maximum": None}, "positive amount"),
    ]
    amounts = [1.0, 1.0, 1.5, 1.0, 1.0, 1.0, -2.0]
    for (kwargs, fragment), amount in zip(cases, amounts, strict=False):
        result = convert_uncertainty(amount=amount, **kwargs)
        assert result.error, f"expected rejection for {kwargs}"
        if fragment:
            assert fragment in result.error, f"{fragment!r} not in {result.error!r}"


def test_degenerate_uncertainty_maps_to_deterministic_with_note() -> None:
    result = convert_uncertainty(
        distribution="normal", rsd95="0", minimum=None, maximum=None, amount=1.0
    )
    assert result.error is None
    assert result.fields == {}
    assert result.notes and "deterministic" in result.notes[0]
    lognormal = convert_uncertainty(
        distribution="log-normal", rsd95="100", minimum=None, maximum=None, amount=1.0
    )
    assert lognormal.error is None
    assert lognormal.fields == {}
    assert lognormal.notes and "deterministic" in lognormal.notes[0]


def test_sampled_moments_match_spec_conversion_formulas() -> None:
    rng = np.random.default_rng(SEED)
    normal = convert_uncertainty(
        distribution="normal", rsd95="20", minimum=None, maximum=None, amount=1.0
    )
    samples = rng.normal(
        loc=normal.fields["loc"], scale=normal.fields["scale"], size=SAMPLES
    )
    assert abs(samples.mean() - 1.0) < 0.002
    assert abs(samples.std() - 0.1) < 0.002

    lognormal = convert_uncertainty(
        distribution="log-normal", rsd95="125", minimum=None, maximum=None, amount=2.0
    )
    log_samples = rng.lognormal(
        mean=lognormal.fields["loc"],
        sigma=lognormal.fields["scale"],
        size=SAMPLES,
    )
    assert abs(np.exp(np.log(log_samples).mean()) - 2.0) < 0.005
    assert abs(np.log(log_samples).std() - 0.5 * np.log(1.25)) < 0.002

    uniform = convert_uncertainty(
        distribution="uniform", rsd95=None, minimum="0.9", maximum="1.1", amount=1.0
    )
    uniform_samples = rng.uniform(
        low=uniform.fields["minimum"], high=uniform.fields["maximum"], size=SAMPLES
    )
    assert abs(uniform_samples.mean() - 1.0) < 0.001
    assert abs(uniform_samples.std() - 0.2 / np.sqrt(12)) < 0.001


def _package_with_normal_uncertainty():
    package = make_two_process_package()
    consumer = next(
        record
        for record in package.records
        if record.category == "processes" and record.uuid == stable_uuid("test:process:consumer")
    )
    exchange = consumer.document["processDataSet"]["exchanges"]["exchange"][1]
    exchange["uncertaintyDistributionType"] = "normal"
    exchange["relativeStandardDeviation95In"] = "20"
    replacement = record_from_document(consumer.document, source_path=consumer.source_path)
    package.records[package.records.index(consumer)] = replacement
    return package, consumer


def test_exchange_uncertainty_maps_into_brightway_payload() -> None:
    package, _consumer = _package_with_normal_uncertainty()
    validation = validate_package(package, require_open=True, strict_references=True)
    assert validation.ok, validation.to_dict()

    payload = TidasMapper(
        package,
        database="foreground",
        biosphere_database="foreground-biosphere",
    ).build()

    assert payload.report.ok, payload.report.to_dict()
    mapped_codes = {
        issue.code
        for issue in payload.report.issues
        if issue.code == "exchange_uncertainty_mapped"
    }
    assert mapped_codes
    uncertain_exchanges = [
        exchange
        for dataset in payload.technosphere.values()
        for exchange in dataset["exchanges"]
        if "uncertainty type" in exchange
    ]
    assert len(uncertain_exchanges) == 1
    fields = uncertain_exchanges[0]
    assert fields["uncertainty type"] == 3
    assert fields["scale"] == pytest.approx(fields["amount"] * 20 / 200)


def test_imported_uncertainty_roundtrips_and_detects_brightway_edits(
    tmp_path, isolated_brightway_dir
) -> None:
    import bw2data as bd

    from tidas_bw import export_tidas, import_tidas

    package, _consumer = _package_with_normal_uncertainty()
    source = tmp_path / "package"
    from tidas_bw.package import write_package

    write_package(package.records, source, manifest=package.manifest)

    brightway_dir = Path(isolated_brightway_dir)
    report = import_tidas(
        source,
        project="uncertainty-roundtrip",
        database="uncertainty-db",
        brightway_dir=brightway_dir,
    )
    assert report.ok, report.to_dict()

    bd.projects.set_current("uncertainty-roundtrip")
    exchange = next(
        exchange
        for dataset in bd.Database("uncertainty-db").load().values()
        for exchange in dataset["exchanges"]
        if exchange.get("type") == "technosphere"
    )
    assert exchange["uncertainty type"] == 3

    unchanged = export_tidas(
        project="uncertainty-roundtrip",
        database="uncertainty-db",
        output=tmp_path / "roundtrip-ok",
        brightway_dir=brightway_dir,
    )
    assert unchanged.ok, unchanged.to_dict()

    data = bd.Database("uncertainty-db").load()
    for dataset in data.values():
        for exchange in dataset.get("exchanges", []):
            if "uncertainty type" in exchange:
                exchange["scale"] = exchange["scale"] * 2
    bd.Database("uncertainty-db").write(data)

    edited = export_tidas(
        project="uncertainty-roundtrip",
        database="uncertainty-db",
        output=tmp_path / "roundtrip-edited",
        brightway_dir=brightway_dir,
    )
    assert not edited.ok
    assert any(
        issue.code == "changed_exchange_uncertainty" for issue in edited.issues
    )
