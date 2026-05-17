"""Adoption-friendly setup facade for kodawari."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from kodawari.cli.contract.command_contract import normalize_mutating_payload
from kodawari.cli.contract.contract_first_generic_cmd import run_architecture_plan_command, run_init_command
from kodawari.cli.core.legacy_runtime_invocation import invoke_cli_handler, legacy_step_result
from kodawari.cli.provenance import build_cli_provenance


def _planning_dir(project_root: Path, feature: str, explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).resolve()
    return (project_root / "planning" / feature).resolve()


def _bootstrap_checks(project_root: Path) -> dict[str, dict[str, Any]]:
    wrapper = (project_root / "scripts" / "kodawari.ps1").resolve()
    planning_root = (project_root / "planning").resolve()
    return {
        "project_root_exists": {
            "status": "PASS" if project_root.exists() else "FAIL",
            "details": str(project_root),
        },
        "wrapper_script": {
            "status": "PASS" if wrapper.exists() else "WARN",
            "details": str(wrapper),
        },
        "planning_root": {
            "status": "PASS" if planning_root.exists() else "WARN",
            "details": str(planning_root),
        },
    }


def _architecture_namespace(args: argparse.Namespace, *, feature: str, planning_dir: Path) -> argparse.Namespace:
    return argparse.Namespace(
        project_root=str(Path(args.project_root).resolve()),
        feature=feature,
        prd=getattr(args, "prd", None),
        intake=getattr(args, "intake", None),
        planning_dir=str(planning_dir),
        output=getattr(args, "output", None),
        emit_md=True,
        mode=str(getattr(args, "mode", "existing") or "existing"),
        archetype=str(getattr(args, "archetype", "auto") or "auto"),
        capability=list(getattr(args, "capability", []) or []),
    )


def _init_namespace(args: argparse.Namespace, *, architecture_plan_path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        project_root=str(Path(args.project_root).resolve()),
        architecture_plan=str(architecture_plan_path),
        archetype=str(getattr(args, "archetype", "auto") or "auto"),
        capability=list(getattr(args, "capability", []) or []),
    )


def _resolve_status_and_rc(steps: list[dict[str, Any]]) -> tuple[str, int]:
    for step in steps:
        if int(step.get("rc", 0)) != 0:
            raw = str(dict(step.get("payload") or {}).get("status") or "FAIL").upper()
            if raw == "BLOCKED":
                return "BLOCKED", 2
            return "FAIL", max(int(step.get("rc", 1)), 1)
    return "PASS", 0


def _is_soft_architecture_confidence_warning(payload: dict[str, Any]) -> bool:
    confidence_issues = list(payload.get("confidence_issues") or [])
    has_output = bool(dict(payload.get("artifacts") or {}).get("ARCHITECTURE_PLAN.json"))
    return bool(confidence_issues) and has_output and not str(payload.get("error") or "").strip()


def run_setup_command(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root).resolve()
    feature = str(getattr(args, "feature", "") or "bootstrap").strip() or "bootstrap"
    planning_dir = _planning_dir(project_root, feature, getattr(args, "planning_dir", None))
    planning_dir.mkdir(parents=True, exist_ok=True)

    steps: list[dict[str, Any]] = []
    arch_rc, arch_payload = invoke_cli_handler(
        run_architecture_plan_command,
        _architecture_namespace(args, feature=feature, planning_dir=planning_dir),
    )
    if arch_rc != 0 and _is_soft_architecture_confidence_warning(arch_payload):
        arch_rc = 0
        arch_payload = dict(arch_payload)
        arch_payload["_rc"] = 0
        arch_payload["status"] = "WARN"
        arch_payload["warning_reason"] = "low_confidence_inputs"
    steps.append(legacy_step_result(name="architecture-plan", rc=arch_rc, payload=arch_payload))

    run_init = bool(getattr(args, "run_init", False))
    architecture_plan_path = planning_dir / "ARCHITECTURE_PLAN.json"
    if run_init and arch_rc == 0 and architecture_plan_path.exists():
        init_rc, init_payload = invoke_cli_handler(
            run_init_command,
            _init_namespace(args, architecture_plan_path=architecture_plan_path),
        )
        steps.append(legacy_step_result(name="init", rc=init_rc, payload=init_payload))
    elif run_init:
        steps.append(
            legacy_step_result(
                name="init",
                rc=0,
                skipped=True,
                reason="architecture_plan_missing",
                payload={},
            )
        )

    status, rc = _resolve_status_and_rc(steps)
    artifacts: dict[str, Any] = {}
    for step in steps:
        step_payload = dict(step.get("payload") or {})
        step_artifacts = step_payload.get("artifacts")
        if isinstance(step_artifacts, dict):
            artifacts.update({str(name): str(path) for name, path in step_artifacts.items()})

    payload = normalize_mutating_payload(
        {
            "_rc": rc,
            "status": status,
            "entrypoint": "kodawari setup",
            "feature": feature,
            "planning_dir": str(planning_dir),
            "steps": steps,
            "bootstrap_checks": _bootstrap_checks(project_root),
            "artifacts": artifacts,
            "canonical_replacement": {
                "primary": "kodawari architecture-plan",
                "commands": ["kodawari architecture-plan", "kodawari init"],
            },
            "provenance": build_cli_provenance(
                command="setup",
                project_root=project_root,
                planning_dir=planning_dir,
                module_file=Path(__file__),
            ),
        }
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return int(payload.get("_rc", 0) or 0)


__all__ = ["run_setup_command"]

