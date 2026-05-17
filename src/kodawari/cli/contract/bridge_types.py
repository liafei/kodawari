"""Shared types for the autopilot contract bridge."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


class AutopilotPlanningBridgeError(ValueError):
    """Raised when autopilot cannot materialize contract-first planning truth."""

    def __init__(
        self,
        *,
        error_code: str,
        message: str,
        remediation: list[str] | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = str(error_code).strip() or "planning_bridge_failed"
        self.remediation = list(remediation or [])
        self.details = dict(details or {})


@dataclass(frozen=True)
class AutopilotPlanningSnapshot:
    planning_mode: str
    archetype: str
    capabilities: tuple[str, ...]
    primary_task_id: str
    task_label: str
    task_scope: str
    task_card_path: Path
    prd_path: Path | None
    steps_run: tuple[str, ...]
    artifacts: dict[str, str]
    planning_status: str = ""
    planning_approval_decision: str = ""
    planning_approval_reason: str = ""
    planning_approval_active_scope_decision: str = ""
    input_fingerprint: str = ""
    task_direction: str = ""
    stage_profile: str = "epic_plan"
    selection_action: str = ""
    selection_reason: str = ""
    planning_source_status: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "planning_mode": self.planning_mode,
            "archetype": self.archetype,
            "capabilities": list(self.capabilities),
            "primary_task_id": self.primary_task_id,
            "task_label": self.task_label,
            "task_scope": self.task_scope,
            "task_card_path": str(self.task_card_path),
            "prd_path": str(self.prd_path) if self.prd_path is not None else "",
            "steps_run": list(self.steps_run),
            "artifacts": dict(self.artifacts),
            "planning_status": self.planning_status,
            "planning_approval_decision": self.planning_approval_decision,
            "planning_approval_reason": self.planning_approval_reason,
            "planning_approval_active_scope_decision": self.planning_approval_active_scope_decision,
            "input_fingerprint": self.input_fingerprint,
            "task_direction": self.task_direction,
            "stage_profile": self.stage_profile,
            "selection_action": self.selection_action,
            "selection_reason": self.selection_reason,
            "planning_source_status": self.planning_source_status,
        }


__all__ = [
    "AutopilotPlanningBridgeError",
    "AutopilotPlanningSnapshot",
]
