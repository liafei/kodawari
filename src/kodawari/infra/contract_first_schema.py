"""Runtime schema validation helpers for contract-first artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from kodawari.infra.artifact_versions import load_versioned_artifact, validate_schema_version
from kodawari.infra.io_atomic import atomic_write_json


_SCHEMA_CACHE: dict[str, dict[str, Any]] = {}
_SCHEMA_NAMES = {
    "PRD_INTAKE.json": "prd_intake",
    "REPO_INVENTORY.json": "repo_inventory",
    "ARCHITECTURE_PLAN.json": "architecture_plan",
    "PLANNING_CONVERSATION.json": "planning_conversation",
    "TASK_GRAPH.json": "task_graph",
    "COMPLIANCE_REPORT.json": "compliance_report",
}


class ContractFirstSchemaValidationError(ValueError):
    """Raised when a contract-first artifact does not satisfy its JSON schema."""

    def __init__(self, schema_name: str, errors: list[dict[str, str]]) -> None:
        super().__init__(f"contract-first schema validation failed: {schema_name}")
        self.schema_name = schema_name
        self.errors = errors


def infer_contract_first_schema_name(path: Path) -> str | None:
    name = path.name
    if name.startswith("TASK_CARD_") and name.endswith(".json"):
        return "task_card"
    return _SCHEMA_NAMES.get(name)


def _schema_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "schemas" / "contract_first"


def _load_schema(schema_name: str) -> dict[str, Any]:
    cached = _SCHEMA_CACHE.get(schema_name)
    if cached is not None:
        return cached
    path = _schema_dir() / f"{schema_name}.schema.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"invalid contract-first schema document: {path}")
    _SCHEMA_CACHE[schema_name] = payload
    return payload


def validate_contract_first_payload(schema_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    schema = _load_schema(schema_name)
    validator = Draft202012Validator(schema)
    errors: list[dict[str, str]] = []
    for error in sorted(validator.iter_errors(payload), key=lambda item: list(item.path)):
        field = ".".join(str(part) for part in error.path) or "<root>"
        errors.append({"field": field, "message": error.message})
    if errors:
        raise ContractFirstSchemaValidationError(schema_name=schema_name, errors=errors)
    return payload


def load_contract_first_artifact(path: Path, *, schema_name: str | None = None) -> dict[str, Any]:
    payload = load_versioned_artifact(path)
    resolved_schema_name = str(schema_name or infer_contract_first_schema_name(path) or "").strip()
    if resolved_schema_name:
        validate_contract_first_payload(resolved_schema_name, payload)
    return payload


def write_contract_first_artifact(path: Path, payload: dict[str, Any], *, schema_name: str | None = None) -> None:
    validate_schema_version(path, payload)
    resolved_schema_name = str(schema_name or infer_contract_first_schema_name(path) or "").strip()
    if resolved_schema_name:
        validate_contract_first_payload(resolved_schema_name, payload)
    atomic_write_json(path, payload)
