"""Telemetry writer for contract-first planning bridge routes."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kodawari.autopilot.core.model_config import load_model_config
from kodawari.cli.contract.bridge_types import AutopilotPlanningSnapshot
from kodawari.cli.io_atomic import append_jsonl_atomic


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_planning_role_models(project_root: Path) -> dict[str, str]:
    try:
        models = load_model_config(project_root)
    except Exception:
        return {}
    role_models: dict[str, str] = {}
    for role_name in ("planner", "plan_reviewer", "impl_reviewer", "executor_recovery"):
        try:
            role = models.get_role(role_name, fallback=False)
        except Exception:
            role = None
        if role is not None and str(role.model or "").strip():
            role_models[role_name] = str(role.model).strip()
    return role_models


def append_planning_telemetry(
    *,
    project_root: Path,
    planning_dir: Path,
    feature: str,
    snapshot: AutopilotPlanningSnapshot,
    route: str,
    force_replan: bool,
) -> None:
    payload = {
        "schema_version": "planning.round_telemetry.v1",
        "timestamp": _utc_now_iso(),
        "feature": str(feature),
        "planning_dir": str(planning_dir),
        "planner_route": str(route),
        "force_replan": bool(force_replan),
        "stage_profile": str(snapshot.stage_profile or ""),
        "selection_action": str(snapshot.selection_action or ""),
        "selection_reason": str(snapshot.selection_reason or ""),
        "planning_source_status": str(snapshot.planning_source_status or ""),
        "primary_task_id": str(snapshot.primary_task_id or ""),
        "planning_status": str(snapshot.planning_status or ""),
        "planning_approval_decision": str(snapshot.planning_approval_decision or ""),
        "planning_approval_active_scope_decision": str(snapshot.planning_approval_active_scope_decision or ""),
        "input_fingerprint": str(snapshot.input_fingerprint or ""),
        "models": safe_planning_role_models(project_root),
        "tokens": None,
        "latency_seconds": None,
        "decision": str(snapshot.planning_approval_decision or snapshot.selection_action or ""),
    }
    try:
        append_jsonl_atomic(planning_dir / ".planning_round_telemetry.jsonl", payload)
    except OSError:
        return


__all__ = [
    "append_planning_telemetry",
    "safe_planning_role_models",
]
