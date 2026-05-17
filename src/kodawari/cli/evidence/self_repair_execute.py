"""Phase 3: env-gated execution of a workflow self-repair proposal.

A ``.workflow_self_repair.json`` proposal is a *task artifact*. Phase 3
turns one of these into the next kodawari autopilot run that actually
edits the SDK code, runs tests, and goes through the normal planner →
implement → verify → gate pipeline. There is **no** silent-patch path:
any code change goes through review and gate just like any other
autopilot task.

Safety architecture
-------------------

Seven gates must all pass before this module spawns a kodawari autopilot:

  env_gate        — ``WORKFLOW_SELF_REPAIR=1`` (explicit operator opt-in)
  depth_gate      — ``WORKFLOW_SELF_REPAIR_DEPTH < 1`` (no recursion: a
                    self-repair run cannot itself spawn another self-repair)
  status_gate     — proposal.status == "ready" (refuse triage_required /
                    not_applicable / unsupported_workflow_failure)
  confidence_gate — root_cause.confidence >= threshold (default 0.85;
                    override via ``WORKFLOW_SELF_REPAIR_CONFIDENCE_MIN``)
  target_files_gate — repair_task.target_files non-empty after
                    containment+denylist filtering, no rejected entries
  target_files_exist_gate — each target_file exists in the SDK worktree
                    unless explicitly listed in repair_task.new_files
  sdk_root_gate   — ``WORKFLOW_SDK_SELF_REPAIR_ROOT`` resolves to the same
                    path as proposal.kodawari_root (operator must
                    confirm the SDK location explicitly)

If any gate fails the function returns ``status=skipped`` with a reason
and does not spawn anything. Operators can override via env. Default
posture is conservative.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from kodawari.cli.evidence.self_repair import (
    SELF_REPAIR_FILENAME,
    _filter_safe_target_files,
    _find_kodawari_root,
)
from kodawari.infra.io_atomic import atomic_write_canonical_json


SELF_REPAIR_EXECUTION_FILENAME = ".workflow_self_repair_execution.json"
SELF_REPAIR_EXECUTION_SCHEMA_VERSION = "workflow.self_repair.execution.v1"

# Env knobs.
ENV_ENABLED = "WORKFLOW_SELF_REPAIR"
ENV_AUTO_EXECUTE = "WORKFLOW_SELF_REPAIR_AUTO_EXECUTE"
ENV_DEPTH = "WORKFLOW_SELF_REPAIR_DEPTH"
ENV_SDK_ROOT = "WORKFLOW_SDK_SELF_REPAIR_ROOT"
ENV_CONFIDENCE_MIN = "WORKFLOW_SELF_REPAIR_CONFIDENCE_MIN"
ENV_FEATURE_PREFIX = "WORKFLOW_SELF_REPAIR_FEATURE_PREFIX"

DEFAULT_CONFIDENCE_MIN = 0.85
DEFAULT_FEATURE_PREFIX = "meta-repair"
SPAWN_TIMEOUT_SECONDS = 1800
SPAWN_DEFAULT_ENV: dict[str, str] = {
    # Keep the child repair run bounded under the default 1800s spawn budget:
    # 2 planning rounds * (600s planner + 300s reviewer). Operators who need
    # more time should override both these values and the spawn timeout.
    "WORKFLOW_PLANNER_TIMEOUT": "600",
    "WORKFLOW_PLAN_REVIEWER_TIMEOUT": "300",
    "WORKFLOW_PLANNING_MAX_ROUNDS": "2",
}


@dataclass
class GateResult:
    name: str
    passed: bool
    reason: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "reason": self.reason, "detail": dict(self.detail)}


def execute_self_repair_proposal(
    *,
    proposal_path: Path,
    sdk_root: Path | None = None,
    dry_run: bool = False,
    confidence_min: float | None = None,
    kodawari: str | None = None,
    spawn_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Validate the gates and (unless ``dry_run``) spawn a kodawari
    autopilot run targeting the SDK repo. Always returns a structured
    record describing what was checked.

    ``dry_run`` runs all seven gates and returns ``status=dry_run`` with
    the gate report — useful for pre-flight inspection.
    """

    proposal_path = Path(proposal_path).resolve()
    proposal = _load_proposal(proposal_path)
    record = _new_record(proposal_path=proposal_path, proposal=proposal, dry_run=dry_run)
    if proposal is None:
        record["status"] = "blocked"
        record["reason"] = "proposal_unreadable_or_missing"
        return record
    sdk_root_resolved = (sdk_root or _find_kodawari_root()).resolve()
    threshold = confidence_min if confidence_min is not None else _resolved_confidence_threshold()
    if _stamp_gate_results(record, proposal=proposal, sdk_root=sdk_root_resolved, threshold=threshold):
        return record
    if dry_run:
        record["status"] = "dry_run"
        record["reason"] = "all_gates_passed"
        return record
    return _execute_spawn(
        record=record,
        proposal=proposal,
        sdk_root_resolved=sdk_root_resolved,
        kodawari=kodawari,
        spawn_env=spawn_env,
    )


def _stamp_gate_results(
    record: dict[str, Any],
    *,
    proposal: dict[str, Any],
    sdk_root: Path,
    threshold: float,
) -> bool:
    """Run all seven gates, write the results into ``record``, and return
    True if any gate failed (so the caller short-circuits)."""

    gates = _evaluate_gates(proposal=proposal, sdk_root=sdk_root, confidence_min=threshold)
    record["gates"] = [g.to_dict() for g in gates]
    record["confidence_threshold"] = threshold
    failed = [g for g in gates if not g.passed]
    if not failed:
        return False
    record["status"] = "skipped"
    record["reason"] = "gate_failed"
    record["failed_gates"] = [g.name for g in failed]
    return True


def _execute_spawn(
    *,
    record: dict[str, Any],
    proposal: dict[str, Any],
    sdk_root_resolved: Path,
    kodawari: str | None,
    spawn_env: dict[str, str] | None,
) -> dict[str, Any]:
    spawn_record = _spawn_sdk_autopilot(
        proposal=proposal,
        sdk_root=sdk_root_resolved,
        kodawari=kodawari,
        env_overrides=spawn_env or {},
    )
    record["spawn"] = spawn_record
    if spawn_record.get("status") == "ok":
        record["status"] = "executed"
        record["reason"] = "spawn_ok"
    else:
        record["status"] = "blocked"
        record["reason"] = "spawn_failed"
    return record


def write_execution_record(planning_dir: Path, record: dict[str, Any]) -> Path:
    path = Path(planning_dir).resolve() / SELF_REPAIR_EXECUTION_FILENAME
    atomic_write_canonical_json(path, record)
    return path


# --- Gate evaluation ------------------------------------------------------


def _evaluate_gates(
    *,
    proposal: dict[str, Any],
    sdk_root: Path,
    confidence_min: float,
) -> list[GateResult]:
    return [
        _env_gate(),
        _depth_gate(),
        _status_gate(proposal),
        _confidence_gate(proposal, confidence_min=confidence_min),
        _target_files_gate(proposal, sdk_root=sdk_root),
        _target_files_exist_gate(proposal, sdk_root=sdk_root),
        _sdk_root_gate(proposal, sdk_root=sdk_root),
    ]


def _env_gate() -> GateResult:
    raw = str(os.environ.get(ENV_ENABLED, "")).strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return GateResult(name="env_gate", passed=True)
    return GateResult(
        name="env_gate",
        passed=False,
        reason=f"{ENV_ENABLED} not set to 1/true/yes/on",
    )


def _depth_gate() -> GateResult:
    raw = str(os.environ.get(ENV_DEPTH, "0")).strip()
    try:
        depth = int(raw)
    except ValueError:
        depth = 0
    if depth < 1:
        return GateResult(name="depth_gate", passed=True, detail={"current_depth": depth})
    return GateResult(
        name="depth_gate",
        passed=False,
        reason=f"recursion limit reached ({ENV_DEPTH}={depth}, max 0)",
        detail={"current_depth": depth},
    )


def _status_gate(proposal: dict[str, Any]) -> GateResult:
    status = str(proposal.get("status") or "").strip()
    if status == "ready":
        return GateResult(name="status_gate", passed=True, detail={"status": status})
    return GateResult(
        name="status_gate",
        passed=False,
        reason=f"proposal status is {status!r}, only 'ready' is auto-executable",
        detail={"status": status},
    )


def _confidence_gate(proposal: dict[str, Any], *, confidence_min: float) -> GateResult:
    root_cause = proposal.get("root_cause") if isinstance(proposal.get("root_cause"), dict) else {}
    confidence = float(root_cause.get("confidence") or 0.0)
    if confidence >= confidence_min:
        return GateResult(
            name="confidence_gate",
            passed=True,
            detail={"confidence": confidence, "threshold": confidence_min},
        )
    return GateResult(
        name="confidence_gate",
        passed=False,
        reason=f"confidence {confidence} below threshold {confidence_min}",
        detail={"confidence": confidence, "threshold": confidence_min},
    )


def _target_files_gate(proposal: dict[str, Any], *, sdk_root: Path) -> GateResult:
    repair_task = proposal.get("repair_task") if isinstance(proposal.get("repair_task"), dict) else {}
    raw_targets = list(repair_task.get("target_files") or [])
    if not raw_targets:
        return GateResult(name="target_files_gate", passed=False, reason="target_files is empty")
    safe, rejected = _filter_safe_target_files(raw_targets, sdk_root=sdk_root)
    if rejected:
        return GateResult(
            name="target_files_gate",
            passed=False,
            reason=f"{len(rejected)} target_file(s) failed containment/denylist",
            detail={"rejected": rejected, "safe": safe},
        )
    if not safe:
        return GateResult(name="target_files_gate", passed=False, reason="no safe target files")
    return GateResult(
        name="target_files_gate",
        passed=True,
        detail={"safe": safe, "count": len(safe)},
    )


def _target_files_exist_gate(proposal: dict[str, Any], *, sdk_root: Path) -> GateResult:
    repair_task = proposal.get("repair_task") if isinstance(proposal.get("repair_task"), dict) else {}
    raw_targets = list(repair_task.get("target_files") or [])
    safe_targets, rejected_targets = _filter_safe_target_files(raw_targets, sdk_root=sdk_root)
    if rejected_targets or not safe_targets:
        return GateResult(
            name="target_files_exist_gate",
            passed=False,
            reason="target_files must pass containment before existence can be checked",
            detail={"rejected": rejected_targets},
        )
    raw_new_files = list(repair_task.get("new_files") or [])
    safe_new_files, rejected_new_files = _filter_safe_target_files(raw_new_files, sdk_root=sdk_root)
    allowed_new = set(safe_new_files)
    missing = [
        path
        for path in safe_targets
        if path not in allowed_new and not (sdk_root / path).exists()
    ]
    if missing:
        return GateResult(
            name="target_files_exist_gate",
            passed=False,
            reason=f"{len(missing)} target_file(s) do not exist in the SDK worktree",
            detail={
                "missing": missing,
                "allowed_new_files": sorted(allowed_new),
                "rejected_new_files": rejected_new_files,
            },
        )
    return GateResult(
        name="target_files_exist_gate",
        passed=True,
        detail={"checked": safe_targets, "allowed_new_files": sorted(allowed_new)},
    )


def _sdk_root_gate(proposal: dict[str, Any], *, sdk_root: Path) -> GateResult:
    declared = os.environ.get(ENV_SDK_ROOT, "").strip()
    if not declared:
        return GateResult(
            name="sdk_root_gate",
            passed=False,
            reason=f"{ENV_SDK_ROOT} not set; operator must declare SDK root explicitly",
        )
    declared_resolved = Path(declared).resolve()
    if declared_resolved != sdk_root:
        return GateResult(
            name="sdk_root_gate",
            passed=False,
            reason=f"{ENV_SDK_ROOT} ({declared_resolved}) != resolved sdk_root ({sdk_root})",
            detail={"declared": str(declared_resolved), "resolved": str(sdk_root)},
        )
    proposal_root = str(proposal.get("kodawari_root") or "").strip()
    if proposal_root and Path(proposal_root).resolve() != sdk_root:
        return GateResult(
            name="sdk_root_gate",
            passed=False,
            reason=f"proposal.kodawari_root ({proposal_root}) != resolved sdk_root ({sdk_root})",
            detail={"proposal_root": proposal_root, "resolved": str(sdk_root)},
        )
    return GateResult(name="sdk_root_gate", passed=True, detail={"sdk_root": str(sdk_root)})


# --- Spawn ----------------------------------------------------------------


def _spawn_sdk_autopilot(
    *,
    proposal: dict[str, Any],
    sdk_root: Path,
    kodawari: str | None,
    env_overrides: dict[str, str],
) -> dict[str, Any]:
    """Run ``kodawari autopilot`` on the SDK repo with a task_direction
    constructed from the proposal. The spawn never bypasses the normal
    autopilot pipeline — planner, reviewer, executor, verify, and gate
    all run in the spawned process.
    """

    task_direction = _compose_task_direction(proposal)
    feature = _compose_feature_name(proposal)
    cmd = _build_kodawari_command(
        kodawari=kodawari,
        sdk_root=sdk_root,
        feature=feature,
        task_direction=task_direction,
    )
    env = _build_spawn_env(env_overrides=env_overrides)
    started_at = datetime.now(timezone.utc).isoformat()
    try:
        completed = subprocess.run(
            cmd,
            cwd=str(sdk_root),
            env=env,
            capture_output=True,
            text=True,
            timeout=SPAWN_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as exc:
        return {
            "status": "error",
            "reason": "kodawari_not_found",
            "command": cmd,
            "started_at": started_at,
            "error": str(exc),
        }
    except subprocess.TimeoutExpired:
        return {
            "status": "error",
            "reason": "spawn_timeout",
            "command": cmd,
            "started_at": started_at,
            "timeout_seconds": SPAWN_TIMEOUT_SECONDS,
        }
    finished_at = datetime.now(timezone.utc).isoformat()
    status = "ok" if completed.returncode == 0 else "non_zero_exit"
    return {
        "status": status,
        "command": cmd,
        "feature": feature,
        "sdk_root": str(sdk_root),
        "exit_code": completed.returncode,
        "started_at": started_at,
        "finished_at": finished_at,
        "stdout_tail": completed.stdout[-4096:] if completed.stdout else "",
        "stderr_tail": completed.stderr[-4096:] if completed.stderr else "",
    }


def _compose_task_direction(proposal: dict[str, Any]) -> str:
    repair_task = proposal.get("repair_task") if isinstance(proposal.get("repair_task"), dict) else {}
    parts = [
        "[kodawari self-repair task — Phase 3 spawn]",
        str(repair_task.get("task_direction") or "").strip(),
        "",
        "Files allowed for change (already validated for SDK-root containment + denylist):",
    ]
    parts.extend(f"  - {item}" for item in list(repair_task.get("target_files") or []))
    parts.extend(_compose_section("Suggested verify commands:", repair_task.get("suggested_tests")))
    parts.extend(_compose_section("Acceptance criteria:", repair_task.get("acceptance")))
    return "\n".join(parts).strip()


def _compose_section(title: str, items: Any) -> list[str]:
    items_list = list(items or [])
    if not items_list:
        return []
    out = ["", title]
    out.extend(f"  - {item}" for item in items_list)
    return out


def _compose_feature_name(proposal: dict[str, Any]) -> str:
    prefix = str(os.environ.get(ENV_FEATURE_PREFIX, DEFAULT_FEATURE_PREFIX)).strip() or DEFAULT_FEATURE_PREFIX
    root_cause = proposal.get("root_cause") if isinstance(proposal.get("root_cause"), dict) else {}
    code = str(root_cause.get("code") or "unknown").strip().lower().replace(" ", "_")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{prefix}-{code}-{timestamp}"


def _build_kodawari_command(
    *,
    kodawari: str | None,
    sdk_root: Path,
    feature: str,
    task_direction: str,
) -> list[str]:
    command_prefix = _resolve_kodawari_command_prefix(sdk_root, kodawari=kodawari)
    return [
        *command_prefix,
        "autopilot",
        "--project-root",
        str(sdk_root),
        "--feature",
        feature,
        "--task",
        task_direction,
        "--planner-route",
        "model",
        "--gate-profile",
        "blocking",
        "--tier",
        "standard",
    ]


def _resolve_kodawari_command_prefix(sdk_root: Path, *, kodawari: str | None) -> list[str]:
    if kodawari:
        return [kodawari]
    candidates = [
        sdk_root / ".workflow_runtime" / "local-env" / ".venv" / "Scripts" / "kodawari.exe",
        sdk_root / ".workflow_runtime" / "local-env" / ".venv" / "bin" / "kodawari",
        sdk_root / ".venv" / "Scripts" / "kodawari.exe",
        sdk_root / ".venv" / "bin" / "kodawari",
    ]
    for candidate in candidates:
        if candidate.exists():
            return [str(candidate)]
    repo_wrapper = sdk_root / "scripts" / "kodawari.ps1"
    if repo_wrapper.exists():
        powershell = shutil.which("powershell") or shutil.which("pwsh") or "powershell"
        return [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(repo_wrapper)]
    repo_shell_wrapper = sdk_root / "scripts" / "kodawari"
    if repo_shell_wrapper.exists():
        return [str(repo_shell_wrapper)]
    located = shutil.which("kodawari")
    if located:
        return [located]
    return ["kodawari"]


def _build_spawn_env(*, env_overrides: dict[str, str]) -> dict[str, str]:
    """Build the env for the spawned autopilot.

    Critical: bump WORKFLOW_SELF_REPAIR_DEPTH so the spawned autopilot
    cannot itself spawn another self-repair (depth_gate would refuse).
    """
    env = dict(os.environ)
    inherited_pythonpath = env.pop("PYTHONPATH", None)
    current_depth_raw = str(env.get(ENV_DEPTH, "0")).strip()
    try:
        current_depth = int(current_depth_raw)
    except ValueError:
        current_depth = 0
    env[ENV_DEPTH] = str(current_depth + 1)
    for key, value in SPAWN_DEFAULT_ENV.items():
        env.setdefault(key, value)
    env.update(env_overrides)
    if "PYTHONPATH" in env_overrides and inherited_pythonpath is not None:
        env["PYTHONPATH"] = env_overrides["PYTHONPATH"]
    return env


# --- Helpers --------------------------------------------------------------


def _resolved_confidence_threshold() -> float:
    raw = str(os.environ.get(ENV_CONFIDENCE_MIN, "")).strip()
    if not raw:
        return DEFAULT_CONFIDENCE_MIN
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_CONFIDENCE_MIN
    return max(0.0, min(1.0, value))


def _load_proposal(proposal_path: Path) -> dict[str, Any] | None:
    if proposal_path.is_dir():
        proposal_path = proposal_path / SELF_REPAIR_FILENAME
    try:
        payload = json.loads(proposal_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _new_record(*, proposal_path: Path, proposal: dict[str, Any] | None, dry_run: bool) -> dict[str, Any]:
    return {
        "schema_version": SELF_REPAIR_EXECUTION_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "proposal_path": str(proposal_path),
        "dry_run": bool(dry_run),
        "proposal_status": str((proposal or {}).get("status") or "missing"),
        "proposal_root_cause": dict((proposal or {}).get("root_cause") or {}),
        "gates": [],
        "status": "pending",
        "reason": "",
    }


__all__ = [
    "DEFAULT_CONFIDENCE_MIN",
    "ENV_CONFIDENCE_MIN",
    "ENV_AUTO_EXECUTE",
    "ENV_DEPTH",
    "ENV_ENABLED",
    "ENV_FEATURE_PREFIX",
    "ENV_SDK_ROOT",
    "GateResult",
    "SELF_REPAIR_EXECUTION_FILENAME",
    "SELF_REPAIR_EXECUTION_SCHEMA_VERSION",
    "SPAWN_TIMEOUT_SECONDS",
    "execute_self_repair_proposal",
    "write_execution_record",
]
