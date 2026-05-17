"""Helpers for syncing gate artifacts back into runtime state snapshots."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from kodawari.autopilot.core.state import AutopilotState, StopReason
from kodawari.cli.delivery.workflow_chain import (
    bind_effective_gate_result,
    load_workflow_chain_snapshot,
    write_workflow_chain_snapshot,
)

logger = logging.getLogger(__name__)


def sync_gate_side_effects(planning_dir: Path, gate_payload: dict[str, Any]) -> None:
    _sync_workflow_chain(planning_dir, gate_payload)
    _sync_autopilot_state(planning_dir, gate_payload)


def _sync_workflow_chain(planning_dir: Path, gate_payload: dict[str, Any]) -> None:
    chain = load_workflow_chain_snapshot(planning_dir)
    effective_chain = bind_effective_gate_result(chain, gate_payload)
    if not effective_chain or effective_chain == chain:
        return
    write_workflow_chain_snapshot(planning_dir, effective_chain)


def _sync_autopilot_state(planning_dir: Path, gate_payload: dict[str, Any]) -> None:
    state_path = (planning_dir / ".autopilot_state.json").resolve()
    if not state_path.exists():
        return
    try:
        state = AutopilotState.load(state_path)
    except Exception:
        logger.warning("failed to load autopilot state during gate sync", exc_info=True)
        return
    if _gate_passed(gate_payload):
        _mark_gate_passed(state)
    elif _gate_blocked(gate_payload):
        _mark_gate_blocked(state, gate_payload)
    else:
        return
    state.save(state_path)


def _gate_passed(gate_payload: dict[str, Any]) -> bool:
    return str(gate_payload.get("total_status") or "").upper() == "PASS"


def _gate_blocked(gate_payload: dict[str, Any]) -> bool:
    return str(gate_payload.get("total_status") or "").upper() == "BLOCKED"


def _mark_gate_passed(state: AutopilotState) -> None:
    state.mark_completed(StopReason.PASS, "PASS")
    state.last_stage_status = "gate_passed"
    state.last_error = None


def _mark_gate_blocked(state: AutopilotState, gate_payload: dict[str, Any]) -> None:
    blocking_reason = str(gate_payload.get("blocking_reason") or "GATE_BLOCKED").strip()
    if blocking_reason and (not state.error_history or state.error_history[-1] != blocking_reason):
        state.add_error(
            blocking_reason,
            phase="GATE",
            action="kodawari gate",
            category="gate",
        )
    else:
        state.last_error = blocking_reason
    state.mark_completed(StopReason.HARD_ERROR, "BLOCKED")
    state.last_stage_status = "gate_blocked"


