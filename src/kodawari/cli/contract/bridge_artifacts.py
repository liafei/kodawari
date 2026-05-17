"""Contract-first artifact IO helpers for autopilot bridge modules."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from kodawari.cli.contract.bridge_types import AutopilotPlanningBridgeError
from kodawari.cli.contract.contract_first_schema import (
    ContractFirstSchemaValidationError,
    load_contract_first_artifact,
    validate_contract_first_payload,
    write_contract_first_artifact,
)
from kodawari.cli.io_atomic import CorruptArtifactError


def load_optional_contract_json(path: Path, *, schema_name: str) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return load_contract_first_artifact(path, schema_name=schema_name)
    except (ContractFirstSchemaValidationError, CorruptArtifactError, ValueError) as exc:
        raise AutopilotPlanningBridgeError(
            error_code="planning_artifact_invalid",
            message=str(exc),
            remediation=[f"Fix or regenerate the invalid planning artifact: {path.name}."],
            details={"artifact": path.name, "schema_name": schema_name},
        ) from exc


def write_contract_artifact(path: Path, payload: dict[str, Any], *, schema_name: str) -> None:
    try:
        validate_contract_first_payload(schema_name, payload)
        write_contract_first_artifact(path, payload, schema_name=schema_name)
    except (ContractFirstSchemaValidationError, CorruptArtifactError, ValueError) as exc:
        raise AutopilotPlanningBridgeError(
            error_code="planning_artifact_write_failed",
            message=str(exc),
            remediation=[f"Fix the {schema_name} payload and rerun autopilot."],
            details={"artifact": path.name, "schema_name": schema_name},
        ) from exc
