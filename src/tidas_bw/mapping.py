"""Map a validated TIDAS package into Brightway database payloads."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from math import isfinite
from typing import Any

from .models import (
    BrightwayMethodPayload,
    BrightwayPayload,
    DatasetRecord,
    MigrationReport,
)
from .package import TidasPackage
from .uncertainty import convert_uncertainty
from .utils import as_list, deep_get, number_string, pick_text, reference_uuid


class TidasMapper:
    def __init__(
        self,
        package: TidasPackage,
        *,
        database: str,
        biosphere_database: str,
    ) -> None:
        self.package = package
        self.database = database
        self.biosphere_database = biosphere_database
        self.report = MigrationReport(
            direction="tidas-to-brightway",
            source=package.source,
            target=f"{database} (+ {biosphere_database})",
        )
        self.exact = package.index()
        self.by_uuid = package.uuid_index()
        self._assert_single_version_per_uuid()
        self.processes = {
            record.uuid.lower(): record for record in package.by_category("processes")
        }
        self.flows = {record.uuid.lower(): record for record in package.by_category("flows")}
        self.flow_directions = self._elementary_flow_directions()
        self.explicit_providers = self._explicit_provider_map()
        self.providers_by_flow = self._reference_providers()

    def build(self) -> BrightwayPayload:
        biosphere = self._build_biosphere()
        technosphere = self._build_technosphere()
        methods = self._build_methods()
        auxiliary = [
            {
                "category": record.category,
                "uuid": record.uuid,
                "version": record.version,
                "document": deepcopy(record.document),
                "source_path": record.source_path,
            }
            for record in self.package.records
            if record.category not in {"processes", "lciamethods"}
        ]
        metadata = {
            "format": "tidas-bw-metadata-v1",
            "source": self.package.source,
            "source_counts": self.package.counts(),
            "process_identities": [
                {"uuid": record.uuid.lower(), "version": record.version}
                for record in self.package.by_category("processes")
            ],
            "biosphere_flow_identities": [
                {"uuid": record.uuid.lower(), "version": record.version}
                for record in self.package.by_category("flows")
                if _flow_type(record) == "Elementary flow"
            ],
            "auxiliary_documents": auxiliary,
            "lcia_documents": [
                deepcopy(record.document) for record in self.package.by_category("lciamethods")
            ],
        }
        self.report.counts.update(
            {
                "activities": len(technosphere),
                "biosphere_flows": len(biosphere),
                "methods": len(methods),
            }
        )
        return BrightwayPayload(
            technosphere=technosphere,
            biosphere=biosphere,
            methods=methods,
            database_metadata=metadata,
            report=self.report,
        )

    def _assert_single_version_per_uuid(self) -> None:
        for (category, uuid), records in self.by_uuid.items():
            versions = {record.version for record in records}
            if len(versions) > 1:
                self.report.add(
                    "error",
                    "multiple_versions",
                    f"Brightway code cannot represent multiple {category} versions: {sorted(versions)}",
                    dataset=f"{category}:{uuid}",
                )

    def _build_biosphere(self) -> dict[tuple[str, str], dict[str, Any]]:
        data: dict[tuple[str, str], dict[str, Any]] = {}
        for record in self.package.by_category("flows"):
            flow_type = _flow_type(record)
            if flow_type != "Elementary flow":
                continue
            categories = _flow_categories(record)
            unit = self._unit_for_flow(record)
            expected_direction = _expected_elementary_direction(record)
            used_directions = self.flow_directions.get(record.uuid.lower(), set())
            if len(used_directions) > 1:
                self.report.add(
                    "error",
                    "ambiguous_elementary_flow_direction",
                    "The same elementary flow is used as both input and output",
                    dataset=record.identity,
                )
            elif (
                expected_direction and used_directions and expected_direction not in used_directions
            ):
                self.report.add(
                    "error",
                    "elementary_direction_mismatch",
                    f"Flow classification implies {expected_direction}, but exchanges use {sorted(used_directions)}",
                    dataset=record.identity,
                )
            effective_direction = expected_direction or next(iter(used_directions), "Output")
            node_type = "natural resource" if effective_direction == "Input" else "emission"
            key = (self.biosphere_database, record.uuid.lower())
            data[key] = {
                "name": _flow_name(record),
                "unit": unit,
                "location": _flow_location(record),
                "categories": tuple(categories),
                "type": node_type,
                "code": record.uuid.lower(),
                "database": self.biosphere_database,
                "tidas": {
                    "uuid": record.uuid,
                    "version": record.version,
                    "kind": "flow",
                    "document": deepcopy(record.document),
                    "brightway_snapshot": {
                        "name": _flow_name(record),
                        "unit": unit,
                        "location": _flow_location(record),
                        "categories": list(categories),
                        "type": node_type,
                    },
                },
            }
        return data

    def _build_technosphere(self) -> dict[tuple[str, str], dict[str, Any]]:
        data: dict[tuple[str, str], dict[str, Any]] = {}
        for record in self.package.by_category("processes"):
            if deep_get(record.document, "processDataSet", "mathematicalRelations"):
                self.report.add(
                    "warning",
                    "parameterization_frozen",
                    "Brightway receives evaluated exchange amounts; the original TIDAS mathematical relations remain only in preserved metadata",
                    dataset=record.identity,
                )
            process_exchanges = _process_exchanges(record)
            exchange_ids = [str(item.get("@dataSetInternalID")) for item in process_exchanges]
            duplicate_ids = sorted(
                identifier for identifier, count in Counter(exchange_ids).items() if count > 1
            )
            for identifier in duplicate_ids:
                self.report.add(
                    "error",
                    "duplicate_exchange_internal_id",
                    f"Process has multiple exchanges with internal ID {identifier}",
                    dataset=record.identity,
                )
            reference_ids = _reference_exchange_ids(record)
            reference_exchanges = [
                exchange
                for exchange in process_exchanges
                if str(exchange.get("@dataSetInternalID")) in reference_ids
            ]
            if len(reference_exchanges) != 1:
                self.report.add(
                    "error",
                    "reference_exchange_count",
                    f"Expected one reference exchange, found {len(reference_exchanges)}",
                    dataset=record.identity,
                )
                continue
            reference_exchange = reference_exchanges[0]
            reference_flow = self._flow_for_reference(
                reference_exchange.get("referenceToFlowDataSet"), record
            )
            if reference_flow is None:
                continue
            if _flow_type(reference_flow) == "Elementary flow":
                self.report.add(
                    "error",
                    "elementary_reference_flow",
                    "A process reference exchange cannot be an elementary flow",
                    dataset=record.identity,
                )
                continue
            if _flow_type(reference_flow) != "Product flow":
                self.report.add(
                    "error",
                    "unsupported_reference_flow_type",
                    f"Reference flow type is not supported: {_flow_type(reference_flow)}",
                    dataset=record.identity,
                )
                continue
            if str(reference_exchange.get("exchangeDirection")) != "Output":
                self.report.add(
                    "error",
                    "unsupported_reference_direction",
                    "Only output reference products are supported",
                    dataset=record.identity,
                )
                continue
            activity_key = (self.database, record.uuid.lower())
            reference_year = deep_get(
                record.document,
                "processDataSet",
                "processInformation",
                "time",
                "common:referenceYear",
            )
            valid_until = deep_get(
                record.document,
                "processDataSet",
                "processInformation",
                "time",
                "common:dataSetValidUntil",
            )
            process_type = deep_get(
                record.document,
                "processDataSet",
                "modellingAndValidation",
                "LCIMethodAndAllocation",
                "typeOfDataSet",
            )
            activity = {
                "name": _process_name(record),
                "reference product": _flow_name(reference_flow),
                "unit": self._unit_for_flow(reference_flow),
                "location": _process_location(record),
                "type": "processwithreferenceproduct",
                "code": record.uuid.lower(),
                "database": self.database,
                "exchanges": [],
                "tidas": {
                    "uuid": record.uuid,
                    "version": record.version,
                    "kind": "process",
                    "reference_flow_uuid": reference_flow.uuid,
                    "reference_flow_version": reference_flow.version,
                    "document": deepcopy(record.document),
                    "brightway_snapshot": {
                        "name": _process_name(record),
                        "reference_product": _flow_name(reference_flow),
                        "unit": self._unit_for_flow(reference_flow),
                        "reference_year": reference_year,
                        "valid_until": valid_until,
                        "tidas_process_type": process_type,
                        "categories": [],
                        "classifications": None,
                    },
                },
            }
            if reference_year is not None:
                activity["reference year"] = reference_year
            if valid_until is not None:
                activity["valid until"] = valid_until
            if process_type is not None:
                activity["tidas process type"] = process_type
            for exchange in process_exchanges:
                mapped = self._map_exchange(record, activity_key, exchange, reference_ids)
                if mapped is not None:
                    activity["exchanges"].append(mapped)
            activity["tidas"]["mapped_exchange_ids"] = [
                exchange["tidas"]["internal_id"] for exchange in activity["exchanges"]
            ]
            data[activity_key] = activity
        return data

    def _map_exchange(
        self,
        process: DatasetRecord,
        activity_key: tuple[str, str],
        exchange: Mapping[str, Any],
        reference_ids: set[str],
    ) -> dict[str, Any] | None:
        function_type = exchange.get("functionType")
        if function_type:
            self.report.add(
                "error",
                "unsupported_reminder_exchange",
                f"Reminder exchange {function_type!s} is informational and must not enter the Brightway matrix",
                dataset=process.identity,
                path=f"exchange:{exchange.get('@dataSetInternalID')}",
            )
            return None
        flow = self._flow_for_reference(exchange.get("referenceToFlowDataSet"), process)
        if flow is None:
            return None
        amount = _exchange_amount(exchange, self.report, process)
        if amount is None:
            return None
        internal_id = str(exchange.get("@dataSetInternalID", ""))
        if exchange.get("referenceToVariable"):
            self.report.add(
                "warning",
                "parameterization_frozen",
                "Brightway receives the evaluated resultingAmount; this exchange's variable link remains only in preserved TIDAS metadata",
                dataset=process.identity,
                path=f"exchange:{internal_id}",
            )
        direction = str(exchange.get("exchangeDirection", ""))
        exchange_location = _exchange_location(exchange)
        flow_type = _flow_type(flow)
        uncertainty = convert_uncertainty(
            distribution=exchange.get("uncertaintyDistributionType"),
            rsd95=exchange.get("relativeStandardDeviation95In"),
            minimum=exchange.get("minimumAmount"),
            maximum=exchange.get("maximumAmount"),
            amount=amount,
        )
        if uncertainty.error:
            self.report.add(
                "error",
                "unmappable_exchange_uncertainty",
                uncertainty.error,
                dataset=process.identity,
                path=f"exchange:{internal_id}",
            )
        for note in uncertainty.notes:
            self.report.add(
                "info",
                "exchange_uncertainty_note",
                note,
                dataset=process.identity,
                path=f"exchange:{internal_id}",
            )
        if uncertainty.fields:
            self.report.add(
                "info",
                "exchange_uncertainty_mapped",
                "Mapped exchange uncertainty to Brightway fields: "
                + ", ".join(
                    f"{key}={value!r}" for key, value in sorted(uncertainty.fields.items())
                ),
                dataset=process.identity,
                path=f"exchange:{internal_id}",
            )
        uncertainty_fields = dict(uncertainty.fields) if not uncertainty.error else {}
        metadata = {
            "internal_id": internal_id,
            "flow_uuid": flow.uuid,
            "flow_version": flow.version,
            "exchange_direction": direction,
            "mean_amount": exchange.get("meanAmount"),
            "resulting_amount": exchange.get("resultingAmount"),
            "brightway_amount": number_string(amount),
            "brightway_location": exchange_location or "",
            "document": deepcopy(dict(exchange)),
        }
        if internal_id in reference_ids:
            if amount <= 0:
                self.report.add(
                    "error",
                    "non_positive_reference_amount",
                    "The TIDAS reference exchange amount must be greater than zero",
                    dataset=process.identity,
                    path=f"exchange:{internal_id}",
                )
            metadata.update(
                {
                    "brightway_type": "production",
                    "input_database": self.database,
                    "input_code": process.uuid.lower(),
                }
            )
            return {
                "input": activity_key,
                "amount": amount,
                "location": exchange_location,
                "type": "production",
                "name": _flow_name(flow),
                "unit": self._unit_for_flow(flow),
                **uncertainty_fields,
                "tidas": metadata,
            }
        if flow_type == "Elementary flow":
            if exchange_location:
                self.report.add(
                    "error",
                    "unsupported_location_specific_biosphere_exchange",
                    f"Location-specific elementary exchange {exchange_location} requires a regionalised Brightway setup",
                    dataset=process.identity,
                    path=f"exchange:{internal_id}",
                )
            metadata.update(
                {
                    "brightway_type": "biosphere",
                    "input_database": self.biosphere_database,
                    "input_code": flow.uuid.lower(),
                }
            )
            return {
                "input": (self.biosphere_database, flow.uuid.lower()),
                "amount": amount,
                "location": exchange_location,
                "type": "biosphere",
                "name": _flow_name(flow),
                "unit": self._unit_for_flow(flow),
                **uncertainty_fields,
                "tidas": metadata,
            }
        if flow_type != "Product flow":
            self.report.add(
                "error",
                "unsupported_flow_type",
                f"Non-reference flow type is not supported: {flow_type}",
                dataset=process.identity,
                path=f"exchange:{internal_id}",
            )
            return None
        if direction == "Input":
            provider = self._provider_for(
                process,
                flow,
                exchange_location=exchange_location,
            )
            if provider is None:
                return None
            metadata.update(
                {
                    "brightway_type": "technosphere",
                    "input_database": self.database,
                    "input_code": provider.uuid.lower(),
                    "provider_process_uuid": provider.uuid,
                }
            )
            return {
                "input": (self.database, provider.uuid.lower()),
                "amount": amount,
                "location": exchange_location,
                "type": "technosphere",
                "name": _flow_name(flow),
                "unit": self._unit_for_flow(flow),
                **uncertainty_fields,
                "tidas": metadata,
            }
        self.report.add(
            "error",
            "unsupported_non_reference_output",
            "Non-reference product outputs and co-products are not supported",
            dataset=process.identity,
            path=f"exchange:{internal_id}",
        )
        return None

    def _provider_for(
        self,
        consumer: DatasetRecord,
        flow: DatasetRecord,
        *,
        exchange_location: str | None,
    ) -> DatasetRecord | None:
        flow_location = _flow_location(flow)
        if exchange_location and flow_location and exchange_location != flow_location:
            self.report.add(
                "error",
                "exchange_flow_location_conflict",
                f"Exchange supply-region anchor {exchange_location} conflicts with flow supply location {flow_location}",
                dataset=consumer.identity,
            )
            return None
        anchor_location = exchange_location or flow_location
        explicit = self.explicit_providers.get(
            (consumer.uuid.lower(), flow.uuid.lower(), anchor_location or ""), []
        )
        if not explicit and anchor_location:
            explicit = self.explicit_providers.get(
                (consumer.uuid.lower(), flow.uuid.lower(), ""), []
            )
        explicit = list(dict.fromkeys(explicit))
        if len(explicit) == 1:
            provider = self.processes.get(explicit[0])
            if (
                provider is not None
                and anchor_location
                and _process_location(provider) != anchor_location
            ):
                self.report.add(
                    "error",
                    "explicit_provider_location_mismatch",
                    f"Lifecycle provider location {_process_location(provider)} conflicts with supply-region anchor {anchor_location}",
                    dataset=consumer.identity,
                )
                return None
            return provider
        if len(explicit) > 1:
            self.report.add(
                "error",
                "ambiguous_explicit_provider",
                f"Lifecycle models select multiple providers for flow {flow.uuid}: {explicit}",
                dataset=consumer.identity,
            )
            return None
        candidates = self.providers_by_flow.get(flow.uuid.lower(), [])
        if anchor_location:
            located = [item for item in candidates if _process_location(item) == anchor_location]
            if len(located) == 1:
                self.report.add(
                    "info",
                    "supply_location_provider",
                    f"Linked flow {flow.uuid} using supply-region anchor {anchor_location}",
                    dataset=consumer.identity,
                )
                return located[0]
            code = (
                "supply_location_provider_not_found"
                if not located
                else "supply_location_provider_ambiguous"
            )
            self.report.add(
                "error",
                code,
                f"Supply-region anchor {anchor_location} matched {len(located)} providers for flow {flow.uuid}",
                dataset=consumer.identity,
            )
            return None
        if len(candidates) == 1:
            self.report.add(
                "warning",
                "unique_provider_fallback",
                f"Linked flow {flow.uuid} to its only reference-output provider",
                dataset=consumer.identity,
            )
            return candidates[0]
        consumer_location = _process_location(consumer)
        located = [item for item in candidates if _process_location(item) == consumer_location]
        if len(located) == 1:
            self.report.add(
                "warning",
                "location_provider_fallback",
                f"Linked flow {flow.uuid} by exact process location {consumer_location}",
                dataset=consumer.identity,
            )
            return located[0]
        code = "provider_not_found" if not candidates else "provider_ambiguous"
        message = (
            f"No provider found for flow {flow.uuid}"
            if not candidates
            else f"Multiple providers found for flow {flow.uuid}: {[item.uuid for item in candidates]}"
        )
        self.report.add("error", code, message, dataset=consumer.identity)
        return None

    def _flow_for_reference(self, reference: Any, owner: DatasetRecord) -> DatasetRecord | None:
        if not isinstance(reference, Mapping):
            self.report.add(
                "error",
                "missing_flow_reference",
                "Exchange has no flow reference",
                dataset=owner.identity,
            )
            return None
        uuid = reference_uuid(reference)
        version = reference.get("@version")
        if not uuid or not version:
            self.report.add(
                "error",
                "incomplete_flow_reference",
                "Flow reference must contain UUID and exact version",
                dataset=owner.identity,
            )
            return None
        result = self.exact.get(("flows", uuid.lower(), str(version)))
        if result is None:
            self.report.add(
                "error",
                "flow_reference_not_found",
                f"Flow {uuid}@{version} is absent",
                dataset=owner.identity,
            )
        return result

    def _unit_for_flow(self, flow: DatasetRecord) -> str:
        flow_root = flow.document["flowDataSet"]
        reference_property_id = deep_get(
            flow_root,
            "flowInformation",
            "quantitativeReference",
            "referenceToReferenceFlowProperty",
        )
        assignments = as_list(deep_get(flow_root, "flowProperties", "flowProperty"))
        matching_assignments = [
            item
            for item in assignments
            if isinstance(item, Mapping)
            and str(item.get("@dataSetInternalID")) == str(reference_property_id)
        ]
        if len(matching_assignments) > 1:
            self.report.add(
                "error",
                "duplicate_flow_property_internal_id",
                f"Flow has multiple flow-property assignments with internal ID {reference_property_id}",
                dataset=flow.identity,
            )
        assignment = matching_assignments[0] if matching_assignments else None
        property_ref = assignment.get("referenceToFlowPropertyDataSet") if assignment else None
        property_record = self._record_for_reference("flowproperties", property_ref)
        if property_record is None:
            self.report.add(
                "error",
                "flow_property_not_found",
                "Could not resolve the reference flow property",
                dataset=flow.identity,
            )
            return "unit"
        return self._unit_for_property(property_record, owner=flow.identity)

    def _unit_for_property(self, property_record: DatasetRecord, *, owner: str) -> str:
        unit_group_ref = deep_get(
            property_record.document,
            "flowPropertyDataSet",
            "flowPropertiesInformation",
            "quantitativeReference",
            "referenceToReferenceUnitGroup",
        )
        unit_group = self._record_for_reference("unitgroups", unit_group_ref)
        if unit_group is None:
            self.report.add(
                "error",
                "unit_group_not_found",
                "Could not resolve the reference unit group",
                dataset=owner,
            )
            return "unit"
        root = unit_group.document["unitGroupDataSet"]
        reference_unit_id = deep_get(
            root,
            "unitGroupInformation",
            "quantitativeReference",
            "referenceToReferenceUnit",
        )
        units = as_list(deep_get(root, "units", "unit"))
        matching_units = [
            item
            for item in units
            if isinstance(item, Mapping)
            and str(item.get("@dataSetInternalID")) == str(reference_unit_id)
        ]
        if len(matching_units) > 1:
            self.report.add(
                "error",
                "duplicate_unit_internal_id",
                f"Unit group has multiple units with internal ID {reference_unit_id}",
                dataset=unit_group.identity,
            )
        unit = matching_units[0] if matching_units else None
        name = unit.get("name") if unit else None
        if not name:
            self.report.add(
                "error",
                "reference_unit_not_found",
                f"Reference unit {reference_unit_id!r} is absent",
                dataset=unit_group.identity,
            )
            return "unit"
        return str(name)

    def _record_for_reference(self, category: str, reference: Any) -> DatasetRecord | None:
        if not isinstance(reference, Mapping):
            return None
        uuid = reference_uuid(reference)
        version = reference.get("@version")
        if not uuid or not version:
            return None
        return self.exact.get((category, uuid.lower(), str(version)))

    def _reference_providers(self) -> dict[str, list[DatasetRecord]]:
        providers: dict[str, list[DatasetRecord]] = defaultdict(list)
        for process in self.package.by_category("processes"):
            reference_ids = _reference_exchange_ids(process)
            for exchange in _process_exchanges(process):
                if str(exchange.get("@dataSetInternalID")) not in reference_ids:
                    continue
                flow_uuid = reference_uuid(exchange.get("referenceToFlowDataSet"))
                if flow_uuid:
                    providers[flow_uuid.lower()].append(process)
        return dict(providers)

    def _elementary_flow_directions(self) -> dict[str, set[str]]:
        result: dict[str, set[str]] = defaultdict(set)
        for process in self.package.by_category("processes"):
            for exchange in _process_exchanges(process):
                flow_uuid = reference_uuid(exchange.get("referenceToFlowDataSet"))
                if not flow_uuid:
                    continue
                flow = self.flows.get(flow_uuid.lower())
                if flow is not None and _flow_type(flow) == "Elementary flow":
                    result[flow_uuid.lower()].add(str(exchange.get("exchangeDirection")))
        return dict(result)

    def _explicit_provider_map(self) -> dict[tuple[str, str, str], list[str]]:
        result: dict[tuple[str, str, str], list[str]] = defaultdict(list)
        for model in self.package.by_category("lifecyclemodels"):
            instances = as_list(
                deep_get(
                    model.document,
                    "lifeCycleModelDataSet",
                    "lifeCycleModelInformation",
                    "technology",
                    "processes",
                    "processInstance",
                )
            )
            instance_process: dict[str, DatasetRecord] = {}
            for instance in instances:
                if not isinstance(instance, Mapping):
                    continue
                process = self._record_for_reference(
                    "processes", instance.get("referenceToProcess")
                )
                if process is None:
                    self.report.add(
                        "error",
                        "lifecycle_process_reference_not_found",
                        "Lifecycle model process instance does not resolve to an exact process version",
                        dataset=model.identity,
                    )
                    continue
                instance_id = str(instance.get("@dataSetInternalID"))
                if instance_id in instance_process:
                    self.report.add(
                        "error",
                        "duplicate_process_instance_id",
                        f"Lifecycle model has multiple process instances with ID {instance_id}",
                        dataset=model.identity,
                    )
                    continue
                instance_process[instance_id] = process
            for instance in instances:
                if not isinstance(instance, Mapping):
                    continue
                provider = instance_process.get(str(instance.get("@dataSetInternalID")))
                if provider is None:
                    continue
                outputs = as_list(deep_get(instance, "connections", "outputExchange"))
                for output in outputs:
                    if not isinstance(output, Mapping) or not output.get("@flowUUID"):
                        continue
                    provider_flow_uuid = str(output["@flowUUID"]).lower()
                    provider_flow_version = str(output.get("@version") or "")
                    provider_flow = self.exact.get(
                        ("flows", provider_flow_uuid, provider_flow_version)
                    )
                    if provider_flow is None or not _process_has_flow(
                        provider,
                        provider_flow_uuid,
                        provider_flow_version,
                        direction="Output",
                        reference_only=True,
                    ):
                        self.report.add(
                            "error",
                            "explicit_provider_flow_mismatch",
                            f"Lifecycle model provider {provider.uuid} does not have {provider_flow_uuid}@{provider_flow_version} as its reference output",
                            dataset=model.identity,
                        )
                        continue
                    for downstream in as_list(output.get("downstreamProcess")):
                        if not isinstance(downstream, Mapping):
                            continue
                        consumer = instance_process.get(str(downstream.get("@id")))
                        if consumer is None:
                            self.report.add(
                                "error",
                                "lifecycle_consumer_not_found",
                                f"Lifecycle connection references unknown process instance {downstream.get('@id')}",
                                dataset=model.identity,
                            )
                            continue
                        consumer_flow_uuid = str(downstream.get("@flowUUID") or "").lower()
                        consumer_flow_version = str(downstream.get("@version") or "")
                        consumer_location = str(downstream.get("@location") or "")
                        consumer_flow = self.exact.get(
                            ("flows", consumer_flow_uuid, consumer_flow_version)
                        )
                        if consumer_flow is None or not _process_has_flow(
                            consumer,
                            consumer_flow_uuid,
                            consumer_flow_version,
                            direction="Input",
                            location=consumer_location or None,
                        ):
                            self.report.add(
                                "error",
                                "lifecycle_consumer_flow_mismatch",
                                f"Lifecycle consumer {consumer.uuid} has no matching input {consumer_flow_uuid}@{consumer_flow_version}",
                                dataset=model.identity,
                            )
                            continue
                        if (
                            provider_flow.uuid.lower() != consumer_flow.uuid.lower()
                            and self._unit_for_flow(provider_flow)
                            != self._unit_for_flow(consumer_flow)
                        ):
                            self.report.add(
                                "error",
                                "explicit_provider_unit_mismatch",
                                f"Lifecycle connection links provider unit {self._unit_for_flow(provider_flow)} to consumer unit {self._unit_for_flow(consumer_flow)} without a conversion factor",
                                dataset=model.identity,
                            )
                            continue
                        result[
                            (
                                consumer.uuid.lower(),
                                consumer_flow_uuid,
                                consumer_location,
                            )
                        ].append(provider.uuid.lower())
        return dict(result)

    def _build_methods(self) -> list[BrightwayMethodPayload]:
        methods: list[BrightwayMethodPayload] = []
        seen_names: set[tuple[str, ...]] = set()
        for record in self.package.by_category("lciamethods"):
            root = record.document["LCIAMethodDataSet"]
            info = deep_get(root, "LCIAMethodInformation", "dataSetInformation", default={})
            title = pick_text(info.get("common:name")) or record.uuid
            methodology = str(info.get("methodology") or "TIDAS")
            impact = str(info.get("impactCategory") or title)
            name = ("TIDAS", methodology, impact, title)
            if name in seen_names:
                self.report.add(
                    "error",
                    "duplicate_method_name",
                    f"Multiple LCIA datasets map to the same Brightway method: {name}",
                    dataset=record.identity,
                )
                continue
            seen_names.add(name)
            factors: list[tuple[Any, ...]] = []
            seen_factor_flows: set[str] = set()
            for factor in as_list(deep_get(root, "characterisationFactors", "factor")):
                if not isinstance(factor, Mapping):
                    continue
                flow_uuid = reference_uuid(factor.get("referenceToFlowDataSet"))
                if not flow_uuid:
                    continue
                flow = self.flows.get(flow_uuid.lower())
                if flow is None or _flow_type(flow) != "Elementary flow":
                    self.report.add(
                        "error",
                        "lcia_flow_not_elementary",
                        f"LCIA factor target is not an included elementary flow: {flow_uuid}",
                        dataset=record.identity,
                    )
                    continue
                if flow_uuid.lower() in seen_factor_flows:
                    self.report.add(
                        "error",
                        "duplicate_lcia_factor",
                        f"Multiple factors target the same Brightway flow: {flow_uuid}",
                        dataset=record.identity,
                    )
                    continue
                seen_factor_flows.add(flow_uuid.lower())
                factor_direction = str(factor.get("exchangeDirection"))
                used_directions = self.flow_directions.get(flow_uuid.lower(), set())
                expected_direction = _expected_elementary_direction(flow)
                if len(used_directions) > 1:
                    self.report.add(
                        "error",
                        "ambiguous_lcia_direction",
                        f"Flow {flow_uuid} is used in both directions",
                        dataset=record.identity,
                    )
                    continue
                if used_directions and factor_direction not in used_directions:
                    self.report.add(
                        "error",
                        "lcia_direction_mismatch",
                        f"Factor direction {factor_direction} does not match process exchanges {sorted(used_directions)}",
                        dataset=record.identity,
                    )
                    continue
                if (
                    not used_directions
                    and expected_direction
                    and factor_direction != expected_direction
                ):
                    self.report.add(
                        "error",
                        "lcia_direction_mismatch",
                        f"Factor direction {factor_direction} does not match flow classification {expected_direction}",
                        dataset=record.identity,
                    )
                    continue
                try:
                    decimal_amount = Decimal(str(factor.get("meanValue")))
                    if not decimal_amount.is_finite():
                        raise InvalidOperation
                    amount = float(decimal_amount)
                    if not isfinite(amount) or (decimal_amount != 0 and amount == 0):
                        raise InvalidOperation
                except (InvalidOperation, OverflowError, TypeError, ValueError):
                    self.report.add(
                        "error",
                        "invalid_characterisation_factor",
                        f"Invalid factor for flow {flow_uuid}",
                        dataset=record.identity,
                    )
                    continue
                if Decimal(str(amount)) != decimal_amount:
                    _report_rounding(
                        self.report,
                        code="characterisation_factor_rounded",
                        message_prefix=f"LCIA factor for flow {flow_uuid}",
                        original=decimal_amount,
                        rounded=amount,
                        dataset=record.identity,
                    )
                uncertainty = convert_uncertainty(
                    distribution=factor.get("uncertaintyDistributionType"),
                    rsd95=factor.get("relativeStandardDeviation95In"),
                    minimum=factor.get("minimumValue"),
                    maximum=factor.get("maximumValue"),
                    amount=amount,
                )
                if uncertainty.error:
                    self.report.add(
                        "error",
                        "unmappable_lcia_uncertainty",
                        uncertainty.error,
                        dataset=record.identity,
                    )
                    continue
                for note in uncertainty.notes:
                    self.report.add(
                        "info",
                        "lcia_uncertainty_note",
                        note,
                        dataset=record.identity,
                    )
                if uncertainty.fields:
                    self.report.add(
                        "info",
                        "lcia_uncertainty_mapped",
                        f"Mapped LCIA factor uncertainty for flow {flow_uuid} to Brightway "
                        "fields: "
                        + ", ".join(
                            f"{key}={value!r}" for key, value in sorted(uncertainty.fields.items())
                        ),
                        dataset=record.identity,
                    )
                location = factor.get("location")
                if location:
                    self.report.add(
                        "error",
                        "unsupported_location_specific_lcia",
                        "Location-specific LCIA factors require a regionalised Brightway setup",
                        dataset=record.identity,
                    )
                    continue
                key = (self.biosphere_database, flow_uuid.lower())
                if uncertainty.fields:
                    factors.append((key, amount, dict(uncertainty.fields)))
                else:
                    factors.append((key, amount))
            # The unit chain is authoritative; short descriptions are only labels.
            method_unit = self._method_unit(root, record)
            methods.append(
                BrightwayMethodPayload(
                    name=name,
                    factors=factors,
                    metadata={
                        "unit": method_unit,
                        "tidas_database": self.database,
                        "tidas": {
                            "uuid": record.uuid,
                            "version": record.version,
                            "document": deepcopy(record.document),
                            "brightway_name": list(name),
                            "brightway_unit": method_unit,
                            "factors": deepcopy(factors),
                        },
                    },
                )
            )
        return methods

    def _method_unit(self, root: Mapping[str, Any], record: DatasetRecord) -> str:
        reference = deep_get(
            root,
            "LCIAMethodInformation",
            "quantitativeReference",
            "referenceQuantity",
        )
        property_record = self._record_for_reference("flowproperties", reference)
        if property_record is not None:
            return self._unit_for_property(property_record, owner=record.identity)
        self.report.add(
            "error",
            "lcia_reference_quantity_not_found",
            "Could not resolve the LCIA reference quantity flow property",
            dataset=record.identity,
        )
        return "unit"


def _process_exchanges(record: DatasetRecord) -> list[Mapping[str, Any]]:
    return [
        item
        for item in as_list(deep_get(record.document, "processDataSet", "exchanges", "exchange"))
        if isinstance(item, Mapping)
    ]


def _reference_exchange_ids(record: DatasetRecord) -> set[str]:
    value = deep_get(
        record.document,
        "processDataSet",
        "processInformation",
        "quantitativeReference",
        "referenceToReferenceFlow",
    )
    return {str(item) for item in as_list(value)}


def _process_has_flow(
    process: DatasetRecord,
    flow_uuid: str,
    flow_version: str,
    *,
    direction: str,
    reference_only: bool = False,
    location: str | None = None,
) -> bool:
    reference_ids = _reference_exchange_ids(process)
    for exchange in _process_exchanges(process):
        if reference_only and str(exchange.get("@dataSetInternalID")) not in reference_ids:
            continue
        reference = exchange.get("referenceToFlowDataSet")
        if not isinstance(reference, Mapping):
            continue
        referenced_uuid = reference_uuid(reference)
        if not referenced_uuid or referenced_uuid.lower() != flow_uuid.lower():
            continue
        if str(reference.get("@version") or "") != flow_version:
            continue
        if str(exchange.get("exchangeDirection") or "") != direction:
            continue
        if location and _exchange_location(exchange) != location:
            continue
        return True
    return False


def _exchange_amount(
    exchange: Mapping[str, Any], report: MigrationReport, process: DatasetRecord
) -> float | None:
    value = exchange.get("resultingAmount", exchange.get("meanAmount"))
    try:
        decimal_value = Decimal(str(value))
        if not decimal_value.is_finite():
            raise InvalidOperation
        result = float(decimal_value)
        if not isfinite(result) or (decimal_value != 0 and result == 0):
            raise InvalidOperation
        if Decimal(str(result)) != decimal_value:
            _report_rounding(
                report,
                code="exchange_amount_rounded",
                message_prefix=f"Exchange amount {value!r}",
                original=decimal_value,
                rounded=result,
                dataset=process.identity,
                path=f"exchange:{exchange.get('@dataSetInternalID')}",
            )
        return result
    except (InvalidOperation, OverflowError, TypeError, ValueError):
        report.add(
            "error",
            "invalid_exchange_amount",
            f"Invalid exchange amount {value!r}",
            dataset=process.identity,
            path=f"exchange:{exchange.get('@dataSetInternalID')}",
        )
        return None


def _report_rounding(
    report: MigrationReport,
    *,
    code: str,
    message_prefix: str,
    original: Decimal,
    rounded: float,
    dataset: str | None = None,
    path: str | None = None,
) -> None:
    """Record an automatic float64 rounding with its exact error bounds."""
    stored = Decimal(str(rounded))
    delta = abs(original - stored)
    relative = delta / abs(original) if original != 0 else Decimal(0)
    report.add(
        "info",
        code,
        (
            f"{message_prefix} was rounded automatically from {original} to {rounded} "
            f"to fit float64 storage (absolute difference {delta:.3e}, "
            f"relative difference {relative:.3e}); "
            "the original decimal remains preserved in the TIDAS metadata"
        ),
        dataset=dataset,
        path=path,
    )


def _exchange_location(exchange: Mapping[str, Any]) -> str | None:
    value = exchange.get("location")
    if isinstance(value, Mapping):
        value = value.get("@location")
    location = str(value or "").strip()
    return None if not location or location.upper() == "NULL" else location


def _process_name(record: DatasetRecord) -> str:
    name = deep_get(
        record.document,
        "processDataSet",
        "processInformation",
        "dataSetInformation",
        "name",
        default={},
    )
    base_name = pick_text(name.get("baseName"))
    if base_name:
        return base_name
    parts = [
        pick_text(name.get(field)) for field in ("treatmentStandardsRoutes", "mixAndLocationTypes")
    ]
    return "; ".join(item for item in parts if item) or record.uuid


def _process_location(record: DatasetRecord) -> str:
    return str(
        deep_get(
            record.document,
            "processDataSet",
            "processInformation",
            "geography",
            "locationOfOperationSupplyOrProduction",
            "@location",
            default="GLO",
        )
        or "GLO"
    )


def _flow_name(record: DatasetRecord) -> str:
    name = deep_get(
        record.document,
        "flowDataSet",
        "flowInformation",
        "dataSetInformation",
        "name",
        default={},
    )
    base_name = pick_text(name.get("baseName"))
    if base_name:
        return base_name
    parts = [
        pick_text(name.get(field)) for field in ("treatmentStandardsRoutes", "mixAndLocationTypes")
    ]
    return "; ".join(item for item in parts if item) or record.uuid


def _flow_location(record: DatasetRecord) -> str | None:
    value = deep_get(
        record.document,
        "flowDataSet",
        "flowInformation",
        "geography",
        "locationOfSupply",
    )
    if isinstance(value, Mapping):
        value = value.get("@location") or value.get("#text")
    location = str(value or "").strip()
    return location or None


def _flow_type(record: DatasetRecord) -> str:
    return str(
        deep_get(
            record.document,
            "flowDataSet",
            "modellingAndValidation",
            "LCIMethod",
            "typeOfDataSet",
            default="Other flow",
        )
    )


def _flow_categories(record: DatasetRecord) -> list[str]:
    info = deep_get(
        record.document,
        "flowDataSet",
        "flowInformation",
        "dataSetInformation",
        "classificationInformation",
        default={},
    )
    categories = deep_get(info, "common:elementaryFlowCategorization", "common:category")
    if categories is None:
        categories = deep_get(info, "common:classification", "common:class")
    values = [str(item.get("#text")) for item in as_list(categories) if isinstance(item, Mapping)]
    return [item for item in values if item and item != "None"]


def _expected_elementary_direction(record: DatasetRecord) -> str | None:
    categories = _flow_categories(record)
    if not categories:
        return None
    top = categories[0].lower()
    if top.startswith("resource") or top.startswith("land use"):
        return "Input"
    if top.startswith("emission"):
        return "Output"
    return None
