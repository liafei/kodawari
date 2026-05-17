"""Schema-version helpers and migrations for machine artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kodawari.infra.io_atomic import load_json_dict


AUTOPILOT_STATE_SCHEMA_VERSION = "autopilot.state.v2"
AUTOPILOT_STATE_COMPAT_VERSIONS = {"autopilot.state.v1"}
TASK_CARD_SCHEMA_VERSION = "contract_first.task_card.v1"
TASK_CARD_COMPAT_VERSIONS = {"contract_first.task_card.v1.1"}

KNOWN_ARTIFACTS: dict[str, dict[str, Any]] = {
    ".autopilot_state.json": {
        "kind": "autopilot_state",
        "current": AUTOPILOT_STATE_SCHEMA_VERSION,
        "compatible": AUTOPILOT_STATE_COMPAT_VERSIONS,
    },
    ".telemetry_snapshot.json": {
        "kind": "telemetry_snapshot",
        "current": "telemetry.snapshot.v1",
        "compatible": set(),
    },
    ".field_report.json": {
        "kind": "field_report",
        "current": "field.report.v1",
        "compatible": set(),
    },
    ".verify_report.json": {
        "kind": "verify_report",
        "current": "verify.report.v1",
        "compatible": set(),
    },
    ".review_evidence.json": {
        "kind": "review_evidence",
        "current": "review.evidence.v1",
        "compatible": set(),
    },
    ".execution_request.json": {
        "kind": "execution_request",
        "current": "execution.request.v1",
        "compatible": set(),
    },
    ".execution_result.json": {
        "kind": "execution_result",
        "current": "execution.result.v1",
        "compatible": set(),
    },
    ".review_bundle.json": {
        "kind": "review_bundle",
        "current": "review.bundle.v1",
        "compatible": set(),
    },
    "AUTOMATION_EVAL_REPORT.json": {
        "kind": "eval_report",
        "current": "eval.report.v1",
        "compatible": set(),
    },
    "AUTOMATION_EVAL_INPUT_LOCK.json": {
        "kind": "eval_input_lock",
        "current": "eval.input_lock.v1",
        "compatible": set(),
    },
    ".worktree_baseline.json": {
        "kind": "worktree_baseline",
        "current": "worktree.baseline.v1",
        "compatible": set(),
    },
    "PRD_INTAKE.json": {
        "kind": "prd_intake",
        "current": "contract_first.prd_intake.v1",
        "compatible": set(),
    },
    "REPO_INVENTORY.json": {
        "kind": "repo_inventory",
        "current": "contract_first.repo_inventory.v1",
        "compatible": set(),
    },
    "ARCHITECTURE_PLAN.json": {
        "kind": "architecture_plan",
        "current": "contract_first.architecture_plan.v1",
        "compatible": set(),
    },
    "PLANNING_CONVERSATION.json": {
        "kind": "planning_conversation",
        "current": "planning.conversation.v1",
        "compatible": {
            "planning.conversation.compat.prd_intake.v1",
            "planning.conversation.compat.architecture_plan.v1",
        },
    },
    "TASK_GRAPH.json": {
        "kind": "task_graph",
        "current": "contract_first.task_graph.v1",
        "compatible": set(),
    },
    "TASK_CARD_ACTIVE.json": {
        "kind": "task_card",
        "current": TASK_CARD_SCHEMA_VERSION,
        "compatible": TASK_CARD_COMPAT_VERSIONS,
    },
    "COMPLIANCE_REPORT.json": {
        "kind": "compliance_report",
        "current": "contract_first.compliance_report.v1",
        "compatible": set(),
    },
}


class ArtifactSchemaVersionError(ValueError):
    """Raised when a machine artifact is missing or using an unsupported schema version."""

    def __init__(
        self,
        path: Path,
        *,
        artifact_kind: str,
        actual_version: str | None,
        expected_versions: list[str],
        message: str,
    ) -> None:
        self.path = path
        self.artifact_kind = artifact_kind
        self.actual_version = actual_version
        self.expected_versions = list(expected_versions)
        super().__init__(message)


@dataclass(frozen=True)
class MigrationResult:
    artifact_kind: str
    from_version: str
    to_version: str
    changed: bool
    payload: dict[str, Any]


def infer_artifact_spec(path: Path) -> dict[str, Any] | None:
    name = path.name
    if name.startswith("TASK_CARD_") and name.endswith(".json"):
        return {
            "kind": "task_card",
            "current": TASK_CARD_SCHEMA_VERSION,
            "compatible": TASK_CARD_COMPAT_VERSIONS,
        }
    return KNOWN_ARTIFACTS.get(name)


def expected_schema_versions(path: Path) -> list[str]:
    spec = infer_artifact_spec(path)
    if spec is None:
        return []
    current = str(spec.get("current") or "").strip()
    compatible = [str(item) for item in list(spec.get("compatible") or set()) if str(item).strip()]
    return [current, *compatible]


def validate_schema_version(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    spec = infer_artifact_spec(path)
    if spec is None:
        return payload
    artifact_kind = str(spec["kind"])
    current = str(spec["current"])
    compatible = {str(item) for item in set(spec.get("compatible") or set()) if str(item).strip()}
    actual = str(payload.get("schema_version") or "").strip()
    if actual == current or actual in compatible:
        return payload
    expected = [current, *sorted(compatible)]
    if not actual:
        raise ArtifactSchemaVersionError(
            path,
            artifact_kind=artifact_kind,
            actual_version=None,
            expected_versions=expected,
            message=(
                f"{artifact_kind} artifact is missing schema_version: {path}. "
                f"Expected one of {expected}. Run `kodawari migrate-artifacts` before retrying."
            ),
        )
    raise ArtifactSchemaVersionError(
        path,
        artifact_kind=artifact_kind,
        actual_version=actual,
        expected_versions=expected,
        message=(
            f"{artifact_kind} artifact schema_version '{actual}' is unsupported for {path}. "
            f"Expected one of {expected}. Run `kodawari migrate-artifacts` before retrying."
        ),
    )


def load_versioned_artifact(path: Path) -> dict[str, Any]:
    payload = load_json_dict(path, required=True, quarantine_on_error=True)
    if payload is None:
        raise ValueError(f"required file not found: {path}")
    return validate_schema_version(path, payload)


def _autopilot_state_migration(
    *,
    artifact_kind: str,
    current: str,
    actual: str,
    migrated: dict[str, Any],
) -> MigrationResult:
    changed = False
    from_version = actual or "legacy.unversioned"
    if migrated.get("revision") is None:
        migrated["revision"] = 0
        changed = True
    if migrated.get("schema_version") != current:
        migrated["schema_version"] = current
        changed = True
    return MigrationResult(
        artifact_kind=artifact_kind,
        from_version=from_version,
        to_version=current,
        changed=changed,
        payload=migrated,
    )


def _legacy_unversioned_migration(
    *,
    artifact_kind: str,
    current: str,
    migrated: dict[str, Any],
) -> MigrationResult:
    migrated["schema_version"] = current
    return MigrationResult(
        artifact_kind=artifact_kind,
        from_version="legacy.unversioned",
        to_version=current,
        changed=True,
        payload=migrated,
    )


def _stable_migration_result(
    *,
    artifact_kind: str,
    current: str,
    actual: str,
    changed: bool,
    migrated: dict[str, Any],
) -> MigrationResult:
    return MigrationResult(
        artifact_kind=artifact_kind,
        from_version=actual,
        to_version=current,
        changed=changed,
        payload=migrated,
    )


def migrate_payload_for_path(path: Path, payload: dict[str, Any]) -> MigrationResult | None:
    spec = infer_artifact_spec(path)
    if spec is None:
        return None
    artifact_kind = str(spec["kind"])
    current = str(spec["current"])
    actual = str(payload.get("schema_version") or "").strip()
    migrated = dict(payload)

    if artifact_kind == "autopilot_state":
        return _autopilot_state_migration(
            artifact_kind=artifact_kind,
            current=current,
            actual=actual,
            migrated=migrated,
        )

    if not actual:
        return _legacy_unversioned_migration(
            artifact_kind=artifact_kind,
            current=current,
            migrated=migrated,
        )

    if actual != current:
        return _stable_migration_result(
            artifact_kind=artifact_kind,
            current=current,
            actual=actual,
            changed=False,
            migrated=migrated,
        )

    return _stable_migration_result(
        artifact_kind=artifact_kind,
        current=current,
        actual=actual,
        changed=False,
        migrated=migrated,
    )
