"""Autopilot CLI wrapper with merged planning contract semantics."""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import signal
import subprocess
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)

from kodawari.autopilot.planning.complexity_detector import (
    ComplexityInput,
    detect_complexity,
    model_advisor_tier_classifier,
)
from kodawari.autopilot.core.delivery_report import generate_delivery_report
from kodawari.autopilot.planning.lane_config import lane_for
from kodawari.autopilot.planning.lane_observation import (
    ActualRunSignals,
    build_lane_observation,
    to_learning_event,
    write_lane_observation,
)
from kodawari.autopilot.engine.workflow_policy import (
    UserPolicyOverrides,
    resolve_workflow_policy,
)
from kodawari.cli.contract.autopilot_contract_bridge import AutopilotPlanningBridgeError
from kodawari.cli.runtime.autopilot_interaction_state import build_interaction_snapshot
from kodawari.cli.runtime import autopilot_lane_signals as _lane_signals
from kodawari.cli.runtime.autopilot_decision_bridge import (
    build_release_decision_spec,
    resolve_decision_payload_for_spec,
)
from kodawari.cli.runtime.autopilot_loop import (
    capture_autopilot_worktree_preflight,
    persist_command_runtime,
    resolve_reliable_changed_files,
)
from kodawari.cli.runtime.autopilot_release_flow import (
    MERGED_CONTRACT_VERSION,
    REQUIRED_PLANNING_ARTIFACTS,
    build_autopilot_payload,
    maybe_pause_for_planning_decision,
    maybe_run_release_tail,
)
from kodawari.cli.runtime.autopilot_release_runtime import run_autopilot_release_tail
from kodawari.cli.runtime.autopilot_release_runtime import AutopilotReleaseTailConfig
from kodawari.cli.runtime.autopilot_runtime_flow import (
    build_default_task,
    bootstrap_command_runtime,
    execute_autopilot_runtime,
    explicit_planning_input_requested,
    load_or_init_state as _load_or_init_state,
    read_requirements_text,
    resolve_planning_paths,
    resolve_requirements_file,
)
from kodawari.cli.runtime.autopilot_workflow_runtime import resolve_primary_task
from kodawari.cli.evidence.changed_files_truth import WORKTREE_BASELINE_FILENAME
from kodawari.cli.evidence.artifact_truth import build_run_truth, write_run_truth
from kodawari.cli.evidence.self_repair import (
    SELF_REPAIR_FILENAME,
    build_self_repair_proposal,
    write_self_repair_markdown,
    write_self_repair_proposal,
)
from kodawari.cli.evidence.self_repair_execute import (
    ENV_AUTO_EXECUTE as _SELF_REPAIR_ENV_AUTO_EXECUTE,
    ENV_DEPTH as _SELF_REPAIR_ENV_DEPTH,
    ENV_ENABLED as _SELF_REPAIR_ENV_ENABLED,
    execute_self_repair_proposal,
    write_execution_record as write_self_repair_execution_record,
)
from kodawari.cli.status.status_cmd import _git_changed_files as _status_git_changed_files
from kodawari.cli.delivery.workflow_chain import write_workflow_chain_snapshot
from kodawari.cli.io_atomic import load_json_dict

__all__ = [
    "MERGED_CONTRACT_VERSION",
    "REQUIRED_PLANNING_ARTIFACTS",
    "_load_or_init_state",
    "run_autopilot_command",
    "run_autopilot_release_tail",
]


def _emit_unavailable_payload(missing_module: str | None) -> int:
    print(
        json.dumps(
            {
                "status": "unavailable",
                "message": "autopilot engine is not restored yet",
                "missing_module": missing_module,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 2


def _import_autopilot_runtime() -> tuple[Any, Any, Any] | None:
    try:
        from kodawari.autopilot.engine.engine import AutopilotConfig, AutopilotEngine
        from kodawari.autopilot.core.state import AutopilotState
    except ModuleNotFoundError:
        return None
    return AutopilotConfig, AutopilotEngine, AutopilotState


def _restore_legacy_active_task(
    *,
    args: argparse.Namespace,
    feature: str,
    requirements_text: str,
    state: Any,
    planning_snapshot: Any,
) -> None:
    has_model_conversation = bool(
        planning_snapshot is not None
        and str(dict(planning_snapshot.artifacts).get("PLANNING_CONVERSATION.json") or "").strip()
    )
    if planning_snapshot is not None and (has_model_conversation or explicit_planning_input_requested(args)):
        return
    task_label, _ = resolve_primary_task(
        args,
        feature=feature,
        requirements_text=requirements_text,
        default_task_factory=build_default_task,
        planning_task_factory=build_default_task,
    )
    state.active_task = task_label


def _resolve_prebootstrap_context(
    args: argparse.Namespace,
) -> tuple[str, Path, str, Any, Any]:
    """Resolve pre-bootstrap context: feature, project root, requirements, decisions.

    Returns: (feature, project_root, requirements_text, complexity_decision, workflow_policy)
    """
    pre_feature = str(getattr(args, "feature", "") or "")
    pre_project_root = Path(getattr(args, "project_root", ".") or ".").resolve()
    pre_requirements_file = resolve_requirements_file(args)
    pre_requirements_text = read_requirements_text(pre_requirements_file)
    pre_requested_tier = str(getattr(args, "tier", "auto") or "auto").strip().lower()

    if pre_requested_tier == "auto":
        try:
            pre_changed_files = tuple(_status_git_changed_files(pre_project_root))
        except Exception:
            pre_changed_files = ()
    else:
        pre_changed_files = ()

    decision, policy = _resolve_tier_and_policy(
        args=args,
        feature=pre_feature,
        requirements_text=pre_requirements_text,
        changed_files=pre_changed_files,
        project_root=pre_project_root,
    )

    if pre_requested_tier == "auto" and not _is_legacy_autopilot_mode():
        import sys as _sys
        print(
            f"[autopilot] auto-detected tier={decision.tier} "
            f"(source={decision.source}). "
            f"Pass --tier {decision.tier} to silence this message, "
            f"--tier heavy for legacy-compatible behavior, or set "
            f"WORKFLOW_AUTOPILOT_LEGACY=1 to fully opt out.",
            file=_sys.stderr,
        )

    return pre_feature, pre_project_root, pre_requirements_text, decision, policy


def _maybe_apply_escalation_resume(
    *,
    args: argparse.Namespace,
    feature: str,
    project_root: Path,
) -> int | None:
    """Check for pending escalation resume and apply it.

    Returns:
        None: no resume pending OR response was an "accept"/"custom" that
              the normal loop should continue with.
        int (0/1): resume was a terminal action (split / skip / abort) and
              autopilot should exit with this code.
    """
    try:
        from kodawari.autopilot.escalation.resume import (
            apply_pending_resume,
            detect_pending_resume,
        )
    except ImportError:
        return None

    planning_dir = (project_root / "planning" / feature).resolve()
    if not planning_dir.exists():
        return None
    pending = detect_pending_resume(planning_dir)
    if pending is None:
        return None

    # Inherit forwarding args for sub-feature spawn (only used for split_proposal)
    autopilot_args = _autopilot_inherit_args(args)

    print(f"[escalation-resume] applying pending {pending['kind']} ...")
    outcome = apply_pending_resume(
        planning_dir=planning_dir,
        feature=feature,
        project_root=project_root,
        autopilot_args=autopilot_args,
    )
    print(f"[escalation-resume] outcome: {json.dumps(outcome, ensure_ascii=False)}")

    kind = outcome.get("kind")
    status = outcome.get("status")
    effect = outcome.get("effect", "")

    # Terminal actions: exit autopilot without running planning/exec
    if kind == "split_proposal":
        # All sub-features have been spawned; this autopilot run is done
        return 0 if status == "applied" else 1
    if kind == "decision_response":
        if effect in {"feature_aborted", "feature_skipped"}:
            return 0
        # For executor/gate accept/custom, let normal loop pick up the
        # legacy sticky-decision / recovery-card files
        return None
    return None


def _autopilot_inherit_args(args: argparse.Namespace) -> list[str]:
    """Build a flat argv list of autopilot flags to pass to sub-feature subprocesses."""
    forward: list[str] = []
    if getattr(args, "task_cycle", None) is True:
        forward.append("--task-cycle")
    elif getattr(args, "task_cycle", None) is False:
        forward.append("--no-task-cycle")
    if getattr(args, "executor_model", "") or getattr(args, "executor-model", ""):
        em = getattr(args, "executor_model", None) or getattr(args, "executor-model", None)
        if em:
            forward.extend(["--executor-model", str(em)])
    mc = getattr(args, "max_cycles", None)
    if mc:
        forward.extend(["--max-cycles", str(mc)])
    mwc = getattr(args, "max_wall_clock_seconds", None)
    if mwc:
        forward.extend(["--max-wall-clock-seconds", str(mwc)])
    gp = getattr(args, "gate_profile", None)
    if gp:
        forward.extend(["--gate-profile", str(gp)])
    tier = getattr(args, "tier", None)
    if tier:
        forward.extend(["--tier", str(tier)])
    return forward


def _bootstrap_autopilot_runtime(
    args: argparse.Namespace,
    runtime: Any,
    policy: Any,
) -> tuple[dict[str, Any], bool] | None:
    """Bootstrap runtime with policy override and check for planning pause.

    Returns: (command_runtime, policy_active) or None if planning pause occurs.
    """
    policy_active = _policy_should_override_runtime(args)
    _apply_policy_to_args(args=args, policy=policy)

    try:
        planning_env_args = argparse.Namespace(
            tier=getattr(policy, "effective_tier", getattr(args, "tier", "auto")),
            plan_reviewer_model=str(getattr(args, "plan_reviewer_model", "") or ""),
        )
        with _planning_env_override_for_tier(planning_env_args):
            command_runtime = bootstrap_command_runtime(
                args=args,
                runtime=runtime,
            )
    except AutopilotPlanningBridgeError as exc:
        raise exc

    return command_runtime, policy_active


_WALL_CLOCK_ABORT_REPORT_FILENAME = "ABORT_REPORT.json"
_WALL_CLOCK_EXIT_CODE = 124  # POSIX timeout convention


class _WallClockBudgetExceeded(Exception):
    """Raised by the wall-clock watchdog when budget is exhausted."""


def _start_wall_clock_watchdog(
    *,
    budget_seconds: int,
    on_expire: "threading.Event",
) -> threading.Thread | None:
    """Spawn a daemon thread that fires KeyboardInterrupt-equivalent after
    ``budget_seconds``. Returns the thread (so the caller can clear the event
    on success and let the daemon die naturally).

    Uses signal.raise_signal(SIGINT) for portable behavior: Python turns SIGINT
    into KeyboardInterrupt in the main thread, regardless of OS. On Windows
    this works via the signal module's SIGBREAK handling indirectly through
    raise_signal."""
    if budget_seconds <= 0:
        return None

    def _watchdog() -> None:
        triggered = on_expire.wait(timeout=budget_seconds)
        if triggered:
            return  # Normal completion before timeout — abort the watchdog.
        try:
            # Portable timeout interrupt: SIGINT becomes KeyboardInterrupt in main thread.
            signal.raise_signal(signal.SIGINT)
        except (ValueError, OSError):
            # Fallback for embedded interpreters where raise_signal is unavailable.
            os.kill(os.getpid(), signal.SIGINT)

    thread = threading.Thread(target=_watchdog, name="autopilot-wallclock-watchdog", daemon=True)
    thread.start()
    return thread


def _write_abort_report(
    *,
    planning_dir: Path,
    budget_seconds: int,
    elapsed_seconds: float,
    feature: str,
    cause: str,
) -> Path | None:
    """Write ABORT_REPORT.json into planning_dir for postmortem inspection."""
    try:
        planning_dir.mkdir(parents=True, exist_ok=True)
        path = planning_dir / _WALL_CLOCK_ABORT_REPORT_FILENAME
        payload = {
            "schema_version": "abort_report.v1",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "feature": feature,
            "cause": cause,
            "budget_seconds": int(budget_seconds),
            "elapsed_seconds": round(float(elapsed_seconds), 3),
            "exit_code": _WALL_CLOCK_EXIT_CODE,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return path
    except OSError:
        logger.warning("failed to write ABORT_REPORT.json", exc_info=True)
        return None


def _resolve_abort_planning_dir(args: argparse.Namespace) -> Path:
    """Best-effort planning_dir resolution for ABORT_REPORT placement, even
    when bootstrap failed before command_runtime was built."""
    candidate = getattr(args, "planning_dir", None)
    if candidate:
        return Path(str(candidate)).resolve()
    project_root = Path(str(getattr(args, "project_root", ".") or ".")).resolve()
    feature = str(getattr(args, "feature", "") or "").strip()
    if feature:
        return project_root / "planning" / feature
    return project_root / "planning"


def run_autopilot_command(args: argparse.Namespace) -> int:
    budget_seconds = int(getattr(args, "max_wall_clock_seconds", 0) or 0)
    if budget_seconds > 0:
        return _run_autopilot_with_wall_clock(args=args, budget_seconds=budget_seconds)
    return _run_autopilot_inner(args)


def _run_autopilot_with_wall_clock(*, args: argparse.Namespace, budget_seconds: int) -> int:
    completed_event = threading.Event()
    start = time.monotonic()
    watchdog = _start_wall_clock_watchdog(
        budget_seconds=budget_seconds,
        on_expire=completed_event,
    )
    try:
        return _run_autopilot_inner(args)
    except KeyboardInterrupt:
        elapsed = time.monotonic() - start
        if elapsed < budget_seconds * 0.95:
            # Genuine user interrupt, not our watchdog — propagate.
            raise
        planning_dir = _resolve_abort_planning_dir(args)
        report_path = _write_abort_report(
            planning_dir=planning_dir,
            budget_seconds=budget_seconds,
            elapsed_seconds=elapsed,
            feature=str(getattr(args, "feature", "") or ""),
            cause="wall_clock_budget_exceeded",
        )
        message = (
            f"autopilot wall-clock budget {budget_seconds}s exceeded "
            f"(elapsed {elapsed:.1f}s); wrote {report_path}"
        )
        logger.warning(message)
        print(message)
        return _WALL_CLOCK_EXIT_CODE
    finally:
        completed_event.set()
        if watchdog is not None and watchdog.is_alive():
            watchdog.join(timeout=1.0)


def _run_autopilot_inner(args: argparse.Namespace) -> int:
    runtime = _import_autopilot_runtime()
    if runtime is None:
        return _emit_unavailable_payload("kodawari.autopilot")

    # §1: Resolve pre-bootstrap context (tier detection, policy decision).
    feature, project_root, requirements_text, decision, policy = _resolve_prebootstrap_context(args)

    # §1.5: Resume pending escalation decisions BEFORE running the normal
    # planning/execution loop. If a previous run ended with an escalation
    # and the user ran `kodawari decide`, this picks up the response:
    # - split_proposal → spawns sub-feature autopilot subprocesses, then exits
    # - skip/abort → marks feature aborted, then exits
    # - accept/custom → records applied_at and falls through to normal loop
    resume_rc = _maybe_apply_escalation_resume(
        args=args,
        feature=feature,
        project_root=project_root,
    )
    if resume_rc is not None:
        return resume_rc
    release_rc = _maybe_resume_existing_passed_workflow_chain_release_tail(
        args=args,
        decision=decision,
        policy=policy,
    )
    if release_rc is not None:
        return release_rc

    # §2: Bootstrap runtime with policy overrides.
    try:
        command_runtime, policy_active = _bootstrap_autopilot_runtime(
            args=args, runtime=runtime, policy=policy,
        )
    except AutopilotPlanningBridgeError as exc:
        release_rc = _maybe_resume_completed_task_graph_release_tail(
            args=args,
            error=exc,
            decision=decision,
            policy=policy,
        )
        if release_rc is not None:
            return release_rc
        return _emit_planning_bridge_error(args=args, error=exc)
    command_runtime["workflow_policy"] = policy
    command_runtime["complexity_decision"] = decision
    command_runtime["policy_active"] = policy_active
    planning_pause = maybe_pause_for_planning_decision(
        args=args,
        planning_dir=command_runtime["planning_dir"],
        planning_snapshot=command_runtime["planning_snapshot"],
        policy=policy if command_runtime["policy_active"] else None,
    )
    if planning_pause is not None:
        if hasattr(command_runtime["state"], "task_claim"):
            command_runtime["state"].task_claim = {}
            try:
                command_runtime["state"].save(command_runtime["state_path"])
            except Exception:
                pass
        planning_finalization = _maybe_finalize_planning_pause_artifacts(
            args=args,
            command_runtime=command_runtime,
            planning_pause=planning_pause,
        )
        if planning_finalization:
            planning_pause["planning_finalization"] = planning_finalization
        print(json.dumps(planning_pause, ensure_ascii=False, indent=2))
        return 0 if planning_pause.get("status") == "awaiting_decision" else 1
    worktree_preflight = capture_autopilot_worktree_preflight(
        project_root=command_runtime["project_root"],
        planning_dir=command_runtime["planning_dir"],
        feature=command_runtime["feature"],
    )
    plan, run_result, planning_artifacts, workflow_chain, task_cycle_rounds = execute_autopilot_runtime(
        args=args,
        engine=command_runtime["engine"],
        project_root=command_runtime["project_root"],
        planning_dir=command_runtime["planning_dir"],
        feature=command_runtime["feature"],
        requirements_text=command_runtime["requirements_text"],
        planning_snapshot=command_runtime["planning_snapshot"],
    )
    if workflow_chain is not None:
        chain_path = write_workflow_chain_snapshot(command_runtime["planning_dir"], workflow_chain)
        planning_artifacts[".workflow_chain.json"] = {
            "path": str(chain_path),
            "exists": True,
        }
    else:
        _clear_stale_workflow_chain_snapshot(command_runtime["planning_dir"], planning_artifacts)
    planning_artifacts[WORKTREE_BASELINE_FILENAME] = {
        "path": str((command_runtime["planning_dir"] / WORKTREE_BASELINE_FILENAME).resolve()),
        "exists": (command_runtime["planning_dir"] / WORKTREE_BASELINE_FILENAME).exists(),
    }
    rounds = persist_command_runtime(
        rounds_path=command_runtime["rounds_path"],
        run_result=run_result,
        task_cycle_rounds=task_cycle_rounds,
    )
    reliable_changed_files, changed_files_source = resolve_reliable_changed_files(
        project_root=command_runtime["project_root"],
        state=command_runtime["state"],
        run_result=run_result,
    )
    _restore_legacy_active_task(
        args=args,
        feature=command_runtime["feature"],
        requirements_text=command_runtime["requirements_text"],
        state=command_runtime["state"],
        planning_snapshot=command_runtime["planning_snapshot"],
    )
    command_runtime["state"].changed_files = set(reliable_changed_files)
    command_runtime["state"].save(command_runtime["state_path"])
    payload = build_autopilot_payload(
        args=args,
        planning_dir=command_runtime["planning_dir"],
        state_path=command_runtime["state_path"],
        rounds_path=command_runtime["rounds_path"],
        plan=plan,
        run_result=run_result,
        rounds=rounds,
        planning_artifacts=planning_artifacts,
        state=command_runtime["state"],
        workflow_chain=workflow_chain,
        changed_files_source=changed_files_source,
        worktree_preflight=worktree_preflight,
        planning_snapshot=command_runtime["planning_snapshot"],
    )
    # Observation-only re-detection. This re-runs the detector on the
    # FINAL reliable_changed_files so callers can inspect whether the lane
    # would have been classified differently if the run itself produced
    # heavy-signal files (e.g. a new migration or contract created mid-run).
    #
    # CRITICAL: The resulting `post_run_complexity_decision` /
    # `post_run_workflow_policy` fields are NEVER consumed by the runtime —
    # not by policy_active, not by `_apply_policy_to_args`, and not by
    # `_cleanup_suppressed_artifacts`. They are pure payload telemetry for
    # auditors and future-tier-learning. Do NOT feed these fields back into
    # runtime decisions; if that is ever needed, gate the change behind an
    # explicit test that proves the semantics of "upgrade-only" vs.
    # "post-run overwrite". See also `.lane_observation.json` which captures
    # predicted-vs-actual from run signals independently of this field.
    final_decision, final_policy = _resolve_tier_and_policy(
        args=args,
        feature=command_runtime["feature"],
        requirements_text=command_runtime["requirements_text"],
        changed_files=tuple(reliable_changed_files or ()),
        project_root=command_runtime["project_root"],
    )
    payload["complexity_decision"] = decision.to_dict()
    payload["workflow_policy"] = policy.to_dict()
    if final_decision.to_dict() != decision.to_dict():
        payload["post_run_complexity_decision"] = final_decision.to_dict()
        payload["post_run_workflow_policy"] = final_policy.to_dict()
    state_to_dict = getattr(command_runtime["state"], "to_dict", None)
    state_payload = state_to_dict() if callable(state_to_dict) else {}
    # Re-snap unified_status now that all per-task `_mark_task_completed` calls have settled.
    # The loop-time snapshot in engine_session_mixin captures completed_tasks_total *before*
    # the post-task append, so payload["unified_status"]/run_result["unified_status"] are stale.
    fresh_unified = _get_unified_status_safe(command_runtime["state"])
    if fresh_unified is not None:
        if isinstance(payload.get("unified_status"), dict):
            payload["unified_status"]["completed_tasks_total"] = fresh_unified.get("completed_tasks_total", 0)
        if isinstance(run_result.get("unified_status"), dict):
            run_result["unified_status"]["completed_tasks_total"] = fresh_unified.get("completed_tasks_total", 0)
    run_truth = build_run_truth(
        project_root=command_runtime["project_root"],
        planning_dir=command_runtime["planning_dir"],
        feature=command_runtime["feature"],
        payload=payload,
        run_result=run_result,
        rounds=rounds,
        state_payload=state_payload,
        reliable_changed_files=tuple(reliable_changed_files or ()),
        changed_files_source=changed_files_source,
    )
    _maybe_warn_unexecuted_tasks(args=args, run_truth=run_truth, payload=payload)
    write_run_truth(command_runtime["planning_dir"], run_truth)
    payload["run_truth"] = dict(run_truth)
    self_repair = _maybe_write_self_repair_artifacts(
        project_root=command_runtime["project_root"],
        planning_dir=command_runtime["planning_dir"],
        run_truth=run_truth,
    )
    if self_repair:
        payload["self_repair"] = self_repair
    _emit_lane_observation(
        planning_dir=command_runtime["planning_dir"],
        feature=command_runtime["feature"],
        decision=decision,
        payload=payload,
        rounds=rounds,
        reliable_changed_files=tuple(reliable_changed_files or ()),
        project_root=command_runtime["project_root"],
    )
    _cleanup_suppressed_artifacts(
        planning_dir=command_runtime["planning_dir"],
        policy=policy,
        policy_active=bool(command_runtime.get("policy_active")),
    )
    payload, early_rc = maybe_run_release_tail(
        args=args,
        command_runtime=command_runtime,
        payload=payload,
        run_release_tail=run_autopilot_release_tail,
    )
    _maybe_write_delivery_report(
        planning_dir=command_runtime["planning_dir"],
        feature=command_runtime["feature"],
        payload=payload,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if early_rc is not None:
        return early_rc
    run_reason = str(run_result.get("reason") or "").upper()
    if payload["status"] == "ok":
        return 0
    if run_reason in {"EXECUTION_BACKEND_BLOCKED", "SELF_REVIEW_BLOCKED"}:
        return 2
    return 1


def _maybe_warn_unexecuted_tasks(
    *,
    args: argparse.Namespace,
    run_truth: dict[str, Any],
    payload: dict[str, Any],
) -> None:
    """Surface a warning when task_cycle is off and the planner produced more tasks than ran.

    Reads `run_truth["unexecuted_task_ids"]` (set by build_run_truth from
    TASK_GRAPH vs. state.completed_tasks). Silent when task_cycle is enabled
    or the plan was single-task / fully consumed — those are not user errors.
    """
    if bool(getattr(args, "task_cycle", False)):
        return
    unexecuted = run_truth.get("unexecuted_task_ids")
    if not isinstance(unexecuted, list) or not unexecuted:
        return
    ids = [str(item).strip() for item in unexecuted if str(item).strip()]
    if not ids:
        return
    message = (
        f"plan had {len(ids)} task(s) the autopilot did not execute "
        f"because task_cycle is disabled (rerun with --task-cycle to complete): "
        f"{', '.join(ids)}"
    )
    payload["task_cycle_warning"] = message
    run_truth["task_cycle_warning"] = message
    logger.warning("autopilot: %s", message)


def _get_unified_status_safe(state: Any) -> dict[str, Any] | None:
    """Best-effort re-snap of state.get_unified_status without raising."""
    getter = getattr(state, "get_unified_status", None)
    if not callable(getter):
        return None
    try:
        snap = getter()
    except Exception:
        return None
    return snap if isinstance(snap, dict) else None


def _maybe_resume_completed_task_graph_release_tail(
    *,
    args: argparse.Namespace,
    error: AutopilotPlanningBridgeError,
    decision: Any,
    policy: Any,
) -> int | None:
    if error.error_code != "task_graph_complete":
        return None
    return _maybe_resume_existing_passed_workflow_chain_release_tail(
        args=args,
        decision=decision,
        policy=policy,
    )


def _maybe_resume_existing_passed_workflow_chain_release_tail(
    *,
    args: argparse.Namespace,
    decision: Any,
    policy: Any,
) -> int | None:
    policy_active = _policy_should_override_runtime(args)
    if policy_active and policy is not None and not getattr(policy, "release_tail_enabled", False):
        return None
    feature = str(getattr(args, "feature", "") or "").strip()
    if not feature:
        return None
    project_root = Path(getattr(args, "project_root", ".") or ".").resolve()
    planning_dir, _, _ = resolve_planning_paths(project_root, feature)
    workflow_chain = _load_workflow_chain(planning_dir)
    if not _workflow_chain_passed(workflow_chain):
        return None

    base_payload = _completed_task_graph_release_base_payload(
        feature=feature,
        planning_dir=planning_dir,
        workflow_chain=workflow_chain,
        decision=decision,
        policy=policy if policy_active else None,
    )
    spec = build_release_decision_spec(feature)
    decision_payload = resolve_decision_payload_for_spec(
        args=args,
        planning_dir=planning_dir,
        planning_snapshot=None,
        spec=spec,
        base_payload=base_payload,
    )
    if decision_payload is not None:
        print(json.dumps(decision_payload, ensure_ascii=False, indent=2))
        return 0 if decision_payload.get("status") == "awaiting_decision" else 1

    release_tail = run_autopilot_release_tail(
        project_root=project_root,
        planning_dir=planning_dir,
        feature=feature,
        config=AutopilotReleaseTailConfig(
            auto_eval=bool(getattr(policy, "eval_required", False)) if policy_active and policy is not None else False,
            risk_profile=str(getattr(policy, "effective_tier", "medium") or "medium"),
        ),
    )
    payload, rc = _completed_task_graph_release_payload(
        base_payload=base_payload,
        release_tail=release_tail,
        feature=feature,
    )
    _maybe_write_delivery_report(
        planning_dir=planning_dir,
        feature=feature,
        payload=payload,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return int(rc or 0)


def _load_workflow_chain(planning_dir: Path) -> dict[str, Any]:
    payload = load_json_dict(planning_dir / ".workflow_chain.json", required=False)
    return payload if isinstance(payload, dict) else {}


def _workflow_chain_passed(payload: dict[str, Any]) -> bool:
    for key in ("chain_final_outcome", "final_outcome"):
        outcome = payload.get(key)
        if isinstance(outcome, dict) and str(outcome.get("status") or "").strip().upper() == "PASS":
            return True
    return str(payload.get("status") or "").strip().upper() == "PASS"


def _completed_task_graph_release_base_payload(
    *,
    feature: str,
    planning_dir: Path,
    workflow_chain: dict[str, Any],
    decision: Any,
    policy: Any,
) -> dict[str, Any]:
    final_outcome = dict(
        workflow_chain.get("final_outcome")
        or workflow_chain.get("chain_final_outcome")
        or {"status": "PASS", "reason": "ALL_TASKS_COMPLETE"}
    )
    payload = {
        "status": "ok",
        "entrypoint": "kodawari autopilot",
        "feature": feature,
        "planning_dir": str(planning_dir),
        "planning_artifact_mode": "contract_first",
        "planning_snapshot": {},
        "run_reason": "PIPELINE_FINISH",
        "workflow_chain": workflow_chain,
        "final_outcome": final_outcome,
        "task_graph_selection": "all_tasks_complete",
    }
    if decision is not None:
        payload["complexity_decision"] = decision.to_dict()
    if policy is not None:
        payload["workflow_policy"] = policy.to_dict()
    return payload


def _completed_task_graph_release_payload(
    *,
    base_payload: dict[str, Any],
    release_tail: dict[str, Any],
    feature: str,
) -> tuple[dict[str, Any], int]:
    payload = dict(base_payload)
    payload["release_tail"] = release_tail
    release_status = str(release_tail.get("status") or "PASS").strip().upper()
    if release_status == "PASS":
        payload.update(
            build_interaction_snapshot(
                decision_pending=False,
                decision_kind="release_approval",
                decision_id=f"{feature}:release_approval",
                decision_request_present=False,
                final_status="PASS",
                stop_reason="PASS",
                blocked=False,
                is_terminal=True,
            )
        )
        return payload, 0
    payload["status"] = "blocked"
    payload["blocking_reason"] = str(release_tail.get("blocking_reason") or "release tail blocked")
    payload["next_action"] = str(release_tail.get("next_action") or "")
    payload.update(
        build_interaction_snapshot(
            decision_pending=False,
            decision_kind="release_approval",
            decision_id=f"{feature}:release_approval",
            decision_request_present=False,
            final_status="BLOCKED",
            stop_reason="BLOCKED",
            blocked=True,
            is_terminal=True,
        )
    )
    return payload, 1


def _clear_stale_workflow_chain_snapshot(
    planning_dir: Path,
    planning_artifacts: dict[str, dict[str, Any]],
) -> None:
    chain_path = planning_dir / ".workflow_chain.json"
    if chain_path.exists():
        try:
            chain_path.unlink()
        except OSError:
            pass
    planning_artifacts[".workflow_chain.json"] = {
        "path": str(chain_path.resolve()),
        "exists": chain_path.exists(),
    }


def _emit_planning_bridge_error(
    *,
    args: argparse.Namespace,
    error: AutopilotPlanningBridgeError,
) -> int:
    project_root = Path(args.project_root).resolve()
    planning_dir, _, _ = resolve_planning_paths(project_root, str(args.feature))
    payload = {
        "status": "blocked",
        "entrypoint": "kodawari autopilot",
        "feature": str(args.feature),
        "planning_dir": str(planning_dir),
        "error": str(error),
        "error_code": error.error_code,
        "remediation": list(error.remediation),
        "details": dict(error.details),
        "next_action": "Fix the planning input or artifact issue, then rerun autopilot.",
    }
    if error.error_code == "context_scout_user_decision_required":
        payload.update(
            build_interaction_snapshot(
                decision_pending=True,
                decision_kind="context_scout",
                decision_id=str(error.details.get("selected_tier") or ""),
                decision_request_present=True,
            )
        )
    if error.error_code in {"prd_missing", "prd_required"}:
        payload.update(
            build_interaction_snapshot(
                decision_pending=False,
                decision_request_present=False,
                environment_error_code=error.error_code,
                environment_blocking_reason=str(error),
            )
        )
    planning_finalization = _maybe_finalize_planning_bridge_error_artifacts(
        args=args,
        planning_dir=planning_dir,
        project_root=project_root,
        error=error,
    )
    if planning_finalization:
        payload["planning_finalization"] = planning_finalization
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 2


def _maybe_finalize_planning_bridge_error_artifacts(
    *,
    args: argparse.Namespace,
    planning_dir: Path,
    project_root: Path,
    error: AutopilotPlanningBridgeError,
) -> dict[str, Any]:
    """Write terminal truth for planning failures raised during bootstrap.

    Fresh model planning can stop before an executable TASK_GRAPH/TASK_CARD is
    approved. That path raises before runtime exists, so it cannot use the
    normal post-runtime finalizer. Still, the run is terminal and should leave
    the same truth/self-repair artifacts as a planning pause.
    """

    planning_status = str(dict(error.details or {}).get("planning_status") or "").strip().lower()
    if planning_status not in {"escalation_required", "precondition_blocked", "error"}:
        return {}
    return _finalize_planning_blocked_artifacts(
        args=args,
        project_root=project_root,
        planning_dir=planning_dir,
        state_payload={},
        fallback_reason=error.error_code,
    )


def _maybe_finalize_planning_pause_artifacts(
    *,
    args: argparse.Namespace,
    command_runtime: dict[str, Any],
    planning_pause: dict[str, Any],
) -> dict[str, Any]:
    """Write terminal truth for planning escalations that pause before runtime.

    The normal run-truth/self-repair finalization happens after executor
    runtime. Planning escalation pauses before that point, so without this
    bridge a planner/reviewer deadlock leaves only an awaiting-decision payload
    and self-repair never sees a terminal artifact.
    """

    if str(planning_pause.get("decision_kind") or "") != "planning_escalation":
        return {}
    state_to_dict = getattr(command_runtime.get("state"), "to_dict", None)
    state_payload = state_to_dict() if callable(state_to_dict) else {}
    return _finalize_planning_blocked_artifacts(
        args=args,
        project_root=Path(command_runtime["project_root"]),
        planning_dir=Path(command_runtime["planning_dir"]),
        state_payload=state_payload,
        fallback_reason="PLANNING_ESCALATION_REQUIRED",
    )


def _finalize_planning_blocked_artifacts(
    *,
    args: argparse.Namespace,
    project_root: Path,
    planning_dir: Path,
    state_payload: dict[str, Any],
    fallback_reason: str,
) -> dict[str, Any]:
    planning_dir = Path(planning_dir)
    project_root = Path(project_root)
    planning_failure = load_json_dict(planning_dir / ".planning_failure.json", required=False) or {}
    conversation = load_json_dict(planning_dir / "PLANNING_CONVERSATION.json", required=False) or {}
    escalation = dict(planning_failure.get("escalation") or conversation.get("escalation") or {})
    reason = (
        str(planning_failure.get("error_code") or "").strip()
        or str(planning_failure.get("reason") or "").strip()
        or str(escalation.get("termination_reason") or "").strip()
        or str(fallback_reason or "").strip()
        or "PLANNING_ESCALATION_REQUIRED"
    )
    run_result = {
        "reason": reason,
        "blocking_reason": reason,
        "final_status": "BLOCKED",
        "unified_status": {"final_status": "BLOCKED", "stage_status": "BLOCKED"},
    }
    payload = {
        "run_reason": reason,
        "blocking_reason": reason,
        "final_status": "BLOCKED",
        "unified_status": {"final_status": "BLOCKED", "stage_status": "BLOCKED"},
    }
    run_truth = build_run_truth(
        project_root=project_root,
        planning_dir=planning_dir,
        feature=str(getattr(args, "feature", "") or planning_dir.name),
        payload=payload,
        run_result=run_result,
        rounds=[],
        state_payload=state_payload,
        reliable_changed_files=(),
        changed_files_source="none",
    )
    write_run_truth(planning_dir, run_truth)
    summary: dict[str, Any] = {
        "status": "finalized",
        "run_truth": ".run_truth.json",
        "run_reason": reason,
    }
    self_repair = _maybe_write_self_repair_artifacts(
        project_root=project_root,
        planning_dir=planning_dir,
        run_truth=run_truth,
    )
    if self_repair:
        summary["self_repair"] = self_repair
    return summary


_PLANNING_SEVERITIES_ENV = "WORKFLOW_PLAN_BLOCKING_SEVERITIES"
_PLANNING_DECISION_POLICY_ENV = "WORKFLOW_PLAN_DECISION_POLICY"
_PLANNING_REVIEWER_MODEL_ENV = "WORKFLOW_PLAN_REVIEWER_MODEL"
_LEGACY_AUTOPILOT_ENV = "WORKFLOW_AUTOPILOT_LEGACY"


def _is_legacy_autopilot_mode() -> bool:
    """Full opt-out: pin behavior to pre-tier autopilot regardless of --tier.

    Intended as a one-release migration escape hatch for CI/scripts that
    depend on the pre-policy runtime (all artifacts emitted, task_cycle on,
    planning env untouched).
    """
    return os.environ.get(_LEGACY_AUTOPILOT_ENV, "").strip() == "1"


@contextmanager
def _planning_env_override_for_tier(args: argparse.Namespace) -> Iterator[None]:
    """Temporarily set planning env from the active tier and optional model.

    Ensures the planning orchestrator that runs inside bootstrap honors the
    lane's review blocking threshold. Always restores the prior env value.
    """
    if _is_legacy_autopilot_mode():
        yield
        return
    requested = str(getattr(args, "tier", "auto") or "auto").strip().lower()
    plan_reviewer_model = str(getattr(args, "plan_reviewer_model", "") or "").strip()
    if requested not in {"lite", "standard", "heavy"} and not plan_reviewer_model:
        yield
        return
    prior_sev = os.environ.get(_PLANNING_SEVERITIES_ENV)
    prior_policy = os.environ.get(_PLANNING_DECISION_POLICY_ENV)
    prior_reviewer_model = os.environ.get(_PLANNING_REVIEWER_MODEL_ENV)
    if requested in {"lite", "standard", "heavy"}:
        from kodawari.autopilot.planning.lane_config import lane_for
        lane = lane_for(requested)
        severities = ",".join(sorted(lane.review_blocking_threshold))
        os.environ[_PLANNING_SEVERITIES_ENV] = severities
        os.environ[_PLANNING_DECISION_POLICY_ENV] = lane.decision_policy
    if plan_reviewer_model:
        os.environ[_PLANNING_REVIEWER_MODEL_ENV] = plan_reviewer_model
    try:
        yield
    finally:
        if prior_sev is None:
            os.environ.pop(_PLANNING_SEVERITIES_ENV, None)
        else:
            os.environ[_PLANNING_SEVERITIES_ENV] = prior_sev
        if prior_policy is None:
            os.environ.pop(_PLANNING_DECISION_POLICY_ENV, None)
        else:
            os.environ[_PLANNING_DECISION_POLICY_ENV] = prior_policy
        if prior_reviewer_model is None:
            os.environ.pop(_PLANNING_REVIEWER_MODEL_ENV, None)
        else:
            os.environ[_PLANNING_REVIEWER_MODEL_ENV] = prior_reviewer_model


def _policy_should_override_runtime(args: argparse.Namespace) -> bool:
    """Resolve lane policy for every autopilot run.

    `--tier=auto` is load-bearing: we still detect a lane and execute the run
    under the resolved WorkflowPolicy. Explicit tiers keep winning because the
    detector marks them as source="explicit" before policy resolution.

    `WORKFLOW_AUTOPILOT_LEGACY=1` opts out entirely, pinning behavior to the
    pre-tier autopilot for CI/scripts that have not migrated yet.
    """
    del args
    if _is_legacy_autopilot_mode():
        return False
    return True


_DEFAULT_EXECUTOR_BACKEND = "claude_code"
_DEFAULT_SELF_REVIEW_BACKEND = ""


def _default_executor_backend() -> str:
    try:
        from kodawari.autopilot.execution.execution_artifacts import is_test_environment
    except Exception:
        return _DEFAULT_EXECUTOR_BACKEND
    return "" if is_test_environment() else _DEFAULT_EXECUTOR_BACKEND


def _apply_policy_to_args(*, args: argparse.Namespace, policy) -> None:
    """Translate WorkflowPolicy decisions into namespace flags consumed by
    downstream legacy code.

    `--tier=auto` is now load-bearing, so the resolved policy applies by
    default. Explicit user flags still win over policy defaults.

    Detection of "user explicit" uses a tri-state sentinel on `args.task_cycle`
    (None = parser default / programmatic caller did not set it). This is
    safer than reading sys.argv, which lies when autopilot is invoked
    programmatically with a manually-built Namespace.

    `WORKFLOW_AUTOPILOT_LEGACY=1` short-circuits all policy application so
    the caller sees the pre-tier Namespace exactly as parsed.
    """
    if _is_legacy_autopilot_mode():
        return
    user_explicit_task_cycle = getattr(args, "task_cycle", None) is not None
    if not user_explicit_task_cycle:
        args.task_cycle = bool(policy.task_cycle_enabled)
    # Backfill zero-param defaults so tiered autopilot needs no extra flags.
    if not str(getattr(args, "executor_backend", "") or "").strip():
        default_executor = _default_executor_backend()
        if default_executor:
            args.executor_backend = default_executor
    if (
        _DEFAULT_SELF_REVIEW_BACKEND
        and not str(getattr(args, "self_review_backend", "") or "").strip()
    ):
        args.self_review_backend = _DEFAULT_SELF_REVIEW_BACKEND
    if policy.review_max_rounds is not None:
        args.collaboration_max_rounds = policy.review_max_rounds
    args.parallel_runtime_enabled = bool(policy.parallel_runtime_enabled)


def _emit_lane_observation(
    *,
    planning_dir: Path,
    feature: str,
    decision,
    payload: dict[str, Any],
    rounds: list[dict[str, Any]],
    reliable_changed_files: tuple[str, ...],
    project_root: Path | None = None,
) -> None:
    """Capture predicted-vs-actual signals as .lane_observation.json.

    On mismatch also ingest a category="lane" event into the instincts
    learning store so future detector runs can pick up learned hints.

    Best-effort — any failure is swallowed. The autopilot run must not be
    blocked by observation/ingest.
    """
    # Legacy mode short-circuit: skip observation and learning when pre-tier behavior is pinned.
    if _is_legacy_autopilot_mode():
        return

    try:
        signals = _collect_actual_signals(
            payload=payload,
            rounds=rounds,
            reliable_changed_files=reliable_changed_files,
            project_root=project_root,
        )
        observation = build_lane_observation(
            feature=feature, predicted=decision, signals=signals,
        )
        write_lane_observation(planning_dir, observation)
        payload["lane_observation"] = observation.to_dict()
        if observation.mismatch and project_root is not None:
            _ingest_lane_observation_to_instincts(
                project_root,
                observation,
                planning_dir=planning_dir,
            )
    except Exception:  # pragma: no cover — observation failure must not break the run
        pass


def _ingest_lane_observation_to_instincts(
    project_root: Path,
    observation,
    *,
    planning_dir: Path | None = None,
) -> None:
    """Feed a mismatched lane observation to the instincts learning engine.

    Reads the autopilot's run_id from ``.autopilot_state.json`` and stamps it
    on the event so PR2.5 distinct-run promotion semantics also apply to lane
    candidates — without this, every lane mismatch carries an empty run_id,
    distinct_run_count never grows, and the lane detector never self-corrects
    across repeated underclassifications.
    """
    try:
        from kodawari.instincts.engine import ingest_lane_event
        event = to_learning_event(observation)
        run_id = _read_autopilot_run_id(planning_dir)
        if run_id:
            event["run_id"] = run_id
        ingest_lane_event(Path(project_root), event)
    except Exception:  # pragma: no cover — ingest failure must not block run
        pass


def _read_autopilot_run_id(planning_dir: Path | None) -> str:
    """Best-effort read of run_id from .autopilot_state.json.

    Returns "" on any failure (missing file, parse error, missing field) —
    the caller falls back to legacy event-count promotion in that case.
    """
    if planning_dir is None:
        return ""
    state_path = Path(planning_dir) / ".autopilot_state.json"
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("run_id") or "").strip()


def _collect_actual_signals(
    *,
    payload: dict[str, Any],
    rounds: list[dict[str, Any]],
    reliable_changed_files: tuple[str, ...],
    project_root: Path | None = None,
) -> ActualRunSignals:
    return _lane_signals.collect_actual_signals(
        payload=payload,
        rounds=rounds,
        reliable_changed_files=reliable_changed_files,
        project_root=project_root,
        diff_loc_fn=_estimate_diff_loc,
    )


def _estimate_diff_loc(
    *,
    project_root: Path | None,
    changed_files: tuple[str, ...],
) -> int:
    return _lane_signals.estimate_diff_loc(
        project_root=project_root,
        changed_files=changed_files,
        subprocess_run=subprocess.run,
    )


def _cleanup_suppressed_artifacts(
    *,
    planning_dir: Path,
    policy: Any,
    policy_active: bool,
) -> None:
    """Best-effort cleanup for lane-suppressed artifacts after a run.

    Runtime components may still materialize compatibility artifacts. For
    explicit tiers, remove anything suppressed by the active policy so the final
    planning directory reflects the lane contract.
    """
    if not policy_active or policy is None:
        return
    suppressed = sorted(set(getattr(policy, "suppressed_artifacts", ()) or ()))
    if not suppressed:
        return

    root = Path(planning_dir).resolve()
    for name in suppressed:
        target = (root / str(name)).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            continue
        try:
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            elif target.exists():
                target.unlink()
        except OSError:
            continue


def _extract_task_card_files(planning_dir: Path) -> tuple[str, ...]:
    """Read task_card_files from TASK_CARD_ACTIVE.json."""
    try:
        data = json.loads((planning_dir / "TASK_CARD_ACTIVE.json").read_text(encoding="utf-8"))
        files = data.get("files_to_change")
        if isinstance(files, list):
            return tuple(str(f).strip() for f in files if str(f).strip())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return ()


def _extract_layers(planning_dir: Path) -> tuple[str, ...]:
    # `layers` is a PRD_INTAKE / PLANNING_CONVERSATION field (values from
    # route/service/repository/model/schema/frontend/util). REPO_INVENTORY
    # does not have a top-level `layers` field.
    #
    # Empty [] on PRD_INTAKE must NOT short-circuit the fallback: the
    # compat layer can synthesize a PRD view from PLANNING_CONVERSATION
    # that legitimately carries non-empty layers even when an earlier
    # PRD stage wrote []. Treat "list but empty" as "this source did not
    # give us layers" and continue to the next file.
    for filename in ("PRD_INTAKE.json", "PLANNING_CONVERSATION.json"):
        try:
            data = json.loads((planning_dir / filename).read_text(encoding="utf-8"))
            layers = data.get("layers")
            if isinstance(layers, (list, tuple)):
                cleaned = tuple(str(l).strip() for l in layers if str(l).strip())
                if cleaned:
                    return cleaned
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            continue
    return ()


def _build_complexity_input(
    *,
    feature: str,
    task_direction: str,
    requirements_text: str,
    changed_files: tuple[str, ...],
    project_root: Path,
) -> ComplexityInput:
    """Build ComplexityInput from pre-bootstrap signals + planning artifacts.

    Reads from planning_dir/<feature>/:
      - TASK_CARD_ACTIVE.json → task_card_files
      - PRD_INTAKE.json / PLANNING_CONVERSATION.json → layers

    source_of_truth from PRD is intentionally NOT mapped into
    source_of_truth_files: PRD's source_of_truth is entity-level
    (e.g. db.primary), not a list of file paths, so feeding it into the
    detector's path-count / path-token scoring would misclassify runs.
    Path-based signals come from task_card_files + changed_files.

    path_type from PRD (read/write/both) is also not consumed here — the
    detector's previous hard rule matched schema_change/contract_change
    which the real contract never emits. Contract/schema risk is already
    covered by the file-path and keyword hard-rules.

    All reads are best-effort.
    """
    planning_dir = (project_root / "planning" / feature).resolve()

    task_card_files = _extract_task_card_files(planning_dir)
    layers = _extract_layers(planning_dir)

    from kodawari.instincts.engine import load_lane_hints
    try:
        learned_hints = tuple(load_lane_hints(project_root))
    except Exception:
        learned_hints = ()

    return ComplexityInput(
        feature=feature,
        task_direction=task_direction,
        requirements_text=requirements_text,
        changed_files=changed_files,
        task_card_files=task_card_files,
        layers=layers,
        learned_hints=learned_hints,
    )


def _resolve_tier_and_policy(
    *,
    args: argparse.Namespace,
    feature: str,
    requirements_text: str,
    changed_files: tuple[str, ...],
    project_root: Path | None = None,
):
    """Build ComplexityInput, run detector, resolve WorkflowPolicy.

    The returned WorkflowPolicy is runtime-active for this command.
    """
    requested_tier = str(getattr(args, "tier", "auto") or "auto")
    resolved_root = project_root or Path(getattr(args, "project_root", ".") or ".").resolve()

    # Build input with complete signal sources (including planning artifacts)
    inp = _build_complexity_input(
        feature=str(feature or ""),
        task_direction=str(getattr(args, "task", "") or ""),
        requirements_text=str(requirements_text or ""),
        changed_files=changed_files,
        project_root=resolved_root,
    )

    decision = detect_complexity(
        inp,
        requested_tier=requested_tier,
        llm_classifier=model_advisor_tier_classifier,
    )
    lane = lane_for(decision.tier)
    policy = resolve_workflow_policy(
        decision=decision, lane=lane, overrides=UserPolicyOverrides(),
    )
    return decision, policy


def _maybe_write_delivery_report(
    *,
    planning_dir: Path,
    feature: str,
    payload: dict[str, Any],
) -> None:
    status = str(payload.get("status") or "").strip().lower()
    if status not in {"ok", "blocked", "awaiting_decision"}:
        return
    try:
        report = generate_delivery_report(planning_dir=planning_dir, feature=feature)
    except Exception:
        return
    report_path = planning_dir / "DELIVERY_REPORT.md"
    try:
        report_path.write_text(report, encoding="utf-8")
    except OSError:
        return


def _maybe_write_self_repair_artifacts(
    *,
    project_root: Path,
    planning_dir: Path,
    run_truth: dict[str, Any],
) -> dict[str, Any]:
    try:
        proposal = build_self_repair_proposal(
            project_root=project_root,
            planning_dir=planning_dir,
            run_truth=run_truth,
        )
    except Exception:
        return {"status": "error", "reason": "self_repair_proposal_failed"}
    if str(proposal.get("status") or "") != "ready":
        return {}
    try:
        artifact = write_self_repair_proposal(planning_dir, proposal)
        markdown = write_self_repair_markdown(planning_dir, proposal)
    except Exception:
        return {"status": "error", "reason": "self_repair_write_failed"}
    root_cause = proposal.get("root_cause") if isinstance(proposal.get("root_cause"), dict) else {}
    summary: dict[str, Any] = {
        "status": "ready",
        "artifact": artifact.name,
        "markdown": markdown.name,
        "root_cause": str(root_cause.get("code") or ""),
    }
    auto_execution = _maybe_auto_execute_self_repair(
        planning_dir=planning_dir,
        proposal_path=artifact,
    )
    if auto_execution:
        summary["auto_execution"] = auto_execution
    return summary


def _maybe_auto_execute_self_repair(
    *,
    planning_dir: Path,
    proposal_path: Path,
) -> dict[str, Any]:
    """End-to-end orchestration: when an autopilot run BLOCKs and writes a
    Phase-1/2 self-repair proposal, fire the Phase-3 execute pipeline only
    if the operator has explicitly opted into auto-execution.

    ``WORKFLOW_SELF_REPAIR=1`` now means "write diagnostic artifacts".
    Automatic SDK repair spawn requires the narrower
    ``WORKFLOW_SELF_REPAIR_AUTO_EXECUTE=1`` flag. The Phase-3
    ``execute_self_repair_proposal`` runs all gates again before spawning,
    so an env mistake at this layer is caught downstream.
    """

    raw_auto_execute = str(os.environ.get(_SELF_REPAIR_ENV_AUTO_EXECUTE, "")).strip().lower()
    if raw_auto_execute not in {"1", "true", "yes", "on"}:
        return {}
    raw_enabled = str(os.environ.get(_SELF_REPAIR_ENV_ENABLED, "")).strip().lower()
    if raw_enabled not in {"1", "true", "yes", "on"}:
        return {"status": "skipped", "reason": "self_repair_diagnostics_disabled"}
    raw_depth = str(os.environ.get(_SELF_REPAIR_ENV_DEPTH, "0")).strip()
    try:
        current_depth = int(raw_depth)
    except ValueError:
        current_depth = 0
    if current_depth >= 1:
        return {"status": "skipped", "reason": "already_inside_self_repair_run"}
    try:
        record = execute_self_repair_proposal(proposal_path=planning_dir / proposal_path.name)
    except Exception as exc:  # pragma: no cover - defensive: never crash finalization
        logger.warning("auto self-repair-execute crashed: %s", exc)
        return {"status": "error", "reason": "auto_execute_crashed", "error": str(exc)}
    try:
        artifact_path = write_self_repair_execution_record(planning_dir, record)
        record["artifact"] = str(artifact_path)
    except Exception:
        pass
    return {
        "status": str(record.get("status") or ""),
        "reason": str(record.get("reason") or ""),
        "failed_gates": list(record.get("failed_gates") or []),
        "spawn_status": (record.get("spawn") or {}).get("status") if isinstance(record.get("spawn"), dict) else "",
        "artifact": record.get("artifact"),
    }


