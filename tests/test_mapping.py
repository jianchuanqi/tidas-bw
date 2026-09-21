from __future__ import annotations

from copy import deepcopy

from tidas_bw.mapping import TidasMapper
from tidas_bw.package import TidasPackage, record_from_document
from tidas_bw.templates import stable_uuid
from tidas_bw.validation import validate_package

from ._fixtures import make_two_process_package, with_lcia_method


def test_tidas_mapping_uses_explicit_provider_units_and_resulting_amounts() -> None:
    package = make_two_process_package()
    payload = TidasMapper(
        package,
        database="foreground",
        biosphere_database="foreground-biosphere",
    ).build()

    assert payload.report.ok, payload.report.to_dict()
    supplier = payload.technosphere[("foreground", stable_uuid("test:process:supplier").lower())]
    consumer = payload.technosphere[("foreground", stable_uuid("test:process:consumer").lower())]
    assert supplier["unit"] == "kilowatt hour"
    assert consumer["unit"] == "kilogram"
    technosphere = next(
        exchange for exchange in consumer["exchanges"] if exchange["type"] == "technosphere"
    )
    assert technosphere["input"] == (
        "foreground",
        stable_uuid("test:process:supplier").lower(),
    )
    assert technosphere["amount"] == 2.5
    water = payload.biosphere[("foreground-biosphere", stable_uuid("test:flow:water").lower())]
    assert water["type"] == "natural resource"
    assert water["unit"] == "cubic meter"


def test_lcia_method_maps_to_elementary_flow_and_reference_unit() -> None:
    package, method_name = with_lcia_method(make_two_process_package())
    validation = validate_package(package, require_open=True, strict_references=True)
    assert validation.ok, validation.to_dict()

    payload = TidasMapper(
        package,
        database="foreground",
        biosphere_database="foreground-biosphere",
    ).build()

    assert payload.report.ok, payload.report.to_dict()
    method = next(item for item in payload.methods if item.name == method_name)
    assert method.metadata["unit"] == "kilogram"
    assert method.factors == [
        (
            ("foreground-biosphere", stable_uuid("test:flow:carbon-dioxide").lower()),
            2.5,
        )
    ]


def test_location_specific_lcia_is_rejected_before_write() -> None:
    package, _ = with_lcia_method(make_two_process_package(), location="CN")
    validation = validate_package(package, require_open=True, strict_references=True)
    assert validation.ok, validation.to_dict()

    payload = TidasMapper(
        package,
        database="foreground",
        biosphere_database="foreground-biosphere",
    ).build()

    assert not payload.report.ok
    assert any(
        issue.code == "unsupported_location_specific_lcia" for issue in payload.report.issues
    )


def test_lcia_factor_direction_must_match_elementary_exchanges() -> None:
    package, _ = with_lcia_method(make_two_process_package())
    method = next(record for record in package.records if record.category == "lciamethods")
    method.document["LCIAMethodDataSet"]["characterisationFactors"]["factor"][
        "exchangeDirection"
    ] = "Input"

    payload = TidasMapper(
        package,
        database="foreground",
        biosphere_database="foreground-biosphere",
    ).build()

    assert not payload.report.ok
    assert any(issue.code == "lcia_direction_mismatch" for issue in payload.report.issues)


def test_quantified_uncertainty_is_rejected_instead_of_silently_dropped() -> None:
    package = make_two_process_package()
    consumer = next(
        record
        for record in package.records
        if record.category == "processes" and record.uuid == stable_uuid("test:process:consumer")
    )
    exchange = consumer.document["processDataSet"]["exchanges"]["exchange"][1]
    exchange.update(
        {
            "uncertaintyDistributionType": "normal",
            "relativeStandardDeviation95In": "20",
            "minimumAmount": "2",
            "maximumAmount": "3",
        }
    )
    package, _ = with_lcia_method(package)
    method = next(record for record in package.records if record.category == "lciamethods")
    factor = method.document["LCIAMethodDataSet"]["characterisationFactors"]["factor"]
    factor.update(
        {
            "uncertaintyDistributionType": "normal",
            "relativeStandardDeviation95In": "10",
            "minimumValue": "2",
            "maximumValue": "3",
        }
    )

    validation = validate_package(package, require_open=True, strict_references=True)
    assert validation.ok, validation.to_dict()
    payload = TidasMapper(
        package,
        database="foreground",
        biosphere_database="foreground-biosphere",
    ).build()

    codes = {issue.code for issue in payload.report.issues}
    assert "unmappable_exchange_uncertainty" in codes
    assert "unmappable_lcia_uncertainty" in codes
    assert not payload.report.ok


def test_exchange_location_anchor_precedes_consumer_location_fallback() -> None:
    package = make_two_process_package()
    supplier = next(
        record
        for record in package.records
        if record.category == "processes" and record.uuid == stable_uuid("test:process:supplier")
    )
    us_supplier_uuid = stable_uuid("test:process:supplier-us")
    us_document = deepcopy(supplier.document)
    us_root = us_document["processDataSet"]
    us_root["processInformation"]["dataSetInformation"]["common:UUID"] = us_supplier_uuid
    us_root["processInformation"]["geography"]["locationOfOperationSupplyOrProduction"][
        "@location"
    ] = "US"
    consumer = next(
        record
        for record in package.records
        if record.category == "processes" and record.uuid == stable_uuid("test:process:consumer")
    )
    consumer.document["processDataSet"]["exchanges"]["exchange"][1]["location"] = "US"
    records = [record for record in package.records if record.category != "lifecyclemodels"]
    records.append(record_from_document(us_document, source_path="generated/us-supplier"))
    anchored = TidasPackage(records=records, source=package.source)

    validation = validate_package(anchored, require_open=True, strict_references=True)
    assert validation.ok, validation.to_dict()
    payload = TidasMapper(
        anchored,
        database="foreground",
        biosphere_database="foreground-biosphere",
    ).build()

    assert payload.report.ok, payload.report.to_dict()
    mapped_consumer = payload.technosphere[
        ("foreground", stable_uuid("test:process:consumer").lower())
    ]
    technosphere = next(
        item for item in mapped_consumer["exchanges"] if item["type"] == "technosphere"
    )
    assert technosphere["input"] == ("foreground", us_supplier_uuid.lower())


def test_product_flow_supply_location_precedes_consumer_location_fallback() -> None:
    package = make_two_process_package()
    electricity = next(
        record
        for record in package.records
        if record.category == "flows" and record.uuid == stable_uuid("test:flow:electricity")
    )
    electricity.document["flowDataSet"]["flowInformation"]["geography"] = {"locationOfSupply": "US"}
    supplier = next(
        record
        for record in package.records
        if record.category == "processes" and record.uuid == stable_uuid("test:process:supplier")
    )
    us_supplier_uuid = stable_uuid("test:process:supplier-us-flow-anchor")
    us_document = deepcopy(supplier.document)
    us_root = us_document["processDataSet"]
    us_root["processInformation"]["dataSetInformation"]["common:UUID"] = us_supplier_uuid
    us_root["processInformation"]["geography"]["locationOfOperationSupplyOrProduction"][
        "@location"
    ] = "US"
    records = [record for record in package.records if record.category != "lifecyclemodels"]
    records.append(record_from_document(us_document, source_path="generated/us-flow-supplier"))
    anchored = TidasPackage(records=records, source=package.source)

    validation = validate_package(anchored, require_open=True, strict_references=True)
    assert validation.ok, validation.to_dict()
    payload = TidasMapper(
        anchored,
        database="foreground",
        biosphere_database="foreground-biosphere",
    ).build()

    assert payload.report.ok, payload.report.to_dict()
    consumer = payload.technosphere[("foreground", stable_uuid("test:process:consumer").lower())]
    technosphere = next(item for item in consumer["exchanges"] if item["type"] == "technosphere")
    assert technosphere["input"] == ("foreground", us_supplier_uuid.lower())


def test_location_specific_elementary_exchange_is_rejected() -> None:
    package = make_two_process_package()
    supplier = next(
        record
        for record in package.records
        if record.category == "processes" and record.uuid == stable_uuid("test:process:supplier")
    )
    supplier.document["processDataSet"]["exchanges"]["exchange"][1]["location"] = "CN"

    payload = TidasMapper(
        package,
        database="foreground",
        biosphere_database="foreground-biosphere",
    ).build()

    assert not payload.report.ok
    assert any(
        issue.code == "unsupported_location_specific_biosphere_exchange"
        for issue in payload.report.issues
    )


def test_lifecycle_model_provider_must_produce_the_connected_flow() -> None:
    package = make_two_process_package()
    supplier = next(
        record
        for record in package.records
        if record.category == "processes" and record.uuid == stable_uuid("test:process:supplier")
    )
    consumer = next(
        record
        for record in package.records
        if record.category == "processes" and record.uuid == stable_uuid("test:process:consumer")
    )
    supplier.document["processDataSet"]["exchanges"]["exchange"][0]["referenceToFlowDataSet"] = (
        deepcopy(
            next(
                exchange["referenceToFlowDataSet"]
                for exchange in consumer.document["processDataSet"]["exchanges"]["exchange"]
                if str(exchange["@dataSetInternalID"]) == "0"
            )
        )
    )

    payload = TidasMapper(
        package,
        database="foreground",
        biosphere_database="foreground-biosphere",
    ).build()

    assert not payload.report.ok
    assert any(issue.code == "explicit_provider_flow_mismatch" for issue in payload.report.issues)


def test_reminder_exchange_is_not_counted_as_inventory() -> None:
    package = make_two_process_package()
    consumer = next(
        record
        for record in package.records
        if record.category == "processes" and record.uuid == stable_uuid("test:process:consumer")
    )
    consumer.document["processDataSet"]["exchanges"]["exchange"][1]["functionType"] = (
        "General reminder flow"
    )

    payload = TidasMapper(
        package,
        database="foreground",
        biosphere_database="foreground-biosphere",
    ).build()

    assert not payload.report.ok
    assert any(issue.code == "unsupported_reminder_exchange" for issue in payload.report.issues)


def test_zero_and_unrepresentable_reference_amounts_fail_preflight() -> None:
    for value in ("0", "1e9999", "1e-9999"):
        package = make_two_process_package()
        supplier = next(
            record
            for record in package.records
            if record.category == "processes"
            and record.uuid == stable_uuid("test:process:supplier")
        )
        exchange = supplier.document["processDataSet"]["exchanges"]["exchange"][0]
        exchange["resultingAmount"] = value
        exchange["meanAmount"] = value

        payload = TidasMapper(
            package,
            database="foreground",
            biosphere_database="foreground-biosphere",
        ).build()

        assert not payload.report.ok
        assert any(
            issue.code in {"non_positive_reference_amount", "invalid_exchange_amount"}
            for issue in payload.report.issues
        )


def test_high_precision_amount_is_rounded_and_reported_not_rejected() -> None:
    package = make_two_process_package()
    supplier = next(
        record
        for record in package.records
        if record.category == "processes" and record.uuid == stable_uuid("test:process:supplier")
    )
    exchange = supplier.document["processDataSet"]["exchanges"]["exchange"][0]
    exchange["meanAmount"] = exchange["resultingAmount"] = "0.123456789012345678"
    replacement = record_from_document(supplier.document, source_path=supplier.source_path)
    package.records[package.records.index(supplier)] = replacement

    payload = TidasMapper(
        package,
        database="foreground",
        biosphere_database="foreground-biosphere",
    ).build()

    assert payload.report.ok
    rounded = [
        issue
        for issue in payload.report.issues
        if issue.code == "exchange_amount_rounded" and issue.severity == "info"
    ]
    assert len(rounded) == 1
    assert "0.123456789012345678" in rounded[0].message
    assert "0.12345678901234568" in rounded[0].message
    assert "float64" in rounded[0].message
