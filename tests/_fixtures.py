from __future__ import annotations

from copy import deepcopy

from tidas_bw.package import TidasPackage, record_from_document
from tidas_bw.templates import (
    FORMAT_SOURCE_UUID,
    TIMESTAMP,
    VERSION,
    contact_document,
    flow_document,
    flow_property_document,
    format_reference,
    full_compliance,
    global_reference,
    lifecycle_model_document,
    model_connection,
    multilingual,
    owner_reference,
    process_document,
    process_exchange,
    process_instance,
    source_document,
    stable_uuid,
    unit_group_document,
)


def make_two_process_package() -> TidasPackage:
    units = {
        "kilogram": (stable_uuid("test:unit:kg"), stable_uuid("test:property:kg")),
        "kilowatt hour": (stable_uuid("test:unit:kwh"), stable_uuid("test:property:kwh")),
        "cubic meter": (stable_uuid("test:unit:m3"), stable_uuid("test:property:m3")),
    }
    electricity_flow = stable_uuid("test:flow:electricity")
    widget_flow = stable_uuid("test:flow:widget")
    carbon_flow = stable_uuid("test:flow:carbon-dioxide")
    water_flow = stable_uuid("test:flow:water")
    supplier_process = stable_uuid("test:process:supplier")
    consumer_process = stable_uuid("test:process:consumer")
    model = stable_uuid("test:model")

    documents = [contact_document(), source_document()]
    for unit, (unit_group, flow_property) in units.items():
        documents.extend(
            [
                unit_group_document(unit_group, unit),
                flow_property_document(flow_property, unit_group, unit),
            ]
        )
    documents.extend(
        [
            flow_document(
                electricity_flow,
                "electricity",
                "kilowatt hour",
                units["kilowatt hour"][1],
                elementary=False,
            ),
            flow_document(
                widget_flow,
                "widget",
                "kilogram",
                units["kilogram"][1],
                elementary=False,
            ),
            flow_document(
                carbon_flow,
                "Carbon dioxide, fossil",
                "kilogram",
                units["kilogram"][1],
                elementary=True,
                categories=("air",),
            ),
            flow_document(
                water_flow,
                "Water",
                "cubic meter",
                units["cubic meter"][1],
                elementary=True,
                categories=("natural resource", "water"),
            ),
            process_document(
                supplier_process,
                "electricity production",
                "electricity",
                "CN",
                [
                    process_exchange(0, electricity_flow, "electricity", 1, "Output"),
                    process_exchange(1, carbon_flow, "Carbon dioxide, fossil", 0.5, "Output"),
                ],
            ),
            process_document(
                consumer_process,
                "widget production",
                "widget",
                "CN",
                [
                    process_exchange(0, widget_flow, "widget", 1, "Output"),
                    process_exchange(1, electricity_flow, "electricity", 2.5, "Input"),
                    process_exchange(2, water_flow, "Water", 0.01, "Input"),
                ],
            ),
            lifecycle_model_document(
                model,
                "widget supply chain",
                [
                    process_instance(
                        0,
                        supplier_process,
                        "electricity production",
                        connections=(model_connection(electricity_flow, (1,)),),
                    ),
                    process_instance(1, consumer_process, "widget production"),
                ],
                reference_process_id="1",
            ),
        ]
    )
    records = [
        record_from_document(document, source_path=f"generated/{index}")
        for index, document in enumerate(documents)
    ]
    return TidasPackage(records=records, source="generated-test-package")


def with_lcia_method(
    package: TidasPackage,
    *,
    location: str | None = None,
) -> tuple[TidasPackage, tuple[str, ...]]:
    carbon_uuid = stable_uuid("test:flow:carbon-dioxide")
    carbon_flow = next(
        record
        for record in package.records
        if record.category == "flows" and record.uuid == carbon_uuid
    )
    property_reference = deepcopy(
        carbon_flow.document["flowDataSet"]["flowProperties"]["flowProperty"][
            "referenceToFlowPropertyDataSet"
        ]
    )
    factor = {
        "referenceToFlowDataSet": global_reference("flows", carbon_uuid, "Carbon dioxide, fossil"),
        "exchangeDirection": "Output",
        "meanValue": "2.5",
        "deviatingRecommendation": "Level I",
    }
    if location:
        factor["location"] = location
    method_uuid = stable_uuid("test:lcia:climate")
    title = "Test climate method"
    document = {
        "LCIAMethodDataSet": {
            "LCIAMethodInformation": {
                "dataSetInformation": {
                    "common:UUID": method_uuid,
                    "common:name": multilingual(title),
                    "methodology": "Test methodology",
                    "impactCategory": "Climate change",
                    "classificationInformation": {
                        "common:classification": {
                            "common:class": [
                                {
                                    "@level": "0",
                                    "@classId": "2",
                                    "#text": "Midpoint level LCIA methods",
                                },
                                {
                                    "@level": "1",
                                    "@classId": "2.2",
                                    "#text": "Climate change",
                                },
                            ]
                        }
                    },
                },
                "quantitativeReference": {"referenceQuantity": property_reference},
                "time": {
                    "referenceYear": multilingual("2000"),
                    "duration": multilingual("100 years"),
                    "timeRepresentativenessDescription": multilingual("Synthetic test method"),
                },
                "impactModel": {
                    "modelName": "Synthetic test model",
                    "modelDescription": multilingual("Synthetic test method"),
                },
            },
            "modellingAndValidation": {
                "LCIAMethodNormalisationAndWeighting": {
                    "typeOfDataSet": "Mid-point indicator",
                    "LCIAMethodPrinciple": "other",
                },
                "dataSources": {
                    "referenceToDataSource": global_reference(
                        "sources", FORMAT_SOURCE_UUID, "tidas-bw format source"
                    )
                },
                "validation": {"review": {"@type": "Not reviewed"}},
                "complianceDeclarations": {"compliance": full_compliance()},
            },
            "administrativeInformation": {
                "dataGenerator": {
                    "common:referenceToPersonOrEntityGeneratingTheDataSet": owner_reference()
                },
                "dataEntryBy": {
                    "common:timeStamp": TIMESTAMP,
                    "common:referenceToDataSetFormat": format_reference(),
                    "recommendationBy": {
                        "referenceToEntity": owner_reference(),
                        "level": "Level I",
                        "meaning": multilingual("Synthetic test method"),
                    },
                },
                "publicationAndOwnership": {
                    "common:dateOfLastRevision": TIMESTAMP,
                    "common:dataSetVersion": VERSION,
                    "common:referenceToOwnershipOfDataSet": owner_reference(),
                    "common:copyright": "false",
                    "common:accessRestrictions": multilingual("Creative Commons CC BY 4.0"),
                },
            },
            "characterisationFactors": {"factor": factor},
        }
    }
    method_record = record_from_document(document, source_path="generated/lcia-method")
    return (
        TidasPackage(records=[*package.records, method_record], source=package.source),
        ("TIDAS", "Test methodology", "Climate change", title),
    )
