"""kodawari doctor commands."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kodawari.autopilot.core.model_doctor import doctor_exit_code, doctor_models
from kodawari.infra.io_atomic import atomic_write_json


_PRD_MIN_BYTES = 100


def run_doctor_models_command(args: Any) -> int:
    report = doctor_models(
        project_root=Path(str(getattr(args, "project_root", ".") or ".")).resolve(),
        offline=bool(getattr(args, "offline", False)),
        probe_tools=bool(getattr(args, "probe_tools", False)),
        smoke=str(getattr(args, "smoke", "") or ""),
        no_cache=bool(getattr(args, "no_cache", False)),
        cache_ttl_seconds=getattr(args, "cache_ttl_seconds", None),
    )
    output = str(getattr(args, "output", "") or "").strip()
    if output:
        atomic_write_json(Path(output), report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return doctor_exit_code(report)


def run_doctor_preflight_command(args: Any) -> int:
    """D2: static configuration checks for first-run users.

    Honors the user-stated constraint: NO network calls. Reviewer auth liveness
    is intentionally NOT probed here — that would couple us to upstream
    instability (gateway 5xx, token expiry) which the user excluded from
    kodawari's scoring responsibility. We only check whether the user has
    set the required env vars, has a writable planning dir, and (if --prd was
    given) a non-trivial PRD file."""
    project_root = Path(str(getattr(args, "project_root", ".") or ".")).resolve()
    feature = str(getattr(args, "feature", "") or "").strip()
    prd_path = getattr(args, "prd", None)
    require_real_review = bool(getattr(args, "require_real_peer_review", False))

    checks = _run_preflight_checks(
        project_root=project_root,
        feature=feature,
        prd_path=Path(prd_path).resolve() if prd_path else None,
        require_real_review=require_real_review,
    )

    blockers = [c for c in checks if c["status"] == "FAIL"]
    warnings_ = [c for c in checks if c["status"] == "WARN"]
    report = {
        "schema_version": "doctor_preflight.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "project_root": str(project_root),
        "feature": feature,
        "status": "FAIL" if blockers else ("WARN" if warnings_ else "PASS"),
        "blockers": len(blockers),
        "warnings": len(warnings_),
        "checks": checks,
    }
    output = str(getattr(args, "output", "") or "").strip()
    if output:
        atomic_write_json(Path(output), report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not blockers else 2


def _run_preflight_checks(
    *,
    project_root: Path,
    feature: str,
    prd_path: Path | None,
    require_real_review: bool,
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    checks.append(_check_project_root_exists(project_root))
    checks.append(_check_planning_dir_writable(project_root, feature))
    checks.append(_check_workflow_runtime_venv(project_root))
    checks.append(_check_workflow_ignore_present(project_root))
    if prd_path is not None:
        checks.append(_check_prd_file(prd_path))
    if require_real_review:
        checks.append(_check_reviewer_env_vars())
    return checks


def _check_project_root_exists(project_root: Path) -> dict[str, Any]:
    if not project_root.exists():
        return _check_result(
            "project_root_exists",
            "FAIL",
            f"project_root does not exist: {project_root}",
            remediation=[f"Create the directory or pass a different --project-root: mkdir -p {project_root}"],
        )
    if not project_root.is_dir():
        return _check_result(
            "project_root_exists",
            "FAIL",
            f"project_root is not a directory: {project_root}",
            remediation=["Pass --project-root pointing at a directory, not a file."],
        )
    return _check_result("project_root_exists", "PASS", str(project_root))


def _check_planning_dir_writable(project_root: Path, feature: str) -> dict[str, Any]:
    if not feature:
        return _check_result(
            "planning_dir_writable",
            "WARN",
            "--feature not given; cannot pre-create planning_dir for this preflight",
            remediation=["Pass --feature so doctor can verify the planning_dir path is writable."],
        )
    planning_dir = project_root / "planning" / feature
    try:
        planning_dir.mkdir(parents=True, exist_ok=True)
        probe = planning_dir / ".doctor_preflight_write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return _check_result(
            "planning_dir_writable",
            "FAIL",
            f"planning_dir not writable: {planning_dir} ({exc})",
            remediation=[f"Check filesystem permissions on {planning_dir.parent}."],
        )
    return _check_result("planning_dir_writable", "PASS", str(planning_dir))


def _check_workflow_runtime_venv(project_root: Path) -> dict[str, Any]:
    candidates = [
        project_root / ".workflow_runtime" / "local-env" / ".venv",
        project_root / ".venv",
    ]
    for path in candidates:
        if path.exists():
            return _check_result("workflow_runtime_venv", "PASS", str(path))
    return _check_result(
        "workflow_runtime_venv",
        "WARN",
        "no workflow runtime venv found; subprocess steps will use the current interpreter",
        remediation=[
            "Run scripts/bootstrap_kodawari.ps1 (Windows) or the platform equivalent",
            "OR ensure subprocess steps inherit the right Python environment.",
        ],
    )


def _check_workflow_ignore_present(project_root: Path) -> dict[str, Any]:
    gitignore = project_root / ".gitignore"
    if not gitignore.exists():
        return _check_result(
            "workflow_ignore_present",
            "WARN",
            ".gitignore missing — workflow runtime artifacts may be staged accidentally",
            remediation=["Add a .gitignore that excludes .workflow/, .workflow_runtime/, planning/.execution_*.json."],
        )
    text = gitignore.read_text(encoding="utf-8", errors="replace")
    missing: list[str] = []
    for marker in (".workflow/", ".workflow_runtime/", "planning/"):
        if marker not in text:
            missing.append(marker)
    if missing:
        return _check_result(
            "workflow_ignore_present",
            "WARN",
            f".gitignore missing entries for workflow runtime: {missing}",
            remediation=[f"Append to .gitignore: {' '.join(missing)}"],
        )
    return _check_result("workflow_ignore_present", "PASS", "gitignore covers workflow runtime")


def _check_prd_file(prd_path: Path) -> dict[str, Any]:
    if not prd_path.exists():
        return _check_result(
            "prd_file",
            "FAIL",
            f"--prd path does not exist: {prd_path}",
            remediation=[f"Write the PRD to {prd_path}, or pass --prd pointing at the correct file."],
        )
    size = prd_path.stat().st_size
    if size < _PRD_MIN_BYTES:
        return _check_result(
            "prd_file",
            "FAIL",
            f"--prd file is too short to be useful ({size} bytes < {_PRD_MIN_BYTES}): {prd_path}",
            remediation=[f"Expand the PRD; planners need at least a few sentences of context to plan."],
        )
    return _check_result("prd_file", "PASS", f"{prd_path} ({size} bytes)")


def _check_reviewer_env_vars() -> dict[str, Any]:
    """D2: when the user opts into real peer review, the reviewer gateway env
    vars must be set. We do NOT call the gateway — that's upstream-instability
    territory and explicitly out of scope."""
    missing: list[str] = []
    for var in ("WORKFLOW_REVIEWER_API_KEY", "WORKFLOW_REVIEWER_BASE_URL"):
        if not str(os.environ.get(var, "") or "").strip():
            missing.append(var)
    if missing:
        return _check_result(
            "reviewer_env_vars",
            "FAIL",
            f"--require-real-peer-review requested but env vars unset: {missing}",
            remediation=[
                f"Set $env:{var} (PowerShell) or export {var}=... (bash)" for var in missing
            ],
        )
    return _check_result("reviewer_env_vars", "PASS", "WORKFLOW_REVIEWER_API_KEY and BASE_URL set")


def _check_result(
    name: str,
    status: str,
    detail: str,
    *,
    remediation: list[str] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"name": name, "status": status, "detail": detail}
    if remediation:
        payload["remediation"] = list(remediation)
    return payload


__all__ = [
    "run_doctor_models_command",
    "run_doctor_preflight_command",
]
