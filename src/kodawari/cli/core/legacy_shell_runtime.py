"""Legacy command-family runtime shim helpers.

REMOVE_AFTER: 2026-08-01
REMOVAL_PLAN: Delete after legacy_cmds.py is removed; no direct external API surface.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from kodawari.cli.core.legacy_runtime_invocation import legacy_deprecation_payload
from kodawari.cli.delivery.workflow_chain import bind_effective_gate_result


def build_legacy_command_replacements(
    *,
    command: str,
    project_root: Path,
    feature: str,
    requirements_file: str | None,
) -> dict[str, Any]:
    requirements_arg = f" --requirements-file {requirements_file}" if requirements_file else ""
    autopilot_cmd = (
        f".\\scripts\\kodawari.ps1 autopilot --project-root {project_root} "
        f"--feature {feature}{requirements_arg}"
    )
    status_cmd = f".\\scripts\\kodawari.ps1 status --project-root {project_root} --feature {feature}"
    gate_cmd = f".\\scripts\\kodawari.ps1 gate --project-root {project_root} --feature {feature} --profile advisory"
    stability_cmd = f".\\scripts\\kodawari.ps1 stability-report --project-root {project_root} --run-id {feature}"

    guidance_map: dict[str, str] = {
        "research": "Use spec/analyzer inputs first, then run autopilot for implementation execution.",
        "develop": "Use autopilot as the canonical merged develop entry.",
        "quick-develop": "Use autopilot with reduced cycles for quick iteration.",
        "optimize-existing-develop": "Run autopilot against the target feature, then run advisory gate/status.",
    }

    command_map: dict[str, list[str]] = {
        "research": [
            f".\\scripts\\kodawari.ps1 spec generate --prd <PRD.md> --output specs --priority P0",
            autopilot_cmd,
            gate_cmd,
            status_cmd,
        ],
        "develop": [autopilot_cmd, gate_cmd, status_cmd],
        "quick-develop": [
            autopilot_cmd + " --max-cycles 3",
            gate_cmd,
            status_cmd,
        ],
        "optimize-existing-develop": [autopilot_cmd, gate_cmd, stability_cmd, status_cmd],
    }

    return {
        "primary": autopilot_cmd,
        "guide": guidance_map.get(command, "Use kodawari canonical entrypoints."),
        "commands": command_map.get(command, [autopilot_cmd, status_cmd]),
    }


def _step_payload(steps: list[dict[str, Any]], name: str) -> dict[str, Any]:
    for step in steps:
        if str(step.get("name")) != name:
            continue
        payload = step.get("payload")
        if isinstance(payload, dict):
            return payload
    return {}


def _unified_status_from_status_step(steps: list[dict[str, Any]]) -> dict[str, Any]:
    status_payload = _step_payload(steps, "status")
    state = status_payload.get("state")
    if not isinstance(state, dict):
        return {}
    unified = state.get("unified_status")
    return dict(unified) if isinstance(unified, dict) else {}


def _executed_and_skipped_steps(steps: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    executed = [str(step.get("name")) for step in steps if not bool(step.get("skipped"))]
    skipped = [str(step.get("name")) for step in steps if bool(step.get("skipped"))]
    return executed, skipped


def _workflow_chain_from_steps(steps: list[dict[str, Any]]) -> dict[str, Any]:
    status_payload = _step_payload(steps, "status")
    status_chain = status_payload.get("workflow_chain")
    if isinstance(status_chain, dict):
        return dict(status_chain)
    autopilot_payload = _step_payload(steps, "autopilot")
    chain = autopilot_payload.get("workflow_chain")
    gate_payload = _step_payload(steps, "gate")
    return bind_effective_gate_result(
        chain if isinstance(chain, dict) else None,
        gate_payload if isinstance(gate_payload, dict) else None,
    )


def _compat_workflow_chain_mode(command: str, workflow_chain: dict[str, Any]) -> dict[str, Any]:
    if command not in {"research", "develop", "optimize-existing-develop"}:
        return workflow_chain
    if not workflow_chain:
        return workflow_chain
    normalized = dict(workflow_chain)
    normalized["mode"] = "peer_review"
    return normalized


def _workflow_chain_final_outcome(workflow_chain: dict[str, Any]) -> dict[str, Any]:
    outcome = workflow_chain.get("final_outcome")
    return dict(outcome) if isinstance(outcome, dict) else {}


def _workflow_chain_chain_final_outcome(workflow_chain: dict[str, Any]) -> dict[str, Any]:
    outcome = workflow_chain.get("chain_final_outcome")
    if isinstance(outcome, dict):
        return dict(outcome)
    return _workflow_chain_final_outcome(workflow_chain)


def _gate_total_status(steps: list[dict[str, Any]]) -> str:
    gate_payload = _step_payload(steps, "gate")
    return str(gate_payload.get("total_status") or "").upper()


def _unified_status_is_terminal(unified_status: dict[str, Any]) -> bool:
    return bool(unified_status.get("is_terminal"))


def _awaiting_gate_final_outcome(
    unified_status: dict[str, Any],
    workflow_chain: dict[str, Any],
) -> dict[str, Any]:
    if _unified_status_is_terminal(unified_status):
        return {}
    current_phase = str(unified_status.get("current_phase") or "").upper()
    chain_outcome = _workflow_chain_final_outcome(workflow_chain)
    if current_phase != "GATE" or str(chain_outcome.get("status") or "").upper() != "PASS":
        return {}
    return {
        "status": "READY_FOR_GATE",
        "reason": "AWAITING_ADVISORY_GATE",
        "blocking_reason": "",
    }


def _effective_flow_final_outcome(
    steps: list[dict[str, Any]],
    workflow_chain: dict[str, Any],
) -> dict[str, Any]:
    del steps
    chain_outcome = _workflow_chain_final_outcome(workflow_chain)
    return chain_outcome


def _legacy_flow_payload(
    *,
    steps: list[dict[str, Any]],
    terminal_rc: int,
    workflow_chain: dict[str, Any],
) -> dict[str, Any]:
    autopilot_payload = _step_payload(steps, "autopilot")
    gate_payload = _step_payload(steps, "gate")
    executed_steps, skipped_steps = _executed_and_skipped_steps(steps)
    chain_final_outcome = _workflow_chain_chain_final_outcome(workflow_chain)
    return {
        "terminal_rc": int(terminal_rc),
        "executed_steps": executed_steps,
        "skipped_steps": skipped_steps,
        "autopilot_run_reason": str(autopilot_payload.get("run_reason") or ""),
        "gate_total_status": str(gate_payload.get("total_status") or ""),
        "unified_status": _unified_status_from_status_step(steps),
        "final_outcome": _effective_flow_final_outcome(steps, workflow_chain),
        "chain_final_outcome": chain_final_outcome,
    }


def build_legacy_runtime_payload(
    *,
    contract_version: str,
    command: str,
    project_root: Path,
    feature: str,
    requirements_file: str | None,
    max_cycles: int,
    terminal_rc: int,
    steps: list[dict[str, Any]],
    runtime_reason: str,
    unsupported_reason_legacy: str,
    provenance: dict[str, Any],
) -> dict[str, Any]:
    workflow_chain = _compat_workflow_chain_mode(command, _workflow_chain_from_steps(steps))
    return {
        "contract_version": contract_version,
        "command": command,
        "entrypoint": f"kodawari {command}",
        "compatibility": {
            "status": "COMPAT_RUNTIME_SHIM",
            "reason": runtime_reason,
            "unsupported_reason_legacy": unsupported_reason_legacy,
        },
        "project_root": str(project_root),
        "feature": feature,
        "requirements_file": requirements_file,
        "max_cycles": int(max_cycles),
        "deprecation": legacy_deprecation_payload(
            entrypoint=f"kodawari {command}",
            replacement=f".\\scripts\\kodawari.ps1 autopilot --project-root {project_root} --feature {feature}",
        ),
        "flow": _legacy_flow_payload(
            steps=steps,
            terminal_rc=terminal_rc,
            workflow_chain=workflow_chain,
        ),
        "steps": steps,
        "workflow_chain": workflow_chain,
        "canonical_replacement": build_legacy_command_replacements(
            command=command,
            project_root=project_root,
            feature=feature,
            requirements_file=requirements_file,
        ),
        "provenance": provenance,
    }

