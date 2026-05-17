"""Recovered autopilot engine core for WS-104 with collaboration loop."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from kodawari.autopilot.execution.execution_artifacts import EXECUTION_RESULT_FILENAME
from kodawari.autopilot.engine.engine_context_mixin import EngineContextMixin
from kodawari.autopilot.engine.engine_implementation_mixin import EngineImplementationMixin
from kodawari.autopilot.engine.engine_recovery_mixin import EngineRecoveryMixin
from kodawari.autopilot.engine.engine_review_mixin import EngineReviewMixin
from kodawari.autopilot.engine.engine_session_mixin import EngineSessionMixin
from kodawari.autopilot.recovery.executor_recovery import RECOVERY_CARD_FILENAME
from kodawari.autopilot.engine.engine_support import AutopilotConfig, ExecutionPhase, ExecutionPlan
from kodawari.autopilot.core.phase_guard import load_task_card
from kodawari.autopilot.core.state import AutopilotState


class AutopilotEngine(
    EngineSessionMixin,
    EngineReviewMixin,
    EngineRecoveryMixin,
    EngineImplementationMixin,
    EngineContextMixin,
):
    def __init__(
        self,
        config: AutopilotConfig,
        *,
        requirements_text: str | None = None,
        pattern_registry: Any | None = None,
        adapter: Any | None = None,
        state: AutopilotState | None = None,
    ) -> None:
        self.config = config
        self.requirements_text = self._resolve_requirements_text(
            requirements_text=requirements_text,
            requirements_file=config.requirements_file,
        )
        self.state = state or AutopilotState(feature=config.feature, project_root=config.project_root)
        self.pattern_registry = pattern_registry or self._build_default_pattern_registry()
        self.adapter = adapter or self._build_default_adapter()
        self._contexts: dict[str, Any] = {}
        self._planning_dir = (self.config.project_root / "planning" / self.config.feature).resolve()
        self._planning_conversation_payload = self._load_contract_json(self._planning_dir / "PLANNING_CONVERSATION.json")
        self._prd_intake_payload = self._load_contract_json(self._planning_dir / "PRD_INTAKE.json")
        self._task_graph_payload = self._load_contract_json(self._planning_dir / "TASK_GRAPH.json")
        # Explicit task_card_path: must load successfully — no silent fallback to
        # TASK_CARD_ACTIVE.json (which may belong to a different/older task and
        # silently produce wrong files_to_change for the requested task).
        # Only fall back to ACTIVE when no explicit path was supplied.
        explicit_card_path = self.config.task_card_path
        if explicit_card_path is not None:
            payload = load_task_card(explicit_card_path)
            if payload is None:
                raise FileNotFoundError(
                    f"task_card_path={explicit_card_path} could not be loaded "
                    f"(missing file or invalid JSON). Refusing silent fallback to "
                    f"TASK_CARD_ACTIVE.json — that would run the wrong task."
                )
            self._task_card_payload = payload
        else:
            self._task_card_payload = self._load_contract_json(self._planning_dir / "TASK_CARD_ACTIVE.json")
        self._task_card_payload = self._resume_pending_executor_recovery_card(self._task_card_payload)

    def _resume_pending_executor_recovery_card(self, active_card: dict[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(active_card, dict):
            return active_card
        if self._executor_recovery_attempts_exhausted():
            return active_card
        result = self._load_contract_json(self._planning_dir / EXECUTION_RESULT_FILENAME)
        if not isinstance(result, dict) or str(result.get("status") or "").upper() != "BLOCKED":
            return active_card
        code = str(result.get("error_code") or result.get("reason") or "").strip()
        resume_runtime_error = code == "OPENAI_TOOL_USE_ERROR" and bool(result.get("scratch_root"))
        if code != "EXECUTION_RUN_LOCK_BUSY" and not resume_runtime_error and not self._recoverable_executor_block(code):
            return active_card
        recovery_card_path = self._planning_dir / RECOVERY_CARD_FILENAME
        recovery_card = load_task_card(recovery_card_path)
        if not isinstance(recovery_card, dict):
            return active_card
        active_id = str(active_card.get("task_id") or "").strip().upper()
        recovery_id = str(recovery_card.get("task_id") or "").strip().upper()
        if not active_id or not recovery_id.startswith(active_id):
            return active_card
        recovery = recovery_card.get("recovery")
        if not isinstance(recovery, dict):
            return active_card
        source_action = str(recovery.get("source_action") or "").strip()
        if not source_action:
            return active_card
        if source_action == "narrow_patch_plan":
            if not isinstance(recovery_card.get("patch_plan"), list) or not recovery_card.get("patch_plan"):
                return active_card
        self._attach_resumed_recovery_base_workspace(recovery_card, result)
        return recovery_card

    def _attach_resumed_recovery_base_workspace(self, recovery_card: dict[str, Any], result: dict[str, Any]) -> None:
        recovery = recovery_card.get("recovery")
        if not isinstance(recovery, dict):
            return
        try:
            workspace = self._executor_recovery_source_root(SimpleNamespace(execution_result=dict(result))).resolve()
            scratch_parent = (self.config.project_root / ".workflow" / ".executor_scratch").resolve()
            relative = workspace.relative_to(scratch_parent)
        except (OSError, ValueError):
            recovery.pop("base_workspace_path", None)
            return
        if len(relative.parts) != 2 or relative.parts[-1] != "workspace":
            recovery.pop("base_workspace_path", None)
            return
        if workspace.exists() and workspace.is_dir():
            recovery["base_workspace_path"] = str(workspace)
        else:
            recovery.pop("base_workspace_path", None)

    def _executor_recovery_attempts_exhausted(self) -> bool:
        status = str(getattr(self.state, "last_stage_status", "") or "").strip().lower()
        if status == "executor_recovery_exhausted":
            return True
        last_error = str(getattr(self.state, "last_error", "") or "").strip().lower()
        return "executor recovery attempts exhausted" in last_error


__all__ = ["AutopilotConfig", "AutopilotEngine", "ExecutionPhase", "ExecutionPlan"]

