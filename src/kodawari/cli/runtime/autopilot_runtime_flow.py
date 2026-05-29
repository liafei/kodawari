"""Runtime bootstrap helpers for the autopilot command."""

from __future__ import annotations

import argparse
import ctypes
from datetime import datetime, timedelta, timezone
import logging
import os
import shutil
import uuid
from pathlib import Path
from typing import Any

from kodawari.cli.contract.autopilot_contract_bridge import (
    AutopilotPlanningBridgeError,
    AutopilotPlanningSnapshot,
    CONTRACT_FIRST_AUTOPILOT_ARTIFACTS,
    ensure_contract_first_planning,
    resolve_autopilot_prd_path,
)
from kodawari.cli.contract.contract_first_backlog import render_task_graph_tasks_markdown
from kodawari.cli.delivery.workflow_chain import parse_task_backlog
from kodawari.autopilot.execution.parallel_coordinator import (
    build_parallel_coordinator_plan,
    build_parallel_runtime_snapshot,
)
from kodawari.autopilot.execution.execution_artifacts import is_test_environment
from kodawari.autopilot.execution.worktree_manager import allocate_worker_worktrees
from kodawari.autopilot.planning.execution_readiness import (
    collect_field_evidence,
    evaluate_execution_readiness,
    write_execution_readiness,
)
from kodawari.cli.runtime.autopilot_workflow_runtime import (
    build_workflow_chain_runtime,
    resolve_primary_task,
)
from kodawari.cli.io_atomic import load_json_dict

logger = logging.getLogger(__name__)
_TASK_CLAIM_TTL_SECONDS = 30 * 60


def arg_text(args: argparse.Namespace, name: str) -> str:
    return str(getattr(args, name, "") or "").strip()


def explicit_prd_requested(args: argparse.Namespace) -> bool:
    return bool(arg_text(args, "prd"))


def explicit_planning_input_requested(args: argparse.Namespace) -> bool:
    return bool(arg_text(args, "task") or arg_text(args, "prd"))


def contract_first_artifacts_present(planning_dir: Path) -> bool:
    return any((planning_dir / name).exists() for name in CONTRACT_FIRST_AUTOPILOT_ARTIFACTS)


def requirements_alias_prefers_contract_first(args: argparse.Namespace) -> bool:
    raw = arg_text(args, "requirements_file")
    if not raw:
        return False
    return Path(raw).suffix.lower() in {".md", ".markdown"}


def prefer_contract_first_runtime(args: argparse.Namespace, planning_dir: Path) -> bool:
    if explicit_planning_input_requested(args):
        return True
    if requirements_alias_prefers_contract_first(args):
        return True
    return contract_first_artifacts_present(planning_dir)


def resolve_optional_input_path(raw: str) -> Path | None:
    text = str(raw or "").strip()
    if not text:
        return None
    candidate = Path(text).resolve()
    if candidate.exists() and candidate.is_file():
        return candidate
    raise AutopilotPlanningBridgeError(
        error_code="prd_missing",
        message=f"PRD file not found: {candidate}",
        remediation=["Provide a valid `--prd <path>` or `--requirements-file <path>` before rerunning autopilot."],
    )


def read_requirements_text(path: Path | None) -> str:
    if path is None or not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="ignore")


def build_default_task(feature: str, requirements_text: str) -> tuple[str, str]:
    task_label = f"T001: Implement feature {feature}"
    scope = requirements_text.strip().splitlines()[0].strip() if requirements_text.strip() else f"Feature scope: {feature}"
    return task_label, scope


def build_planning_stage_task(feature: str, requirements_text: str) -> tuple[str, str]:
    scope = requirements_text.strip().splitlines()[0].strip() if requirements_text.strip() else f"Planning scope: {feature}"
    return f"PLAN: Prepare workflow for {feature}", scope


def ensure_text_file(path: Path, content: str) -> None:
    if not path.exists():
        path.write_text(content, encoding="utf-8")


def requirements_summary(feature: str, requirements_text: str) -> str:
    stripped = requirements_text.strip()
    if not stripped:
        return f"Feature: {feature}"
    return stripped.splitlines()[0].strip()


def plan_markdown(feature: str, summary: str) -> str:
    return (
        "\n".join(
            [
                f"# PLAN ({feature})",
                "",
                "## Scope",
                f"- {summary}",
                "",
                "## Contract",
                "- This folder follows merged workflow-claude/kodawari planning contract.",
            ]
        )
        + "\n"
    )


def tasks_markdown(feature: str, task_label: str) -> str:
    return (
        "\n".join(
            [
                f"# TASKS ({feature})",
                "",
                f"- [ ] {task_label}",
            ]
        )
        + "\n"
    )


def acceptance_markdown(feature: str) -> str:
    return (
        "\n".join(
            [
                f"# ACCEPTANCE ({feature})",
                "",
                "- [ ] Implementation changes are present in scoped files",
                "- [ ] Scoped verify command passes",
                "- [ ] Gate report is generated in planning directory",
            ]
        )
        + "\n"
    )


def planning_artifact_paths(planning_dir: Path) -> dict[str, Path]:
    return {
        "PLAN.md": planning_dir / "PLAN.md",
        "TASKS.md": planning_dir / "TASKS.md",
        "ACCEPTANCE.md": planning_dir / "ACCEPTANCE.md",
        "GATE.md": planning_dir / "GATE.md",
        ".gate_result.json": planning_dir / ".gate_result.json",
        ".workflow_chain.json": planning_dir / ".workflow_chain.json",
    }


def ensure_planning_contract_artifacts(
    *,
    planning_dir: Path,
    feature: str,
    task_label: str,
    requirements_text: str,
) -> dict[str, dict[str, Any]]:
    paths = planning_artifact_paths(planning_dir)
    summary = requirements_summary(feature, requirements_text)
    ensure_text_file(paths["PLAN.md"], plan_markdown(feature, summary))
    contract_first_tasks = _contract_first_tasks_markdown(
        planning_dir=planning_dir,
        feature=feature,
    )
    if contract_first_tasks is not None:
        existing_tasks = paths["TASKS.md"].read_text(encoding="utf-8") if paths["TASKS.md"].exists() else None
        if existing_tasks != contract_first_tasks:
            paths["TASKS.md"].write_text(contract_first_tasks, encoding="utf-8")
    else:
        ensure_text_file(paths["TASKS.md"], tasks_markdown(feature, task_label))
    ensure_text_file(paths["ACCEPTANCE.md"], acceptance_markdown(feature))
    return {
        name: {"path": str(path), "exists": path.exists()}
        for name, path in paths.items()
    }


def _contract_first_tasks_markdown(
    *,
    planning_dir: Path,
    feature: str,
) -> str | None:
    completed_ids = {
        str(item.get("task_id") or "").strip().upper()
        for item in parse_task_backlog(planning_dir / "TASKS.md", include_completed=True)
        if bool(item.get("completed"))
    }
    return render_task_graph_tasks_markdown(
        planning_dir,
        feature=feature,
        completed_task_ids=completed_ids,
    )


def resolve_requirements_file(args: argparse.Namespace) -> Path | None:
    if explicit_prd_requested(args):
        return resolve_autopilot_prd_path(args)
    return resolve_optional_input_path(arg_text(args, "requirements_file"))


def has_legacy_runtime_inputs(args: argparse.Namespace, planning_dir: Path, state_path: Path) -> bool:
    if str(getattr(args, "task_label", "") or "").strip():
        return True
    return state_path.exists() or (planning_dir / "TASKS.md").exists()


def should_use_contract_first_bridge(
    args: argparse.Namespace,
    *,
    planning_dir: Path,
    state_path: Path,
) -> bool:
    if prefer_contract_first_runtime(args, planning_dir):
        return True
    if has_legacy_runtime_inputs(args, planning_dir, state_path):
        return False
    return requirements_alias_prefers_contract_first(args)


def maybe_bootstrap_contract_first(
    *,
    args: argparse.Namespace,
    project_root: Path,
    planning_dir: Path,
    state_path: Path,
    feature: str,
    requirements_file: Path | None,
) -> AutopilotPlanningSnapshot | None:
    if not should_use_contract_first_bridge(
        args,
        planning_dir=planning_dir,
        state_path=state_path,
    ):
        return None
    # Explicit --planner-route, when set to model/generic, overrides the legacy
    # heuristic. 'auto' (default) preserves the previous behavior of inferring
    # from --task / PLANNING_CONVERSATION.json so callers that rely on the
    # implicit detection still work unchanged.
    explicit_route = str(getattr(args, "planner_route", "") or "auto").strip().lower()
    if explicit_route == "model":
        use_model_planning = True
    elif explicit_route == "generic":
        use_model_planning = False
    else:
        use_model_planning = (
            explicit_planning_input_requested(args)
            or (planning_dir / "PLANNING_CONVERSATION.json").exists()
            or (planning_dir / "TASK_GRAPH.json").exists()
        )
        if is_test_environment() and os.environ.get("WORKFLOW_FORCE_MODEL_PLANNING", "") != "1":
            use_model_planning = (planning_dir / "PLANNING_CONVERSATION.json").exists()
    return ensure_contract_first_planning(
        project_root=project_root,
        planning_dir=planning_dir,
        feature=feature,
        prd_path=requirements_file,
        task_direction=arg_text(args, "task"),
        use_model_planning=use_model_planning,
        force_replan=bool(getattr(args, "replan", False)),
    )


def load_or_init_state(
    *,
    state_path: Path,
    feature: str,
    project_root: Path,
    state_cls: Any,
) -> Any:
    if state_path.exists():
        try:
            return state_cls.load(state_path)
        except Exception:
            logger.warning("failed to load existing autopilot state; reinitializing", exc_info=True)
    return state_cls(feature=feature, project_root=project_root)


def resolve_init_stage(state: Any) -> Any | None:
    current_stage = getattr(state, "current_stage", None)
    stage_cls = type(current_stage)
    return getattr(stage_cls, "INIT", None)


def reset_state_for_fresh_run(state: Any) -> None:
    # Stamp a fresh run_id and clear ALL session-scoped fields so that a
    # 2-week-old `error_events` / `warning_noise_*` from a prior crashed
    # session does not bleed into this one. Mirrors task-run's run_id discipline.
    if hasattr(state, "run_id"):
        state.run_id = uuid.uuid4().hex
    state.cycle = 0
    state.stop_reason = None
    state.final_status = None
    state.last_error = None
    state.last_stage_status = None
    state.error_history = []
    if hasattr(state, "error_events"):
        state.error_events = []
    if hasattr(state, "verify_setup_recovery_attempted"):
        state.verify_setup_recovery_attempted = 0
    if hasattr(state, "verify_setup_recovery_succeeded"):
        state.verify_setup_recovery_succeeded = 0
    if hasattr(state, "verify_setup_recovery_last_error"):
        state.verify_setup_recovery_last_error = None
    if hasattr(state, "warning_noise_events"):
        state.warning_noise_events = 0
    if hasattr(state, "warning_noise_degraded_events"):
        state.warning_noise_degraded_events = 0
    if hasattr(state, "warning_noise_by_task"):
        state.warning_noise_by_task = {}
    state.active_task = None
    state.active_subtask = None
    state.active_pid = None
    state.active_attempt = None
    state.subtasks = {}
    if hasattr(state, "parallel_runtime"):
        state.parallel_runtime = {}
    init_stage = resolve_init_stage(state)
    if init_stage is not None:
        state.current_stage = init_stage


def _parse_claim_expiry(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _claim_is_active(claim: dict[str, Any], *, now: datetime) -> bool:
    expires = _parse_claim_expiry(claim.get("claim_expires_at"))
    if expires is None:
        return False
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires <= now:
        return False
    local_pid_alive = _claim_local_pid_alive(claim)
    if local_pid_alive is False:
        return False
    return True


def _claim_local_pid_alive(claim: dict[str, Any]) -> bool | None:
    claimed_by = str(claim.get("claimed_by") or "").strip().lower()
    if not claimed_by.startswith("pid:"):
        return None
    try:
        pid = int(claimed_by.split(":", 1)[1])
    except ValueError:
        return None
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        return _windows_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _windows_pid_alive(pid: int) -> bool:
    kernel32 = ctypes.windll.kernel32
    process_query_limited_information = 0x1000
    still_active = 259
    handle = kernel32.OpenProcess(process_query_limited_information, False, int(pid))
    if not handle:
        return False
    exit_code = ctypes.c_ulong()
    try:
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True
        return int(exit_code.value) == still_active
    finally:
        kernel32.CloseHandle(handle)


def _claim_planning_task(
    *,
    state: Any,
    state_path: Path,
    planning_snapshot: AutopilotPlanningSnapshot | None,
) -> None:
    if planning_snapshot is None:
        return
    task_id = str(getattr(planning_snapshot, "primary_task_id", "") or "").strip().upper()
    if not task_id:
        return
    now = datetime.now(timezone.utc)
    current_claim = dict(getattr(state, "task_claim", {}) or {})
    current_task = str(current_claim.get("task_id") or "").strip().upper()
    current_run = str(current_claim.get("run_id") or "").strip()
    state_run = str(getattr(state, "run_id", "") or "").strip()
    if current_claim and _claim_is_active(current_claim, now=now) and current_run != state_run:
        raise AutopilotPlanningBridgeError(
            error_code="task_claimed_by_other",
            message=f"Task graph is already claimed by another runner: {current_task or 'unknown task'}.",
            remediation=[
                "Wait for the current runner to finish, or retry after the claim expires.",
                "If the runner crashed, inspect .autopilot_state.json and remove an expired task_claim only after confirming no workflow is active.",
            ],
            details={"task_claim": current_claim},
        )
    expires = now + timedelta(seconds=_TASK_CLAIM_TTL_SECONDS)
    state.task_claim = {
        "schema_version": "workflow.task_claim.v1",
        "task_id": task_id,
        "task_label": str(getattr(planning_snapshot, "task_label", "") or ""),
        "run_id": state_run,
        "claimed_by": f"pid:{os.getpid()}",
        "claimed_at": now.isoformat(),
        "claim_expires_at": expires.isoformat(),
        "ttl_seconds": _TASK_CLAIM_TTL_SECONDS,
    }
    try:
        state.save(state_path)
    except Exception as exc:
        raise AutopilotPlanningBridgeError(
            error_code="task_claim_conflict",
            message="Unable to claim the selected task before execution.",
            remediation=["Retry after the other workflow runner exits or its task claim expires."],
            details={"task_id": task_id, "error": str(exc)},
        ) from exc


def build_engine_config(
    *,
    args: argparse.Namespace,
    project_root: Path,
    requirements_file: Path | None,
    task_card_path: Path | None,
    config_cls: Any,
) -> Any:
    config_kwargs = {
        "project_root": project_root,
        "feature": args.feature,
        "task_direction": str(getattr(args, "task", "") or ""),
        "requirements_file": requirements_file,
        "task_card_path": task_card_path,
        "contract_first_mode": "warn" if task_card_path is not None else "off",
        "profile": getattr(args, "profile", "profiles/generic.yaml"),
        "verify_cmd": getattr(args, "verify_cmd", "pytest -q"),
        "max_cycles": int(getattr(args, "max_cycles", 8) or 8),
        "token_budget": int(getattr(args, "token_budget", 300000) or 300000),
        "executor_backend": str(getattr(args, "executor_backend", "") or ""),
        "executor_command": str(getattr(args, "executor_command", "") or ""),
        "self_review_backend": str(getattr(args, "self_review_backend", "") or ""),
        "self_review_command": str(getattr(args, "self_review_command", "") or ""),
        "real_peer_review": bool(getattr(args, "real_peer_review", False) or getattr(args, "real_opus_review", False)),
        "require_real_peer_review": bool(getattr(args, "require_real_peer_review", False) or getattr(args, "require_real_opus_review", False)),
        "opus_reviewer_backend": str(getattr(args, "opus_reviewer_backend", "") or ""),
        "executor_model": str(getattr(args, "executor_model", "") or ""),
        "reviewer_backend": str(getattr(args, "reviewer_backend", "") or ""),
        "reviewer_model": str(getattr(args, "reviewer_model", "") or ""),
        "reviewer_api_format": str(getattr(args, "reviewer_api_format", "") or ""),
        "reviewer_base_url": str(getattr(args, "reviewer_base_url", "") or ""),
        "peer_review_max_tokens": int(getattr(args, "peer_review_max_tokens", 4096) or 4096),
        "rollback_on_failure": bool(getattr(args, "rollback_on_failure", False)),
        "max_verify_retries": int(getattr(args, "max_verify_retries", 2) or 2),
        "collaboration_max_rounds": int(getattr(args, "collaboration_max_rounds", 6) or 6),
    }
    try:
        return config_cls(**config_kwargs)
    except TypeError:
        return config_cls(project_root=project_root, feature=args.feature)


def resolve_planning_paths(project_root: Path, feature: str) -> tuple[Path, Path, Path]:
    planning_dir = (project_root / "planning" / feature).resolve()
    planning_dir.mkdir(parents=True, exist_ok=True)
    state_path = planning_dir / ".autopilot_state.json"
    rounds_path = planning_dir / ".autopilot_rounds.jsonl"
    return planning_dir, state_path, rounds_path


def runtime_task(
    *,
    args: argparse.Namespace,
    feature: str,
    requirements_text: str,
    planning_snapshot: AutopilotPlanningSnapshot | None,
) -> tuple[str, str]:
    if planning_snapshot is not None:
        return planning_snapshot.task_label, planning_snapshot.task_scope
    return resolve_primary_task(
        args,
        feature=feature,
        requirements_text=requirements_text,
        default_task_factory=build_default_task,
        planning_task_factory=build_planning_stage_task,
    )


def contract_first_planning_artifacts(
    planning_snapshot: AutopilotPlanningSnapshot | None,
) -> dict[str, dict[str, Any]]:
    if planning_snapshot is None:
        return {}
    return {
        name: {"path": path, "exists": Path(path).exists()}
        for name, path in dict(planning_snapshot.artifacts).items()
    }


def autopilot_peer_review_enabled(
    args: argparse.Namespace,
    planning_snapshot: AutopilotPlanningSnapshot | None,
) -> bool:
    del planning_snapshot
    explicit = getattr(args, "enable_peer_review", None)
    if explicit is not None:
        return bool(explicit)
    return True


def _mark_primary_task_complete_if_pass(state: Any, *, task_label: str, run_result: dict[str, Any]) -> None:
    reason = str(run_result.get("reason") or "").strip().upper()
    if reason not in {"PROCEED_TO_GATE", "PIPELINE_FINISH"}:
        return
    completed = list(getattr(state, "completed_tasks", []) or [])
    if task_label and task_label not in completed:
        completed.append(task_label)
        state.completed_tasks = completed
    # A task can recover from an earlier executor/review failure inside the
    # same collaboration loop. Once the loop reaches a pass handoff, the
    # current blocking fields must stop shadowing the successful run.
    state.last_error = None
    if hasattr(state, "verify_setup_recovery_last_error"):
        state.verify_setup_recovery_last_error = None
    _clear_task_claim(state)


def _clear_task_claim(state: Any) -> None:
    if hasattr(state, "task_claim"):
        state.task_claim = {}


def bootstrap_command_runtime(
    *,
    args: argparse.Namespace,
    runtime: tuple[Any, Any, Any],
) -> dict[str, Any]:
    autopilot_config_cls, autopilot_engine_cls, autopilot_state_cls = runtime
    project_root = Path(args.project_root).resolve()
    feature = str(args.feature)
    planning_dir, state_path, rounds_path = resolve_planning_paths(project_root, feature)
    requirements_file = resolve_requirements_file(args)
    planning_snapshot = maybe_bootstrap_contract_first(
        args=args,
        project_root=project_root,
        planning_dir=planning_dir,
        state_path=state_path,
        feature=feature,
        requirements_file=requirements_file,
    )
    state = load_or_init_state(
        state_path=state_path,
        feature=args.feature,
        project_root=project_root,
        state_cls=autopilot_state_cls,
    )
    reset_state_for_fresh_run(state)
    _claim_planning_task(
        state=state,
        state_path=state_path,
        planning_snapshot=planning_snapshot,
    )
    config = build_engine_config(
        args=args,
        project_root=project_root,
        requirements_file=requirements_file,
        task_card_path=planning_snapshot.task_card_path if planning_snapshot is not None else None,
        config_cls=autopilot_config_cls,
    )
    requirements_text = read_requirements_text(requirements_file)
    engine = autopilot_engine_cls(config=config, requirements_text=requirements_text, state=state)
    return {
        "feature": feature,
        "project_root": project_root,
        "planning_dir": planning_dir,
        "state_path": state_path,
        "rounds_path": rounds_path,
        "state": state,
        "requirements_text": requirements_text,
        "planning_snapshot": planning_snapshot,
        "engine": engine,
    }


def execute_autopilot_runtime(
    *,
    args: argparse.Namespace,
    engine: Any,
    project_root: Path,
    planning_dir: Path,
    feature: str,
    requirements_text: str,
    planning_snapshot: AutopilotPlanningSnapshot | None,
) -> tuple[Any, dict[str, Any], dict[str, dict[str, Any]], dict[str, Any] | None, list[dict[str, Any]]]:
    peer_review_enabled = autopilot_peer_review_enabled(args, planning_snapshot)
    if getattr(args, "enable_peer_review", None) is None:
        setattr(args, "enable_peer_review", peer_review_enabled)
    task_label, task_scope = runtime_task(
        args=args,
        feature=feature,
        requirements_text=requirements_text,
        planning_snapshot=planning_snapshot,
    )
    planning_artifacts = ensure_planning_contract_artifacts(
        planning_dir=planning_dir,
        feature=feature,
        task_label=task_label,
        requirements_text=requirements_text,
    )
    planning_artifacts.update(contract_first_planning_artifacts(planning_snapshot))
    plan = engine.generate_execution_plan()
    readiness = _evaluate_active_task_readiness(
        project_root=project_root,
        planning_snapshot=planning_snapshot,
    )
    if readiness and str(readiness.get("status") or "").upper() == "BLOCKED":
        replanned = _maybe_auto_replan_on_precondition_block(
            args=args,
            engine=engine,
            project_root=project_root,
            planning_dir=planning_dir,
            feature=feature,
            requirements_text=requirements_text,
            readiness=readiness,
        )
        if replanned is not None:
            planning_snapshot = replanned
            plan = engine.generate_execution_plan()
            readiness = _evaluate_active_task_readiness(
                project_root=project_root,
                planning_snapshot=planning_snapshot,
            )
    if readiness and str(readiness.get("status") or "").upper() == "BLOCKED":
        write_execution_readiness(planning_dir, readiness)
        _clear_task_claim(engine.state)
        run_result = _blocked_by_readiness_result(
            task_label=task_label,
            readiness=readiness,
        )
        if hasattr(engine.state, "last_error"):
            engine.state.last_error = str(readiness.get("suggested_next_task") or readiness.get("reason") or "")
        if hasattr(engine.state, "last_stage_status"):
            engine.state.last_stage_status = "blocked_by_precondition"
        run_result["parallel_runtime"] = _disabled_parallel_runtime("readiness_blocked")
        return plan, run_result, planning_artifacts, None, []
    if readiness:
        write_execution_readiness(planning_dir, readiness)
    run_result = engine.run_collaboration_loop(
        task_label=task_label,
        task_scope=task_scope,
        enable_peer_review=peer_review_enabled,
    )
    _mark_primary_task_complete_if_pass(engine.state, task_label=task_label, run_result=run_result)
    workflow_chain, task_cycle_rounds = build_workflow_chain_runtime(
        args=args,
        engine=engine,
        project_root=project_root,
        planning_dir=planning_dir,
        feature=feature,
        upstream_task_label=task_label,
        upstream_payload=run_result,
    )
    parallel_runtime_enabled = bool(getattr(args, "parallel_runtime_enabled", True))
    if parallel_runtime_enabled:
        parallel_workers = int(getattr(args, "parallel_workers", 2) or 2)
        parallel_plan = build_parallel_coordinator_plan(
            state=engine.state,
            feature=feature,
            planning_dir=planning_dir,
            max_workers=parallel_workers,
        )
        worktree_snapshot = allocate_worker_worktrees(
            planning_dir=planning_dir,
            workers=list(parallel_plan.get("workers") or []),
            mode="directory_isolation",
        )
        parallel_runtime = build_parallel_runtime_snapshot(
            plan=parallel_plan,
            state=engine.state,
            worktree_snapshot=worktree_snapshot,
        )
    else:
        shutil.rmtree((planning_dir / ".parallel_workers").resolve(), ignore_errors=True)
        parallel_runtime = _disabled_parallel_runtime("policy.parallel_runtime_enabled=false")
    run_result["parallel_runtime"] = parallel_runtime
    if hasattr(engine.state, "parallel_runtime"):
        engine.state.parallel_runtime = dict(parallel_runtime)
    return plan, run_result, planning_artifacts, workflow_chain, task_cycle_rounds


def _disabled_parallel_runtime(reason: str) -> dict[str, Any]:
    return {
        "schema_version": "parallel.runtime.v1",
        "strategy": "disabled",
        "workers": [],
        "assignments": [],
        "worktree": {},
        "merge_status": "DISABLED",
        "totals": {
            "pending": 0,
            "running": 0,
            "failed": 0,
            "done": 0,
        },
        "generated_at": "",
        "reason": reason,
    }


_PRECONDITION_REPLAN_HINT_FILENAME = ".precondition_replan_hint.json"
_PRECONDITION_REPLAN_FLAG_KEY = "_workflow_precondition_replan_attempted"


def _maybe_auto_replan_on_precondition_block(
    *,
    args: argparse.Namespace,
    engine: Any,
    project_root: Path,
    planning_dir: Path,
    feature: str,
    requirements_text: str,
    readiness: dict[str, Any],
) -> AutopilotPlanningSnapshot | None:
    """When readiness says BLOCKED, optionally trigger a one-shot re-plan with
    a structured hint so the planner can insert prerequisite tasks.

    Gating: env var ``WORKFLOW_AUTOPILOT_AUTO_REPLAN_ON_PRECONDITION=1``.
    Only one retry per autopilot invocation (state is stamped on engine.state
    via ``_workflow_precondition_replan_attempted`` so a second BLOCK in the
    same run does not loop).
    """

    flag = str(os.environ.get("WORKFLOW_AUTOPILOT_AUTO_REPLAN_ON_PRECONDITION") or "").strip().lower()
    if flag not in {"1", "true", "yes", "on"}:
        return None
    if getattr(engine.state, _PRECONDITION_REPLAN_FLAG_KEY, False):
        return None
    setattr(engine.state, _PRECONDITION_REPLAN_FLAG_KEY, True)

    missing_fields = list(readiness.get("missing_field_preconditions") or [])
    field_evidence = collect_field_evidence(missing_fields, project_root) if missing_fields else {}
    hint_payload = {
        "schema_version": "planning.precondition_hint.v2",
        "feature": feature,
        "missing_field_preconditions": missing_fields,
        "missing_symbol_preconditions": list(readiness.get("missing_symbol_preconditions") or []),
        "checked_preconditions": list(readiness.get("checked_preconditions") or []),
        "suggested_next_task": str(readiness.get("suggested_next_task") or ""),
        "field_evidence": field_evidence,
    }
    try:
        import json as _json
        (planning_dir / _PRECONDITION_REPLAN_HINT_FILENAME).write_text(
            _json.dumps(hint_payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError:
        return None

    # Re-plan needs *something* to plan from. Order of fallbacks:
    #   1. --prd <path> on the autopilot invocation
    #   2. The task_direction the user passed (requirements_text)
    #   3. The task_direction baked into PLANNING_CONVERSATION.json from the
    #      previous run — autopilot was almost always invoked without --prd
    #      because planning was already complete; we don't want to lose that
    #      task_direction just because we're re-planning
    prd_path = resolve_autopilot_prd_path(args)
    effective_task_direction = str(requirements_text or "").strip()
    if not effective_task_direction:
        try:
            import json as _json
            existing = _json.loads(
                (planning_dir / "PLANNING_CONVERSATION.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            existing = {}
        if isinstance(existing, dict):
            effective_task_direction = str(existing.get("task_direction") or "").strip()
    if not effective_task_direction and prd_path is None:
        logger.warning(
            "auto-replan-on-precondition: no task_direction or PRD available; skipping replan"
        )
        return None
    # Force model planning so the planner consumes the precondition hint via
    # planning context. Generic bootstrap is strict about PRD presence and
    # does not yet wire the hint into its prompt rendering.
    try:
        snapshot = ensure_contract_first_planning(
            project_root=project_root,
            planning_dir=planning_dir,
            feature=feature,
            prd_path=prd_path,
            task_direction=effective_task_direction,
            use_model_planning=True,
            force_replan=True,
        )
    except AutopilotPlanningBridgeError as exc:
        logger.warning("auto-replan-on-precondition: planner failed: %s", exc)
        return None
    return snapshot


def _evaluate_active_task_readiness(
    *,
    project_root: Path,
    planning_snapshot: AutopilotPlanningSnapshot | None,
) -> dict[str, Any]:
    if planning_snapshot is None or not planning_snapshot.task_card_path:
        return {}
    task_card = load_json_dict(Path(planning_snapshot.task_card_path))
    if not task_card:
        return {}
    return evaluate_execution_readiness(project_root=project_root, task_card=task_card)


def _blocked_by_readiness_result(*, task_label: str, readiness: dict[str, Any]) -> dict[str, Any]:
    reason = str(readiness.get("suggested_next_task") or readiness.get("reason") or "precondition blocked")
    return {
        "stopped": True,
        "reason": "BLOCKED_BY_PRECONDITION",
        "task": task_label,
        "rounds": [],
        "changed_files": [],
        "blocking_reason": reason,
        "execution_readiness": dict(readiness),
        "peer_review_summary": {"review_count": 0, "approved": False, "skipped": True},
        "review_rounds_used": 0,
        "execution_result": {
            "schema_version": "execution.result.v1",
            "status": "BLOCKED",
            "error_code": "BLOCKED_BY_PRECONDITION",
            "blocking_reason": reason,
            "changed_files": [],
        },
        "verify_check": {"status": "SKIPPED", "blocking_reason": reason},
        "gate_check": {"total_status": "SKIPPED", "blocking_reason": reason},
        "unified_status": {
            "stage_status": "blocked_by_precondition",
            "final_status": "BLOCKED",
            "stop_reason": "BLOCKED_BY_PRECONDITION",
            "blocking_reason": reason,
        },
    }


