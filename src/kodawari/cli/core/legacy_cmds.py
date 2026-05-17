"""Legacy compatibility command wrappers for historical kodawari shells.

REMOVE_AFTER: 2026-08-01
REMOVAL_PLAN: Remove once legacy CLI shells are fully retired; verify no external scripts call these.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

from kodawari.autopilot.engine.hook_lifecycle import build_pre_compact_payload
from kodawari.cli.status.absorption_status import absorption_status_snapshot
from kodawari.cli.runtime.autopilot_cmd import run_autopilot_command
from kodawari.cli.gate.gate_cmd import _cmd_gate
from kodawari.cli.io_atomic import atomic_write_json, atomic_write_text
from kodawari.cli.core.legacy_runtime_invocation import (
    invoke_cli_handler,
    legacy_deprecation_payload,
    legacy_autopilot_namespace,
    legacy_gate_namespace,
    legacy_stability_namespace,
    legacy_status_namespace,
    legacy_step_result,
    warn_legacy_entrypoint,
)
from kodawari.cli.core.legacy_shell_runtime import build_legacy_runtime_payload
from kodawari.cli.main_support import (
    LEGACY_RUNTIME_REASON,
    LEGACY_UNSUPPORTED_REASON,
    MERGED_CONTRACT_VERSION,
    _build_cli_provenance,
    _resolve_feature_planning_dir,
    _write_json_output,
)
from kodawari.cli.status.stability_report_cmd import run_stability_report_command
from kodawari.cli.status.status_cmd import _cmd_status


def _resolved_autopilot_command() -> Any:
    main_module = sys.modules.get("kodawari.cli.main")
    override = getattr(main_module, "run_autopilot_command", None) if main_module is not None else None
    if callable(override) and override is not run_autopilot_command:
        return override
    return run_autopilot_command


def _legacy_autopilot_step(args: argparse.Namespace) -> dict[str, Any]:
    command = str(getattr(args, "command", "") or "")
    rc, payload = invoke_cli_handler(
        _resolved_autopilot_command(),
        legacy_autopilot_namespace(args, command=command),
    )
    return legacy_step_result(name="autopilot", rc=rc, payload=payload)


def _legacy_status_step(args: argparse.Namespace) -> dict[str, Any]:
    rc, payload = invoke_cli_handler(_cmd_status, legacy_status_namespace(args))
    return legacy_step_result(name="status", rc=rc, payload=payload)


def _legacy_gate_step(args: argparse.Namespace) -> dict[str, Any]:
    rc, payload = invoke_cli_handler(_cmd_gate, legacy_gate_namespace(args))
    return legacy_step_result(name="gate", rc=rc, payload=payload)


def _legacy_stability_step(args: argparse.Namespace) -> dict[str, Any]:
    rc, payload = invoke_cli_handler(
        run_stability_report_command,
        legacy_stability_namespace(args),
    )
    return legacy_step_result(name="stability-report", rc=rc, payload=payload)


def _legacy_step_names(command: str) -> tuple[str, ...]:
    mapping: dict[str, tuple[str, ...]] = {
        "research": ("autopilot", "gate", "status"),
        "develop": ("autopilot", "gate", "status"),
        "quick-develop": ("autopilot", "gate", "status"),
        "optimize-existing-develop": ("autopilot", "gate", "stability-report", "status"),
    }
    return mapping.get(command, ("autopilot", "status"))


def _legacy_blocking_autopilot(command: str) -> bool:
    return command in {"develop", "quick-develop", "optimize-existing-develop", "research"}


def _legacy_runtime_steps(args: argparse.Namespace) -> list[dict[str, Any]]:
    command = str(args.command)
    steps: list[dict[str, Any]] = []
    step_names = _legacy_step_names(command)
    step_builders: dict[str, Any] = {
        "autopilot": _legacy_autopilot_step,
        "gate": _legacy_gate_step,
        "stability-report": _legacy_stability_step,
        "status": _legacy_status_step,
    }

    autopilot_ok = True
    for name in step_names:
        if name != "autopilot" and not autopilot_ok and name in {"gate", "stability-report"}:
            steps.append(
                legacy_step_result(
                    name=name,
                    rc=0,
                    skipped=True,
                    reason="autopilot_not_successful",
                )
            )
            continue
        step = step_builders[name](args)
        steps.append(step)
        if name == "autopilot":
            autopilot_ok = int(step["rc"]) == 0
    return steps


def _legacy_step_rc(steps: list[dict[str, Any]], name: str) -> int:
    for step in steps:
        if str(step.get("name")) == name:
            return int(step.get("rc", 0))
    return 0


def _legacy_terminal_rc(command: str, steps: list[dict[str, Any]]) -> int:
    autopilot_rc = _legacy_step_rc(steps, "autopilot")
    status_rc = _legacy_step_rc(steps, "status")
    if _legacy_blocking_autopilot(command) and autopilot_rc != 0:
        return autopilot_rc
    if status_rc != 0:
        return status_rc
    return 0


def _compact_artifact_paths(planning_dir: Path) -> dict[str, Path]:
    return {
        "COMPACT_CONTEXT.md": (planning_dir / "COMPACT_CONTEXT.md").resolve(),
        "compact_context.json": (planning_dir / "compact_context.json").resolve(),
    }


def _write_compact_artifacts(planning_dir: Path, payload: dict[str, Any]) -> dict[str, str]:
    paths = _compact_artifact_paths(planning_dir)
    atomic_write_text(paths["COMPACT_CONTEXT.md"], str(payload["compact_markdown"]))
    atomic_write_json(paths["compact_context.json"], dict(payload["compact_json"]))
    return {name: str(path) for name, path in paths.items()}


def _legacy_compact_commands(project_root: Path, feature: str) -> tuple[str, str]:
    status_cmd = f".\\scripts\\kodawari.ps1 status --project-root {project_root} --feature {feature}"
    gate_cmd = f".\\scripts\\kodawari.ps1 gate --project-root {project_root} --feature {feature} --profile advisory"
    return status_cmd, gate_cmd


def _legacy_context_compact_payload(
    payload: dict[str, Any],
    artifacts: dict[str, str],
) -> dict[str, Any]:
    return {
        "requested": True,
        "runtime_triggered": False,
        "status": str(payload.get("compact_status") or "partial"),
        "mode": str(payload.get("compact_mode") or "compat"),
        "entrypoint_scope": "compat_shim_only",
        "artifacts_written": bool(artifacts),
        "merged_absorption_status": dict(payload.get("merged_absorption_status") or {}),
    }


def _legacy_instincts_payload(
    *,
    include_instincts: bool,
    payload: dict[str, Any],
) -> dict[str, Any]:
    return {
        "requested": include_instincts,
        "loaded": bool(payload.get("instincts_loaded", False)),
        "status": str(payload.get("instincts_status") or "placeholder_unloaded"),
        "source": str(payload.get("instincts_source") or "kodawari.instincts"),
        "hints_count": int(payload.get("instinct_hints_count", 0) or 0),
        "store_path": str(payload.get("instincts_store_path") or ""),
    }


def _legacy_compact_preview(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "compact_markdown": payload["compact_markdown"],
        "compact_json": payload["compact_json"],
    }


def _legacy_compact_result(
    *,
    args: argparse.Namespace,
    project_root: Path,
    planning_dir: Path,
    payload: dict[str, Any],
    artifacts: dict[str, str],
) -> dict[str, Any]:
    feature = str(args.feature)
    status_cmd, gate_cmd = _legacy_compact_commands(project_root, feature)
    include_instincts = bool(args.include_instincts)
    result = {
        "contract_version": MERGED_CONTRACT_VERSION,
        "command": "compact",
        "entrypoint": "kodawari compact",
        "compatibility": {
            "status": "COMPAT_SHIM",
            "reason": "Historical compact shell merged as minimal compatibility shim.",
        },
        "project_root": str(project_root),
        "feature": feature,
        "planning_dir": str(planning_dir),
        "include_instincts": include_instincts,
        "log_tail_lines": int(args.log_tail_lines),
    }
    result["deprecation"] = legacy_deprecation_payload(
        entrypoint="kodawari compact",
        replacement=status_cmd,
    )
    result["context_compact"] = _legacy_context_compact_payload(payload, artifacts)
    result["instincts"] = _legacy_instincts_payload(
        include_instincts=include_instincts,
        payload=payload,
    )
    result["absorption_status"] = absorption_status_snapshot()
    result["merged_absorption_status"] = dict(payload.get("merged_absorption_status") or {})
    result["artifacts"] = artifacts
    result["compact_preview"] = _legacy_compact_preview(payload)
    result["canonical_replacement"] = {
        "primary": status_cmd,
        "commands": [status_cmd, gate_cmd],
    }
    result["provenance"] = _build_cli_provenance(
        command="compact",
        project_root=project_root,
        planning_dir=planning_dir,
    )
    return result


def _cmd_legacy_compact(args: argparse.Namespace) -> int:
    warn_legacy_entrypoint(
        entrypoint="kodawari compact",
        replacement="kodawari status",
    )
    project_root = Path(args.project_root).resolve()
    planning_dir = _resolve_feature_planning_dir(
        project_root=project_root,
        feature=args.feature,
        planning_dir=getattr(args, "planning_dir", None),
    )
    planning_dir.mkdir(parents=True, exist_ok=True)
    payload = build_pre_compact_payload(
        project_root=project_root,
        feature=args.feature,
        include_instincts=bool(args.include_instincts),
        log_tail_lines=int(args.log_tail_lines),
    )
    artifacts = _write_compact_artifacts(planning_dir, payload)
    result = _legacy_compact_result(
        args=args,
        project_root=project_root,
        planning_dir=planning_dir,
        payload=payload,
        artifacts=artifacts,
    )
    _write_json_output(getattr(args, "output", None), result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _cmd_legacy_runtime(args: argparse.Namespace) -> int:
    warn_legacy_entrypoint(
        entrypoint=f"kodawari {args.command}",
        replacement="kodawari autopilot",
    )
    project_root = Path(args.project_root).resolve()
    steps = _legacy_runtime_steps(args)
    terminal_rc = _legacy_terminal_rc(str(args.command), steps)
    payload = build_legacy_runtime_payload(
        contract_version=MERGED_CONTRACT_VERSION,
        command=str(args.command),
        project_root=project_root,
        feature=str(args.feature),
        requirements_file=getattr(args, "requirements_file", None),
        max_cycles=int(getattr(args, "max_cycles", 8)),
        terminal_rc=terminal_rc,
        steps=steps,
        runtime_reason=LEGACY_RUNTIME_REASON,
        unsupported_reason_legacy=LEGACY_UNSUPPORTED_REASON,
        provenance=_build_cli_provenance(
            command=str(args.command),
            project_root=project_root,
        ),
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return int(terminal_rc)


__all__ = [
    "_cmd_legacy_compact",
    "_cmd_legacy_runtime",
]


