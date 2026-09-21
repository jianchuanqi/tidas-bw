from __future__ import annotations

from copy import deepcopy

from tidas_bw.licenses import is_open_license
from tidas_bw.package import TidasPackage, record_from_document
from tidas_bw.templates import flow_document, stable_uuid
from tidas_bw.validation import validate_package

from ._fixtures import make_two_process_package


def test_generated_package_passes_schema_and_strict_reference_closure() -> None:
    package = make_two_process_package()

    report = validate_package(package, require_open=True, strict_references=True)

    assert report.ok, report.to_dict()
    assert not report.issues


def test_all_elementary_category_branches_pass_schema() -> None:
    package = make_two_process_package()
    flow_property = next(
        record for record in package.records if record.category == "flowproperties"
    )
    branches = (
        ("air",),
        ("water",),
        ("soil",),
        ("natural resource", "water"),
        ("natural resource", "air"),
        ("natural resource", "biosphere"),
        ("natural resource", "ground"),
        ("natural resource",),
        ("unspecified",),
    )
    generated = []
    for index, categories in enumerate(branches):
        document = flow_document(
            stable_uuid(f"test:elementary-branch:{index}"),
            f"elementary branch {index}",
            "kilogram",
            flow_property.uuid,
            elementary=True,
            categories=categories,
        )
        generated.append(record_from_document(document, source_path=f"branch/{index}"))
    closure = [
        deepcopy(record)
        for record in package.records
        if record.category in {"contacts", "sources", "flowproperties", "unitgroups"}
    ]

    report = validate_package(
        TidasPackage(records=[*closure, *generated], source="category-branches", manifest=package.manifest),
        require_open=True,
        strict_references=True,
    )

    assert report.ok, report.to_dict()


def test_restricted_licence_is_never_bypassed() -> None:
    package = make_two_process_package()
    process = next(record for record in package.records if record.category == "processes")
    process.document["processDataSet"]["administrativeInformation"]["publicationAndOwnership"][
        "common:licenseType"
    ] = "License fee"
    process.license_type = "License fee"

    report = validate_package(package, require_open=False, strict_references=True)

    assert not report.ok
    assert any(issue.code == "restricted_license" for issue in report.issues)


def test_open_licence_text_cannot_be_negated_by_restricted_wording() -> None:
    assert not is_open_license("Not licensed under Creative Commons; all rights reserved")
    assert not is_open_license("Creative Commons CC BY-NC 4.0")
    assert not is_open_license("None")


def test_access_restrictions_override_an_open_licence_declaration() -> None:
    package = make_two_process_package()
    process = next(record for record in package.records if record.category == "processes")
    process.document["processDataSet"]["administrativeInformation"]["publicationAndOwnership"][
        "common:accessRestrictions"
    ] = [{"@xml:lang": "en", "#text": "Not for redistribution"}]
    replacement = record_from_document(process.document, source_path=process.source_path)
    package.records[package.records.index(process)] = replacement

    report = validate_package(package, require_open=False, strict_references=True)

    assert not report.ok
    assert replacement.license_type is not None
    assert "Not for redistribution" in replacement.license_type
    assert any(issue.code == "restricted_license" for issue in report.issues)


def test_unknown_use_restriction_cannot_hide_behind_open_declaration() -> None:
    package = make_two_process_package()
    process = next(record for record in package.records if record.category == "processes")
    process.document["processDataSet"]["administrativeInformation"]["publicationAndOwnership"][
        "common:accessRestrictions"
    ] = [{"@xml:lang": "en", "#text": "Use requires written permission from the curator"}]
    package.records[package.records.index(process)] = record_from_document(
        process.document, source_path=process.source_path
    )

    report = validate_package(package, require_open=True, strict_references=True)

    assert not report.ok
    assert any(issue.code == "unrecognised_access_restriction" for issue in report.issues)


def test_every_dataset_must_prove_open_use() -> None:
    package = make_two_process_package()
    flow = next(record for record in package.records if record.category == "flows")
    publication = flow.document["flowDataSet"]["administrativeInformation"][
        "publicationAndOwnership"
    ]
    publication.pop("common:licenseType")
    package.records[package.records.index(flow)] = record_from_document(
        flow.document, source_path=flow.source_path
    )

    report = validate_package(package, require_open=True, strict_references=True)

    assert not report.ok
    assert any(
        issue.code == "open_license_not_proven" and issue.dataset == flow.identity
        for issue in report.issues
    )


def test_no_access_restriction_does_not_itself_grant_an_open_licence() -> None:
    package = make_two_process_package()
    flow = next(record for record in package.records if record.category == "flows")
    publication = flow.document["flowDataSet"]["administrativeInformation"][
        "publicationAndOwnership"
    ]
    publication.pop("common:licenseType")
    publication["common:accessRestrictions"] = [{"@xml:lang": "en", "#text": "None"}]
    package.records[package.records.index(flow)] = record_from_document(
        flow.document, source_path=flow.source_path
    )

    report = validate_package(package, require_open=True, strict_references=True)

    assert not report.ok
    assert any(
        issue.code == "open_license_not_proven" and issue.dataset == flow.identity
        for issue in report.issues
    )


def test_lcia_access_restrictions_can_prove_open_use() -> None:
    method_uuid = stable_uuid("test:lcia-method")
    record = record_from_document(
        {
            "LCIAMethodDataSet": {
                "LCIAMethodInformation": {"dataSetInformation": {"common:UUID": method_uuid}},
                "administrativeInformation": {
                    "publicationAndOwnership": {
                        "common:dataSetVersion": "01.00.000",
                        "common:accessRestrictions": [
                            {"@xml:lang": "en", "#text": "Creative Commons CC BY 4.0"}
                        ],
                    }
                },
            }
        },
        source_path="lcia-method.json",
    )

    assert record.license_type == "Creative Commons CC BY 4.0"
    assert is_open_license(record.license_type)


def test_bad_dataset_shape_is_reported_instead_of_crashing() -> None:
    package = make_two_process_package()
    process = next(record for record in package.records if record.category == "processes")
    process.document["processDataSet"]["processInformation"] = "not an object"

    report = validate_package(package, require_open=True, strict_references=True)

    assert not report.ok
    assert any(issue.code == "schema_validation" for issue in report.issues)


def test_structural_records_need_no_own_licence_fields() -> None:
    """The official eILCD/XSD projection rejects licence fields on contacts,
    flow properties, and unit groups (issue #1); stripping them must not break
    the internal open-licence check."""
    package = make_two_process_package()
    stripped_categories = {"contacts", "flowproperties", "unitgroups"}
    stripped = []
    for record in package.records:
        document = deepcopy(record.document)
        if record.category in stripped_categories:
            publication = document[record.root_key]["administrativeInformation"][
                "publicationAndOwnership"
            ]
            for field in ("common:copyright", "common:licenseType", "common:accessRestrictions"):
                publication.pop(field, None)
        stripped.append(record_from_document(document, source_path=record.source_path))

    report = validate_package(
        TidasPackage(records=stripped, source=package.source, manifest=package.manifest),
        require_open=True,
        strict_references=True,
    )

    assert report.ok, report.to_dict()


def test_structural_records_with_restricted_licence_are_still_rejected() -> None:
    package = make_two_process_package()
    unit_group = next(record for record in package.records if record.category == "unitgroups")
    unit_group.document[unit_group.root_key]["administrativeInformation"][
        "publicationAndOwnership"
    ]["common:licenseType"] = "Not for redistribution"
    replacement = record_from_document(
        unit_group.document, source_path=unit_group.source_path
    )
    package.records[package.records.index(unit_group)] = replacement

    report = validate_package(package, require_open=True, strict_references=True)

    assert not report.ok
    assert any(
        issue.code == "restricted_license" and issue.dataset == unit_group.identity
        for issue in report.issues
    )


def test_generated_documents_place_licence_fields_only_where_official_schema_allows() -> None:
    from tidas_bw.templates import (
        contact_document,
        flow_property_document,
        licensed_publication,
        process_document,
        source_document,
        unit_group_document,
    )

    forbidden = {"common:copyright", "common:licenseType", "common:accessRestrictions"}
    for document in (
        contact_document(),
        unit_group_document(stable_uuid("test:ug"), "kilogram"),
        flow_property_document(stable_uuid("test:fp"), stable_uuid("test:ug"), "kilogram"),
    ):
        root = next(iter(document.values()))
        publication = root["administrativeInformation"]["publicationAndOwnership"]
        assert not (forbidden & publication.keys()), sorted(forbidden & publication.keys())

    process = process_document(
        stable_uuid("test:proc"), "p", "product", "GLO", exchanges=()
    )
    assert (
        "common:licenseType"
        in process["processDataSet"]["administrativeInformation"]["publicationAndOwnership"]
    )
    assert (
        "common:licenseType"
        in source_document()["sourceDataSet"]["administrativeInformation"][
            "publicationAndOwnership"
        ]
    )
    licensed = licensed_publication("x")
    assert {"common:copyright", "common:licenseType"} <= set(licensed)
    assert "common:accessRestrictions" not in licensed
    assert "common:permanentDataSetURI" in licensed
