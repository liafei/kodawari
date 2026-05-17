from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class PRD:
    source_path: str
    clauses: list["Clause"] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Clause:
    id: str
    title: str
    content: str
    epic: str
    priority: str
    spec_types: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Spec:
    spec_id: str
    spec_version: str
    prd_clause: str
    epic: str
    priority: str
    spec_types: list[str] = field(default_factory=list)
    algorithm: list[dict[str, Any]] = field(default_factory=list)
    data_structure: list[dict[str, Any]] = field(default_factory=list)
    api_contract: list[dict[str, Any]] = field(default_factory=list)
    acceptance_tests: list[dict[str, Any]] = field(default_factory=list)
    dependencies: list[dict[str, Any]] = field(default_factory=list)
    risks: list[dict[str, Any]] = field(default_factory=list)
    assumptions: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class CoverageMatrixItem:
    prd_clause: str
    epic: str
    priority: str
    status: str
    spec_id: str | None = None
    test_ids: list[str] = field(default_factory=list)
    blocking_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class CoverageMatrix:
    items: list[CoverageMatrixItem] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"items": [item.to_dict() for item in self.items]}


@dataclass(slots=True)
class ValidationMessage:
    level: str
    message: str
    field: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ValidationResult:
    valid: bool
    errors: list[ValidationMessage] = field(default_factory=list)
    warnings: list[ValidationMessage] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "errors": [item.to_dict() for item in self.errors],
            "warnings": [item.to_dict() for item in self.warnings],
        }


@dataclass(slots=True)
class SectionFlags:
    has_algorithm: bool
    has_data_structure: bool
    has_api_contract: bool
    has_ui: bool
    confidence: float = 1.0

    def spec_types(self) -> list[str]:
        types: list[str] = []
        if self.has_algorithm:
            types.append("algorithm")
        if self.has_data_structure:
            types.append("data")
        if self.has_api_contract:
            types.append("api")
        if self.has_ui:
            types.append("ui")
        if not types:
            types.append("unknown")
        return types
