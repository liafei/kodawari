"""Status command implementation and planning contract helpers."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
import subprocess
import sys
from typing import Any

from kodawari.cli.status.absorption_status import absorption_status_snapshot
from kodawari.autopilot.planning.effort_scoring import score_effort_profile
from kodawari.cli.artifact_versions import ArtifactSchemaVersionError, load_versioned_artifact
from kodawari.cli.runtime.autopilot_decision_runtime import decision_runtime_snapshot
from kodawari.cli.runtime.autopilot_interaction_state import build_interaction_snapshot
from kodawari.cli.evidence.artifact_truth import (
    resolve_authoritative_changed_files,
    resolve_effective_review_truth_source,
    resolve_effective_verify_truth_source,
    resolve_review_artifact_truth,
    resolve_review_evidence_truth,
    resolve_verify_artifact_truth,
)
from kodawari.cli.evidence.changed_files_truth import resolve_task_delta_changed_files
from kodawari.cli.delivery.delivery_common import (
    LEGACY_PLANNING_ARTIFACTS,
    _attach_payload_digest,
    _load_contract_compliance_report,
    _required_planning_artifacts_status,
    _utc_now_iso,
    _write_json,
)
from kodawari.cli.delivery.delivery_release import _resolve_execution_check, _resolve_verify_check
from kodawari.cli.io_atomic import CorruptArtifactError, atomic_write_text
from kodawari.cli.main_support import (
    ARTIFACT_SEMANTICS,
    DEFAULT_GATE_REDLINE,
    MERGED_CONTRACT_VERSION,
    _build_cli_provenance,
    _load_json_dict,
    _load_optional_json_dict,
    _normalized_error_payload,
)
from kodawari.cli.contract.status_contract_first import (
    build_contract_first_planning_status,
    detect_status_planning_mode,
)
from kodawari.cli.contract.planning_conversation_compat import load_prd_intake_compatible
from kodawari.cli.status.status_runtime import (
    budget_snapshot,
    execution_runtime_summary,
    execution_truth_source,
    review_runtime_summary,
    review_truth_source,
    verify_runtime_summary,
    verify_truth_source,
)
from kodawari.cli.runtime.task_run_state_sync import derive_task_run_terminal_state
from kodawari.cli.status.status_markdown import render_status_markdown
from kodawari.cli.delivery.workflow_chain import (
    bind_effective_gate_result,
    load_workflow_chain_snapshot,
)

_STATUS_ARTIFACT_ORDER = (
    "PRD_INTAKE.json",
    "REPO_INVENTORY.json",
    "ARCHITECTURE_PLAN.json",
    "PLANNING_CONVERSATION.json",
    "TASK_GRAPH.json",
    "TASK_CARD_ACTIVE.json",
    "PLAN.md",
    "TASKS.md",
    "ACCEPTANCE.md",
    "GATE.md",
    "COMPLIANCE_REPORT.json",
    ".autopilot_state.json",
    ".autopilot_rounds.jsonl",
    ".gate_result.json",
    ".workflow_chain.json",
    "DESIGN.md",
    "REVIEW.md",
    "QA_REPORT.md",
    "RELEASE.md",
    ".review_result.json",
    ".review_evidence.json",
    ".execution_request.json",
    ".execution_result.json",
    ".review_bundle.json",
    ".verify_report.json",
    ".qa_report.json",
    ".ship_readiness.json",
    ".status_snapshot.json",
    "STATUS.md",
)
STATUS_SNAPSHOT_FILENAME = ".status_snapshot.json"
STATUS_MARKDOWN_FILENAME = "STATUS.md"
logger = logging.getLogger(__name__)


def _extract_task_id(active_task: str | None) -> str | None:
    text = str(active_task or "").strip()
    if not text:
        return None
    if ":" in text:
        return text.split(":", 1)[0].strip() or None
    return text


def _coerce_subtask_map(autopilot_state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = autopilot_state.get("subtasks", {})
    if not isinstance(raw, dict):
        return {}
    normalized: dict[str, dict[str, Any]] = {}
    for subtask_id, payload in raw.items():
        if isinstance(payload, dict):
            normalized[str(subtask_id)] = payload
    return normalized


def _partition_subtasks(subtasks: dict[str, dict[str, Any]]) -> dict[str, list[str]]:
    buckets = {"failed": [], "pending": [], "running": []}
    status_map = {
        "FAILED": "failed",
        "PENDING": "pending",
        "IN_PROGRESS": "running",
        "RUNNING": "running",
    }
    for subtask_id, payload in subtasks.items():
        status = str(payload.get("status") or "").upper()
        bucket = status_map.get(status)
        if bucket:
            buckets[bucket].append(subtask_id)
    for values in buckets.values():
        values.sort()
    return buckets


def _is_terminal_phase(current_phase: str, final_status: str) -> bool:
    return current_phase in {"COMPLETED", "FAILED", "CANCELLED"} or bool(final_status)


def _derive_blocking_reason(
    autopilot_state: dict[str, Any],
    *,
    subtasks: dict[str, dict[str, Any]],
    failed_subtasks: list[str],
    stop_reason: str,
) -> Any:
    blocking_reason = autopilot_state.get("last_error")
    if not blocking_reason and failed_subtasks:
        first_failed = subtasks.get(failed_subtasks[0], {})
        blocking_reason = first_failed.get("error")
    if not blocking_reason and stop_reason and stop_reason != "PASS":
        blocking_reason = stop_reason
    return blocking_reason


def _derive_next_action(
    *,
    failed_subtasks: list[str],
    pending_subtasks: list[str],
    running_subtasks: list[str],
    is_terminal: bool,
    final_status: str,
    stop_reason: str,
) -> str:
    if failed_subtasks:
        return "Repair the failed subtask and rerun scoped verify"
    if is_terminal and (final_status == "PASS" or stop_reason == "PASS"):
        return "Start the next queued task or close the automation run"
    if is_terminal:
        return "Review blocking reason and decide whether to retry or stop"
    if pending_subtasks or running_subtasks:
        return "Continue active task and execute pending subtasks"
    return "Continue current stage and monitor verify/gate outcomes"


def _build_fallback_unified_status(autopilot_state: dict[str, Any]) -> dict[str, Any]:
    current_phase = str(autopilot_state.get("current_stage") or "UNKNOWN").upper()
    active_task = str(autopilot_state.get("active_task") or "").strip() or None
    subtasks = _coerce_subtask_map(autopilot_state)
    partitioned = _partition_subtasks(subtasks)
    failed_subtasks = partitioned["failed"]
    pending_subtasks = partitioned["pending"]
    running_subtasks = partitioned["running"]
    final_status = str(autopilot_state.get("final_status") or "").upper()
    stop_reason = str(autopilot_state.get("stop_reason") or "").upper()
    is_terminal = _is_terminal_phase(current_phase, final_status)
    blocking_reason = _derive_blocking_reason(
        autopilot_state,
        subtasks=subtasks,
        failed_subtasks=failed_subtasks,
        stop_reason=stop_reason,
    )
    next_action = _derive_next_action(
        failed_subtasks=failed_subtasks,
        pending_subtasks=pending_subtasks,
        running_subtasks=running_subtasks,
        is_terminal=is_terminal,
        final_status=final_status,
        stop_reason=stop_reason,
    )

    return {
        "current_phase": current_phase,
        "current_task": active_task,
        "current_task_id": _extract_task_id(active_task),
        "active_subtask": autopilot_state.get("active_subtask"),
        "pending_subtasks": pending_subtasks,
        "running_subtasks": running_subtasks,
        "failed_subtasks": failed_subtasks,
        "is_terminal": is_terminal,
        "blocking_reason": blocking_reason,
        "next_action": next_action,
    }


def _load_autopilot_state_model() -> Any | None:
    try:
        from kodawari.autopilot.state import AutopilotState  # type: ignore
    except Exception:
        logger.warning("autopilot state model unavailable while building status payload", exc_info=True)
        return None
    return AutopilotState


def _state_from_payload(state_cls: Any, autopilot_state: dict[str, Any]) -> Any | None:
    if not hasattr(state_cls, "from_dict"):
        return None
    try:
        return state_cls.from_dict(autopilot_state)
    except Exception:
        logger.warning("failed to hydrate autopilot state model from status payload", exc_info=True)
        return None


def _state_unified_status(state: Any) -> dict[str, Any] | None:
    if not hasattr(state, "get_unified_status"):
        return None
    try:
        return state.get_unified_status()
    except Exception:
        logger.warning("failed to read unified autopilot status from state model", exc_info=True)
        return None


def _try_state_model_unified_status(autopilot_state: dict[str, Any]) -> dict[str, Any] | None:
    state_cls = _load_autopilot_state_model()
    if state_cls is None:
        return None
    state = _state_from_payload(state_cls, autopilot_state)
    if state is None:
        return None
    return _state_unified_status(state)


def _build_unified_autopilot_status(autopilot_state: dict[str, Any]) -> dict[str, Any]:
    resolved = _try_state_model_unified_status(autopilot_state)
    if resolved is not None:
        return resolved
    return _build_fallback_unified_status(autopilot_state)


def _normalize_changed_path(raw: Any) -> str:
    text = str(raw or "").strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text


def _dedupe_changed_paths(values: list[Any]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for item in values:
        normalized = _normalize_changed_path(item)
        if not normalized:
            continue
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(normalized)
    return deduped


def _existing_changed_paths(project_root: Path, changed_files: list[str]) -> list[str]:
    existing: list[str] = []
    for item in changed_files:
        candidate = (project_root / item).resolve()
        if candidate.exists():
            existing.append(item)
    return existing


def _changed_files_from_subtasks(autopilot_state: dict[str, Any]) -> list[str]:
    subtasks = dict(autopilot_state.get("subtasks") or {})
    values: list[Any] = []
    for payload in subtasks.values():
        if not isinstance(payload, dict):
            continue
        changed = payload.get("changed_files")
        if isinstance(changed, list):
            values.extend(changed)
    return _dedupe_changed_paths(values)


def _changed_files_from_rounds(rounds_path: Path) -> list[str]:
    if not rounds_path.exists():
        return []
    values: list[Any] = []
    for raw in rounds_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        direct = payload.get("changed_files")
        if isinstance(direct, list):
            values.extend(direct)
        details = payload.get("details")
        if isinstance(details, dict):
            nested = details.get("changed_files")
            if isinstance(nested, list):
                values.extend(nested)
            verify = details.get("verify")
            if isinstance(verify, dict):
                artifacts = verify.get("artifacts")
                if isinstance(artifacts, list):
                    values.extend(artifacts)
            scope_drift = details.get("scope_drift")
            if isinstance(scope_drift, dict):
                drift_changed = scope_drift.get("changed_files")
                if isinstance(drift_changed, list):
                    values.extend(drift_changed)
    return _dedupe_changed_paths(values)


def _git_changed_files(project_root: Path) -> list[str]:
    diff_cmd = [
        "git",
        "-C",
        str(project_root),
        "diff",
        "--name-only",
        "--diff-filter=ACMR",
    ]
    untracked_cmd = [
        "git",
        "-C",
        str(project_root),
        "ls-files",
        "--others",
        "--exclude-standard",
    ]
    values: list[str] = []
    try:
        diff = subprocess.run(diff_cmd, check=True, capture_output=True, text=True, encoding="utf-8", errors="replace")
        values.extend(line for line in diff.stdout.splitlines() if line.strip())
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    try:
        untracked = subprocess.run(untracked_cmd, check=True, capture_output=True, text=True, encoding="utf-8", errors="replace")
        values.extend(line for line in untracked.stdout.splitlines() if line.strip())
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    return _dedupe_changed_paths(values)


def _resolved_git_changed_files(project_root: Path) -> list[str]:
    main_module = sys.modules.get("kodawari.cli.main")
    override = getattr(main_module, "_git_changed_files", None) if main_module is not None else None
    if callable(override) and override is not _git_changed_files:
        return _dedupe_changed_paths(list(override(project_root) or []))
    return _git_changed_files(project_root)


def _resolved_task_delta_changed_files(
    *,
    project_root: Path,
    planning_dir: Path,
    fallback_candidates: list[tuple[str, list[str]]],
) -> tuple[list[str], str]:
    main_module = sys.modules.get("kodawari.cli.main")
    override = getattr(main_module, "resolve_task_delta_changed_files", None) if main_module is not None else None
    if callable(override) and override is not resolve_task_delta_changed_files:
        return override(
            project_root=project_root,
            planning_dir=planning_dir,
            fallback_candidates=fallback_candidates,
        )
    return resolve_task_delta_changed_files(
        project_root=project_root,
        planning_dir=planning_dir,
        fallback_candidates=fallback_candidates,
    )


def _resolve_status_changed_files(
    *,
    project_root: Path,
    planning_dir: Path,
    autopilot_state: dict[str, Any],
) -> tuple[list[str], str]:
    state_changed = _dedupe_changed_paths(list(autopilot_state.get("changed_files") or []))
    subtask_changed = _changed_files_from_subtasks(autopilot_state)
    round_changed = _changed_files_from_rounds(planning_dir / ".autopilot_rounds.jsonl")
    git_changed = _resolved_git_changed_files(project_root)

    for source, values in (
        ("state_changed_files", state_changed),
        ("subtask_changed_files", subtask_changed),
        ("rounds_changed_files", round_changed),
        ("git_worktree", git_changed),
    ):
        existing = _existing_changed_paths(project_root, values)
        if existing:
            return existing, f"{source}:existing"

    for source, values in (
        ("state_changed_files", state_changed),
        ("subtask_changed_files", subtask_changed),
        ("rounds_changed_files", round_changed),
        ("git_worktree", git_changed),
    ):
        if values:
            return values, f"{source}:raw"

    return [], "none"


def _enrich_autopilot_state_payload(
    autopilot_state: dict[str, Any] | None,
    *,
    project_root: Path,
    planning_dir: Path,
) -> dict[str, Any] | None:
    if autopilot_state is None:
        return None
    enriched = dict(autopilot_state)
    changed_files, changed_files_source = _resolve_status_changed_files(
        project_root=project_root,
        planning_dir=planning_dir,
        autopilot_state=autopilot_state,
    )
    task_delta_changed_files, task_delta_changed_files_source = _resolved_task_delta_changed_files(
        project_root=project_root,
        planning_dir=planning_dir,
        fallback_candidates=[
            ("state_changed_files", list(autopilot_state.get("changed_files") or [])),
            ("subtask_changed_files", _changed_files_from_subtasks(autopilot_state)),
            ("rounds_changed_files", _changed_files_from_rounds(planning_dir / ".autopilot_rounds.jsonl")),
            ("git_worktree", _resolved_git_changed_files(project_root)),
        ],
    )
    enriched["changed_files"] = changed_files
    enriched["changed_files_source"] = changed_files_source
    enriched["task_delta_changed_files"] = task_delta_changed_files
    enriched["task_delta_changed_files_source"] = task_delta_changed_files_source
    enriched["unified_status"] = _build_unified_autopilot_status(autopilot_state)
    return enriched


def _resolve_planning_dir(args: argparse.Namespace) -> Path:
    if getattr(args, "planning_dir", None):
        return Path(args.planning_dir).resolve()
    feature = str(getattr(args, "feature", "") or "").strip()
    if not feature:
        raise ValueError("status requires --feature when --planning-dir is not provided")
    return (Path(args.project_root).resolve() / "planning" / feature).resolve()


def _status_interaction_payload(
    *,
    planning_dir: Path,
    state_payload: dict[str, Any] | None,
    execution_check: dict[str, Any],
    semantic_compact: dict[str, Any] | None,
) -> dict[str, Any]:
    decision = decision_runtime_snapshot(planning_dir)
    unified = dict((state_payload or {}).get("unified_status") or {})
    if not unified:
        unified = _fallback_terminal_unified_status(
            planning_dir=planning_dir,
            semantic_compact=semantic_compact,
        )
    return build_interaction_snapshot(
        decision_pending=bool(decision.get("decision_pending", False)),
        decision_kind=decision.get("decision_kind"),
        decision_id=decision.get("decision_id"),
        decision_request_present=bool(decision.get("decision_request_present", False)),
        environment_error_code=execution_check.get("execution_status"),
        environment_blocking_reason=execution_check.get("reason"),
        final_status=unified.get("final_status"),
        stop_reason=unified.get("stop_reason"),
        blocked=bool(unified.get("is_blocked", False)),
        is_terminal=bool(unified.get("is_terminal", False)),
    )


def _fallback_terminal_unified_status(
    *,
    planning_dir: Path,
    semantic_compact: dict[str, Any] | None,
) -> dict[str, Any]:
    loop_outcome = dict((semantic_compact or {}).get("loop_outcome") or {})
    final_status = str(loop_outcome.get("final_status") or "").strip().upper()
    stop_reason = str(loop_outcome.get("stop_reason") or "").strip().upper()
    blocked = bool(loop_outcome.get("is_blocked", False))
    if final_status or stop_reason or blocked:
        return {
            "final_status": final_status,
            "stop_reason": stop_reason,
            "is_blocked": blocked or final_status == "BLOCKED",
            "is_terminal": True,
        }
    run_result = _load_optional_json_dict(planning_dir / ".task_run_result.json") or {}
    terminal = derive_task_run_terminal_state(run_result)
    if terminal is None:
        return {}
    return {
        "final_status": str(terminal.get("final_status") or "").upper(),
        "stop_reason": str(terminal.get("stop_reason") or "").upper(),
        "is_blocked": str(terminal.get("final_status") or "").upper() == "BLOCKED",
        "is_terminal": True,
    }


def _planning_contract_artifacts(planning_dir: Path) -> dict[str, dict[str, Any]]:
    artifacts: dict[str, dict[str, Any]] = {}
    for name in _STATUS_ARTIFACT_ORDER:
        path = (planning_dir / name).resolve()
        artifacts[name] = {
            "path": str(path),
            "exists": path.exists(),
            "semantic": ARTIFACT_SEMANTICS.get(name, ""),
        }
    return artifacts


def _status_effort_profile(
    *,
    planning_dir: Path,
    state_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    task_card = _load_optional_json_dict(planning_dir / "TASK_CARD_ACTIVE.json") or {}
    repo_inventory = _load_optional_json_dict(planning_dir / "REPO_INVENTORY.json") or {}
    planning_context = load_prd_intake_compatible(planning_dir) or {}
    state = dict(state_payload or {})
    unified = dict(state.get("unified_status") or {})
    task_label = str(
        state.get("active_task")
        or unified.get("current_task")
        or task_card.get("task_name")
        or ""
    ).strip()
    requirements = "\n".join(
        part
        for part in (
            str(planning_context.get("business_outcome") or "").strip(),
            " ".join(str(item) for item in list(planning_context.get("out_of_scope") or []) if str(item).strip()),
            " ".join(str(item) for item in list(task_card.get("invariants") or []) if str(item).strip()),
        )
        if part
    )
    changed_files = [str(item) for item in list(state.get("task_delta_changed_files") or state.get("changed_files") or []) if str(item).strip()]
    project_model = {
        "surface": str(task_card.get("layer") or "").strip(),
        "capabilities": [str(item) for item in list(repo_inventory.get("capabilities") or []) if str(item).strip()],
    }
    return score_effort_profile(
        task_label=task_label,
        task_scope=str(task_card.get("task_name") or "").strip(),
        requirements=requirements,
        task_card=task_card,
        changed_files=changed_files,
        prior_failures=len(list(state.get("error_events") or [])),
        project_model=project_model,
    )


def _load_gate_summary(planning_dir: Path) -> dict[str, Any] | None:
    gate_json = _load_json_dict(planning_dir / ".gate_result.json")
    if isinstance(gate_json, dict):
        return {
            "source": ".gate_result.json",
            "total_status": gate_json.get("total_status"),
            "profile": (
                (gate_json.get("profile") or {}).get("name")
                if isinstance(gate_json.get("profile"), dict)
                else None
            ),
            "blocking_violations": gate_json.get("blocking_violations"),
            "total_violations": gate_json.get("total_violations"),
            "blocking_reason": gate_json.get("blocking_reason"),
        }
    compliance_json = _load_contract_compliance_report(planning_dir)
    if isinstance(compliance_json, dict):
        compliance_status = str(compliance_json.get("status") or "UNKNOWN").upper()
        return {
            "source": "COMPLIANCE_REPORT.json",
            "total_status": compliance_status,
            "profile": "contract_first",
            "blocking_violations": 1 if compliance_status == "BLOCKED" else 0,
            "total_violations": len(list(compliance_json.get("checks") or [])),
            "blocking_reason": str(compliance_json.get("blocking_reason") or ""),
        }

    gate_md = planning_dir / "GATE.md"
    if gate_md.exists():
        return {"source": "GATE.md", "total_status": "UNKNOWN"}
    return None


def _planning_contract_summary(planning_dir: Path) -> dict[str, Any]:
    mode = detect_status_planning_mode(planning_dir)
    artifacts = _planning_contract_artifacts(planning_dir)
    if mode == "contract_first":
        planning_status = build_contract_first_planning_status(planning_dir)
        required_artifacts = list(planning_status.get("required_artifacts") or [])
        complete = bool(planning_status.get("planning_complete"))
        status_read_order = [
            ".execution_result.json",
            ".review_evidence.json",
            ".review_result.json",
            ".verify_report.json",
            ".qa_report.json",
            ".ship_readiness.json",
            "COMPLIANCE_REPORT.json",
        ]
        truth_source = str(planning_status.get("planning_truth_source") or "")
        planning_requirements = dict(planning_status.get("planning_requirements") or {})
        invalid_artifacts = list(planning_status.get("invalid_artifacts") or [])
    else:
        required_status = _required_planning_artifacts_status(planning_dir, include_delivery_artifacts=False)
        required_artifacts = list(required_status.get("required") or [])
        complete = bool(required_status.get("all_present"))
        status_read_order = [".autopilot_state.json", ".gate_result.json", "GATE.md"]
        truth_source = "PLAN.md+TASKS.md+ACCEPTANCE.md+GATE.md"
        planning_requirements = {
            "mode": "legacy",
            "planning_mode": "legacy",
            "surface_count": 0,
            "requires_architecture_plan": False,
            "required_artifacts": required_artifacts,
        }
        invalid_artifacts = list(required_status.get("invalid") or [])
    return {
        "version": MERGED_CONTRACT_VERSION,
        "artifact_mode": mode,
        "directory": str(planning_dir),
        "required_artifacts": required_artifacts,
        # required_artifacts may reference entries outside _STATUS_ARTIFACT_ORDER
        # (e.g. schema-versioned additions from contract_first). Fall back to the
        # global ARTIFACT_SEMANTICS registry so status never KeyErrors on drift.
        "artifact_semantics": {
            name: (
                artifacts[name]["semantic"]
                if name in artifacts
                else ARTIFACT_SEMANTICS.get(name, "")
            )
            for name in required_artifacts
        },
        "complete": complete,
        "truth_source": truth_source,
        "planning_requirements": planning_requirements,
        "invalid_artifacts": invalid_artifacts,
        "status_read_order": status_read_order,
        "gate_defaults": {
            "profile": "advisory",
            "redline": dict(DEFAULT_GATE_REDLINE),
            "mode": "non-blocking",
            "item_status": ["PASS", "PARTIAL", "FAIL"],
            "total_status": ["PASS", "BLOCKED"],
        },
    }


def _review_complete(
    planning_dir: Path,
    *,
    review_result_truth: dict[str, Any],
    review_evidence_truth: dict[str, Any],
) -> bool:
    if bool(review_evidence_truth.get("usable")) or bool(review_result_truth.get("usable")):
        return True
    if (planning_dir / "REVIEW.md").exists() and not (
        bool(review_result_truth.get("exists")) or bool(review_evidence_truth.get("exists"))
    ):
        return True
    return False


def _release_complete(planning_dir: Path) -> bool:
    qa_ready = any((planning_dir / name).exists() for name in (".qa_report.json", "QA_REPORT.md"))
    ship_ready = any((planning_dir / name).exists() for name in (".ship_readiness.json", "RELEASE.md"))
    return qa_ready and ship_ready


def _build_status_payload(
    *,
    planning_dir: Path,
    project_root: Path,
    planning_mode: str,
    contract_planning: dict[str, Any],
    planning_artifacts: dict[str, Any],
    execution_check: dict[str, Any],
    verify_check: dict[str, Any],
    execution_runtime: dict[str, Any],
    review_runtime: dict[str, Any],
    verify_runtime: dict[str, Any],
    interaction: dict[str, Any],
    budget: dict[str, Any],
    state_payload: dict[str, Any] | None,
    artifacts: dict[str, Any],
    compact_context: dict[str, Any] | None,
    semantic_compact: dict[str, Any] | None,
    gate_summary: dict[str, Any] | None,
    workflow_chain: dict[str, Any] | None,
    effort_profile: dict[str, Any],
    review_result_truth: dict[str, Any],
    review_evidence_truth: dict[str, Any],
    verify_artifact_truth: dict[str, Any],
    authoritative_changed_files: dict[str, Any],
) -> dict[str, Any]:
    unified_status = dict((state_payload or {}).get("unified_status") or {})
    parallel_runtime = dict(unified_status.get("parallel_runtime") or {})
    worker_statuses = list(unified_status.get("worker_statuses") or parallel_runtime.get("worker_statuses") or [])
    payload = {
        "contract_version": MERGED_CONTRACT_VERSION,
        "planning_dir": str(planning_dir),
        "planning_artifact_mode": planning_mode,
        "repo_inventory_present": bool(contract_planning.get("repo_inventory_present", False)),
        "architecture_plan_present": bool(contract_planning.get("architecture_plan_present", False)),
        "planning_requirements": dict(
            contract_planning.get("planning_requirements")
            or {
                "mode": "legacy",
                "planning_mode": "legacy",
                "surface_count": 0,
                "requires_architecture_plan": False,
                "required_artifacts": list(planning_artifacts.get("required") or []),
            }
        ),
        "planning_truth_source": str(
            contract_planning.get("planning_truth_source")
            or "PLAN.md+TASKS.md+ACCEPTANCE.md+GATE.md"
        ),
        "planning_complete": bool(
            contract_planning.get("planning_complete")
            if planning_mode == "contract_first"
            else planning_artifacts.get("all_present")
        ),
        "execution_complete": str(execution_check.get("execution_status") or "").upper() not in {"", "UNKNOWN", "MISSING", "INVALID"},
        "review_complete": _review_complete(
            planning_dir,
            review_result_truth=review_result_truth,
            review_evidence_truth=review_evidence_truth,
        ),
        "verify_complete": bool(verify_artifact_truth.get("usable"))
        and str(verify_check.get("verify_status") or "").upper() not in {"", "UNKNOWN", "MISSING", "INVALID"},
        "release_complete": _release_complete(planning_dir),
        "execution_truth_source": execution_truth_source(execution_check),
        "review_truth_source": review_truth_source(
            planning_dir,
            {
                "truth_source": resolve_effective_review_truth_source(
                    planning_dir=planning_dir,
                    review_result_truth=review_result_truth,
                    review_evidence_truth=review_evidence_truth,
                )
            },
        ),
        "verify_truth_source": verify_truth_source(
            verify_check,
            {"truth_source": resolve_effective_verify_truth_source(verify_artifact_truth)},
        ),
        "status_truth_source": STATUS_SNAPSHOT_FILENAME,
        "execution_backend": execution_runtime["execution_backend"],
        "execution_backend_capabilities": execution_runtime["execution_backend_capabilities"],
        "execution_backend_capability_truth": execution_runtime["execution_backend_capability_truth"],
        "execution_host_probe": execution_runtime["execution_host_probe"],
        "execution_guard": execution_runtime["execution_guard"],
        "reasoning_tier": str(effort_profile.get("tier") or "economy"),
        "effort_score": int(effort_profile.get("score") or 0),
        "effort_reasons": list(effort_profile.get("reasons") or []),
        "effort_profile": dict(effort_profile),
        "parallel_merge_status": str(
            unified_status.get("parallel_merge_status")
            or parallel_runtime.get("merge_status")
            or ""
        ),
        "worker_statuses": worker_statuses,
        "parallel_runtime": parallel_runtime,
        "review_mode": review_runtime["review_mode"],
        "real_review_requested": review_runtime["real_review_requested"],
        "real_review_required": review_runtime["real_review_required"],
        "fallback_used": review_runtime["fallback_used"],
        "review_quality": review_runtime["review_quality"],
        "semantic_review_performed": review_runtime["semantic_review_performed"],
        "verify_scope_mode": verify_runtime["verify_scope_mode"],
        "verify_surfaces": verify_runtime["verify_surfaces"],
        **interaction,
        "tokens_used": int(budget.get("tokens_used") or 0),
        "token_budget": budget.get("token_budget"),
        "budget_exhausted": bool(budget.get("budget_exhausted", False)),
        "state_source": "autopilot_state" if state_payload else "none",
        "state": state_payload,
        "planning_contract": _planning_contract_summary(planning_dir),
        "artifacts": artifacts,
        "artifact_truth": {
            "authoritative_changed_files": authoritative_changed_files,
            "review_result": review_result_truth,
            "review_evidence": review_evidence_truth,
            "verify_report": verify_artifact_truth,
        },
        "compact_context": compact_context,
        "semantic_compact": semantic_compact,
        "gate": gate_summary,
        "workflow_chain": workflow_chain,
        "absorption_status": absorption_status_snapshot(),
        "blocking_reason": str(
            (gate_summary or {}).get("blocking_reason")
            or unified_status.get("blocking_reason")
            or execution_check.get("reason")
            or verify_check.get("reason")
            or ""
        ),
        "next_action": str(unified_status.get("next_action") or ""),
        "first_run_hint": _compute_first_run_hint(planning_dir),
        "generated_at": _utc_now_iso(),
        "provenance": _build_cli_provenance(
            command="status",
            project_root=project_root,
            planning_dir=planning_dir,
        ),
    }
    return _attach_payload_digest(payload)


def _compute_first_run_hint(planning_dir: Path) -> dict[str, Any]:
    """D3: derive a concrete 'what to do next' suggestion from planning_dir
    state. First-run users currently see status output without obvious next
    steps; this surfaces an actionable command tied to the current artifact
    state so they don't have to guess.

    Ordered by precedence — the first matching state wins:
    1. .planning_decision_request.json present without applied_at -> decide
    2. PRD_INTAKE.json or TASK_GRAPH.json missing -> plan
    3. TASK_GRAPH.json present but no .autopilot_state.json -> work
    4. work complete but review_complete=false -> review
    5. review complete but no RELEASE.md -> release
    6. nothing actionable -> "all complete"
    """
    feature_hint = planning_dir.name if planning_dir.name not in {"planning", ""} else "<feature>"
    decision_request = planning_dir / ".planning_decision_request.json"
    if decision_request.exists():
        payload = _load_optional_json_dict(decision_request) or {}
        if not str(payload.get("applied_at") or "").strip():
            return _hint_payload(
                state="awaiting_decision",
                message="Planner emitted a decision request that needs your response.",
                command=f"kodawari decide --feature {feature_hint} --action accept",
                artifact=str(decision_request),
            )
    if not (planning_dir / "PRD_INTAKE.json").exists() or not (planning_dir / "TASK_GRAPH.json").exists():
        return _hint_payload(
            state="needs_planning",
            message="Planning artifacts missing; run `kodawari plan` to materialize them.",
            command=f"kodawari plan --feature {feature_hint} --prd <path-to-prd>",
        )
    if not (planning_dir / ".autopilot_state.json").exists():
        return _hint_payload(
            state="ready_to_work",
            message="TASK_GRAPH.json ready; run `kodawari work` to execute the first task.",
            command=f"kodawari work --feature {feature_hint}",
        )
    if not (planning_dir / "REVIEW_RESULT.json").exists() and not (planning_dir / ".review_result.json").exists():
        return _hint_payload(
            state="needs_review",
            message="Execution complete; run `kodawari review` for the dual-review gate.",
            command=f"kodawari review --feature {feature_hint}",
        )
    if not (planning_dir / "RELEASE.md").exists():
        return _hint_payload(
            state="needs_release",
            message="Review complete; run `kodawari release` to finalize.",
            command=f"kodawari release --feature {feature_hint}",
        )
    return _hint_payload(
        state="all_complete",
        message="All phases complete. Inspect RELEASE.md or run `kodawari ship-readiness` for the final checklist.",
        command="",
    )


def _hint_payload(
    *,
    state: str,
    message: str,
    command: str,
    artifact: str = "",
) -> dict[str, Any]:
    payload: dict[str, Any] = {"state": state, "message": message}
    if command:
        payload["command"] = command
    if artifact:
        payload["artifact"] = artifact
    return payload


def _persist_status_truth_and_markdown(*, planning_dir: Path, payload: dict[str, Any]) -> None:
    planning_dir.mkdir(parents=True, exist_ok=True)
    _write_json(planning_dir / STATUS_SNAPSHOT_FILENAME, payload)
    atomic_write_text(planning_dir / STATUS_MARKDOWN_FILENAME, render_status_markdown(payload))


def _cmd_status(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root).resolve()
    planning_dir = _resolve_planning_dir(args)
    try:
        raw_state = load_versioned_artifact(planning_dir / ".autopilot_state.json")
    except ArtifactSchemaVersionError as exc:
        payload = _normalized_error_payload(
            command="status",
            project_root=project_root,
            planning_dir=planning_dir,
            error=str(exc),
            error_code="artifact_schema_version_invalid",
            remediation=[
                "Run `kodawari migrate-artifacts --project-root <root> --feature <feature>` before rerunning status."
            ],
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 2
    except CorruptArtifactError as exc:
        remediation = ["Inspect or regenerate the quarantined autopilot state artifact before rerunning status."]
        if exc.quarantine_path is not None:
            remediation.append(f"Quarantined copy: {exc.quarantine_path}")
        payload = _normalized_error_payload(
            command="status",
            project_root=project_root,
            planning_dir=planning_dir,
            error=str(exc),
            error_code="artifact_corrupt",
            remediation=remediation,
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 2
    except ValueError as exc:
        if "required file not found:" in str(exc):
            raw_state = None
        else:
            payload = _normalized_error_payload(
                command="status",
                project_root=project_root,
                planning_dir=planning_dir,
                error=str(exc),
                error_code="status_failed",
                remediation=["Inspect the planning directory contents and rerun status."],
            )
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 2
    state_payload = _enrich_autopilot_state_payload(
        raw_state,
        project_root=project_root,
        planning_dir=planning_dir,
    )
    artifacts = _planning_contract_artifacts(planning_dir)
    compact_context = _load_optional_json_dict(planning_dir / "compact_context.json")
    semantic_compact = _load_optional_json_dict(planning_dir / "semantic_compact.json")
    gate_summary = _load_gate_summary(planning_dir)
    review_payload = _load_optional_json_dict(planning_dir / ".review_result.json")
    authoritative_changed_files = resolve_authoritative_changed_files(
        project_root=project_root,
        planning_dir=planning_dir,
        state_payload=raw_state,
    )
    review_result_truth = resolve_review_artifact_truth(
        project_root=project_root,
        planning_dir=planning_dir,
        authoritative_changed_files=authoritative_changed_files,
    )
    review_evidence_truth = resolve_review_evidence_truth(
        planning_dir=planning_dir,
        review_result_truth=review_result_truth,
    )
    verify_artifact_truth = resolve_verify_artifact_truth(
        project_root=project_root,
        planning_dir=planning_dir,
        authoritative_changed_files=authoritative_changed_files,
        review_result_truth=review_result_truth,
    )
    workflow_chain = bind_effective_gate_result(
        load_workflow_chain_snapshot(planning_dir),
        gate_summary,
        state_payload=state_payload,
    )
    planning_mode = detect_status_planning_mode(planning_dir)
    contract_planning = (
        build_contract_first_planning_status(planning_dir)
        if planning_mode == "contract_first"
        else {}
    )
    planning_artifacts = _required_planning_artifacts_status(planning_dir, include_delivery_artifacts=False)
    execution_check = _resolve_execution_check(planning_dir)
    verify_check = _resolve_verify_check(
        planning_dir=planning_dir,
        workflow_chain=workflow_chain,
        semantic_compact=semantic_compact,
        state_payload=state_payload,
    )
    budget = budget_snapshot(
        state_payload=state_payload,
        semantic_compact=semantic_compact,
    )
    execution_runtime = execution_runtime_summary(execution_check)
    review_runtime = review_runtime_summary(
        planning_dir=planning_dir,
        review_payload=review_payload,
        workflow_chain=workflow_chain,
    )
    verify_runtime = verify_runtime_summary(verify_check)
    interaction = _status_interaction_payload(
        planning_dir=planning_dir,
        state_payload=state_payload,
        execution_check=execution_check,
        semantic_compact=semantic_compact,
    )
    effort_profile = _status_effort_profile(
        planning_dir=planning_dir,
        state_payload=state_payload,
    )
    payload = _build_status_payload(
        planning_dir=planning_dir,
        project_root=project_root,
        planning_mode=planning_mode,
        contract_planning=contract_planning,
        planning_artifacts=planning_artifacts,
        execution_check=execution_check,
        verify_check=verify_check,
        execution_runtime=execution_runtime,
        review_runtime=review_runtime,
        verify_runtime=verify_runtime,
        interaction=interaction,
        budget=budget,
        state_payload=state_payload,
        artifacts=artifacts,
        compact_context=compact_context,
        semantic_compact=semantic_compact,
        gate_summary=gate_summary,
        workflow_chain=workflow_chain,
        effort_profile=effort_profile,
        review_result_truth=review_result_truth,
        review_evidence_truth=review_evidence_truth,
        verify_artifact_truth=verify_artifact_truth,
        authoritative_changed_files=authoritative_changed_files,
    )
    _persist_status_truth_and_markdown(planning_dir=planning_dir, payload=payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


__all__ = ["_cmd_status", "_enrich_autopilot_state_payload", "_planning_contract_artifacts", "_planning_contract_summary", "_resolve_planning_dir"]


