"""Verify workflow helpers."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from kodawari.autopilot.core.runtime_checks import build_verify_check
from kodawari.autopilot.core.verify_surfaces import (
    VerifySurfacePlanningError,
    build_verify_surface_plan,
    execute_verify_surface_plan,
)
from kodawari.autopilot.execution.verify_targeting import resolve_verify_targeting
from kodawari.cli.evidence.artifact_truth import (
    resolve_authoritative_changed_files,
    resolve_review_artifact_truth,
)
from kodawari.cli.evidence.changed_files_truth import git_worktree_changed_files, load_worktree_baseline
from kodawari.cli.delivery.delivery_common import (
    VERIFY_REPORT_FILENAME,
    _git_diff_files,
    _load_contract_task_card,
    _load_json_dict,
    _load_verify_report,
    _normalize_relpath,
    _review_result_payload,
    _task_run_payload,
    _utc_now_iso,
)
from kodawari.cli.evidence.verify_report import (
    VerifyReportSchemaValidationError,
    build_verify_report_payload,
    write_verify_report_artifact,
)


logger = logging.getLogger(__name__)


def _classify_verify_input_confidence(changed_source: str) -> str:
    normalized = str(changed_source or "").strip().lower()
    if normalized in {"cli_override", "cli_override:existing", "cli_override:raw"}:
        return "explicit"
    if normalized.startswith("execution_result") or normalized.startswith(".execution_result.json"):
        return "curated"
    if normalized.startswith("review_result") or normalized.startswith(".review_result.json"):
        return "curated"
    if normalized.startswith("task_run_result") or normalized.startswith(".task_run_result.json"):
        return "curated"
    return "fallback"


def _review_result_changed_files(review_payload: dict[str, Any] | None) -> list[str]:
    changed = dict((review_payload or {}).get("changed_files") or {})
    return [_normalize_relpath(item) for item in list(changed.get("items") or []) if _normalize_relpath(item)]


def _baseline_delta_changed_files(*, project_root: Path, planning_dir: Path) -> list[str]:
    baseline = load_worktree_baseline(planning_dir)
    if baseline is None:
        return []
    baseline_dirty = {
        _normalize_relpath(item).lower()
        for item in list(baseline.get("dirty_files") or [])
        if _normalize_relpath(item)
    }
    current_dirty = git_worktree_changed_files(project_root)
    return [item for item in current_dirty if item.lower() not in baseline_dirty]


def _resolve_verify_changed_files(
    *,
    project_root: Path,
    planning_dir: Path,
    base_branch: str,
    state_payload: dict[str, Any] | None,
    changed_files_override: list[str] | None,
) -> tuple[list[str], str]:
    override = [_normalize_relpath(item) for item in list(changed_files_override or []) if _normalize_relpath(item)]
    if override:
        return sorted(dict.fromkeys(override)), "cli_override"
    authoritative = resolve_authoritative_changed_files(
        project_root=project_root,
        planning_dir=planning_dir,
        state_payload=state_payload,
    )
    review_truth = resolve_review_artifact_truth(
        project_root=project_root,
        planning_dir=planning_dir,
        authoritative_changed_files=authoritative,
    )
    if bool(review_truth.get("usable")):
        changed = _review_result_changed_files(_review_result_payload(planning_dir) or {})
        if changed:
            return changed, ".review_result.json.changed_files"
    if list(authoritative.get("items") or []):
        return list(authoritative.get("items") or []), str(authoritative.get("source") or "none")
    changed = _task_run_changed_files(planning_dir)
    if changed:
        return changed, ".task_run_result.json.task_delta_changed_files"
    changed = _state_changed_files(state_payload)
    if changed:
        return changed, ".autopilot_state.json.changed_files"
    changed = _baseline_delta_changed_files(project_root=project_root, planning_dir=planning_dir)
    if changed:
        return changed, "baseline_delta:git_worktree"
    changed = _git_diff_files(project_root=project_root, base_branch=base_branch)
    if changed:
        return changed, "git_diff"
    return [], "none"


def _task_run_changed_files(planning_dir: Path) -> list[str]:
    task_run = _task_run_payload(planning_dir) or {}
    values = task_run.get("task_delta_changed_files") or task_run.get("changed_files") or []
    return sorted(
        dict.fromkeys(
            _normalize_relpath(item)
            for item in list(values)
            if _normalize_relpath(item)
        )
    )


def _state_changed_files(state_payload: dict[str, Any] | None) -> list[str]:
    return sorted(
        dict.fromkeys(
            _normalize_relpath(item)
            for item in list((state_payload or {}).get("changed_files") or [])
            if _normalize_relpath(item)
        )
    )


def _execution_result_payload(planning_dir: Path) -> dict[str, Any]:
    payload = _load_json_dict(planning_dir / ".execution_result.json")
    return dict(payload or {}) if isinstance(payload, dict) else {}


def _path_set(values: list[str]) -> set[str]:
    return {_normalize_relpath(item).lower() for item in list(values or []) if _normalize_relpath(item)}


def _verify_passed(payload: dict[str, Any]) -> bool:
    status = str(payload.get("status") or "").strip().upper()
    if status != "PASS":
        return False
    if "passed" in payload and not bool(payload.get("passed")):
        return False
    return True


def _execution_verify_summary(
    *,
    planning_dir: Path,
    changed_files: list[str],
) -> dict[str, Any] | None:
    execution_result = _execution_result_payload(planning_dir)
    if not execution_result or str(execution_result.get("status") or "").strip().upper() != "PASS":
        return None
    execution_changed = [
        _normalize_relpath(item)
        for item in list(execution_result.get("changed_files") or [])
        if _normalize_relpath(item)
    ]
    if changed_files and execution_changed and _path_set(changed_files) != _path_set(execution_changed):
        return None
    for key in ("verify_summary", "verify_check"):
        candidate = execution_result.get(key)
        if not isinstance(candidate, dict):
            continue
        verify_check = dict(candidate)
        if not _verify_passed(verify_check):
            continue
        if "returncode" in verify_check and verify_check.get("returncode") not in (0, "0"):
            continue
        if not bool(verify_check.get("command_executed")):
            continue
        command = _runtime_verify_command(verify_check)
        if not command or _is_broad_default_verify_command(command):
            continue
        source = f".execution_result.json.{key}"
        verify_check["source"] = source
        verify_check["verify_cmd"] = command
        verify_check["verify_cmd_resolved"] = command
        return verify_check
    return None


def _runtime_verify_command(verify_check: dict[str, Any]) -> str:
    resolved = str(verify_check.get("verify_cmd_resolved") or "").strip()
    if resolved and resolved != "surface_plan":
        return resolved
    command = str(verify_check.get("verify_cmd") or "").strip()
    if command and command != "surface_plan":
        return command
    return ""


def _runtime_verify_command_kind(command: str) -> str:
    return _normalize_requested_command_kind(requested_command=command, kind=None)


def _is_broad_default_verify_command(command: str) -> bool:
    normalized = " ".join(str(command or "").strip().lower().split())
    return normalized in {"pytest", "pytest -q", "python -m pytest", "python -m pytest -q"}


def _execution_verify_payload(
    *,
    planning_dir: Path,
    changed_files: list[str],
) -> dict[str, Any] | None:
    verify_check = _execution_verify_summary(planning_dir=planning_dir, changed_files=changed_files)
    if verify_check is None:
        return None
    evidence_source = str(verify_check.get("source") or ".execution_result.json.verify_summary").strip()
    return {
        "verify_check": verify_check,
        "surface_results": [],
        "surface_summary": {"selection_source": evidence_source},
        "verify_scope_mode": "custom",
    }


def _resolve_requested_verify_command(
    *,
    project_root: Path,
    planning_dir: Path,
    state_payload: dict[str, Any] | None,
    command_file_override: str | None = None,
    override: str | None = None,
) -> tuple[str, str]:
    if str(command_file_override or "").strip() and str(override or "").strip():
        raise ValueError("--command-file and --command cannot be used together")
    if str(command_file_override or "").strip():
        normalized = _normalize_verify_command_file(
            project_root=project_root,
            command_file=command_file_override,
        )
        return normalized, "file"
    requested = str(override or "").strip()
    if requested:
        return requested, "inline"
    from_report = _requested_verify_command_from_report(
        project_root=project_root,
        planning_dir=planning_dir,
    )
    if from_report is not None:
        return from_report
    from_runtime = _requested_verify_command_from_runtime(
        state_payload=state_payload,
        planning_dir=planning_dir,
    )
    if from_runtime is not None:
        return from_runtime
    if _surface_planning_available(planning_dir):
        return "pytest -q", "default"
    return "pytest -q", "default"


def _requested_verify_command_from_report(
    *,
    project_root: Path,
    planning_dir: Path,
) -> tuple[str, str] | None:
    try:
        verify_report = _load_verify_report(planning_dir) or {}
    except VerifyReportSchemaValidationError:
        return None
    if str(verify_report.get("entrypoint") or "").strip() != "kodawari verify":
        return None
    requested_command = str(verify_report.get("requested_command") or "").strip()
    kind = _normalize_requested_command_kind(
        requested_command=requested_command,
        kind=verify_report.get("requested_command_kind"),
    )
    if not requested_command:
        return None
    if kind == "file":
        normalized = _normalize_verify_command_file(
            project_root=project_root,
            command_file=requested_command,
        )
        return normalized, "file"
    return requested_command, kind


def _requested_verify_command_from_runtime(
    *,
    state_payload: dict[str, Any] | None,
    planning_dir: Path,
) -> tuple[str, str] | None:
    execution_verify = _execution_verify_summary(planning_dir=planning_dir, changed_files=[])
    if execution_verify is not None:
        command = _runtime_verify_command(execution_verify)
        if command:
            return command, _runtime_verify_command_kind(command)
    task_run = _task_run_payload(planning_dir) or {}
    task_run_verify = dict(task_run.get("verify_check") or {})
    task_run_command = str(task_run_verify.get("verify_cmd") or "").strip()
    if task_run_command:
        kind = _normalize_requested_command_kind(requested_command=task_run_command, kind=None)
        return task_run_command, kind
    state_command = str((state_payload or {}).get("verify_cmd") or "").strip()
    if not state_command:
        return None
    kind = _normalize_requested_command_kind(requested_command=state_command, kind=None)
    return state_command, kind


def _surface_planning_available(planning_dir: Path) -> bool:
    if (planning_dir / "PLANNING_CONVERSATION.json").exists():
        return True
    if (planning_dir / "ARCHITECTURE_PLAN.json").exists():
        return True
    return (planning_dir / "REPO_INVENTORY.json").exists()


def _normalize_requested_command_kind(*, requested_command: str, kind: Any) -> str:
    normalized = str(kind or "").strip().lower()
    if normalized in {"file", "inline", "default"}:
        return normalized
    return "default" if str(requested_command or "").strip() == "pytest -q" else "inline"


def _normalize_verify_command_file(*, project_root: Path, command_file: str) -> str:
    raw_path = str(command_file or "").strip()
    if not raw_path:
        raise ValueError("verify command file path cannot be empty")
    root = project_root.resolve()
    candidate = Path(raw_path)
    candidate = (root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("--command-file must stay within --project-root") from exc
    if not candidate.exists() or not candidate.is_file():
        raise ValueError(f"verify command file not found: {candidate}")
    return str(relative).replace("\\", "/")


def _task_card_files(task_card: dict[str, Any]) -> list[str]:
    return [
        _normalize_relpath(item)
        for item in list(task_card.get("files_to_change") or [])
        if _normalize_relpath(item)
    ]


def _single_verify_check(
    *,
    project_root: Path,
    feature: str,
    task_label: str,
    requested_command: str,
    changed_files: list[str],
    input_confidence: str,
) -> dict[str, Any]:
    verify_targeting = resolve_verify_targeting(
        project_root=project_root,
        verify_cmd=requested_command,
        changed_files=changed_files,
        feature=feature,
        task_label=task_label,
        instinct_hints=None,
    )
    if _is_weak_fallback(input_confidence=input_confidence, verify_targeting=verify_targeting):
        return _blocked_verify_check(
            requested_command=str(verify_targeting.get("verify_cmd") or requested_command),
            resolved_command=str(verify_targeting.get("verify_cmd_resolved") or requested_command),
            target_source=str(verify_targeting.get("verify_target_source") or "default"),
            targets=list(verify_targeting.get("verify_targets") or []),
            reason=_weak_fallback_reason(),
            source="verify_input_determinism",
        )
    return build_verify_check(
        project_root=project_root,
        feature=feature,
        task_label=task_label,
        verify_cmd=requested_command,
        changed_files=changed_files,
        qa_payload=None,
    )


def _is_weak_fallback(
    *,
    input_confidence: str,
    verify_targeting: dict[str, Any],
) -> bool:
    return (
        input_confidence == "fallback"
        and str(verify_targeting.get("verify_target_source") or "") == "default"
        and str(verify_targeting.get("verify_cmd") or "") == "pytest -q"
    )


def _weak_fallback_reason() -> str:
    return (
        "verify input scope is not deterministic enough; provide --changed-file, run review first, "
        "or pass an explicit --command-file/--command"
    )


def _blocked_verify_check(
    *,
    requested_command: str,
    resolved_command: str,
    target_source: str,
    targets: list[str],
    reason: str,
    source: str,
) -> dict[str, Any]:
    return {
        "status": "BLOCKED",
        "passed": False,
        "mode": "planning_guard",
        "source": source,
        "verify_cmd": requested_command,
        "verify_cmd_resolved": resolved_command,
        "verify_target_source": target_source,
        "verify_targets": list(targets),
        "summary": reason,
        "blocking_reason": reason,
        "command_executed": False,
        "artifacts": [],
        "returncode": None,
        "stdout_excerpt": "",
        "stderr_excerpt": "",
    }


def _verify_execution_payload(
    *,
    planning_dir: Path,
    project_root: Path,
    feature: str,
    task_label: str,
    requested_command: str,
    requested_command_kind: str,
    changed_files: list[str],
    task_card_files: list[str],
    input_confidence: str,
    task_surface: str = "",
) -> dict[str, Any]:
    plan = _verify_surface_plan_payload(
        planning_dir=planning_dir,
        project_root=project_root,
        feature=feature,
        task_label=task_label,
        requested_command=requested_command,
        requested_command_kind=requested_command_kind,
        changed_files=changed_files,
        task_card_files=task_card_files,
        task_surface=task_surface,
    )
    if plan is not None:
        return plan
    return {
        "verify_check": _single_verify_check(
            project_root=project_root,
            feature=feature,
            task_label=task_label,
            requested_command=requested_command,
            changed_files=changed_files,
            input_confidence=input_confidence,
        ),
        "surface_results": [],
        "surface_summary": {},
        "verify_scope_mode": "single_command",
    }


def _verify_surface_plan_payload(
    *,
    planning_dir: Path,
    project_root: Path,
    feature: str,
    task_label: str,
    requested_command: str,
    requested_command_kind: str,
    changed_files: list[str],
    task_card_files: list[str],
    task_surface: str = "",
) -> dict[str, Any] | None:
    try:
        plan = build_verify_surface_plan(
            planning_dir=planning_dir,
            requested_command=requested_command,
            requested_command_kind=requested_command_kind,
            changed_files=changed_files,
            task_card_files=task_card_files,
            task_surface=task_surface,
        )
    except VerifySurfacePlanningError as exc:
        logger.warning("verify surface planning blocked: %s", exc)
        return {
            "verify_check": _blocked_verify_check(
                requested_command=requested_command,
                resolved_command=requested_command,
                target_source=str(exc.error_code),
                targets=[],
                reason=str(exc),
                source=str(exc.error_code),
            ),
            "surface_results": [],
            "surface_summary": {
                "selection_source": str(exc.error_code),
                "required_surfaces": [],
                "available_surfaces": [],
            },
            "verify_scope_mode": "surface_plan",
        }
    if plan is None:
        return None
    surface_results, verify_check = execute_verify_surface_plan(
        project_root=project_root,
        feature=feature,
        task_label=task_label,
        plan=plan,
    )
    return {
        "verify_check": verify_check,
        "surface_results": surface_results,
        "surface_summary": dict(plan.get("surface_summary") or {}),
        "verify_scope_mode": str(plan.get("verify_scope_mode") or "surface_plan"),
    }


def _task_label(feature: str, task_card: dict[str, Any]) -> str:
    task_label = str(task_card.get("task_id") or feature).strip()
    task_name = str(task_card.get("task_name") or "").strip()
    return f"{task_label}: {task_name}" if task_name else task_label


def build_verify_report(
    *,
    project_root: Path,
    planning_dir: Path,
    feature: str,
    base_branch: str = "main",
    changed_files_override: list[str] | None = None,
    verify_command_file: str | None = None,
    verify_command: str | None = None,
) -> dict[str, Any]:
    planning_dir.mkdir(parents=True, exist_ok=True)
    state = _load_json_dict(planning_dir / ".autopilot_state.json")
    changed_files, changed_source = _resolve_verify_changed_files(
        project_root=project_root,
        planning_dir=planning_dir,
        state_payload=state,
        base_branch=base_branch,
        changed_files_override=changed_files_override,
    )
    input_confidence = _classify_verify_input_confidence(changed_source)
    explicit_verify_requested = bool(str(verify_command_file or "").strip() or str(verify_command or "").strip())
    execution_verify_payload = None
    if not explicit_verify_requested:
        execution_verify_payload = _execution_verify_payload(
            planning_dir=planning_dir,
            changed_files=changed_files,
        )
    if execution_verify_payload is not None:
        requested_command = _runtime_verify_command(dict(execution_verify_payload["verify_check"]))
        requested_command_kind = _runtime_verify_command_kind(requested_command)
        verify_payload = execution_verify_payload
        input_confidence = "curated"
    else:
        requested_command, requested_command_kind = _resolve_requested_verify_command(
            project_root=project_root,
            planning_dir=planning_dir,
            state_payload=state,
            command_file_override=verify_command_file,
            override=verify_command,
        )
        task_card = _load_contract_task_card(planning_dir) or {}
        verify_payload = _verify_execution_payload(
            planning_dir=planning_dir,
            project_root=project_root,
            feature=feature,
            task_label=_task_label(feature, task_card),
            requested_command=requested_command,
            requested_command_kind=requested_command_kind,
            changed_files=changed_files,
            task_card_files=_task_card_files(task_card),
            input_confidence=input_confidence,
            task_surface=str(task_card.get("surface") or "").strip(),
        )
    artifact = build_verify_report_payload(
        feature=feature,
        planning_dir=planning_dir,
        verify_check=dict(verify_payload["verify_check"]),
        changed_files=changed_files,
        changed_files_source=changed_source,
        input_confidence=input_confidence,
        requested_command=requested_command,
        requested_command_kind=requested_command_kind,
        entrypoint="kodawari verify",
        surface_results=list(verify_payload.get("surface_results") or []),
        surface_summary=dict(verify_payload.get("surface_summary") or {}),
        verify_scope_mode=str(verify_payload.get("verify_scope_mode") or ""),
    )
    write_verify_report_artifact(planning_dir / VERIFY_REPORT_FILENAME, artifact)
    return _verify_cli_payload(
        planning_dir=planning_dir,
        feature=feature,
        changed_files=changed_files,
        changed_source=changed_source,
        input_confidence=input_confidence,
        requested_command=requested_command,
        requested_command_kind=requested_command_kind,
        verify_payload=verify_payload,
        artifact=artifact,
    )


def _verify_cli_payload(
    *,
    planning_dir: Path,
    feature: str,
    changed_files: list[str],
    changed_source: str,
    input_confidence: str,
    requested_command: str,
    requested_command_kind: str,
    verify_payload: dict[str, Any],
    artifact: dict[str, Any],
) -> dict[str, Any]:
    verify_check = dict(verify_payload["verify_check"])
    return {
        "status": str(verify_check.get("status") or artifact.get("status") or "UNKNOWN").upper(),
        "entrypoint": "kodawari verify",
        "feature": feature,
        "planning_dir": str(planning_dir),
        "artifacts": {VERIFY_REPORT_FILENAME: str((planning_dir / VERIFY_REPORT_FILENAME).resolve())},
        "changed_files": {
            "source": changed_source,
            "items": changed_files,
            "count": len(changed_files),
        },
        "input_confidence": input_confidence,
        "requested_command": requested_command,
        "requested_command_kind": requested_command_kind,
        "verify_check": verify_check,
        "surface_results": list(artifact.get("surface_results") or []),
        "surface_summary": dict(artifact.get("surface_summary") or {}),
        "verify_scope_mode": str(artifact.get("verify_scope_mode") or ""),
        "summary": str(verify_check.get("summary") or ""),
        "generated_at": _utc_now_iso(),
    }


__all__ = ["build_verify_report"]


