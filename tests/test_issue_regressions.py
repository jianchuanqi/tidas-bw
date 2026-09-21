"""Acceptance checks using actual Brightway matrices and stochastic calculation."""

from decimal import Decimal

import numpy as np
import pytest

from tidas_bw import export_tidas, import_tidas, preflight_import
from tidas_bw.models import MigrationReport
from tidas_bw.package import read_package, write_package
from tidas_bw.precision import PrecisionPolicy
from tidas_bw.uncertainty import convert_uncertainty
from tidas_bw.utils import semantic_hash
from tidas_bw.validation import validate_package

from ._fixtures import make_two_process_package, stable_uuid, with_lcia_method


def consumer_exchange(package):
    consumer = next(
        r
        for r in package.by_category("processes")
        if r.uuid == stable_uuid("test:process:consumer")
    )
    return consumer.document["processDataSet"]["exchanges"]["exchange"][1]


@pytest.mark.parametrize("distribution", ["normal", "log-normal", "uniform"])
@pytest.mark.parametrize("target", ["exchange", "factor"])
def test_real_brightway_sampling_and_parameter_roundtrip(
    tmp_path, isolated_brightway_dir, distribution, target
):
    import bw2calc as bc
    import bw2data as bd

    package, method = with_lcia_method(make_two_process_package())
    declaration = (
        consumer_exchange(package)
        if target == "exchange"
        else package.by_category("lciamethods")[0].document["LCIAMethodDataSet"][
            "characterisationFactors"
        ]["factor"]
    )
    declaration["uncertaintyDistributionType"] = distribution
    if distribution == "uniform":
        suffix = "Amount" if target == "exchange" else "Value"
        declaration[f"minimum{suffix}"] = "2"
        declaration[f"maximum{suffix}"] = "3"
        mean, std = 2.5, 1 / np.sqrt(12)
    elif distribution == "normal":
        declaration["relativeStandardDeviation95In"] = "20"
        mean, std = 2.5, 0.25
    else:
        declaration["relativeStandardDeviation95In"] = "125"
        scale = np.log(1.25) / 2
        mean = 2.5 * np.exp(scale**2 / 2)
        std = mean * np.sqrt(np.exp(scale**2) - 1)
    source = tmp_path / "source"
    write_package(package.records, source, manifest=package.manifest)
    assert preflight_import(source, database="data").ok
    report = import_tidas(
        source,
        project=f"sampling-{target}-{distribution}",
        database="data",
        brightway_dir=isolated_brightway_dir,
    )
    assert report.ok, report.to_dict()
    node = bd.get_node(database="data", name="widget production")
    lca = bc.LCA({node: 1}, method=method, use_distributions=True, seed_override=20260921)
    lca.lci()
    lca.lcia()
    scores = []
    for _ in range(2048):
        next(lca)
        scores.append(lca.score)
    # Only one parameter is uncertain: score = parameter * 0.5 * 2.5.
    assert np.mean(scores) == pytest.approx(mean * 1.25, rel=0.025)
    assert np.std(scores) == pytest.approx(std * 1.25, rel=0.08)
    output = tmp_path / "restored"
    result = export_tidas(
        project=bd.projects.current,
        database="data",
        output=output,
        brightway_dir=isolated_brightway_dir,
    )
    assert result.ok, result.to_dict()
    restored = read_package(output)
    assert {r.identity: r.document for r in restored.records} == {
        r.identity: r.document for r in package.records
    }
    assert restored.manifest["license_evidence"] == package.manifest["license_evidence"]

    # A small but real change must not be hidden by rounding signatures to 12 digits.
    field = "minimum" if distribution == "uniform" else "scale"
    if target == "factor":
        rows = bd.Method(method).load()
        rows[0][1][field] += 1e-13
        bd.Method(method).write(rows)
    else:
        exchange = next(iter(node.technosphere()))
        exchange[field] += 1e-13
        exchange.save()
    changed = export_tidas(
        project=bd.projects.current,
        database="data",
        output=tmp_path / "changed",
        brightway_dir=isolated_brightway_dir,
    )
    assert not changed.ok
    assert any(
        i.code in {"changed_exchange_uncertainty", "changed_brightway_method"}
        for i in changed.issues
    )
    assert not (tmp_path / "changed").exists()


@pytest.mark.parametrize(
    "params",
    [
        dict(distribution="normal", rsd95="1e308", minimum=None, maximum=None, amount=1e308),
        dict(distribution="normal", rsd95="1e-308", minimum=None, maximum=None, amount=1e-308),
        dict(distribution="normal", rsd95="20", minimum=None, maximum=None, amount=0),
        dict(distribution="uniform", rsd95="20", minimum="2", maximum="3", amount=2.5),
        dict(distribution="uniform", rsd95=None, minimum="-1e400", maximum="1e400", amount=2.5),
        dict(
            distribution="uniform",
            rsd95=None,
            minimum="1.00000000000000001",
            maximum="1.00000000000000002",
            amount=1,
        ),
        dict(distribution="uniform", rsd95=None, minimum="2e-20", maximum="3e-20", amount=0),
    ],
)
def test_invalid_uncertainty_is_never_simplified(params):
    assert convert_uncertainty(**params).error


@pytest.mark.parametrize(
    "mutation", ["missing", "owner", "source", "restricted", "hash", "version"]
)
def test_structural_license_evidence_cannot_be_inherited(mutation):
    package = make_two_process_package()
    record = package.by_category("unitgroups")[0]
    evidence = package.manifest["license_evidence"][record.identity]
    if mutation == "missing":
        del package.manifest["license_evidence"][record.identity]
    elif mutation in {"owner", "source"}:
        evidence[mutation] = ""
    elif mutation == "restricted":
        evidence["license"] = "CC BY-NC 4.0"
    elif mutation == "hash":
        evidence["document_sha256"] = "0" * 64
    else:
        record.version = "02.00.000"
    report = validate_package(package, require_open=True)
    assert not report.ok
    assert any(
        i.dataset == record.identity and i.code in {"open_license_not_proven", "restricted_license"}
        for i in report.issues
    )


@pytest.mark.parametrize("target", ["exchange", "factor"])
def test_strict_preflight_then_explicit_precision_and_actual_matrix(
    tmp_path, isolated_brightway_dir, target
):
    import bw2calc as bc
    import bw2data as bd

    package, method = with_lcia_method(make_two_process_package())
    original = "0.123456789012345678"
    if target == "exchange":
        consumer_exchange(package).update(meanAmount=original, resultingAmount=original)
    else:
        package.by_category("lciamethods")[0].document["LCIAMethodDataSet"][
            "characterisationFactors"
        ]["factor"]["meanValue"] = original
    source = tmp_path / "source"
    write_package(package.records, source, manifest=package.manifest)
    refused = import_tidas(
        source, project="must-not-exist", database="data", brightway_dir=tmp_path / "no-data"
    )
    assert not refused.ok
    assert not (tmp_path / "no-data").exists()
    assert not preflight_import(source, database="data").ok
    assert not preflight_import(
        source, database="data", allow_rounding=True, relative_tolerance="1e-20"
    ).ok
    options = dict(allow_rounding=True, relative_tolerance="1e-15")
    assert preflight_import(source, database="data", **options).ok
    report = import_tidas(
        source,
        project=f"precision-{target}",
        database="data",
        brightway_dir=isolated_brightway_dir,
        **options,
    )
    assert report.ok, report.to_dict()
    rounded = next(i for i in report.issues if i.code.endswith("amount_rounded"))
    assert "exact binary value" in rounded.message and "relative error" in rounded.message
    node = bd.get_node(database="data", name="widget production")
    lca = bc.LCA({node: 1}, method=method)
    lca.lci()
    lca.lcia()
    if target == "exchange":
        saved = next(iter(node.technosphere()))["amount"]
        supplier = bd.get_node(database="data", name="electricity production")
        matrix = -lca.technosphere_matrix[
            lca.dicts.product[supplier.id], lca.dicts.activity[node.id]
        ]
    else:
        saved = bd.Method(method).load()[0][1]
        co2 = bd.get_node(database="data-biosphere", name="Carbon dioxide, fossil")
        matrix = lca.characterization_matrix[
            lca.dicts.biosphere[co2.id], lca.dicts.biosphere[co2.id]
        ]
    assert saved == matrix == float(original)
    assert lca.score == pytest.approx(float(Decimal(original) * Decimal("1.25")), rel=1e-14)
    out = tmp_path / "restored"
    assert export_tidas(
        project=bd.projects.current,
        database="data",
        output=out,
        brightway_dir=isolated_brightway_dir,
    ).ok
    assert {r.identity: semantic_hash(r.document) for r in read_package(out).records} == {
        r.identity: semantic_hash(r.document) for r in package.records
    }


@pytest.mark.parametrize("value", ["nan", "Infinity", "1e400", "1e-400"])
def test_invalid_numeric_range_is_rejected_even_with_tolerance(value):
    report = MigrationReport(direction="validate", source="test")
    assert (
        PrecisionPolicy(True, "1", "1").convert(value, report, dataset="test", path="amount")
        is None
    )
    assert not report.ok


@pytest.mark.parametrize("value", ["0", "0.01", "-2.5", "1e-300", "1e300"])
def test_strict_ordinary_decimal_contract(value):
    report = MigrationReport(direction="validate", source="test")
    assert PrecisionPolicy().convert(value, report, dataset="test", path="amount") == float(value)
    assert report.ok


def test_near_zero_uses_explicit_absolute_tolerance():
    value = "0.000000000000000000001234567890123456789"
    report = MigrationReport(direction="validate", source="test")
    assert (
        PrecisionPolicy(True, "1e-35", "0").convert(value, report, dataset="test", path="amount")
        is not None
    )
    assert report.ok
    report = MigrationReport(direction="validate", source="test")
    assert (
        PrecisionPolicy(True, "1e-40", "0").convert(value, report, dataset="test", path="amount")
        is None
    )


def test_preflight_does_not_map_invalid_schema(tmp_path):
    package = make_two_process_package()
    package.by_category("processes")[0].document["processDataSet"]["exchanges"] = {
        "exchange": "broken"
    }
    source = tmp_path / "broken"
    write_package(package.records, source, manifest=package.manifest)
    report = preflight_import(source, database="data")
    assert not report.ok
    assert report.metadata["package_ok"] is False
    assert report.metadata["mapping_ok"] is None


def test_float64_replacement_failure_restores_actual_calculation(
    tmp_path, isolated_brightway_dir, monkeypatch
):
    import bw2calc as bc
    import bw2data as bd

    import tidas_bw.brightway as boundary
    from tidas_bw.errors import BrightwayError

    package, method = with_lcia_method(make_two_process_package())
    consumer_exchange(package).update(
        meanAmount="0.1234567890123456", resultingAmount="0.1234567890123456"
    )
    source = tmp_path / "original"
    write_package(package.records, source, manifest=package.manifest)
    assert import_tidas(
        source, project="rollback-float64", database="data", brightway_dir=isolated_brightway_dir
    ).ok

    def score():
        node = bd.get_node(database="data", name="widget production")
        lca = bc.LCA({node: 1}, method=method)
        lca.lci()
        lca.lcia()
        return lca.score

    before = score()
    writer = boundary.write_database_calculation_data
    calls = []

    def fail_once(*args, **kwargs):
        calls.append(True)
        if len(calls) == 1:
            raise OSError("simulated calculation file failure")
        return writer(*args, **kwargs)

    monkeypatch.setattr(boundary, "write_database_calculation_data", fail_once)
    with pytest.raises(BrightwayError, match="was rolled back"):
        import_tidas(
            source,
            project="rollback-float64",
            database="data",
            replace=True,
            brightway_dir=isolated_brightway_dir,
        )
    assert score() == before


@pytest.mark.parametrize(
    "distribution,fields",
    [
        ("triangular", {"minimumAmount": "2", "maximumAmount": "3"}),
        ("normal", {"relativeStandardDeviation95In": "20", "maximumAmount": "3"}),
        ("log-normal", {"relativeStandardDeviation95In": "50"}),
    ],
)
def test_unsupported_uncertainty_stops_before_creating_data(tmp_path, distribution, fields):
    package = make_two_process_package()
    consumer_exchange(package).update(uncertaintyDistributionType=distribution, **fields)
    write_package(package.records, tmp_path / "source", manifest=package.manifest)
    result = import_tidas(
        tmp_path / "source", project="not-created", database="data", brightway_dir=tmp_path / "bw"
    )
    assert not result.ok
    assert not (tmp_path / "bw").exists()


def test_positive_log_deviation_near_one_is_not_erased():
    mapped = convert_uncertainty(
        distribution="log-normal",
        rsd95="100.00000000000000000000000000001",
        minimum=None,
        maximum=None,
        amount=2.5,
    )
    assert not mapped.error
    assert mapped.fields["scale"] > 0


@pytest.mark.parametrize(
    "distribution,rsd", [("normal", "20"), ("log-normal", "125"), ("uniform", None)]
)
def test_distribution_parameters_follow_unit_scale(distribution, rsd):
    results = []
    for ratio in (1, 1000):
        mapped = convert_uncertainty(
            distribution=distribution,
            rsd95=rsd,
            minimum=str(2 * ratio) if distribution == "uniform" else None,
            maximum=str(3 * ratio) if distribution == "uniform" else None,
            amount=2.5 * ratio,
        )
        assert not mapped.error
        results.append(mapped.fields)
    a, b = results
    if distribution == "normal":
        assert b["loc"] == a["loc"] * 1000
        assert b["scale"] == a["scale"] * 1000
    elif distribution == "log-normal":
        assert b["loc"] == pytest.approx(a["loc"] + np.log(1000))
        assert b["scale"] == a["scale"]
    else:
        assert b["minimum"] == a["minimum"] * 1000
        assert b["maximum"] == a["maximum"] * 1000


@pytest.mark.parametrize("reason", ["variable", "different_mean", "correlation"])
def test_uncertain_parameter_relationships_are_refused(reason):
    from tidas_bw.mapping import TidasMapper

    package = make_two_process_package()
    ex = consumer_exchange(package)
    ex.update(uncertaintyDistributionType="normal", relativeStandardDeviation95In="20")
    if reason == "variable":
        ex["referenceToVariable"] = "shared_parameter"
    elif reason == "different_mean":
        ex["meanAmount"] = "1"
    else:
        ex["common:other"] = {"correlationMatrix": [[1, 0.5], [0.5, 1]]}
    report = TidasMapper(package, database="data", biosphere_database="bio").build().report
    assert not report.ok
    assert any(
        i.code in {"unsupported_parameterized_uncertainty", "unsupported_correlated_uncertainty"}
        for i in report.issues
    )


def test_negative_normal_runs_in_actual_brightway(tmp_path, isolated_brightway_dir):
    import bw2calc as bc
    import bw2data as bd

    package, method = with_lcia_method(make_two_process_package())
    consumer_exchange(package).update(
        meanAmount="-2.5",
        resultingAmount="-2.5",
        uncertaintyDistributionType="normal",
        relativeStandardDeviation95In="20",
    )
    write_package(package.records, tmp_path / "source", manifest=package.manifest)
    assert import_tidas(
        tmp_path / "source",
        project="negative-normal",
        database="data",
        brightway_dir=isolated_brightway_dir,
    ).ok
    node = bd.get_node(database="data", name="widget production")
    lca = bc.LCA({node: 1}, method=method, use_distributions=True, seed_override=20260921)
    lca.lci()
    lca.lcia()
    samples = []
    for _ in range(2048):
        next(lca)
        samples.append(lca.score)
    assert np.mean(samples) == pytest.approx(-3.125, rel=0.025)
    assert np.std(samples) == pytest.approx(0.3125, rel=0.08)


def test_incomplete_restricted_evidence_is_never_bypassed():
    package = make_two_process_package()
    record = package.by_category("unitgroups")[0]
    package.manifest["license_evidence"][record.identity] = {"license": "CC BY-NC 4.0"}
    report = validate_package(package, require_open=False)
    assert not report.ok
    assert any(i.code == "restricted_license" and i.dataset == record.identity for i in report.issues)


@pytest.mark.parametrize("missing", ["namespace", "geography"])
def test_lcia_sdk_omissions_that_fail_official_xsd_are_rejected(missing):
    package, _ = with_lcia_method(make_two_process_package())
    root = package.by_category("lciamethods")[0].document["LCIAMethodDataSet"]
    if missing == "namespace":
        del root["@xmlns"]
    else:
        del root["LCIAMethodInformation"]["geography"]
    report = validate_package(package)
    assert not report.ok
    assert any(i.code == "eilcd_compatibility" for i in report.issues)
