"""Build TIDAS records from a Brightway database."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from math import isfinite
from typing import Any
from uuid import UUID

from .licenses import is_open_license, is_restricted_license
from .models import DatasetRecord, MigrationReport
from .package import record_from_document
from .templates import (
    FORMAT_SOURCE_UUID,
    OWNER_UUID,
    contact_document,
    flow_document,
    flow_property_document,
    global_reference,
    process_document,
    process_exchange,
    source_document,
    stable_uuid,
    unit_group_document,
)
from .uncertainty import convert_uncertainty
from .utils import as_list, deep_get, multilingual, number_string, pick_text, semantic_hash

# Categories whose eILCD/XSD projection accepts the standard licence fields
# (they share the ILCD PublicationAndOwnershipType, which contains copyright,
# licenceType, and accessRestrictions); generated documents for other
# categories must not carry them (issue #1).
LICENSE_FIELD_CATEGORIES = frozenset(
    {"processes", "flows", "sources", "lifecyclemodels", "lciamethods"}
)

# Brightway exchange fields that describe a quantified uncertainty.
BW_UNCERTAINTY_FIELDS = (
    "uncertainty type",
    "loc",
    "scale",
    "sigma",
    "minimum",
    "maximum",
    "negative",
)

PROCESS_TYPES = {
    "Unit process, single operation",
    "Unit process, black box",
    "LCI result",
    "Partly terminated system",
    "Avoided product system",
}


class BrightwayExporter:
    def __init__(
        self,
        database_data: dict[Any, Any],
        database_metadata: Mapping[str, Any],
        referenced_nodes: Mapping[tuple[str, str], Mapping[str, Any]],
        *,
        project: str,
        database: str,
        method_states: Mapping[str, Mapping[str, Any]] | None = None,
        native_license: str | None = None,
        native_owner: str | None = None,
        native_source: str | None = None,
        parameter_counts: Mapping[str, int] | None = None,
        source_database_metadata: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> None:
        self.data = {_key(key, value): deepcopy(value) for key, value in database_data.items()}
        self.metadata = deepcopy(dict(database_metadata))
        self.referenced_nodes = {
            _key(key, value): deepcopy(dict(value)) for key, value in referenced_nodes.items()
        }
        self.project = project
        self.database = database
        self.method_states = deepcopy(dict(method_states or {}))
        self.native_license = native_license
        self.native_owner = native_owner
        self.native_source = native_source
        self.parameter_counts = dict(parameter_counts or {})
        self.source_database_metadata = {
            str(name): deepcopy(dict(metadata))
            for name, metadata in (source_database_metadata or {}).items()
        }
        self.native_provenance: dict[str, str] | None = None
        self.native_database_provenance: dict[str, dict[str, str]] = {}
        self.generated_process_ids: dict[tuple[str, str], str] = {}
        self.report = MigrationReport(
            direction="brightway-to-tidas",
            source=f"{project}/{database}",
        )

    def build(self) -> tuple[list[DatasetRecord], dict[str, Any], MigrationReport]:
        if "tidas_bw" in self.metadata:
            if self._is_complete_tidas_import():
                self._verify_preserved_identity_sets()
                records = self._restore_preserved_records()
                mode = "preserved-tidas"
            else:
                self.report.add(
                    "error",
                    "mixed_or_incomplete_tidas_metadata",
                    "This database is marked as a TIDAS import, but one or more activities lost their preserved TIDAS document",
                )
                records = []
                mode = "invalid-preserved-tidas"
        else:
            records = self._synthesise_records()
            mode = "native-brightway"
        manifest = {
            "format": "tidas-bw-manifest-v1",
            "direction": "brightway-to-tidas",
            "mode": mode,
            "source": {"project": self.project, "database": self.database},
            "counts": _counts(records),
            "issues": [
                {
                    "severity": issue.severity,
                    "code": issue.code,
                    "message": issue.message,
                    "dataset": issue.dataset,
                    "path": issue.path,
                }
                for issue in self.report.issues
            ],
            "brightway_nodes": [
                {
                    "database": key[0],
                    "code": key[1],
                    "tidas_uuid": self.generated_process_ids.get(key) or _process_uuid(key, value),
                }
                for key, value in sorted(self.data.items())
            ],
        }
        self.report.counts.update(_counts(records))
        if self.native_provenance:
            manifest["native_provenance"] = deepcopy(self.native_provenance)
        if self.native_database_provenance:
            manifest["native_database_provenance"] = deepcopy(self.native_database_provenance)
        return records, manifest, self.report

    def _is_complete_tidas_import(self) -> bool:
        marker = self.metadata.get("tidas_bw")
        if not isinstance(marker, Mapping) or marker.get("format") != "tidas-bw-metadata-v1":
            return False
        return bool(self.data) and all(
            isinstance(dataset.get("tidas"), Mapping)
            and isinstance(dataset["tidas"].get("document"), Mapping)
            for dataset in self.data.values()
        )

    def _restore_preserved_records(self) -> list[DatasetRecord]:
        documents: list[dict[str, Any]] = []
        marker = self.metadata.get("tidas_bw", {})
        biosphere_database = str(marker.get("biosphere_database") or "")
        for dataset in self.data.values():
            document = deepcopy(dataset["tidas"]["document"])
            self._overlay_process(document, dataset)
            documents.append(document)
        for key, node in self.referenced_nodes.items():
            if key[0] != biosphere_database:
                continue
            tidas = node.get("tidas")
            if not isinstance(tidas, Mapping) or tidas.get("kind") != "flow":
                self.report.add(
                    "error",
                    "unmapped_brightway_flow",
                    "The companion biosphere database contains a flow without preserved TIDAS identity metadata",
                    dataset=f"{key[0]}:{key[1]}",
                )
                continue
            if isinstance(tidas.get("document"), Mapping):
                self._verify_preserved_flow_node(node, tidas)
                documents.append(deepcopy(tidas["document"]))
        for item in as_list(marker.get("auxiliary_documents")):
            if isinstance(item, Mapping) and isinstance(item.get("document"), Mapping):
                documents.append(deepcopy(item["document"]))
        for document in as_list(marker.get("lcia_documents")):
            if isinstance(document, Mapping):
                self._verify_preserved_method(document)
                documents.append(deepcopy(document))
        records = _deduplicate_documents(documents, self.report)
        self.report.add(
            "info",
            "metadata_restored",
            "Verified Brightway content and restored the original TIDAS documents unchanged",
        )
        if any(
            exchange.get("type") == "technosphere"
            for dataset in self.data.values()
            for exchange in dataset.get("exchanges", [])
        ):
            self.report.add(
                "info",
                "lifecycle_model_preserved",
                "Original lifecycle-model connections were preserved after exchange integrity checks",
            )
        return records

    def _verify_preserved_identity_sets(self) -> None:
        marker = self.metadata.get("tidas_bw", {})
        expected_processes = _identity_set(marker.get("process_identities"))
        current_processes = {
            (str(tidas.get("uuid")).lower(), str(tidas.get("version")))
            for dataset in self.data.values()
            if isinstance((tidas := dataset.get("tidas")), Mapping)
        }
        biosphere_database = str(marker.get("biosphere_database") or "")
        expected_flows = _identity_set(marker.get("biosphere_flow_identities"))
        current_flows = {
            (str(tidas.get("uuid")).lower(), str(tidas.get("version")))
            for key, node in self.referenced_nodes.items()
            if key[0] == biosphere_database
            and isinstance((tidas := node.get("tidas")), Mapping)
            and tidas.get("kind") == "flow"
        }
        if expected_processes:
            for identity in sorted(expected_processes - current_processes):
                self.report.add(
                    "error",
                    "removed_brightway_activity",
                    f"Imported TIDAS process {identity[0]}@{identity[1]} was removed from Brightway",
                )
            for identity in sorted(current_processes - expected_processes):
                self.report.add(
                    "error",
                    "unexpected_brightway_activity_identity",
                    f"Brightway contains an unexpected TIDAS process identity {identity[0]}@{identity[1]}",
                )
        if expected_flows:
            for identity in sorted(expected_flows - current_flows):
                self.report.add(
                    "error",
                    "removed_brightway_flow",
                    f"Imported TIDAS elementary flow {identity[0]}@{identity[1]} was removed from Brightway",
                )
            for identity in sorted(current_flows - expected_flows):
                self.report.add(
                    "error",
                    "unexpected_brightway_flow_identity",
                    f"Brightway contains an unexpected TIDAS flow identity {identity[0]}@{identity[1]}",
                )

    def _verify_preserved_method(self, document: Mapping[str, Any]) -> None:
        uuid = deep_get(
            document,
            "LCIAMethodDataSet",
            "LCIAMethodInformation",
            "dataSetInformation",
            "common:UUID",
        )
        state = self.method_states.get(str(uuid).lower()) if uuid else None
        if not isinstance(state, Mapping):
            self.report.add(
                "error",
                "removed_brightway_method",
                f"Imported LCIA method {uuid or '<unknown>'} is missing from Brightway",
            )
            return
        metadata = state.get("metadata")
        tidas = metadata.get("tidas") if isinstance(metadata, Mapping) else None
        if not isinstance(tidas, Mapping) or "factors" not in tidas:
            self.report.add(
                "error",
                "unverifiable_brightway_method",
                f"Imported LCIA method {uuid} has no factor snapshot",
            )
            return
        if _factor_signature(state.get("data")) != _factor_signature(tidas.get("factors")):
            self.report.add(
                "error",
                "changed_brightway_method",
                f"Imported LCIA method {uuid} was changed in Brightway",
            )
        raw_name = tidas.get("brightway_name")
        name_parts = raw_name if isinstance(raw_name, list | tuple) else as_list(raw_name)
        expected_name = tuple(str(item) for item in name_parts)
        if expected_name and tuple(state.get("name", ())) != expected_name:
            self.report.add(
                "error",
                "changed_brightway_method_name",
                f"Imported LCIA method {uuid} was renamed in Brightway",
            )
        if tidas.get("brightway_unit") and metadata.get("unit") != tidas.get("brightway_unit"):
            self.report.add(
                "error",
                "changed_brightway_method_unit",
                f"Imported LCIA method {uuid} changed unit metadata",
            )

    def _verify_preserved_flow_node(
        self,
        node: Mapping[str, Any],
        tidas: Mapping[str, Any],
    ) -> None:
        snapshot = tidas.get("brightway_snapshot")
        if not isinstance(snapshot, Mapping):
            return
        current = {
            "name": node.get("name"),
            "unit": node.get("unit"),
            "location": node.get("location"),
            "categories": list(node.get("categories") or ()),
            "type": node.get("type"),
        }
        changed = [key for key, value in current.items() if value != snapshot.get(key)]
        if changed:
            self.report.add(
                "error",
                "unsupported_flow_metadata_change",
                f"Brightway flow fields changed and cannot be safely overlaid: {', '.join(changed)}",
                dataset=str(node.get("code") or node.get("name")),
            )

    def _overlay_process(self, document: dict[str, Any], dataset: Mapping[str, Any]) -> None:
        root = document.get("processDataSet")
        if not isinstance(root, dict):
            return
        tidas_dataset = dataset.get("tidas")
        snapshot = (
            tidas_dataset.get("brightway_snapshot") if isinstance(tidas_dataset, Mapping) else None
        )
        if isinstance(snapshot, Mapping):
            current = {
                "name": dataset.get("name"),
                "reference_product": dataset.get("reference product"),
                "unit": dataset.get("unit"),
                "reference_year": dataset.get("reference year", dataset.get("reference_year")),
                "valid_until": dataset.get("valid until", dataset.get("valid_until")),
                "tidas_process_type": dataset.get("tidas process type"),
                "categories": list(dataset.get("categories") or ()),
                "classifications": dataset.get("classifications"),
            }
            changed = [key for key, value in current.items() if value != snapshot.get(key)]
            if changed:
                self.report.add(
                    "error",
                    "unsupported_activity_metadata_change",
                    f"Brightway fields changed and cannot be safely overlaid: {', '.join(changed)}",
                    dataset=str(dataset.get("code") or dataset.get("name")),
                )
        location = str(dataset.get("location") or "").strip()
        geography = deep_get(root, "processInformation", "geography")
        location_node = deep_get(
            root,
            "processInformation",
            "geography",
            "locationOfOperationSupplyOrProduction",
        )
        if isinstance(location_node, dict):
            original_location = str(location_node.get("@location") or "")
            if not location:
                self.report.add(
                    "error",
                    "removed_process_location",
                    "Brightway process location was removed and cannot be safely restored",
                    dataset=str(dataset.get("code") or dataset.get("name")),
                )
            elif location != original_location:
                detail = (
                    " while dependent sublocation, coordinate, or restriction metadata is present"
                    if _has_dependent_geography(geography, location_node)
                    else ""
                )
                self.report.add(
                    "error",
                    "changed_process_location",
                    f"Process location changed from {original_location} to {location}{detail}; create a new TIDAS version instead",
                    dataset=str(dataset.get("code") or dataset.get("name")),
                )
        raw_exchanges = as_list(deep_get(root, "exchanges", "exchange"))
        by_id = {
            str(item.get("@dataSetInternalID")): item
            for item in raw_exchanges
            if isinstance(item, dict)
        }
        expected_ids = (
            {str(item) for item in as_list(tidas_dataset.get("mapped_exchange_ids"))}
            if isinstance(tidas_dataset, Mapping)
            else set()
        )
        current_ids: set[str] = set()
        for exchange in dataset.get("exchanges", []):
            tidas = exchange.get("tidas")
            if not isinstance(tidas, Mapping):
                self.report.add(
                    "error",
                    "unmapped_brightway_exchange",
                    "A new Brightway exchange has no TIDAS identity metadata",
                    dataset=str(dataset.get("code") or dataset.get("name")),
                )
                continue
            internal_id = str(tidas.get("internal_id"))
            if internal_id in current_ids:
                self.report.add(
                    "error",
                    "duplicate_exchange_identity",
                    f"Multiple Brightway exchanges claim TIDAS exchange {internal_id}",
                    dataset=str(dataset.get("code") or dataset.get("name")),
                )
                continue
            current_ids.add(internal_id)
            raw = by_id.get(internal_id)
            if raw is None:
                self.report.add(
                    "error",
                    "exchange_identity_not_found",
                    f"TIDAS exchange {tidas.get('internal_id')} is absent from preserved process",
                    dataset=str(dataset.get("code") or dataset.get("name")),
                )
                continue
            expected_type = tidas.get("brightway_type")
            input_key = _input_key(exchange)
            if expected_type and exchange.get("type") != expected_type:
                self.report.add(
                    "error",
                    "changed_exchange_type",
                    f"Brightway exchange {internal_id} changed type",
                    dataset=str(dataset.get("code") or dataset.get("name")),
                )
            expected_input = (
                str(tidas.get("input_database")),
                str(tidas.get("input_code")),
            )
            if tidas.get("input_code") and (
                input_key is None
                or input_key[1] != expected_input[1]
                or (tidas.get("input_database") and input_key[0] != expected_input[0])
            ):
                self.report.add(
                    "error",
                    "changed_exchange_link",
                    f"Brightway exchange {internal_id} was relinked",
                    dataset=str(dataset.get("code") or dataset.get("name")),
                )
            current_location = str(exchange.get("location") or "")
            if current_location != str(tidas.get("brightway_location") or ""):
                self.report.add(
                    "error",
                    "changed_exchange_location",
                    f"Brightway exchange {internal_id} changed location metadata",
                    dataset=str(dataset.get("code") or dataset.get("name")),
                )
            unsupported_fields = {"formula", "parameters"}.intersection(exchange)
            if unsupported_fields:
                self.report.add(
                    "error",
                    "unsupported_brightway_exchange_metadata",
                    f"Brightway exchange {internal_id} gained unsupported parameter fields: "
                    f"{', '.join(sorted(unsupported_fields))}",
                    dataset=str(dataset.get("code") or dataset.get("name")),
                )
            amount = _brightway_amount(
                exchange.get("amount", 0),
                report=self.report,
                dataset=str(dataset.get("code") or dataset.get("name")),
                exchange=internal_id,
            )
            if amount is None:
                continue
            self._verify_exchange_uncertainty(
                exchange, tidas, amount, dataset=str(dataset.get("code") or dataset.get("name"))
            )
            if expected_type == "production" and amount <= 0:
                self.report.add(
                    "error",
                    "non_positive_reference_amount",
                    f"Brightway reference production exchange {internal_id} must remain greater than zero",
                    dataset=str(dataset.get("code") or dataset.get("name")),
                )
                continue
            baseline_amount = _decimal_value(tidas.get("brightway_amount"))
            if baseline_amount is None:
                baseline_amount = _decimal_value(raw.get("resultingAmount", raw.get("meanAmount")))
            amount_changed = baseline_amount is None or amount != baseline_amount
            if raw.get("referenceToVariable") and amount_changed:
                self.report.add(
                    "error",
                    "changed_parameterized_exchange",
                    f"Brightway exchange {internal_id} changed amount, but its TIDAS value depends on a variable",
                    dataset=str(dataset.get("code") or dataset.get("name")),
                )
            elif amount_changed:
                self.report.add(
                    "error",
                    "changed_exchange_amount",
                    f"Brightway exchange {internal_id} changed amount; create a new TIDAS dataset version instead of reusing the original identity",
                    dataset=str(dataset.get("code") or dataset.get("name")),
                )
            if tidas.get("exchange_direction"):
                raw["exchangeDirection"] = str(tidas["exchange_direction"])
        missing = expected_ids - current_ids
        for internal_id in sorted(missing):
            self.report.add(
                "error",
                "removed_brightway_exchange",
                f"Mapped TIDAS exchange {internal_id} was removed in Brightway",
                dataset=str(dataset.get("code") or dataset.get("name")),
            )

    def _verify_exchange_uncertainty(
        self,
        exchange: Mapping[str, Any],
        tidas: Mapping[str, Any],
        amount: float,
        *,
        dataset: str,
    ) -> None:
        """Compare the Brightway uncertainty fields with the preserved TIDAS declaration."""
        internal_id = tidas.get("internal_id") or exchange.get("output") or exchange.get("input")
        document = tidas.get("document")
        actual = {
            key: exchange[key] for key in BW_UNCERTAINTY_FIELDS if key in exchange
        }
        if not isinstance(document, Mapping):
            if actual:
                self.report.add(
                    "error",
                    "unverifiable_exchange_uncertainty",
                    f"Brightway exchange {internal_id} has uncertainty fields but the "
                    "preserved TIDAS document is unavailable for comparison",
                    dataset=dataset,
                )
            return
        expected = convert_uncertainty(
            distribution=document.get("uncertaintyDistributionType"),
            rsd95=document.get("relativeStandardDeviation95In"),
            minimum=document.get("minimumAmount"),
            maximum=document.get("maximumAmount"),
            amount=amount,
        )
        if expected.error:
            self.report.add(
                "error",
                "unverifiable_exchange_uncertainty",
                f"The preserved TIDAS uncertainty of exchange {internal_id} can no longer "
                f"be mapped: {expected.error}",
                dataset=dataset,
            )
            return
        if _uncertainty_signature(actual) != _uncertainty_signature(expected.fields):
            self.report.add(
                "error",
                "changed_exchange_uncertainty",
                f"Brightway exchange {internal_id} changed uncertainty: expected "
                f"{_uncertainty_signature(expected.fields)}, found "
                f"{_uncertainty_signature(actual)}",
                dataset=dataset,
            )

    def _synthesise_records(self) -> list[DatasetRecord]:
        provenance = self._resolve_native_provenance()
        database_provenance: dict[str, dict[str, str]] = {}
        if provenance is not None:
            database_provenance[self.database] = provenance
        records: list[DatasetRecord] = []
        records.extend(
            [
                record_from_document(contact_document(), source_path="generated/contact"),
                record_from_document(source_document(), source_path="generated/source"),
            ]
        )
        if not self.data:
            self.report.add(
                "error",
                "empty_brightway_database",
                "The selected Brightway database contains no activities",
            )
            return records
        self._report_native_metadata_limits()
        for key in self.referenced_nodes:
            if not self._is_biosphere_reference(key) or key[0] in database_provenance:
                continue
            source_provenance = self._resolve_source_database_provenance(key[0])
            if source_provenance is not None:
                database_provenance[key[0]] = source_provenance
        units = self._units()
        unit_context: dict[str, tuple[str, str]] = {}
        for unit in sorted(units):
            unit_group_uuid = stable_uuid(f"unit-group:{unit}")
            property_uuid = stable_uuid(f"flow-property:{unit}")
            unit_context[unit] = (unit_group_uuid, property_uuid)
            records.append(
                record_from_document(
                    unit_group_document(unit_group_uuid, unit),
                    source_path=f"generated/unitgroups/{unit}",
                )
            )
            records.append(
                record_from_document(
                    flow_property_document(property_uuid, unit_group_uuid, unit),
                    source_path=f"generated/flowproperties/{unit}",
                )
            )

        main_namespace = _provenance_namespace(
            provenance
            or {
                "license": "missing",
                "owner": "missing",
                "source": f"{self.project}/{self.database}",
            }
        )
        product_flow_ids = {
            key: stable_uuid(
                f"native-product-flow:{self.project}:{key[0]}:{key[1]}:{main_namespace}:"
                f"{value.get('reference product')}:{value.get('unit')}"
            )
            for key, value in self.data.items()
        }
        elementary_ids: dict[tuple[str, str], str] = {}
        for key, node in sorted(self.referenced_nodes.items()):
            if key in self.data or not self._is_biosphere_reference(key):
                continue
            self.report.add(
                "warning",
                "flow_classification_generalized",
                "Brightway elementary-flow categories were preserved as text and mapped only to the closest TIDAS compartment",
                dataset=f"{key[0]}:{key[1]}",
            )
            source_provenance = database_provenance.get(
                key[0],
                {
                    "license": "missing",
                    "owner": "missing",
                    "source": key[0],
                },
            )
            uuid = stable_uuid(
                f"native-elementary-flow:{key[0]}:{key[1]}:"
                f"{_provenance_namespace(source_provenance)}:{semantic_hash(node)}"
            )
            elementary_ids[key] = uuid
            unit = str(node.get("unit") or "unit")
            _, property_uuid = unit_context[unit]
            records.append(
                record_from_document(
                    flow_document(
                        uuid,
                        str(node.get("name") or key[1]),
                        unit,
                        property_uuid,
                        elementary=True,
                        categories=(
                            str(node.get("type") or ""),
                            *tuple(str(item) for item in (node.get("categories") or ())),
                        ),
                    ),
                    source_path=f"generated/flows/{uuid}",
                )
            )

        process_ids = {
            key: stable_uuid(
                f"native-process:{self.project}:{key[0]}:{key[1]}:{main_namespace}:"
                f"{semantic_hash({'dataset': value, 'resolved_flow_ids': _resolved_flow_ids(value, product_flow_ids, elementary_ids)})}"
            )
            for key, value in self.data.items()
        }
        self.generated_process_ids = process_ids

        for key, dataset in sorted(self.data.items()):
            unit = str(dataset.get("unit") or "unit")
            _, property_uuid = unit_context[unit]
            flow_uuid = product_flow_ids[key]
            records.append(
                record_from_document(
                    flow_document(
                        flow_uuid,
                        str(dataset.get("reference product") or dataset.get("name") or key[1]),
                        unit,
                        property_uuid,
                        elementary=False,
                    ),
                    source_path=f"generated/flows/{flow_uuid}",
                )
            )

        for key, dataset in sorted(self.data.items()):
            all_production = [
                exchange
                for exchange in dataset.get("exchanges", [])
                if exchange.get("type") == "production"
            ]
            production = [exchange for exchange in all_production if _input_key(exchange) == key]
            if len(production) != 1:
                self.report.add(
                    "error",
                    "production_exchange_count",
                    f"Expected one self-production exchange, found {len(production)}",
                    dataset=f"{key[0]}:{key[1]}",
                )
                continue
            if len(all_production) != 1:
                self.report.add(
                    "error",
                    "unsupported_additional_production",
                    "Additional production or co-product exchanges are not supported",
                    dataset=f"{key[0]}:{key[1]}",
                )
            if "amount" not in production[0]:
                self.report.add(
                    "error",
                    "missing_brightway_amount",
                    "Reference production exchange has no amount",
                    dataset=f"{key[0]}:{key[1]}",
                )
                production_amount = None
            else:
                production_amount = _brightway_amount(
                    production[0]["amount"],
                    report=self.report,
                    dataset=f"{key[0]}:{key[1]}",
                    exchange="reference production",
                )
            if production_amount is None or production_amount <= 0:
                self.report.add(
                    "error",
                    "non_positive_reference_amount",
                    "The self-production amount must be greater than zero",
                    dataset=f"{key[0]}:{key[1]}",
                )
                production_amount = Decimal("1")
            production_uncertainty = {
                field_name: production[0][field_name]
                for field_name in BW_UNCERTAINTY_FIELDS
                if field_name in production[0]
                and not (
                    field_name == "uncertainty type"
                    and production[0][field_name] in (None, 0)
                )
                and not (field_name == "negative" and production[0][field_name] is False)
            }
            if production_uncertainty:
                self.report.add(
                    "error",
                    "unsupported_native_exchange_uncertainty",
                    "Native Brightway reference production uncertainty cannot yet be "
                    f"exported to TIDAS without changing its statistical meaning: "
                    f"{production_uncertainty}",
                    dataset=f"{key[0]}:{key[1]}",
                )
            exchanges = [
                process_exchange(
                    0,
                    product_flow_ids[key],
                    str(dataset.get("reference product") or dataset.get("name") or key[1]),
                    production_amount,
                    "Output",
                )
            ]
            next_id = 1
            for exchange in dataset.get("exchanges", []):
                exchange_type = exchange.get("type")
                if exchange is production[0] or exchange_type == "production":
                    continue
                native_uncertainty = {
                    key: exchange[key]
                    for key in BW_UNCERTAINTY_FIELDS
                    if key in exchange
                    and not (key == "uncertainty type" and exchange[key] in (None, 0))
                    and not (key == "negative" and exchange[key] is False)
                }
                if native_uncertainty:
                    self.report.add(
                        "error",
                        "unsupported_native_exchange_uncertainty",
                        "Native Brightway exchange uncertainty cannot yet be exported to "
                        f"TIDAS without changing its statistical meaning: {native_uncertainty}",
                        dataset=f"{key[0]}:{key[1]}",
                    )
                    continue
                input_key = _input_key(exchange)
                if input_key is None:
                    self.report.add(
                        "error",
                        "missing_brightway_input",
                        "Exchange has no two-part Brightway input key",
                        dataset=f"{key[0]}:{key[1]}",
                    )
                    continue
                exchange_location: str | None = None
                if exchange_type == "technosphere":
                    provider = self.data.get(input_key)
                    if provider is None:
                        self.report.add(
                            "error",
                            "external_technosphere_dependency",
                            f"Provider {input_key} is outside database {self.database}",
                            dataset=f"{key[0]}:{key[1]}",
                        )
                        continue
                    flow_uuid = product_flow_ids[input_key]
                    flow_name = str(
                        provider.get("reference product") or provider.get("name") or input_key[1]
                    )
                    direction = "Input"
                    raw_location = str(exchange.get("location") or "").strip()
                    exchange_location = (
                        raw_location if raw_location and raw_location.upper() != "NULL" else None
                    )
                elif exchange_type == "biosphere":
                    node = self.referenced_nodes.get(input_key)
                    flow_uuid = elementary_ids.get(input_key)
                    if node is None or flow_uuid is None:
                        self.report.add(
                            "error",
                            "biosphere_node_not_found",
                            f"Biosphere node {input_key} is unavailable",
                            dataset=f"{key[0]}:{key[1]}",
                        )
                        continue
                    flow_name = str(node.get("name") or input_key[1])
                    direction = _native_biosphere_direction(node)
                    if direction is None:
                        continue
                else:
                    self.report.add(
                        "error",
                        "unsupported_exchange_type",
                        f"Unsupported Brightway exchange type: {exchange_type}",
                        dataset=f"{key[0]}:{key[1]}",
                    )
                    continue
                if "amount" not in exchange:
                    self.report.add(
                        "error",
                        "missing_brightway_amount",
                        f"{exchange_type} exchange {input_key} has no amount",
                        dataset=f"{key[0]}:{key[1]}",
                    )
                    continue
                amount = _brightway_amount(
                    exchange["amount"],
                    report=self.report,
                    dataset=f"{key[0]}:{key[1]}",
                    exchange=f"{exchange_type}:{input_key}",
                )
                if amount is None:
                    continue
                exchanges.append(
                    process_exchange(
                        next_id,
                        flow_uuid,
                        flow_name,
                        amount,
                        direction,
                        location=exchange_location,
                    )
                )
                next_id += 1
            reference_year = _year_value(
                dataset.get("reference year", dataset.get("reference_year"))
            )
            valid_until = _year_value(dataset.get("valid until", dataset.get("valid_until")))
            if reference_year is None:
                self.report.add(
                    "warning",
                    "generated_placeholder_reference_year",
                    "Brightway had no reference year; TIDAS uses a clearly labelled schema placeholder",
                    dataset=f"{key[0]}:{key[1]}",
                )
            process_type = str(dataset.get("tidas process type") or "")
            if process_type not in PROCESS_TYPES:
                process_type = "Unit process, black box"
                self.report.add(
                    "warning",
                    "generated_process_type_assumption",
                    "Brightway had no TIDAS process type; exported conservatively as a unit-process black box",
                    dataset=f"{key[0]}:{key[1]}",
                )
            records.append(
                record_from_document(
                    process_document(
                        process_ids[key],
                        str(dataset.get("name") or key[1]),
                        str(dataset.get("reference product") or dataset.get("name") or key[1]),
                        str(dataset.get("location") or "GLO"),
                        exchanges,
                        reference_year=reference_year,
                        valid_until=valid_until,
                        process_type=process_type,
                    ),
                    source_path=f"generated/processes/{process_ids[key]}",
                )
            )

        if provenance is not None and all(
            key[0] in database_provenance
            for key in self.referenced_nodes
            if self._is_biosphere_reference(key)
        ):
            record_provenance: dict[str, Mapping[str, str]] = {
                **{process_uuid: provenance for process_uuid in process_ids.values()},
                **{flow_uuid: provenance for flow_uuid in product_flow_ids.values()},
                **{
                    flow_uuid: database_provenance[key[0]]
                    for key, flow_uuid in elementary_ids.items()
                },
            }
            records = _apply_native_provenance(records, record_provenance)
            self.native_provenance = provenance
            self.native_database_provenance = {
                name: dict(item) for name, item in sorted(database_provenance.items())
            }
        return records

    def _resolve_native_provenance(self) -> dict[str, str] | None:
        license_text = self.native_license or _metadata_text(
            self.metadata,
            ("license", "licence", "license_name", "license_url"),
        )
        owner = self.native_owner or _metadata_text(
            self.metadata,
            ("owner", "publisher", "copyright_holder", "author", "authors"),
        )
        source = self.native_source or _metadata_text(
            self.metadata,
            ("source", "source_url", "url", "homepage"),
        )
        if not license_text:
            self.report.add(
                "error",
                "missing_native_open_license",
                "Native Brightway export requires an explicit open licence declaration",
            )
        elif (
            license_text.strip().lower() == "none"
            or is_restricted_license(license_text)
            or not is_open_license(license_text)
        ):
            self.report.add(
                "error",
                "native_license_not_open",
                f"Native Brightway licence is not recognised as open: {license_text}",
            )
        if not owner:
            self.report.add(
                "error",
                "missing_native_owner",
                "Native Brightway export requires the data owner or publisher",
            )
        if not source:
            self.report.add(
                "error",
                "missing_native_source",
                "Native Brightway export requires a source citation or URL",
            )
        if (
            not license_text
            or license_text.strip().lower() == "none"
            or not owner
            or not source
            or not is_open_license(license_text)
        ):
            return None
        return {"license": license_text, "owner": owner, "source": source}

    def _resolve_source_database_provenance(self, database: str) -> dict[str, str] | None:
        metadata = self.source_database_metadata.get(database, {})
        license_text = _metadata_text(
            metadata,
            ("license", "licence", "license_name", "license_url"),
        )
        owner = _metadata_text(
            metadata,
            ("owner", "publisher", "copyright_holder", "author", "authors"),
        )
        source = _metadata_text(
            metadata,
            ("source", "source_url", "url", "homepage"),
        )
        identity = f"Brightway database {database}"
        if (
            not license_text
            or license_text.strip().lower() == "none"
            or is_restricted_license(license_text)
            or not is_open_license(license_text)
        ):
            self.report.add(
                "error",
                "source_database_license_not_open",
                f"{identity} requires its own explicit, recognised open licence",
            )
        if not owner:
            self.report.add(
                "error",
                "missing_source_database_owner",
                f"{identity} requires its own owner or publisher",
            )
        if not source:
            self.report.add(
                "error",
                "missing_source_database_source",
                f"{identity} requires its own source citation or URL",
            )
        if not license_text or not owner or not source or not is_open_license(license_text):
            return None
        return {"license": license_text, "owner": owner, "source": source}

    def _report_native_metadata_limits(self) -> None:
        uncertainty_keys = {
            "uncertainty type",
            "loc",
            "scale",
            "shape",
            "minimum",
            "maximum",
        }
        uncertain = 0
        parameterised = 0
        for key, dataset in self.data.items():
            identity = f"{key[0]}:{key[1]}"
            if not dataset.get("name"):
                self.report.add(
                    "error",
                    "missing_activity_name",
                    "Native Brightway activity has no name",
                    dataset=identity,
                )
            if not dataset.get("reference product"):
                self.report.add(
                    "error",
                    "missing_reference_product",
                    "Native Brightway activity has no explicit reference product",
                    dataset=identity,
                )
            if not dataset.get("unit"):
                self.report.add(
                    "error",
                    "missing_activity_unit",
                    "Native Brightway activity has no unit",
                    dataset=identity,
                )
            if not dataset.get("location"):
                self.report.add(
                    "error",
                    "missing_activity_location",
                    "Native Brightway activity has no explicit location; use GLO only when it is scientifically intended",
                    dataset=identity,
                )
            if dataset.get("categories") or dataset.get("classifications"):
                self.report.add(
                    "warning",
                    "process_classification_not_migrated",
                    "Brightway activity classifications are retained in the manifest only and not mapped to a TIDAS classification",
                    dataset=identity,
                )
            if any(key in dataset for key in ("parameters", "formula")):
                parameterised += 1
            for exchange in dataset.get("exchanges", []):
                if uncertainty_keys.intersection(exchange):
                    uncertain += 1
                if any(field in exchange for field in ("formula", "parameters")):
                    parameterised += 1
                if exchange.get("type") == "biosphere":
                    location = str(exchange.get("location") or "").strip()
                    if location and location.upper() not in {"GLO", "NULL"}:
                        self.report.add(
                            "error",
                            "unsupported_native_biosphere_location",
                            f"Location-specific biosphere exchange {location} requires regionalised mapping",
                            dataset=identity,
                        )
                elif exchange.get("type") == "technosphere":
                    location = str(exchange.get("location") or "").strip()
                    input_key = _input_key(exchange)
                    provider = self.data.get(input_key) if input_key is not None else None
                    if (
                        location
                        and location.upper() != "NULL"
                        and provider is not None
                        and location != str(provider.get("location") or "")
                    ):
                        self.report.add(
                            "error",
                            "technosphere_location_provider_mismatch",
                            f"Technosphere exchange anchor {location} does not match provider location {provider.get('location')}",
                            dataset=identity,
                        )
        for key, node in self.referenced_nodes.items():
            if not self._is_biosphere_reference(key):
                continue
            identity = f"{key[0]}:{key[1]}"
            if not node.get("name"):
                self.report.add(
                    "error",
                    "missing_biosphere_name",
                    "Native Brightway biosphere flow has no name",
                    dataset=identity,
                )
            if not node.get("unit"):
                self.report.add(
                    "error",
                    "missing_biosphere_unit",
                    "Native Brightway biosphere flow has no unit",
                    dataset=identity,
                )
            location = str(node.get("location") or "").strip()
            if location and location.upper() not in {"GLO", "NULL"}:
                self.report.add(
                    "error",
                    "unsupported_native_biosphere_location",
                    f"Location-specific biosphere flow {location} requires regionalised mapping",
                    dataset=identity,
                )
            if _native_biosphere_direction(node) is None:
                self.report.add(
                    "error",
                    "ambiguous_native_biosphere_direction",
                    "Biosphere flow must have consistent emission or natural-resource direction evidence",
                    dataset=identity,
                )
        if uncertain:
            self.report.add(
                "error",
                "unsupported_native_exchange_uncertainty",
                f"{uncertain} Brightway exchanges have uncertainty fields that cannot yet be migrated without loss",
            )
        if parameterised:
            self.report.add(
                "error",
                "unsupported_native_parameters",
                f"{parameterised} Brightway activities or exchanges have parameters or formulas that cannot yet be migrated without loss",
            )
        selected_parameters = self.parameter_counts.get("activity", 0) + self.parameter_counts.get(
            "database", 0
        )
        if selected_parameters:
            self.report.add(
                "error",
                "unsupported_native_parameter_tables",
                f"{selected_parameters} activity or database parameters are attached to the selected Brightway database",
            )
        if self.parameter_counts.get("project", 0):
            self.report.add(
                "warning",
                "project_parameters_not_migrated",
                f"The Brightway project has {self.parameter_counts['project']} project parameters; verify they are unrelated to this database",
            )

    def _units(self) -> set[str]:
        values = {str(dataset.get("unit") or "unit") for dataset in self.data.values()}
        for key, node in self.referenced_nodes.items():
            if self._is_biosphere_reference(key):
                values.add(str(node.get("unit") or "unit"))
        return values

    def _is_biosphere_reference(self, key: tuple[str, str]) -> bool:
        return any(
            exchange.get("type") == "biosphere" and _input_key(exchange) == key
            for dataset in self.data.values()
            for exchange in dataset.get("exchanges", [])
        )


def _key(key: Any, value: Mapping[str, Any]) -> tuple[str, str]:
    if isinstance(key, tuple | list) and len(key) == 2:
        return str(key[0]), str(key[1])
    database = value.get("database")
    code = value.get("code")
    if database is None or code is None:
        raise ValueError(f"Brightway dataset has no stable key: {key!r}")
    return str(database), str(code)


def _input_key(exchange: Mapping[str, Any]) -> tuple[str, str] | None:
    value = exchange.get("input")
    if isinstance(value, tuple | list) and len(value) == 2:
        return str(value[0]), str(value[1])
    return None


def _resolved_flow_ids(
    dataset: Mapping[str, Any],
    product_flow_ids: Mapping[tuple[str, str], str],
    elementary_ids: Mapping[tuple[str, str], str],
) -> list[dict[str, Any]]:
    """Include the generated flow references in a native process identity."""
    result: list[dict[str, Any]] = []
    for exchange in dataset.get("exchanges", []):
        input_key = _input_key(exchange)
        exchange_type = str(exchange.get("type") or "")
        if exchange_type in {"production", "technosphere"}:
            flow_uuid = product_flow_ids.get(input_key) if input_key is not None else None
        elif exchange_type == "biosphere":
            flow_uuid = elementary_ids.get(input_key) if input_key is not None else None
        else:
            flow_uuid = None
        result.append(
            {
                "type": exchange_type,
                "input": list(input_key) if input_key is not None else None,
                "generated_flow_uuid": flow_uuid,
            }
        )
    return result


def _identity_set(value: Any) -> set[tuple[str, str]]:
    result: set[tuple[str, str]] = set()
    for item in as_list(value):
        if not isinstance(item, Mapping) or not item.get("uuid") or not item.get("version"):
            continue
        result.add((str(item["uuid"]).lower(), str(item["version"])))
    return result


def _has_dependent_geography(geography: Any, location_node: Mapping[str, Any]) -> bool:
    if any(key != "@location" for key in location_node):
        return True
    if not isinstance(geography, Mapping):
        return False
    return any(key != "locationOfOperationSupplyOrProduction" for key in geography)


def _year_value(value: Any) -> int | None:
    try:
        year = int(str(value))
    except (TypeError, ValueError):
        return None
    return year if 0 <= year <= 9999 else None


def _native_biosphere_direction(node: Mapping[str, Any]) -> str | None:
    node_type = str(node.get("type") or "").lower()
    categories = " ".join(str(item).lower() for item in node.get("categories", ()))
    resource = any(marker in node_type for marker in ("resource", "natural")) or any(
        marker in categories for marker in ("resource", "natural")
    )
    emission = "emission" in node_type or "emission" in categories
    if resource == emission:
        return None
    return "Input" if resource else "Output"


def _metadata_text(metadata: Mapping[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, list | tuple):
            parts = [str(item).strip() for item in value if str(item).strip()]
            if parts:
                return "; ".join(parts)
        if isinstance(value, Mapping):
            for nested_key in ("name", "title", "url", "text"):
                nested = value.get(nested_key)
                if nested:
                    return str(nested).strip()
    return None


def _apply_native_provenance(
    records: list[DatasetRecord],
    record_provenance: Mapping[str, Mapping[str, str]],
) -> list[DatasetRecord]:
    result: list[DatasetRecord] = []
    for record in records:
        document = deepcopy(record.document)
        provenance = record_provenance.get(record.uuid)
        if provenance is None or record.uuid in {OWNER_UUID, FORMAT_SOURCE_UUID}:
            _rewrite_publication_nodes(
                document,
                owner_uuid=OWNER_UUID,
                owner="tidas-bw",
                license_text="MIT License",
                write_license_fields=record.category in LICENSE_FIELD_CATEGORIES,
            )
        else:
            owner_uuid = _provenance_owner_uuid(provenance)
            _rewrite_publication_nodes(
                document,
                owner_uuid=owner_uuid,
                owner=provenance["owner"],
                license_text=provenance["license"],
                write_license_fields=record.category in LICENSE_FIELD_CATEGORIES,
            )
        root = document.get(record.root_key)
        if isinstance(root, dict) and provenance is not None:
            if record.category == "processes":
                info = deep_get(root, "processInformation", "dataSetInformation")
            elif record.category == "flows":
                info = deep_get(root, "flowInformation", "dataSetInformation")
            else:
                info = None
            if isinstance(info, dict):
                existing_comment = pick_text(info.get("common:generalComment"))
                source_comment = (
                    f"Generated from Brightway source: {provenance['source']}. "
                    f"Source licence: {provenance['license']}."
                )
                info["common:generalComment"] = multilingual(
                    f"{existing_comment} {source_comment}".strip()
                )
        result.append(record_from_document(document, source_path=record.source_path))
    unique_provenance = {
        (item["license"], item["owner"], item["source"]): item
        for item in record_provenance.values()
    }
    for provenance in unique_provenance.values():
        owner_uuid = _provenance_owner_uuid(provenance)
        owner_document = contact_document()
        owner_info = deep_get(
            owner_document,
            "contactDataSet",
            "contactInformation",
            "dataSetInformation",
        )
        if isinstance(owner_info, dict):
            owner_info["common:UUID"] = owner_uuid
            owner_info["common:shortName"] = multilingual(provenance["owner"])
            owner_info["common:name"] = multilingual(provenance["owner"])
            if str(provenance["source"]).startswith(("http://", "https://")):
                owner_info["WWWAddress"] = provenance["source"]
            else:
                owner_info.pop("WWWAddress", None)
        _rewrite_publication_nodes(
            owner_document,
            owner_uuid=owner_uuid,
            owner=provenance["owner"],
            license_text=provenance["license"],
            write_license_fields=False,
        )
        result.append(
            record_from_document(
                owner_document,
                source_path=f"generated/native-data-owner/{owner_uuid}",
            )
        )
    return result


def _provenance_owner_uuid(provenance: Mapping[str, str]) -> str:
    return stable_uuid(
        f"native-owner:{provenance['owner']}:{provenance['source']}:{provenance['license']}"
    )


def _provenance_namespace(provenance: Mapping[str, str]) -> str:
    return semantic_hash(
        {
            "license": provenance["license"],
            "owner": provenance["owner"],
            "source": provenance["source"],
        }
    )


def _rewrite_publication_nodes(
    value: Any,
    *,
    owner_uuid: str,
    owner: str,
    license_text: str,
    write_license_fields: bool,
) -> None:
    if isinstance(value, dict):
        if "common:referenceToOwnershipOfDataSet" in value:
            value["common:referenceToOwnershipOfDataSet"] = global_reference(
                "contacts", owner_uuid, owner
            )
            if write_license_fields:
                normalised_license = license_text.lower()
                value["common:copyright"] = (
                    "false"
                    if "cc0" in normalised_license or "public domain" in normalised_license
                    else "true"
                )
                value["common:licenseType"] = "Free of charge for all users and uses"
                value["common:accessRestrictions"] = multilingual(license_text)
            else:
                # These fields break the official eILCD/XSD projection on
                # structural record types (issue #1); strip stale copies.
                value.pop("common:copyright", None)
                value.pop("common:licenseType", None)
                value.pop("common:accessRestrictions", None)
        for child in value.values():
            _rewrite_publication_nodes(
                child,
                owner_uuid=owner_uuid,
                owner=owner,
                license_text=license_text,
                write_license_fields=write_license_fields,
            )
    elif isinstance(value, list):
        for child in value:
            _rewrite_publication_nodes(
                child,
                owner_uuid=owner_uuid,
                owner=owner,
                license_text=license_text,
                write_license_fields=write_license_fields,
            )


def _brightway_amount(
    value: Any,
    *,
    report: MigrationReport,
    dataset: str,
    exchange: str,
) -> Decimal | None:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        amount = Decimal("NaN")
    try:
        fits_float = isfinite(float(amount))
    except (OverflowError, ValueError):
        fits_float = False
    if not amount.is_finite() or not fits_float or (amount != 0 and float(amount) == 0):
        report.add(
            "error",
            "invalid_brightway_amount",
            f"Exchange {exchange} has an invalid amount: {value!r}",
            dataset=dataset,
        )
        return None
    return amount


def _decimal_value(value: Any) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _uncertainty_signature(fields: Mapping[str, Any]) -> tuple[tuple[str, Any], ...]:
    """Canonical, order-stable signature of an uncertainty field mapping."""
    result: list[tuple[str, Any]] = []
    for key, value in fields.items():
        result.append((str(key), _canonical_value(value)))
    return tuple(sorted(result))


def _canonical_value(value: Any) -> Any:
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int | float):
        return round(float(value), 12)
    try:
        return round(float(str(value)), 12)
    except (TypeError, ValueError):
        return str(value)


def _factor_signature(rows: Any) -> list[tuple[str, str, str, str]]:
    result: list[tuple[str, str, str, str]] = []
    for row in as_list(rows):
        if not isinstance(row, tuple | list) or len(row) < 2:
            continue
        key = row[0]
        if not isinstance(key, tuple | list) or len(key) != 2:
            continue
        try:
            amount = number_string(row[1])
        except (InvalidOperation, TypeError, ValueError):
            amount = str(row[1])
        extra = ""
        if len(row) > 2 and row[2] is not None:
            if isinstance(row[2], Mapping):
                extra = repr(_uncertainty_signature(row[2]))
            else:
                extra = str(row[2])
        result.append((str(key[0]), str(key[1]), amount, extra))
    return sorted(result)


def _valid_uuid(value: Any) -> str | None:
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return None


def _process_uuid(key: tuple[str, str], dataset: Mapping[str, Any]) -> str:
    tidas = dataset.get("tidas")
    if isinstance(tidas, Mapping) and _valid_uuid(tidas.get("uuid")):
        return str(tidas["uuid"])
    return _valid_uuid(key[1]) or stable_uuid(f"process:{key[0]}:{key[1]}")


def _node_uuid(key: tuple[str, str], node: Mapping[str, Any], *, kind: str) -> str:
    tidas = node.get("tidas")
    if isinstance(tidas, Mapping) and _valid_uuid(tidas.get("uuid")):
        return str(tidas["uuid"])
    return _valid_uuid(key[1]) or stable_uuid(f"{kind}:{key[0]}:{key[1]}")


def _deduplicate_documents(
    documents: list[dict[str, Any]], report: MigrationReport
) -> list[DatasetRecord]:
    records: dict[tuple[str, str, str], DatasetRecord] = {}
    for index, document in enumerate(documents):
        record = record_from_document(document, source_path=f"preserved/{index}")
        key = (record.category, record.uuid.lower(), record.version)
        existing = records.get(key)
        if existing is not None and existing.document != record.document:
            report.add(
                "error",
                "conflicting_preserved_document",
                "Two different preserved documents have the same TIDAS identity",
                dataset=record.identity,
            )
            continue
        records[key] = record
    return list(records.values())


def _counts(records: list[DatasetRecord]) -> dict[str, int]:
    result: dict[str, int] = defaultdict(int)
    for record in records:
        result[record.category] += 1
    return dict(sorted(result.items()))
