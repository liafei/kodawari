"""Shared mutating-command preflight and output normalization."""

from __future__ import annotations

from importlib.util import find_spec
from pathlib import Path
from typing import Any

from kodawari.cli.provenance import build_cli_provenance


def build_mutating_preflight(
    *,
    command: str,
    project_root: Path,
    planning_dir: Path | None,
    module_file: Path,
    required_modules: list[str] | None = None,
    require_existing_planning_dir: bool = False,
) -> dict[str, Any]:
    checks: list[dict[str, str]] = []

    if project_root.exists() and project_root.is_dir():
        checks.append(
            {
                "name": "project_root",
                "status": "PASS",
                "details": f"project_root={project_root}",
            }
        )
    else:
        checks.append(
            {
                "name": "project_root",
                "status": "FAIL",
                "details": f"project_root not found: {project_root}",
            }
        )

    if planning_dir is not None:
        if planning_dir.exists():
            checks.append(
                {
                    "name": "planning_dir",
                    "status": "PASS",
                    "details": f"planning_dir={planning_dir}",
                }
            )
        elif require_existing_planning_dir:
            checks.append(
                {
                    "name": "planning_dir",
                    "status": "FAIL",
                    "details": f"planning_dir not found: {planning_dir}",
                }
            )
        else:
            checks.append(
                {
                    "name": "planning_dir",
                    "status": "PASS",
                    "details": f"planning_dir will be materialized on demand: {planning_dir}",
                }
            )

    for module_name in list(required_modules or []):
        if find_spec(module_name) is None:
            checks.append(
                {
                    "name": f"dependency:{module_name}",
                    "status": "FAIL",
                    "details": f"Python dependency unavailable: {module_name}",
                }
            )
            continue
        checks.append(
            {
                "name": f"dependency:{module_name}",
                "status": "PASS",
                "details": f"Python dependency available: {module_name}",
            }
        )

    failures = [item for item in checks if item["status"] == "FAIL"]
    blocking_reason = failures[0]["details"] if failures else ""
    remediation: list[str] = []
    if failures:
        remediation.append("Fix the failing preflight checks before mutating workflow artifacts.")
        if any(item["name"] == "project_root" for item in failures):
            remediation.append("Pass a valid --project-root that points to the target repository.")
        if any(item["name"] == "planning_dir" for item in failures):
            remediation.append("Create the expected planning directory or pass the correct --feature/--planning-dir.")
        missing_modules = [
            item["name"].split(":", 1)[1]
            for item in failures
            if item["name"].startswith("dependency:")
        ]
        if missing_modules:
            remediation.append(
                f"Install missing Python dependencies before rerunning {command}: {', '.join(missing_modules)}."
            )
    status = "BLOCKED" if failures else "PASS"
    return {
        "command": command,
        "status": status,
        "checks": checks,
        "blocking_reason": blocking_reason,
        "next_action": "" if not failures else f"Resolve preflight failures, then rerun `kodawari {command}`.",
        "remediation": remediation,
        "provenance": build_cli_provenance(
            command=command,
            project_root=project_root,
            planning_dir=planning_dir,
            module_file=module_file,
        ),
    }


def build_error_payload(
    *,
    command: str,
    project_root: Path,
    planning_dir: Path | None,
    module_file: Path,
    error: str,
    error_code: str,
    blocking_reason: str | None = None,
    remediation: list[str] | None = None,
    next_action: str | None = None,
    resolved_planning_dirs: list[Path] | None = None,
    preflight: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": "ERROR",
        "error": str(error),
        "error_code": str(error_code),
        "blocking_reason": str(blocking_reason or error),
        "remediation": list(remediation or []),
        "next_action": str(next_action or ""),
        "provenance": build_cli_provenance(
            command=command,
            project_root=project_root,
            planning_dir=planning_dir,
            resolved_planning_dirs=resolved_planning_dirs,
            module_file=module_file,
        ),
    }
    if preflight is not None:
        payload["preflight"] = preflight
    if extra:
        payload.update(extra)
    return payload


def normalize_mutating_payload(
    payload: dict[str, Any],
    *,
    default_next_action: str = "",
) -> dict[str, Any]:
    normalized = dict(payload)
    status = str(normalized.get("status") or "").upper()
    blocking_reason = str(normalized.get("blocking_reason") or "").strip()
    if not blocking_reason and status in {"BLOCKED", "FAIL", "ERROR"}:
        blocking_reason = (
            str(normalized.get("error") or "").strip()
            or str(normalized.get("reason") or "").strip()
            or str(normalized.get("summary") or "").strip()
        )
    normalized["blocking_reason"] = blocking_reason
    remediation = normalized.get("remediation")
    if not isinstance(remediation, list):
        remediation = [] if remediation in {None, ""} else [str(remediation)]
    normalized["remediation"] = [str(item) for item in remediation if str(item).strip()]
    next_action = str(normalized.get("next_action") or "").strip() or default_next_action
    normalized["next_action"] = next_action
    return normalized
