"""Contract-first CLI command family."""

from __future__ import annotations

import argparse
import json
import uuid
from pathlib import Path
from typing import Any

from kodawari.autopilot.engine import AutopilotConfig, AutopilotEngine
from kodawari.autopilot.execution.execution_backend import execution_backend_choices, self_review_backend_choices
from kodawari.autopilot.execution.execution_artifacts import (
    EXECUTION_REQUEST_FILENAME,
    EXECUTION_RESULT_FILENAME,
)
from kodawari.autopilot.core.phase_guard import dirty_core_guard, scope_guard
from kodawari.autopilot.review.contract import derive_runtime_review_evidence
from kodawari.autopilot.planning.prd_contract import (
    build_prd_intake,
    render_prd_intake_markdown,
    validate_prd_intake,
    write_json,
)
from kodawari.autopilot.planning.task_card import (
    build_task_card,
    render_task_card_markdown,
    validate_task_card,
)
from kodawari.autopilot.planning.task_card_file_preflight import (
    FilePreflightReport,
    run_file_preflight,
)
from kodawari.autopilot.planning.task_graph import (
    build_task_graph,
    render_task_graph_markdown,
    validate_task_graph,
)
from kodawari.cli.contract.command_contract import build_error_payload, normalize_mutating_payload
from kodawari.cli.contract.contract_first_schema import (
    ContractFirstSchemaValidationError,
    load_contract_first_artifact,
    validate_contract_first_payload,
    write_contract_first_artifact,
)
from kodawari.cli.io_atomic import CorruptArtifactError, atomic_write_text, load_json_dict
from kodawari.cli.runtime.task_run_state_sync import sync_task_run_terminal_state
from kodawari.cli.runtime.task_run_manifest import (
    carry_over_files_for_task,
    write_task_run_manifest,
)
from kodawari.cli.artifact_versions import ArtifactSchemaVersionError
from kodawari.cli.evidence.review_evidence_artifact import (
    REVIEW_EVIDENCE_FILENAME,
    ReviewEvidenceSchemaValidationError,
    build_review_evidence_artifact,
    coerce_review_evidence_payload,
    extract_review_evidence_from_compliance_report,
    load_review_evidence_artifact,
    write_review_evidence_artifact,
)
from kodawari.cli.evidence.verify_report import (
    VERIFY_REPORT_FILENAME,
    build_verify_report_payload,
    write_verify_report_artifact,
)
from kodawari.cli.evidence.changed_files_truth import (
    WORKTREE_BASELINE_FILENAME,
    capture_worktree_baseline,
    dedupe_paths,
    resolve_task_delta_changed_files,
)
from kodawari.cli.provenance import build_cli_provenance
from kodawari.gate.checkers import check_scope_drift
from kodawari.gate.checkers import build_contract_compliance_report, discover_project_schema_files


def _emit(payload: dict[str, Any]) -> int:
    normalized = normalize_mutating_payload(payload)
    print(json.dumps(normalized, ensure_ascii=False, indent=2))
    return int(normalized.get("_rc", 0) or 0)


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = load_json_dict(path, required=True)
    except ValueError as exc:
        if "required file not found:" in str(exc):
            raise FileNotFoundError(str(exc)) from exc
        raise
    if not isinstance(payload, dict):
        raise ValueError(f"Expected object JSON: {path}")
    return payload


def _load_contract_json(path: Path, *, schema_name: str | None = None) -> dict[str, Any]:
    try:
        return load_contract_first_artifact(path, schema_name=schema_name)
    except ValueError as exc:
        if "required file not found:" in str(exc):
            raise FileNotFoundError(str(exc)) from exc
        raise


def _write_markdown(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, content)


def _planning_dir(project_root: Path, feature: str, explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).resolve()
    return (project_root / "planning" / feature).resolve()


def _provenance(command: str, *, project_root: Path, planning_dir: Path | None = None) -> dict[str, Any]:
    return build_cli_provenance(
        command=command,
        project_root=project_root,
        planning_dir=planning_dir,
        module_file=Path(__file__),
    )


def _emit_contract_error(
    *,
    command: str,
    project_root: Path,
    planning_dir: Path,
    feature: str,
    error: Exception,
    remediation: list[str],
    extra: dict[str, Any] | None = None,
) -> int:
    error_code = "contract_first_artifact_invalid"
    validation_errors: list[dict[str, str]] = []
    if isinstance(error, ArtifactSchemaVersionError):
        error_code = "artifact_schema_version_invalid"
    elif isinstance(error, ContractFirstSchemaValidationError):
        error_code = "artifact_schema_invalid"
        validation_errors = list(error.errors)
    elif isinstance(error, CorruptArtifactError):
        error_code = "artifact_corrupt"
        if error.quarantine_path is not None:
            remediation = [*remediation, f"Quarantined copy: {error.quarantine_path}"]
    payload = build_error_payload(
        command=command,
        project_root=project_root,
        planning_dir=planning_dir,
        module_file=Path(__file__),
        error=str(error),
        error_code=error_code,
        remediation=remediation,
        next_action=f"Fix the contract-first artifact problem, then rerun `kodawari {command}`.",
        extra={
            "_rc": 2,
            "status": "FAIL",
            "entrypoint": f"kodawari {command}",
            "feature": feature,
            "planning_dir": str(planning_dir),
            **({"validation_errors": validation_errors} if validation_errors else {}),
            **(extra or {}),
        },
    )
    return _emit(payload)


def _load_review_evidence(planning_dir: Path) -> dict[str, Any] | None:
    review_evidence_path = planning_dir / REVIEW_EVIDENCE_FILENAME
    if review_evidence_path.exists():
        return coerce_review_evidence_payload(
            load_review_evidence_artifact(review_evidence_path),
            source=REVIEW_EVIDENCE_FILENAME,
            explicit=True,
        )
    task_run_path = planning_dir / ".task_run_result.json"
    if task_run_path.exists():
        payload = _load_json(task_run_path)
        direct = payload.get("review_evidence")
        if isinstance(direct, dict):
            return coerce_review_evidence_payload(
                direct,
                source=".task_run_result.json.review_evidence",
                explicit=True,
            )
        compliance = extract_review_evidence_from_compliance_report(
            payload.get("compliance_report"),
            source=".task_run_result.json.compliance_report.review_evidence",
            explicit=False,
        )
        if compliance is not None:
            return compliance

    report_path = planning_dir / "COMPLIANCE_REPORT.json"
    if report_path.exists():
        return extract_review_evidence_from_compliance_report(
            _load_json(report_path),
            source="COMPLIANCE_REPORT.json.review_evidence",
            explicit=False,
        )
    return None


def _cmd_prd_intake(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root).resolve()
    feature = str(args.feature).strip()
    planning_dir = _planning_dir(project_root, feature, getattr(args, "planning_dir", None))
    prd_path = Path(args.prd).resolve()
    prd_text = prd_path.read_text(encoding="utf-8")
    payload = build_prd_intake(prd_text, feature=feature)
    issues = [item.to_dict() for item in validate_prd_intake(payload)]
    confidence_issues = [str(item) for item in list(payload.get("confidence_issues") or []) if str(item).strip()]
    output_path = Path(args.output).resolve() if getattr(args, "output", None) else (planning_dir / "PRD_INTAKE.json")
    try:
        validate_contract_first_payload("prd_intake", payload)
        write_contract_first_artifact(output_path, payload, schema_name="prd_intake")
    except (ArtifactSchemaVersionError, ContractFirstSchemaValidationError, CorruptArtifactError, ValueError) as exc:
        return _emit_contract_error(
            command="prd-intake",
            project_root=project_root,
            planning_dir=planning_dir,
            feature=feature,
            error=exc,
            remediation=["Inspect the PRD extraction fields and contract-first schema before rerunning prd-intake."],
            extra={
                "confidence": str(payload.get("confidence") or "high"),
                "confidence_issues": confidence_issues,
                "validation_issues": issues,
            },
        )
    markdown_path: Path | None = None
    if bool(getattr(args, "emit_md", False)):
        markdown_path = output_path.with_suffix(".md")
        _write_markdown(markdown_path, render_prd_intake_markdown(payload))
    has_validation_issues = bool(issues)
    has_confidence_issues = bool(confidence_issues)
    status = "PASS"
    if has_validation_issues:
        status = "FAIL"
    elif has_confidence_issues:
        status = "BLOCKED"
    result = {
        "_rc": 0 if status == "PASS" else 2,
        "status": status,
        "entrypoint": "kodawari prd-intake",
        "feature": feature,
        "planning_dir": str(planning_dir),
        "artifacts": {
            "PRD_INTAKE.json": str(output_path),
            **({"PRD_INTAKE.md": str(markdown_path)} if markdown_path else {}),
        },
        "validation_issues": issues,
        "confidence": str(payload.get("confidence") or "high"),
        "confidence_issues": confidence_issues,
        "provenance": _provenance("prd-intake", project_root=project_root, planning_dir=planning_dir),
        "blocking_reason": "semantic confidence below threshold" if status == "BLOCKED" else "",
        "next_action": "Refine the PRD or run architecture-plan with human review." if status == "BLOCKED" else "",
    }
    return _emit(result)


def _cmd_task_plan(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root).resolve()
    feature = str(args.feature).strip()
    planning_dir = _planning_dir(project_root, feature, getattr(args, "planning_dir", None))
    try:
        intake = _load_contract_json(Path(args.intake).resolve(), schema_name="prd_intake")
    except (FileNotFoundError, ArtifactSchemaVersionError, ContractFirstSchemaValidationError, CorruptArtifactError, ValueError) as exc:
        return _emit_contract_error(
            command="task-plan",
            project_root=project_root,
            planning_dir=planning_dir,
            feature=feature,
            error=exc,
            remediation=["Regenerate or migrate PRD_INTAKE.json before rerunning task-plan."],
        )
    graph = build_task_graph(
        intake,
        project_root=project_root,
        project_profile=str(getattr(args, "project_profile", "auto")),
    )
    errors = validate_task_graph(graph)
    intake_confidence = str(intake.get("confidence") or "high").strip().lower()
    intake_confidence_issues = [str(item) for item in list(intake.get("confidence_issues") or []) if str(item).strip()]
    if intake_confidence == "low":
        errors.extend([f"input PRD intake low confidence: {item}" for item in intake_confidence_issues or ["semantic confidence below threshold"]])
    output_path = Path(args.output).resolve() if getattr(args, "output", None) else (planning_dir / "TASK_GRAPH.json")
    try:
        validate_contract_first_payload("task_graph", graph)
        write_contract_first_artifact(output_path, graph, schema_name="task_graph")
    except (ArtifactSchemaVersionError, ContractFirstSchemaValidationError, CorruptArtifactError, ValueError) as exc:
        return _emit_contract_error(
            command="task-plan",
            project_root=project_root,
            planning_dir=planning_dir,
            feature=feature,
            error=exc,
            remediation=["Fix task graph generation/schema mismatch before rerunning task-plan."],
            extra={
                "input_intake_confidence": intake_confidence,
                "input_confidence_issues": intake_confidence_issues,
                "validation_errors": errors,
            },
        )
    markdown_path: Path | None = None
    if bool(getattr(args, "emit_md", False)):
        markdown_path = output_path.with_suffix(".md")
        _write_markdown(markdown_path, render_task_graph_markdown(graph))
    result = {
        "_rc": 0 if not errors else 2,
        "status": "PASS" if not errors else "FAIL",
        "entrypoint": "kodawari task-plan",
        "feature": feature,
        "planning_dir": str(planning_dir),
        "artifacts": {
            "TASK_GRAPH.json": str(output_path),
            **({"TASK_GRAPH.md": str(markdown_path)} if markdown_path else {}),
        },
        "input_intake_confidence": intake_confidence,
        "input_confidence_issues": intake_confidence_issues,
        "validation_errors": errors,
        "provenance": _provenance("task-plan", project_root=project_root, planning_dir=planning_dir),
    }
    return _emit(result)


def _cmd_task_prepare(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root).resolve()
    feature = str(args.feature).strip()
    planning_dir = _planning_dir(project_root, feature, getattr(args, "planning_dir", None))
    task_id = str(args.task).strip()
    try:
        graph = _load_contract_json(Path(args.graph).resolve(), schema_name="task_graph")
    except (FileNotFoundError, ArtifactSchemaVersionError, ContractFirstSchemaValidationError, CorruptArtifactError, ValueError) as exc:
        return _emit_contract_error(
            command="task-prepare",
            project_root=project_root,
            planning_dir=planning_dir,
            feature=feature,
            error=exc,
            remediation=["Regenerate or migrate TASK_GRAPH.json before rerunning task-prepare."],
            extra={"task_id": task_id},
        )
    try:
        card = build_task_card(graph, task_id)
    except ValueError as exc:
        return _emit(
            {
                "_rc": 2,
                "status": "FAIL",
                "entrypoint": "kodawari task-prepare",
                "feature": feature,
                "task_id": task_id,
                "planning_dir": str(planning_dir),
                "validation_errors": [str(exc)],
                "provenance": _provenance("task-prepare", project_root=project_root, planning_dir=planning_dir),
            }
        )
    errors = validate_task_card(card, planning_mode=str(graph.get("planning_mode") or "existing"))
    default_name = f"TASK_CARD_{card['task_id']}.json"
    output_path = Path(args.output).resolve() if getattr(args, "output", None) else (planning_dir / default_name)
    active_path = planning_dir / "TASK_CARD_ACTIVE.json"
    try:
        validate_contract_first_payload("task_card", card)
        write_contract_first_artifact(output_path, card, schema_name="task_card")
        write_contract_first_artifact(active_path, card, schema_name="task_card")
    except (ArtifactSchemaVersionError, ContractFirstSchemaValidationError, CorruptArtifactError, ValueError) as exc:
        return _emit_contract_error(
            command="task-prepare",
            project_root=project_root,
            planning_dir=planning_dir,
            feature=feature,
            error=exc,
            remediation=["Fix task card generation/schema mismatch before rerunning task-prepare."],
            extra={"task_id": task_id, "validation_errors": errors},
        )
    markdown_path: Path | None = None
    if bool(getattr(args, "emit_md", False)):
        markdown_path = output_path.with_suffix(".md")
        _write_markdown(markdown_path, render_task_card_markdown(card))
    result = {
        "_rc": 0 if not errors else 2,
        "status": "PASS" if not errors else "FAIL",
        "entrypoint": "kodawari task-prepare",
        "feature": feature,
        "task_id": card["task_id"],
        "planning_dir": str(planning_dir),
        "artifacts": {
            "TASK_CARD.json": str(output_path),
            "TASK_CARD_ACTIVE.json": str(active_path),
            **({"TASK_CARD.md": str(markdown_path)} if markdown_path else {}),
        },
        "validation_errors": errors,
        "provenance": _provenance("task-prepare", project_root=project_root, planning_dir=planning_dir),
    }
    return _emit(result)


def _run_task_card(
    args: argparse.Namespace,
    *,
    card: dict[str, Any],
    card_path: Path | None = None,
    run_id: str = "",
) -> dict[str, Any]:
    project_root = Path(args.project_root).resolve()
    feature = str(args.feature).strip()
    planning_dir = _planning_dir(project_root, feature, getattr(args, "planning_dir", None))
    task_id = str(card.get("task_id") or "TASK")
    initial_changed_files = carry_over_files_for_task(planning_dir=planning_dir, task_id=task_id)
    requirements_file = Path(args.requirements_file).resolve() if getattr(args, "requirements_file", None) else None
    requirements_text = requirements_file.read_text(encoding="utf-8") if requirements_file and requirements_file.exists() else ""
    config = AutopilotConfig(
        project_root=project_root,
        feature=feature,
        requirements_file=requirements_file,
        initial_changed_files=initial_changed_files,
        verify_cmd=str(getattr(args, "verify_cmd", "pytest -q")),
        max_cycles=int(getattr(args, "max_cycles", 8)),
        token_budget=int(getattr(args, "token_budget", 300000)),
        executor_backend=str(getattr(args, "executor_backend", "") or ""),
        executor_command=str(getattr(args, "executor_command", "") or ""),
        self_review_backend=str(getattr(args, "self_review_backend", "") or ""),
        self_review_command=str(getattr(args, "self_review_command", "") or ""),
        contract_first_mode=str(getattr(args, "contract_mode", "strict")),
        phase_mode=str(getattr(args, "phase_mode", "implement")),
        strict_scope=bool(getattr(args, "strict_scope", False)),
        task_card_path=card_path if card_path is not None else Path(args.card).resolve(),
        real_peer_review=bool(getattr(args, "real_peer_review", False) or getattr(args, "real_opus_review", False)),
        require_real_peer_review=bool(getattr(args, "require_real_peer_review", False) or getattr(args, "require_real_opus_review", False)),
        opus_reviewer_backend=str(getattr(args, "opus_reviewer_backend", "") or ""),
        executor_model=str(getattr(args, "executor_model", "") or ""),
        reviewer_backend=str(getattr(args, "reviewer_backend", "") or ""),
        reviewer_model=str(getattr(args, "reviewer_model", "") or ""),
        reviewer_api_format=str(getattr(args, "reviewer_api_format", "") or ""),
        reviewer_base_url=str(getattr(args, "reviewer_base_url", "") or ""),
        enforce_dual_review=True,
        peer_review_max_tokens=int(getattr(args, "peer_review_max_tokens", 4096) or 4096),
        rollback_on_failure=bool(getattr(args, "rollback_on_failure", False)),
        max_verify_retries=int(getattr(args, "max_verify_retries", 2) or 2),
    )
    engine = AutopilotEngine(config=config, requirements_text=requirements_text or None)
    # Stamp run_id BEFORE the engine starts running so every ErrorEvent
    # produced during this invocation carries the same identifier. Without
    # this, the run_id ends up empty for in-flight events and only the
    # terminal sync (sync_task_run_terminal_state) knows about it — which
    # means error_history accumulates a stale run's events.
    if run_id:
        engine.state.run_id = run_id
    task_name = str(card.get("task_name") or "Contract-first task").strip()
    task_label = f"{task_id}: {task_name}"
    files = [str(item) for item in list(card.get("files_to_change") or []) if str(item).strip()]
    task_scope = f"files_to_change={files}; test_plan={card.get('test_plan', '')}"
    return engine.run_collaboration_loop(
        task_label=task_label,
        task_scope=task_scope,
        enable_peer_review=not bool(getattr(args, "no_enable_peer_review", False)),
    )


def _task_allowed_files(card: dict[str, Any]) -> list[str]:
    return [str(item) for item in list(card.get("files_to_change") or []) if str(item).strip()]


def _merge_same_task_carry_over(
    *,
    planning_dir: Path,
    task_id: str,
    changed_files: list[str],
    changed_files_source: str,
) -> tuple[list[str], str]:
    carried = carry_over_files_for_task(planning_dir=planning_dir, task_id=task_id)
    if not carried:
        return list(changed_files), changed_files_source
    merged = dedupe_paths([*carried, *changed_files])
    if merged == list(changed_files):
        return list(changed_files), changed_files_source
    carry_source = "task_run_manifest:same_task"
    if not changed_files:
        return merged, carry_source
    source = str(changed_files_source or "unknown").strip() or "unknown"
    return merged, f"{source}+{carry_source}"


def _task_run_scope_guard(
    *,
    card: dict[str, Any],
    changed_files: list[str],
    strict_scope: bool,
    contract_mode: str,
) -> dict[str, Any]:
    allowed_files = _task_allowed_files(card)
    if not allowed_files or not changed_files:
        return {
            "allowed_files": allowed_files,
            "changed_files": list(changed_files),
            "scope_drift": {},
            "guard": {"blocked": False, "status": "PASS", "reason": ""},
        }
    scope_drift_payload = check_scope_drift(changed_files, allowed_files)
    guard = scope_guard(
        changed_files=changed_files,
        task_card=card,
        strict_scope=strict_scope,
        contract_mode=contract_mode,
    ).to_dict()
    return {
        "allowed_files": allowed_files,
        "changed_files": list(changed_files),
        "scope_drift": scope_drift_payload,
        "guard": guard,
    }


def _task_run_preflight(
    *,
    project_root: Path,
    planning_dir: Path,
    feature: str,
    card: dict[str, Any],
    contract_mode: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    baseline = capture_worktree_baseline(
        project_root=project_root,
        planning_dir=planning_dir,
        feature=feature,
        command="task-run",
        mode="fail" if str(contract_mode or "").strip().lower() == "strict" else "warn",
        allowed_files=_task_allowed_files(card),
    )
    # Carry-over: when the previous task-run for THIS SAME task_id already
    # produced changed_files in project_root (sync_isolated_workspace_to_project_root
    # leaves them dirty by design), the next preflight must not block on them.
    # The manifest written at the prior task-run's exit declares which files
    # are expected dirty. Cross-task contamination is intentionally NOT
    # carried over — those still need explicit user resolution.
    task_id = str(card.get("task_id") or "").strip()
    if task_id:
        carried = set(carry_over_files_for_task(planning_dir=planning_dir, task_id=task_id))
        if carried:
            original_core_dirty = list(baseline.get("core_dirty_files") or [])
            filtered = [item for item in original_core_dirty if item not in carried]
            actually_carried = [item for item in original_core_dirty if item in carried]
            if actually_carried:
                baseline["core_dirty_files"] = filtered
                baseline["carried_over_files"] = actually_carried
                # Recompute baseline status: FAIL only when core files remain
                # dirty after subtracting carry-over.
                if str(baseline.get("mode") or "").lower() == "fail" and not filtered:
                    baseline["status"] = "WARN" if baseline.get("dirty_files") else "PASS"
                # Update details so it reflects the post-carry-over reality, not
                # the pre-filter state computed by _baseline_details.
                baseline["details"] = (
                    f"Carried over {len(actually_carried)} file(s) from prior task-run "
                    f"(same task_id={task_id}); core dirty files now empty"
                    if not filtered else
                    f"Carried over {len(actually_carried)} file(s) but {len(filtered)} core dirty remain: {filtered}"
                )
    guard = dirty_core_guard(
        core_dirty_files=list(baseline.get("core_dirty_files") or []),
        contract_mode=contract_mode,
    ).to_dict()
    return baseline, guard


_PREFLIGHT_KIND_TO_REASON: dict[str, str] = {
    "missing_source": "TASK_CARD_MISSING_SOURCE",
    "new_file_already_exists": "TASK_CARD_NEW_FILE_EXISTS",
    "invalid_verify_cmd": "TASK_CARD_INVALID_VERIFY_CMD",
    "large_file_requires_target_symbols": "LARGE_FILE_TASK_REQUIRES_TARGET_SYMBOLS",
    "symbol_not_found": "TASK_CARD_SYMBOL_NOT_FOUND",
    "stale_task_card": "TASK_CARD_STALE",
    "unauthorized_mutation": "TASK_CARD_UNAUTHORIZED_MUTATION",
    "new_files_not_subset": "TASK_CARD_INVALID_NEW_FILES",
    "path_outside_project_root": "TASK_CARD_INVALID_PATH",
}


def _task_run_preflight_reason(issue_kind: str) -> str:
    normalized = str(issue_kind or "").strip().lower()
    return _PREFLIGHT_KIND_TO_REASON.get(normalized, "TASK_CARD_FILE_PREFLIGHT_FAILED")


def _task_run_preflight_next_action(reason: str) -> str:
    normalized = str(reason or "").strip().upper()
    if normalized == "TASK_CARD_MISSING_SOURCE":
        return "Fix files_to_change typo or list the file under new_files if executor should create it."
    if normalized == "TASK_CARD_NEW_FILE_EXISTS":
        return "Remove existing paths from new_files or rename the target before rerunning task-run."
    if normalized == "TASK_CARD_INVALID_VERIFY_CMD":
        return "Provide a non-empty verify_cmd scoped to project_root, then rerun task-run."
    if normalized == "LARGE_FILE_TASK_REQUIRES_TARGET_SYMBOLS":
        return "Add target_symbols for large files, or run deep mode with explicit user acknowledgement."
    if normalized == "TASK_CARD_SYMBOL_NOT_FOUND":
        return "Update target_symbols/read_only_symbols to existing definitions and rerun task-run."
    if normalized == "TASK_CARD_STALE":
        return "Regenerate task card to refresh freshness hashes/symbol fingerprints for current HEAD."
    if normalized == "TASK_CARD_UNAUTHORIZED_MUTATION":
        return "Align allowed_test_mutations with behavior_changes and allowed test files in the task card."
    if normalized in {"TASK_CARD_INVALID_NEW_FILES", "TASK_CARD_INVALID_PATH"}:
        return "Use repository-relative paths and ensure new_files is a subset of files_to_change."
    return (
        "Fix the task card preflight issues and rerun task-run. "
        "Review preflight_issues for exact file-level failures."
    )


def _task_run_allows_preexisting_new_files(args: argparse.Namespace, card: dict[str, Any]) -> bool:
    backend = str(getattr(args, "executor_backend", "") or "").strip()
    if backend != "openai_tool_use":
        return False
    if not [str(item).strip() for item in list(card.get("new_files") or []) if str(item).strip()]:
        return False
    return bool(str(card.get("verify_cmd") or "").strip())


def _task_run_exit_code(result: dict[str, Any], *, strict_scope: bool) -> int:
    reason = str(result.get("reason") or "").upper()
    if reason in {
        "PHASE_GUARD_BLOCKED",
        "OPUS_REVIEW_BLOCKED",
        "SELF_REVIEW_BLOCKED",
        "EXECUTION_BACKEND_BLOCKED",
        "DIRTY_WORKTREE_BLOCKED",
        "MAX_CYCLES_REACHED",
        "MAX_CYCLES",
        "COLLABORATION_ROUND_LIMIT",
    }:
        return 2
    if strict_scope:
        for round_item in list(result.get("rounds") or []):
            if not isinstance(round_item, dict):
                continue
            details = dict(round_item.get("details") or {})
            scope = dict(details.get("scope_drift") or {})
            guard = dict(scope.get("guard") or {})
            if bool(guard.get("blocked")):
                return 2
    compliance = dict(result.get("compliance_report") or {})
    if str(compliance.get("status") or "").upper() == "FAIL":
        return 2
    return 0


def _task_run_next_action(
    *,
    reason: str,
    phase_mode: str,
    strict_scope: bool,
    task_id: str,
) -> str:
    normalized_reason = str(reason or "").strip().upper()
    normalized_phase = str(phase_mode or "").strip().lower()
    if normalized_reason == "MAX_CYCLES_REACHED":
        if normalized_phase == "analyze":
            return (
                "Analyze phase reached max cycles. Increase --max-cycles for deeper analysis "
                "or switch to --phase-mode implement for code execution."
            )
        return "Increase --max-cycles or split the task into smaller task cards before rerun."
    if normalized_reason == "PHASE_GUARD_BLOCKED":
        return "Phase guard blocked implementation. Verify TASK_CARD and rerun with --phase-mode implement."
    if normalized_reason == "EXECUTION_BACKEND_BLOCKED":
        return "Provide a real executor backend/command or materialize a valid .execution_result.json artifact, then rerun task-run."
    if normalized_reason == "SELF_REVIEW_BLOCKED":
        return "Configure the self-review path through the executor backend or use a test-only noop backend under pytest, then rerun task-run."
    if normalized_reason == "TASK_CARD_MISSING":
        return "Generate task card first with `kodawari task-prepare` and rerun task-run."
    if normalized_reason == "TASK_CARD_INVALID":
        return "Fix TASK_CARD schema/required fields and rerun task-run."
    if normalized_reason in {
        "TASK_CARD_MISSING_SOURCE",
        "TASK_CARD_NEW_FILE_EXISTS",
        "TASK_CARD_INVALID_VERIFY_CMD",
        "LARGE_FILE_TASK_REQUIRES_TARGET_SYMBOLS",
        "TASK_CARD_SYMBOL_NOT_FOUND",
        "TASK_CARD_STALE",
        "TASK_CARD_UNAUTHORIZED_MUTATION",
        "TASK_CARD_INVALID_NEW_FILES",
        "TASK_CARD_INVALID_PATH",
        "TASK_CARD_FILE_PREFLIGHT_FAILED",
    }:
        return _task_run_preflight_next_action(normalized_reason)
    if normalized_reason == "DIRTY_WORKTREE_BLOCKED":
        return "Clean or isolate pre-existing dirty core files before rerunning task-run."
    if strict_scope:
        return f"Review scope drift for {task_id} and keep changes inside files_to_change."
    return "Inspect .task_run_result.json for detailed round history and retry."


def _cmd_task_run(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root).resolve()
    feature = str(args.feature).strip()
    planning_dir = _planning_dir(project_root, feature, getattr(args, "planning_dir", None))
    # Fresh run_id per invocation. The terminal-state sync uses this to detect
    # when the existing .autopilot_state.json belongs to a stale prior run and
    # must reset session-scoped fields (error_history etc.) before writing
    # this run's outcome. See task_run_state_sync.sync_task_run_terminal_state.
    task_run_run_id = uuid.uuid4().hex
    card_path = Path(args.card).resolve()
    if not card_path.exists() and not Path(args.card).is_absolute():
        # Convenience fallback: accept bare task IDs like "T1" → TASK_CARD_T1.json
        candidate = planning_dir / f"TASK_CARD_{args.card}.json"
        if candidate.exists():
            card_path = candidate
    try:
        card = _load_contract_json(card_path, schema_name="task_card")
    except FileNotFoundError:
        return _emit(
            {
                "_rc": 2,
                "status": "FAIL",
                "entrypoint": "kodawari task-run",
                "feature": feature,
                "planning_dir": str(planning_dir),
                "task_id": "",
                "strict_scope": bool(getattr(args, "strict_scope", False)),
                "reason": "TASK_CARD_MISSING",
                "error": f"Task card not found: {card_path}",
                "next_action": _task_run_next_action(
                    reason="TASK_CARD_MISSING",
                    phase_mode=str(getattr(args, "phase_mode", "implement")),
                    strict_scope=bool(getattr(args, "strict_scope", False)),
                    task_id="",
                ),
                "artifacts": {},
                "scope_summary": {"allowed_files": [], "changed_files": []},
                "provenance": _provenance("task-run", project_root=project_root, planning_dir=planning_dir),
            }
        )
    except (ArtifactSchemaVersionError, ContractFirstSchemaValidationError, CorruptArtifactError, json.JSONDecodeError, ValueError) as exc:
        error_code = "artifact_schema_invalid" if isinstance(exc, ContractFirstSchemaValidationError) else "artifact_schema_version_invalid" if isinstance(exc, ArtifactSchemaVersionError) else "artifact_corrupt" if isinstance(exc, CorruptArtifactError) else "task_card_invalid"
        return _emit(
            {
                "_rc": 2,
                "status": "FAIL",
                "entrypoint": "kodawari task-run",
                "feature": feature,
                "planning_dir": str(planning_dir),
                "task_id": "",
                "strict_scope": bool(getattr(args, "strict_scope", False)),
                "reason": "TASK_CARD_INVALID",
                "error": str(exc),
                "error_code": error_code,
                **({"validation_errors": list(exc.errors)} if isinstance(exc, ContractFirstSchemaValidationError) else {}),
                "next_action": _task_run_next_action(
                    reason="TASK_CARD_INVALID",
                    phase_mode=str(getattr(args, "phase_mode", "implement")),
                    strict_scope=bool(getattr(args, "strict_scope", False)),
                    task_id="",
                ),
                "artifacts": {},
                "scope_summary": {"allowed_files": [], "changed_files": []},
                "provenance": _provenance("task-run", project_root=project_root, planning_dir=planning_dir),
            }
        )
    try:
        baseline, dirty_guard = _task_run_preflight(
            project_root=project_root,
            planning_dir=planning_dir,
            feature=feature,
            card=card,
            contract_mode=str(getattr(args, "contract_mode", "strict")),
        )
    except ValueError as exc:
        return _emit(
            {
                "_rc": 2,
                "status": "FAIL",
                "entrypoint": "kodawari task-run",
                "feature": feature,
                "planning_dir": str(planning_dir),
                "task_id": str(card.get("task_id") or ""),
                "strict_scope": bool(getattr(args, "strict_scope", False)),
                "reason": "WORKTREE_BASELINE_INVALID",
                "error": str(exc),
                "next_action": "Fix the worktree baseline artifact/schema and rerun task-run.",
                "artifacts": {WORKTREE_BASELINE_FILENAME: str((planning_dir / WORKTREE_BASELINE_FILENAME).resolve())},
                "scope_summary": {"allowed_files": _task_allowed_files(card), "changed_files": []},
                "provenance": _provenance("task-run", project_root=project_root, planning_dir=planning_dir),
            }
        )
    if bool(dirty_guard.get("blocked")):
        return _emit(
            {
                "_rc": 2,
                "status": "FAIL",
                "entrypoint": "kodawari task-run",
                "feature": feature,
                "planning_dir": str(planning_dir),
                "task_id": str(card.get("task_id") or ""),
                "strict_scope": bool(getattr(args, "strict_scope", False)),
                "reason": "DIRTY_WORKTREE_BLOCKED",
                "error": str(dirty_guard.get("reason") or "pre-existing dirty core files detected"),
                "next_action": _task_run_next_action(
                    reason="DIRTY_WORKTREE_BLOCKED",
                    phase_mode=str(getattr(args, "phase_mode", "implement")),
                    strict_scope=bool(getattr(args, "strict_scope", False)),
                    task_id=str(card.get("task_id") or ""),
                ),
                "artifacts": {WORKTREE_BASELINE_FILENAME: str((planning_dir / WORKTREE_BASELINE_FILENAME).resolve())},
                "scope_summary": {"allowed_files": _task_allowed_files(card), "changed_files": []},
                "worktree_preflight": baseline,
                "dirty_core_guard": dirty_guard,
                "provenance": _provenance("task-run", project_root=project_root, planning_dir=planning_dir),
            }
        )
    file_preflight = run_file_preflight(
        card,
        project_root,
        allow_existing_new_files=_task_run_allows_preexisting_new_files(args, card),
    )
    if file_preflight.blocked:
        issues_payload = [issue.to_dict() for issue in file_preflight.issues]
        warnings_payload = [warning.to_dict() for warning in file_preflight.warnings]
        first_issue = file_preflight.issues[0]
        preflight_reason = _task_run_preflight_reason(first_issue.kind)
        error_summary = f"{first_issue.kind}: {first_issue.path}"
        if first_issue.possible_matches:
            error_summary += f" (possible matches: {', '.join(first_issue.possible_matches)})"
        return _emit(
            {
                "_rc": 2,
                "status": "FAIL",
                "entrypoint": "kodawari task-run",
                "feature": feature,
                "planning_dir": str(planning_dir),
                "task_id": str(card.get("task_id") or ""),
                "strict_scope": bool(getattr(args, "strict_scope", False)),
                "reason": preflight_reason,
                "error": error_summary,
                "next_action": _task_run_preflight_next_action(preflight_reason),
                "artifacts": {WORKTREE_BASELINE_FILENAME: str((planning_dir / WORKTREE_BASELINE_FILENAME).resolve())},
                "scope_summary": {"allowed_files": _task_allowed_files(card), "changed_files": []},
                "worktree_preflight": baseline,
                "dirty_core_guard": dirty_guard,
                "file_preflight": file_preflight.to_dict(),
                "preflight_issues": issues_payload,
                "preflight_warnings": warnings_payload,
                "provenance": _provenance("task-run", project_root=project_root, planning_dir=planning_dir),
            }
        )
    try:
        run_result = _run_task_card(args, card=card, card_path=card_path, run_id=task_run_run_id)
    except Exception as exc:  # pragma: no cover - defensive CLI guard.
        run_result = {"reason": "IMPLEMENTATION_ERROR", "error": str(exc), "rounds": []}
    state_path = planning_dir / ".autopilot_state.json"
    state_payload = _load_json(state_path) if state_path.exists() else {}
    execution_result_payload = dict(run_result.get("execution_result") or {}) if isinstance(run_result, dict) else {}
    review_execution_backend = str(
        execution_result_payload.get("backend")
        or (run_result.get("execution_backend") if isinstance(run_result, dict) else "")
        or ""
    ).strip()
    # NOTE: state.changed_files is CUMULATIVE across all prior cycles/tasks on
    # this planning_dir. It must not be used as a fallback here — a task-run
    # cares only about changes produced by THIS task. Using cumulative state
    # would false-fire SCOPE_DRIFT on files modified by a previous task that
    # happen to sit outside the current task's files_to_change.
    baseline_diagnostics: list[dict[str, Any]] = []
    changed_files, changed_files_source = resolve_task_delta_changed_files(
        project_root=project_root,
        planning_dir=planning_dir,
        fallback_candidates=[
            ("runtime_changed_files", list(run_result.get("changed_files") or [])),
            ("execution_result", list(execution_result_payload.get("changed_files") or [])),
        ],
        baseline_diagnostic_callback=baseline_diagnostics.append,
    )
    task_id = str(card.get("task_id") or "").strip()
    if task_id:
        changed_files, changed_files_source = _merge_same_task_carry_over(
            planning_dir=planning_dir,
            task_id=task_id,
            changed_files=changed_files,
            changed_files_source=changed_files_source,
        )
    if isinstance(run_result, dict):
        run_result["task_delta_changed_files"] = list(changed_files)
        run_result["task_delta_changed_files_source"] = changed_files_source
        if baseline_diagnostics:
            existing_warnings = run_result.get("warnings")
            if isinstance(existing_warnings, list):
                existing_warnings.extend(baseline_diagnostics)
            elif existing_warnings:
                run_result["warnings"] = [existing_warnings, *baseline_diagnostics]
            else:
                run_result["warnings"] = list(baseline_diagnostics)
    strict_scope = bool(getattr(args, "strict_scope", False))
    task_run_scope_guard = _task_run_scope_guard(
        card=card,
        changed_files=changed_files,
        strict_scope=strict_scope,
        contract_mode=str(getattr(args, "contract_mode", "strict")),
    )
    if isinstance(run_result, dict):
        run_result["post_execution_scope_guard"] = dict(task_run_scope_guard)
    scope_guard_blocked = bool(dict(task_run_scope_guard.get("guard") or {}).get("blocked"))
    if scope_guard_blocked and isinstance(run_result, dict):
        run_result["reason"] = "SCOPE_DRIFT_BLOCKED"
    rc = _task_run_exit_code(run_result, strict_scope=bool(getattr(args, "strict_scope", False)))
    if scope_guard_blocked:
        rc = 2
    # Propagate terminal outcome into .autopilot_state.json so `kodawari
    # status` does not report RUNNING after a task-run was blocked by review.
    # See Phase A2 / CAPABILITY_MAP.md "interaction_state truth".
    if isinstance(run_result, dict):
        sync_task_run_terminal_state(
            state_path=state_path,
            run_result=run_result,
            run_id=task_run_run_id,
        )
    output_path = planning_dir / ".task_run_result.json"
    write_json(output_path, run_result if isinstance(run_result, dict) else {"run_result": run_result})
    verify_check = dict(run_result.get("verify_check") or {}) if isinstance(run_result, dict) else {}
    verify_report_path: str | None = None
    if verify_check:
        verify_report_payload = build_verify_report_payload(
            feature=feature,
            planning_dir=planning_dir,
            verify_check=verify_check,
            changed_files=changed_files,
            changed_files_source=changed_files_source,
            input_confidence="curated",
            requested_command=str(verify_check.get("verify_cmd") or getattr(args, "verify_cmd", "pytest -q")),
            entrypoint="kodawari task-run",
        )
        verify_report_file = planning_dir / VERIFY_REPORT_FILENAME
        write_verify_report_artifact(verify_report_file, verify_report_payload)
        verify_report_path = str(verify_report_file)
    review_evidence_path: str | None = None
    review_evidence_payload = None
    if isinstance(run_result, dict) and isinstance(run_result.get("review_evidence"), dict):
        review_evidence_payload = dict(run_result.get("review_evidence") or {})
    if review_evidence_payload is None and isinstance(run_result, dict):
        review_evidence_payload = derive_runtime_review_evidence(
            run_result=run_result,
            execution_backend=review_execution_backend,
        )
    if review_evidence_payload is None:
        review_evidence_payload = extract_review_evidence_from_compliance_report(
            run_result.get("compliance_report") if isinstance(run_result, dict) else None,
            source=".task_run_result.json.compliance_report.review_evidence",
            explicit=True,
        )
    if isinstance(review_evidence_payload, dict):
        review_evidence_artifact = build_review_evidence_artifact(
            feature=feature,
            planning_dir=planning_dir,
            review_evidence=dict(review_evidence_payload),
            entrypoint="kodawari task-run",
        )
        review_evidence_file = planning_dir / REVIEW_EVIDENCE_FILENAME
        write_review_evidence_artifact(review_evidence_file, review_evidence_artifact)
        review_evidence_path = str(review_evidence_file)
    top_level_execution_backend = review_execution_backend
    top_level_execution_backend_capabilities = dict(
        execution_result_payload.get("backend_capabilities")
        or (
            run_result.get("execution_backend_capabilities")
            if isinstance(run_result, dict)
            else {}
        )
        or {}
    )
    payload = {
        "_rc": rc,
        "status": "PASS" if rc == 0 else "FAIL",
        "entrypoint": "kodawari task-run",
        "feature": feature,
        "planning_dir": str(planning_dir),
        "task_id": str(card.get("task_id") or ""),
        "strict_scope": bool(getattr(args, "strict_scope", False)),
        "execution_backend": top_level_execution_backend,
        "execution_backend_capabilities": top_level_execution_backend_capabilities,
        "reason": str(run_result.get("reason") or ""),
        "next_action": _task_run_next_action(
            reason=str(run_result.get("reason") or ""),
            phase_mode=str(getattr(args, "phase_mode", "implement")),
            strict_scope=bool(getattr(args, "strict_scope", False)),
            task_id=str(card.get("task_id") or ""),
        ),
        "artifacts": {
            ".task_run_result.json": str(output_path),
            WORKTREE_BASELINE_FILENAME: str((planning_dir / WORKTREE_BASELINE_FILENAME).resolve()),
            **({EXECUTION_REQUEST_FILENAME: str((planning_dir / EXECUTION_REQUEST_FILENAME).resolve())} if (planning_dir / EXECUTION_REQUEST_FILENAME).exists() else {}),
            **({EXECUTION_RESULT_FILENAME: str((planning_dir / EXECUTION_RESULT_FILENAME).resolve())} if (planning_dir / EXECUTION_RESULT_FILENAME).exists() else {}),
            **({".review_bundle.json": str((planning_dir / ".review_bundle.json").resolve())} if (planning_dir / ".review_bundle.json").exists() else {}),
            **({REVIEW_EVIDENCE_FILENAME: str((planning_dir / REVIEW_EVIDENCE_FILENAME).resolve())} if review_evidence_path else {}),
            **({VERIFY_REPORT_FILENAME: str((planning_dir / VERIFY_REPORT_FILENAME).resolve())} if verify_report_path else {}),
        },
        "scope_summary": {
            "allowed_files": _task_allowed_files(card),
            "changed_files": changed_files,
            "guard": dict(task_run_scope_guard.get("guard") or {}),
        },
        "scope_guard": task_run_scope_guard,
        "task_delta_changed_files": changed_files,
        "task_delta_changed_files_source": changed_files_source,
        "worktree_preflight": baseline,
        "dirty_core_guard": dirty_guard,
        "file_preflight": file_preflight.to_dict(),
        "preflight_warnings": [warning.to_dict() for warning in file_preflight.warnings],
        "run_result": run_result,
        "provenance": _provenance("task-run", project_root=project_root, planning_dir=planning_dir),
    }
    # Carry-over manifest: list of files this task-run wrote into project_root
    # (via sync_isolated_workspace_to_project_root). Read by the next task-run's
    # preflight to avoid spurious DIRTY_WORKTREE_BLOCKED on retry of the same task.
    write_task_run_manifest(
        planning_dir=planning_dir,
        task_id=str(card.get("task_id") or ""),
        status=str(payload.get("status") or ""),
        carried_files=list(changed_files or []),
    )
    return _emit(payload)


def _cmd_compliance_check(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root).resolve()
    feature = str(args.feature).strip()
    planning_dir = _planning_dir(project_root, feature, getattr(args, "planning_dir", None))
    state_path = planning_dir / ".autopilot_state.json"
    state_payload = _load_json(state_path) if state_path.exists() else {}
    task_run_path = planning_dir / ".task_run_result.json"
    task_run_payload = _load_json(task_run_path) if task_run_path.exists() else {}
    changed_files = [str(item) for item in list(getattr(args, "changed_file", []) or []) if str(item).strip()]
    changed_files_source = "cli_override" if changed_files else "none"
    if not changed_files:
        changed_files, changed_files_source = resolve_task_delta_changed_files(
            project_root=project_root,
            planning_dir=planning_dir,
            fallback_candidates=[
                (
                    "task_run_result",
                    list(task_run_payload.get("task_delta_changed_files") or task_run_payload.get("changed_files") or []),
                ),
                ("state_changed_files", list(state_payload.get("changed_files") or [])),
            ],
        )
    try:
        task_card = _load_contract_json(planning_dir / "TASK_CARD_ACTIVE.json", schema_name="task_card") if (planning_dir / "TASK_CARD_ACTIVE.json").exists() else None
        task_graph = _load_contract_json(planning_dir / "TASK_GRAPH.json", schema_name="task_graph") if (planning_dir / "TASK_GRAPH.json").exists() else None
        prd_intake = _load_contract_json(planning_dir / "PRD_INTAKE.json", schema_name="prd_intake") if (planning_dir / "PRD_INTAKE.json").exists() else None
    except (FileNotFoundError, ArtifactSchemaVersionError, ContractFirstSchemaValidationError, CorruptArtifactError, ValueError) as exc:
        return _emit_contract_error(
            command="compliance-check",
            project_root=project_root,
            planning_dir=planning_dir,
            feature=feature,
            error=exc,
            remediation=["Regenerate or migrate the contract-first planning artifacts before rerunning compliance-check."],
        )
    try:
        review_evidence = _load_review_evidence(planning_dir)
    except (ArtifactSchemaVersionError, ReviewEvidenceSchemaValidationError, CorruptArtifactError, ValueError) as exc:
        return _emit_contract_error(
            command="compliance-check",
            project_root=project_root,
            planning_dir=planning_dir,
            feature=feature,
            error=exc,
            remediation=["Fix or regenerate `.review_evidence.json` before rerunning compliance-check."],
        )
    schema_files = discover_project_schema_files(project_root)
    report = build_contract_compliance_report(
        project_root=project_root,
        changed_files=changed_files,
        task_card=task_card,
        task_graph=task_graph,
        prd_intake=prd_intake,
        review_evidence=review_evidence,
        schema_files=schema_files,
    )
    output_path = Path(args.output).resolve() if getattr(args, "output", None) else (planning_dir / "COMPLIANCE_REPORT.json")
    try:
        write_contract_first_artifact(output_path, report, schema_name="compliance_report")
    except (ArtifactSchemaVersionError, ContractFirstSchemaValidationError, CorruptArtifactError, ValueError) as exc:
        return _emit_contract_error(
            command="compliance-check",
            project_root=project_root,
            planning_dir=planning_dir,
            feature=feature,
            error=exc,
            remediation=["Fix the compliance report/schema mismatch before rerunning compliance-check."],
            extra={"changed_files": changed_files, "changed_files_source": changed_files_source},
        )
    payload = {
        "_rc": 0 if str(report.get("status") or "").upper() == "PASS" else 2,
        "status": str(report.get("status") or "FAIL"),
        "entrypoint": "kodawari compliance-check",
        "feature": feature,
        "planning_dir": str(planning_dir),
        "artifacts": {"COMPLIANCE_REPORT.json": str(output_path)},
        "changed_files": changed_files,
        "changed_files_source": changed_files_source,
        "report": report,
        "provenance": _provenance("compliance-check", project_root=project_root, planning_dir=planning_dir),
    }
    return _emit(payload)


def register_contract_first_commands(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    prd_intake = sub.add_parser("prd-intake", help="Generate PRD_INTAKE contract artifact")
    prd_intake.add_argument("--project-root", default=".")
    prd_intake.add_argument("--feature", required=True)
    prd_intake.add_argument("--prd", required=True, help="Path to PRD text/markdown file")
    prd_intake.add_argument("--planning-dir")
    prd_intake.add_argument("--output", help="Optional PRD_INTAKE.json output path")
    prd_intake.add_argument("--emit-md", action="store_true", help="Also emit PRD_INTAKE markdown mirror")
    prd_intake.set_defaults(handler=_cmd_prd_intake)

    task_plan = sub.add_parser("task-plan", help="Build TASK_GRAPH from PRD_INTAKE")
    task_plan.add_argument("--project-root", default=".")
    task_plan.add_argument("--feature", required=True)
    task_plan.add_argument("--intake", required=True, help="Path to PRD_INTAKE.json")
    task_plan.add_argument("--planning-dir")
    task_plan.add_argument("--output", help="Optional TASK_GRAPH.json output path")
    task_plan.add_argument("--emit-md", action="store_true", help="Also emit TASK_GRAPH markdown mirror")
    task_plan.add_argument("--project-profile", default="auto", help="Project profile hint (auto|python|fastapi|flask|django|node)")
    task_plan.set_defaults(handler=_cmd_task_plan)

    task_prepare = sub.add_parser("task-prepare", help="Build TASK_CARD for one task")
    task_prepare.add_argument("--project-root", default=".")
    task_prepare.add_argument("--feature", required=True)
    task_prepare.add_argument("--graph", required=True, help="Path to TASK_GRAPH.json")
    task_prepare.add_argument("--task", required=True, help="Task id, e.g. T1")
    task_prepare.add_argument("--planning-dir")
    task_prepare.add_argument("--output", help="Optional TASK_CARD output path")
    task_prepare.add_argument("--emit-md", action="store_true", help="Also emit TASK_CARD markdown mirror")
    task_prepare.set_defaults(handler=_cmd_task_prepare)

    task_run = sub.add_parser("task-run", help="Execute one task card with optional strict scope")
    task_run.add_argument("--project-root", default=".")
    task_run.add_argument("--feature", required=True)
    task_run.add_argument("--card", required=True, help="Path to TASK_CARD json")
    task_run.add_argument("--planning-dir")
    task_run.add_argument("--requirements-file")
    task_run.add_argument("--verify-cmd", default="pytest -q")
    task_run.add_argument("--max-cycles", type=int, default=8)
    task_run.add_argument("--token-budget", type=int, default=300000)
    task_run.add_argument("--contract-mode", default="strict", choices=["off", "warn", "strict"])
    task_run.add_argument("--phase-mode", default="implement", choices=["analyze", "implement"])
    task_run.add_argument("--strict-scope", action="store_true")
    task_run.add_argument("--executor-backend", choices=execution_backend_choices())
    task_run.add_argument("--executor-command", help="Command used when --executor-backend=external_cli")
    task_run.add_argument("--self-review-backend", choices=self_review_backend_choices())
    task_run.add_argument("--self-review-command", help="Command used when --self-review-backend=external_cli")
    task_run.add_argument("--real-peer-review", action="store_true")
    task_run.add_argument("--require-real-peer-review", action="store_true")
    task_run.add_argument("--real-opus-review", action="store_true", help="Legacy alias for --real-peer-review")
    task_run.add_argument("--require-real-opus-review", action="store_true", help="Legacy alias for --require-real-peer-review")
    task_run.add_argument(
        "--opus-reviewer-backend",
        default="",
        choices=["", "auto", "api", "cli", "mcp", "codex"],
        help="Deprecated alias for --reviewer-backend; will be removed after 2026-08-01.",
    )
    task_run.add_argument(
        "--reviewer-backend",
        default="",
        choices=["", "auto", "api", "cli", "mcp", "codex"],
        help="Reviewer backend: api (HTTP+key), cli (Claude CLI), mcp (Claude CLI+MCP), codex (Codex CLI).",
    )
    task_run.add_argument("--executor-model", default="", help="Model override for executor (e.g. claude-opus-4-5).")
    task_run.add_argument("--reviewer-model", default="", help="Model override for reviewer (API backend).")
    task_run.add_argument(
        "--reviewer-api-format",
        default="",
        choices=["", "auto", "anthropic", "openai"],
        help="API format for reviewer (api backend). env: WORKFLOW_REVIEWER_API_FORMAT.",
    )
    task_run.add_argument("--reviewer-base-url", default="", help="Base URL for reviewer (api backend). env: WORKFLOW_REVIEWER_BASE_URL.")
    task_run.add_argument("--peer-review-max-tokens", type=int, default=4096)
    task_run.add_argument("--rollback-on-failure", action="store_true", help="Rollback implement changes before verify/gate retries.")
    task_run.add_argument("--max-verify-retries", type=int, default=2, help="Maximum verify retry attempts when rollback-on-failure is enabled.")
    task_run.add_argument("--no-enable-peer-review", action="store_true")
    task_run.set_defaults(handler=_cmd_task_run)

    compliance = sub.add_parser("compliance-check", help="Generate COMPLIANCE_REPORT from planning artifacts")
    compliance.add_argument("--project-root", default=".")
    compliance.add_argument("--feature", required=True)
    compliance.add_argument("--planning-dir")
    compliance.add_argument("--changed-file", action="append", help="Override changed files (repeatable)")
    compliance.add_argument("--output", help="Optional COMPLIANCE_REPORT output path")
    compliance.set_defaults(handler=_cmd_compliance_check)


