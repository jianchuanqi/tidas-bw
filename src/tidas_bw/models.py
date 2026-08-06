"""Small, serialisable models shared by readers and writers."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

Direction = Literal["tidas-to-brightway", "brightway-to-tidas", "validate"]


@dataclass(slots=True)
class Issue:
    severity: Literal["error", "warning", "info"]
    code: str
    message: str
    dataset: str | None = None
    path: str | None = None


@dataclass(slots=True)
class DatasetRecord:
    category: str
    root_key: str
    uuid: str
    version: str
    document: dict[str, Any]
    source_path: str
    license_type: str | None = None

    @property
    def identity(self) -> str:
        return f"{self.category}:{self.uuid}@{self.version}"


@dataclass(slots=True)
class MigrationReport:
    direction: Direction
    source: str
    target: str | None = None
    counts: dict[str, int] = field(default_factory=dict)
    issues: list[Issue] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not any(issue.severity == "error" for issue in self.issues)

    def add(
        self,
        severity: Literal["error", "warning", "info"],
        code: str,
        message: str,
        *,
        dataset: str | None = None,
        path: str | Path | None = None,
    ) -> None:
        self.issues.append(
            Issue(
                severity=severity,
                code=code,
                message=message,
                dataset=dataset,
                path=str(path) if path is not None else None,
            )
        )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["ok"] = self.ok
        return payload


@dataclass(slots=True)
class BrightwayMethodPayload:
    name: tuple[str, ...]
    factors: list[tuple[Any, ...]]
    metadata: dict[str, Any]


@dataclass(slots=True)
class BrightwayPayload:
    technosphere: dict[tuple[str, str], dict[str, Any]]
    biosphere: dict[tuple[str, str], dict[str, Any]]
    methods: list[BrightwayMethodPayload]
    database_metadata: dict[str, Any]
    report: MigrationReport
