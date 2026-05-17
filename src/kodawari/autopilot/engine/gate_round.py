"""Gate-round helpers extracted from the autopilot engine."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from kodawari.autopilot.execution.diff_scope_guard import (
    DiffScopeReport,
    guard_diff_scope,
    scoped_executor_enabled,
)
from kodawari.autopilot.verify.failure_analyzer import (
    classify_failure,
    parse_pytest_failures,
)

_VERIFY_ANALYZER_ENV = "WORKFLOW_VERIFY_ANALYZER"


def _verify_analyzer_enabled() -> bool:
    """Return True unless WORKFLOW_VERIFY_ANALYZER is explicitly disabled."""
    raw = os.environ.get(_VERIFY_ANALYZER_ENV)
    if raw is None:
        return True
    return str(raw).strip().lower() not in {"0", "off", "false", "no"}


def _analyze_verify_stdout(stdout: str, allowed_mutations: list[dict]) -> list[dict]:
    """解析 pytest 输出，对每个失败用 failure_analyzer 分类，返回分类结果列表。"""
    if not stdout:
        return []
    results: list[dict] = []
    for failure in parse_pytest_failures(stdout):
        row = classify_failure(failure, allowed_mutations).to_dict()
        row["failure"] = failure.to_dict()
        results.append(row)
    return results


def _tier_counts(analysis: list[dict]) -> tuple[int, int, int]:
    """返回 (Tier A 已授权, Tier A 未授权, Tier B) 三项计数。"""
    a_auth = sum(1 for r in analysis if r.get("tier") == "A" and r.get("authorized_mutation"))
    a_unauth = sum(1 for r in analysis if r.get("tier") == "A" and not r.get("authorized_mutation"))
    b_count = sum(1 for r in analysis if r.get("tier") == "B")
    return a_auth, a_unauth, b_count


def _build_fix_round_msg(blocking_reason: str, stdout: str, analysis: list[dict]) -> str:
    """构造 fix_round 的 must_fix 消息，有 analyzer 分析时附上分层摘要。"""
    msg = f"Fix verify failure: {blocking_reason}"
    if stdout:
        msg += f"\n\nVerify output (last failure):\n{stdout}"
    if not analysis:
        return msg
    a_auth, a_unauth, b_count = _tier_counts(analysis)
    lines = ["\n\nVerify Failure Analysis:"]
    if a_auth:
        lines.append(
            f"  Tier A authorized ({a_auth}): stale literal assertion — executor may apply mutation from allowed_test_mutations"
        )
    if a_unauth:
        lines.append(
            f"  Tier A unauthorized ({a_unauth}): stale literal assertion — must fix implementation or update task card allowed_test_mutations"
        )
    if b_count:
        lines.append(
            f"  Tier B ({b_count}): implementation/environment failure — fix implementation, do NOT mutate tests"
        )
    return msg + "\n".join(lines)

from kodawari.autopilot.collaboration import (
    record_rules_gate_result,
    record_verify_result,
    update_round_record_outcome,
)
from kodawari.autopilot.review.contract import derive_runtime_review_evidence
from kodawari.autopilot.core.runtime_checks import (
    build_verify_check,
    evaluate_runtime_gate,
    gate_must_fix_items,
)
from kodawari.autopilot.core.prompt_profiles import model_family
from kodawari.autopilot.core.state import Stage, StopReason
from kodawari.infra.gate_artifacts import write_gate_artifacts
from kodawari.infra.review_evidence_artifact import (
    REVIEW_EVIDENCE_FILENAME,
    build_review_evidence_artifact,
    write_review_evidence_artifact,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Rollback helpers
# ---------------------------------------------------------------------------

def _rollback_enabled(engine: Any) -> bool:
    return bool(getattr(engine.config, "rollback_on_failure", False))


def _verify_retry_budget_remaining(engine: Any, runtime: Any) -> bool:
    max_retries = int(getattr(engine.config, "max_verify_retries", 2))
    verify_failures = sum(
        1 for r in runtime.round_records
        if r.get("stage_status") == "blocked"
        and "verify_check" in (r.get("details") or {})
    )
    return verify_failures < max_retries


def _maybe_rollback(engine: Any, runtime: Any) -> dict[str, Any] | None:
    checkpoint = getattr(runtime, "rollback_checkpoint", None)
    if checkpoint is None:
        return None
    result = checkpoint.rollback(
        project_root=engine.config.project_root,
        changed_files=list(runtime.last_changed_files),
    )
    for path in result["reverted"] + result["removed"]:
        engine.state.changed_files.discard(path)
    runtime.last_changed_files = []
    runtime.rollback_checkpoint = None
    return result


def _rollback_is_clean(rollback_result: dict[str, Any] | None) -> bool:
    """Only a fully verified rollback counts as a clean retry state."""
    if rollback_result is None:
        return False
    return (
        len(rollback_result.get("skipped", [])) == 0
        and rollback_result.get("extra_dirty_found", 0) == 0
        and bool(rollback_result.get("dirty_scan_available", True))
    )


def _reopen_after_verify_block(runtime: Any, verify_check: dict[str, Any], rollback_result: dict[str, Any]) -> None:
    blocking_reason = str(verify_check.get("blocking_reason") or "verify failed")
    runtime.context.review_feedback.approved = False
    runtime.context.review_feedback.summary = (
        f"Verify failed: {blocking_reason}. Files rolled back, retry from clean state."
    )
    runtime.context.review_feedback.must_fix = [
        f"Fix verify failure: {blocking_reason}",
    ]
    runtime.context.review_feedback.gate_recommendation = "REVIEW_FIX_REQUIRED"
    record_verify_result(runtime.context, passed=False)


def _card_string_list(card: dict[str, Any], key: str) -> list[str]:
    return [str(f) for f in (card.get(key) or []) if str(f).strip()]


def _check_diff_scope(engine: Any, runtime: Any) -> DiffScopeReport | None:
    """返回 DiffScopeReport；功能关闭或无 task card 时返回 None。"""
    if not scoped_executor_enabled():
        return None
    card = getattr(engine, "_task_card_payload", None)
    if not isinstance(card, dict):
        return None
    files_to_change = _card_string_list(card, "files_to_change")
    new_files = _card_string_list(card, "new_files")
    return guard_diff_scope(list(runtime.last_changed_files), files_to_change, new_files)


def _finish_scope_violation(
    engine: Any,
    runtime: Any,
    action: Any,
    round_record: dict[str, Any],
    scope_report: DiffScopeReport,
) -> dict[str, Any]:
    """Executor 越界修改文件时直接拒绝，不进 verify 和 review。"""
    files = list(scope_report.out_of_scope_files)
    last_error = f"DIFF_SCOPE_VIOLATION: executor modified files outside task card scope: {files}"
    engine.state.add_error(
        last_error,
        phase=Stage.VERIFY.value,
        action=str(getattr(action, "value", action)),
        category="scope",
    )
    engine.state.mark_completed(StopReason.HARD_ERROR, "BLOCKED")
    engine.state.last_stage_status = "scope_violation"
    _store_blocked_round(
        round_record,
        last_error=last_error,
        details={"scope_report": scope_report.to_dict(), "message": "executor modified out-of-scope files"},
    )
    runtime.round_records.append(update_round_record_outcome(round_record, runtime.context))
    return engine._finish_loop(runtime, reason="DIFF_SCOPE_VIOLATION", last_error=last_error, action=action)


def run_verify_round(engine: Any, *, runtime: Any, action: Any, round_record: dict[str, Any]) -> dict[str, Any] | None:
    scope_report = _check_diff_scope(engine, runtime)
    if scope_report is not None and scope_report.blocked:
        return _finish_scope_violation(engine, runtime, action, round_record, scope_report)
    verify_check = _resolve_verify_check(engine, runtime)
    if not bool(verify_check.get("passed")):
        return _finish_verify_block(engine, runtime, action, round_record, verify_check)
    record_verify_result(runtime.context, passed=True)
    engine.state.last_stage_status = "verify_passed"
    round_record["stage_status"] = "pass"
    round_record["details"] = {"message": "local verify passed", "verify_check": verify_check}
    runtime.round_records.append(update_round_record_outcome(round_record, runtime.context))
    return None


def run_rules_gate_round(engine: Any, *, runtime: Any, action: Any, round_record: dict[str, Any]) -> dict[str, Any] | None:
    gate_attempt = _gate_attempt_number(runtime)
    _emit_pre_gate(engine, runtime, action, gate_attempt=gate_attempt)
    gate_check = _resolve_gate_check(engine, runtime)
    if _gate_blocked(gate_check):
        return _handle_gate_block(engine, runtime, action, round_record, dict(runtime.verify_check or {}), gate_check, gate_attempt=gate_attempt)
    _finish_gate_ready(engine, runtime, round_record, dict(runtime.verify_check or {}), gate_check, gate_attempt=gate_attempt)
    return None


def _gate_attempt_number(runtime: Any) -> int:
    """Return the 1-based attempt number for the current gate round.

    Computed from round_records already stored: each record whose ``details``
    contains a ``gate_check`` key represents a completed gate attempt.
    """
    return sum(
        1 for r in runtime.round_records
        if "gate_check" in (r.get("details") or {})
    ) + 1


def _emit_pre_gate(engine: Any, runtime: Any, action: Any, *, gate_attempt: int) -> None:
    engine._maybe_emit_hook(
        runtime.hook_events,
        event="pre_gate",
        task_id=runtime.task_id,
        task_label=runtime.task_label,
        action=action,
        task_scope=runtime.task_scope,
        details={"gate_attempt": gate_attempt},
    )


def _resolve_verify_check(engine: Any, runtime: Any) -> dict[str, Any]:
    engine.state.current_stage = Stage.VERIFY
    runtime.post_execution_qa = engine._resolve_post_execution_qa(runtime)
    runtime.verify_check = build_verify_check(
        project_root=engine.config.project_root,
        feature=engine.config.feature,
        task_label=runtime.task_label,
        verify_cmd=engine.config.verify_cmd,
        changed_files=list(runtime.last_changed_files),
        qa_payload=runtime.post_execution_qa,
        instinct_hints=_runtime_instinct_hints(runtime),
    )
    return runtime.verify_check


def _runtime_instinct_hints(runtime: Any) -> list[dict[str, Any]]:
    raw = runtime.pre_compact_payload.get("instinct_hints")
    if not isinstance(raw, list):
        return []
    return [dict(item) for item in raw if isinstance(item, dict)]


def _resolve_gate_check(engine: Any, runtime: Any) -> dict[str, Any]:
    engine.state.current_stage = Stage.GATE
    runtime.gate_check = evaluate_runtime_gate(
        project_root=engine.config.project_root,
        changed_files=list(runtime.last_changed_files),
        profile_name="blocking",
    )
    return runtime.gate_check


def _gate_blocked(gate_check: dict[str, Any]) -> bool:
    return str(gate_check.get("total_status") or "").upper() == "BLOCKED"


def _reopen_for_fix_round(
    runtime: Any,
    verify_check: dict[str, Any],
    *,
    allowed_mutations: list[dict] | None = None,
) -> list[dict]:
    """将 verify 失败注入 review_feedback，触发下一轮 fix_round。

    与 _reopen_after_verify_block 不同，此路径保留磁盘上的部分实现。
    当 WORKFLOW_VERIFY_ANALYZER 未禁用时，附上 Tier A/B 分层分析，帮助 executor 区分
    「代码 bug」与「旧断言过期」。返回 failure_analysis 列表供调用方写入 round_record。
    """
    blocking_reason = str(verify_check.get("blocking_reason") or "verify failed")
    stdout = str(verify_check.get("stdout_excerpt") or "").strip()
    analysis: list[dict] = []
    if _verify_analyzer_enabled() and stdout:
        analysis = _analyze_verify_stdout(stdout, list(allowed_mutations or []))
    must_fix_msg = _build_fix_round_msg(blocking_reason, stdout, analysis)
    runtime.context.review_feedback.approved = False
    runtime.context.review_feedback.summary = (
        f"Verify failed: {blocking_reason}. Partial implementation kept — fix the remaining issues."
    )
    runtime.context.review_feedback.must_fix = [must_fix_msg]
    runtime.context.review_feedback.gate_recommendation = "REVIEW_FIX_REQUIRED"
    record_verify_result(runtime.context, passed=False)
    return analysis


def _finish_verify_block(
    engine: Any,
    runtime: Any,
    action: Any,
    round_record: dict[str, Any],
    verify_check: dict[str, Any],
) -> dict[str, Any] | None:
    last_error = str(verify_check.get("blocking_reason") or "VERIFY_BLOCKED")
    engine.state.add_error(
        last_error,
        phase=Stage.VERIFY.value,
        action=str(getattr(action, "value", action)),
        category="verify",
    )
    allowed_mutations = list(getattr(engine.config, "allowed_test_mutations", None) or [])

    # Rollback-on-failure: attempt clean retry if enabled and budget remains
    can_retry = (
        runtime.peer_review_enabled
        and _rollback_enabled(engine)
        and _verify_retry_budget_remaining(engine, runtime)
    )
    if can_retry:
        rollback_result = _maybe_rollback(engine, runtime)
        # checkpoint missing or partial rollback → not clean → terminate
        if rollback_result is None or not _rollback_is_clean(rollback_result):
            can_retry = False
        if can_retry:
            _reopen_after_verify_block(runtime, verify_check, rollback_result)
            engine.state.last_stage_status = "verify_blocked"
            _store_blocked_round(
                round_record,
                last_error=last_error,
                details={"verify_check": verify_check, "rollback": rollback_result},
            )
            runtime.round_records.append(update_round_record_outcome(round_record, runtime.context))
            return None  # continue loop for retry

    # Fix-on-fail (no rollback): inject verify failure as a must_fix item and
    # let the executor repair the remaining issues in the next fix_round. This
    # applies whenever rollback is disabled but retry budget remains. The partial
    # implementation is kept on disk so the executor can build on it.
    if _verify_retry_budget_remaining(engine, runtime):
        analysis = _reopen_for_fix_round(runtime, verify_check, allowed_mutations=allowed_mutations)
        prompt_lesson_learning = _ingest_verify_failure_prompt_lessons(engine, runtime, analysis)
        engine.state.last_stage_status = "verify_blocked"
        details = {"verify_check": verify_check, "fix_path": "no_rollback", "failure_analysis": analysis}
        if prompt_lesson_learning:
            details["prompt_lesson_learning"] = prompt_lesson_learning
        _store_blocked_round(round_record, last_error=last_error, details=details)
        runtime.round_records.append(update_round_record_outcome(round_record, runtime.context))
        return None  # continue loop → fix_round

    engine.state.mark_completed(StopReason.HARD_ERROR, "BLOCKED")
    engine.state.last_stage_status = "verify_blocked"
    _store_blocked_round(round_record, last_error=last_error, details={"verify_check": verify_check})
    runtime.round_records.append(update_round_record_outcome(round_record, runtime.context))
    return engine._finish_loop(runtime, reason="VERIFY_BLOCKED", last_error=last_error, action=action)


def _ingest_verify_failure_prompt_lessons(engine: Any, runtime: Any, analysis: list[dict]) -> dict[str, Any]:
    if not analysis:
        return {}
    try:
        from kodawari.instincts import ingest_verify_failure_prompt_lessons
    except Exception:
        logger.debug("prompt lesson verify ingest unavailable", exc_info=True)
        return {"processed": 0, "promoted": 0, "reason": "prompt_lesson_module_unavailable"}
    try:
        family = model_family(
            model=str(getattr(engine.config, "executor_model", "") or ""),
            driver=str(getattr(engine.config, "executor_backend", "") or ""),
        )
        return ingest_verify_failure_prompt_lessons(
            Path(engine.config.project_root),
            analysis,
            executor_family=family,
            run_id=_prompt_lesson_run_id(engine, runtime),
        )
    except Exception:
        logger.warning("verify failure prompt lesson ingest failed", exc_info=True)
        return {"processed": 0, "promoted": 0, "reason": "prompt_lesson_ingest_failed"}


def _prompt_lesson_run_id(engine: Any, runtime: Any) -> str:
    state_run_id = str(getattr(getattr(engine, "state", None), "run_id", "") or "").strip()
    if state_run_id:
        return state_run_id
    feature = str(getattr(engine.config, "feature", "") or "").strip()
    task_id = str(getattr(runtime, "task_id", "") or "").strip()
    return ":".join(part for part in (feature, task_id) if part)


def _handle_gate_block(
    engine: Any,
    runtime: Any,
    action: Any,
    round_record: dict[str, Any],
    verify_check: dict[str, Any],
    gate_check: dict[str, Any],
    *,
    gate_attempt: int,
) -> dict[str, Any] | None:
    last_error = str(gate_check.get("blocking_reason") or "GATE_BLOCKED")
    engine.state.add_error(
        last_error,
        phase=Stage.GATE.value,
        action=str(getattr(action, "value", action)),
        category="gate",
    )
    engine.state.last_stage_status = "gate_blocked"
    _store_blocked_round(
        round_record,
        last_error=last_error,
        details={
            "message": "runtime gate blocked",
            "gate_attempt": gate_attempt,
            "verify_check": verify_check,
            "gate_check": gate_check,
        },
    )
    if runtime.peer_review_enabled:
        _reopen_after_gate_block(runtime, gate_check)
        rollback_result = _maybe_rollback(engine, runtime)
        if rollback_result is not None:
            round_record["details"]["rollback"] = rollback_result
        # Incomplete rollback (skipped > 0 or extra_dirty > 0) or missing checkpoint
        # → cannot guarantee clean state → terminate instead of retrying
        if rollback_result is None or not _rollback_is_clean(rollback_result):
            if _rollback_enabled(engine):
                reason = "ROLLBACK_INCOMPLETE" if rollback_result else "ROLLBACK_CHECKPOINT_MISSING"
                detail = (
                    f"skipped={len(rollback_result['skipped'])}, extra_dirty={rollback_result['extra_dirty_found']}"
                    if rollback_result else "no checkpoint available"
                )
                engine.state.mark_completed(StopReason.HARD_ERROR, "BLOCKED")
                runtime.round_records.append(update_round_record_outcome(round_record, runtime.context))
                return engine._finish_loop(
                    runtime,
                    reason=reason,
                    last_error=f"Cannot guarantee clean retry: {detail}",
                    action=action,
                )
        runtime.round_records.append(update_round_record_outcome(round_record, runtime.context))
        _refresh_incremental_compact(engine, runtime, reason="GATE_BLOCKED", trigger_event="gate_blocked")
        _emit_retry_hook(engine, runtime, action, verify_check, gate_check, gate_attempt=gate_attempt)
        return None
    engine.state.mark_completed(StopReason.HARD_ERROR, "BLOCKED")
    runtime.round_records.append(update_round_record_outcome(round_record, runtime.context))
    return engine._finish_loop(runtime, reason="GATE_BLOCKED", last_error=last_error, action=action)


def _refresh_incremental_compact(engine: Any, runtime: Any, *, reason: str, trigger_event: str) -> None:
    refresher = getattr(engine, "_refresh_semantic_compact", None)
    if not callable(refresher):
        return
    try:
        refresher(
            runtime,
            reason=reason,
            trigger_event=trigger_event,
            mode="incremental",
        )
    except Exception:
        logger.warning("incremental semantic compact refresh failed after gate block", exc_info=True)
        return


def _persist_gate_artifacts(engine: Any, gate_check: dict[str, Any]) -> None:
    planning_dir = getattr(engine, "_planning_dir", None)
    if planning_dir is None:
        return
    try:
        # sync_side_effects=False: engine manages its own state; letting
        # sync_gate_side_effects save state here would cause a revision conflict.
        write_gate_artifacts(dict(gate_check), Path(planning_dir), sync_side_effects=False)
    except Exception:
        logger.debug("gate artifact write failed (non-fatal)", exc_info=True)


def _persist_review_evidence_artifact(engine: Any, runtime: Any) -> None:
    planning_dir = getattr(engine, "_planning_dir", None)
    if planning_dir is None:
        return
    artifact_path = Path(planning_dir) / REVIEW_EVIDENCE_FILENAME
    execution_backend = str(
        dict(runtime.execution_result or {}).get("backend")
        or getattr(engine.config, "executor_backend", "")
        or ""
    ).strip()
    run_result = {
        "codex_self_reviews": list(getattr(runtime, "codex_self_reviews", None) or []),
        "peer_review_summary": dict(getattr(runtime, "peer_review_summary", None) or {}),
        "must_fix_open_items": list(
            getattr(runtime, "context", None) and runtime.context.review_feedback.must_fix or []
        ),
    }
    try:
        evidence = derive_runtime_review_evidence(
            run_result=run_result,
            execution_backend=execution_backend,
        )
        if evidence is None:
            return
        artifact = build_review_evidence_artifact(
            feature=str(engine.config.feature),
            planning_dir=Path(planning_dir),
            review_evidence=evidence,
            entrypoint="kodawari autopilot",
        )
        # The current gate-ready runtime is authoritative. A stale FAIL
        # artifact from an earlier blocked run must not keep release blocked.
        write_review_evidence_artifact(artifact_path, artifact)
    except Exception:
        logger.debug("review evidence artifact write failed (non-fatal)", exc_info=True)


def _finish_gate_ready(
    engine: Any,
    runtime: Any,
    round_record: dict[str, Any],
    verify_check: dict[str, Any],
    gate_check: dict[str, Any],
    *,
    gate_attempt: int,
) -> None:
    _persist_gate_artifacts(engine, gate_check)
    _persist_review_evidence_artifact(engine, runtime)
    record_rules_gate_result(runtime.context, passed=True)
    engine.state.last_stage_status = "rules_gate_passed"
    round_record["stage_status"] = "pass"
    round_record["details"] = {
        "message": "rules gate passed",
        "gate_attempt": gate_attempt,
        "verify_check": verify_check,
        "gate_check": gate_check,
        "peer_review_summary": runtime.peer_review_summary,
        "post_execution_qa": runtime.post_execution_qa,
        "architecture_decision_ids": [item.decision_id for item in runtime.context.architecture_decisions],
        "must_fix_remaining": len(runtime.context.review_feedback.must_fix),
        "gate_recommendation": runtime.context.review_feedback.gate_recommendation,
    }
    runtime.round_records.append(update_round_record_outcome(round_record, runtime.context))
    _emit_ready_hook(engine, runtime, verify_check, gate_check, gate_attempt=gate_attempt)


def _store_blocked_round(round_record: dict[str, Any], *, last_error: str, details: dict[str, Any]) -> None:
    round_record["stage_status"] = "blocked"
    round_record["last_error"] = last_error
    round_record["details"] = details


def _reopen_after_gate_block(runtime: Any, gate_check: dict[str, Any]) -> None:
    must_fix_items = gate_must_fix_items(gate_check)
    runtime.context.review_feedback.approved = False
    runtime.context.review_feedback.summary = "Runtime gate blocked; apply targeted fix before retry."
    runtime.context.review_feedback.must_fix = list(must_fix_items)
    runtime.context.review_feedback.blocking_items = list(must_fix_items)
    runtime.context.review_feedback.gate_recommendation = "REVIEW_FIX_REQUIRED"
    record_rules_gate_result(runtime.context, passed=False)
    runtime.peer_review_summary.update(
        {
            "approved": False,
            "must_fix_remaining": len(must_fix_items),
            "last_gate_recommendation": "REVIEW_FIX_REQUIRED",
        }
    )


def _emit_retry_hook(engine: Any, runtime: Any, action: Any, verify_check: dict[str, Any], gate_check: dict[str, Any], *, gate_attempt: int) -> None:
    engine._maybe_emit_hook(
        runtime.hook_events,
        event="post_gate",
        task_id=runtime.task_id,
        task_label=runtime.task_label,
        action=action,
        task_scope=runtime.task_scope,
        details={
            "status": "blocked_retry",
            "gate_attempt": gate_attempt,
            "verify_check": verify_check,
            "gate_check": gate_check,
        },
    )


def _emit_ready_hook(engine: Any, runtime: Any, verify_check: dict[str, Any], gate_check: dict[str, Any], *, gate_attempt: int) -> None:
    engine._maybe_emit_hook(
        runtime.hook_events,
        event="post_gate",
        task_id=runtime.task_id,
        task_label=runtime.task_label,
        action=None,
        task_scope=runtime.task_scope,
        details={
            "status": "rules_gate_passed",
            "gate_attempt": gate_attempt,
            "verify_check": verify_check,
            "gate_check": gate_check,
            "peer_review_summary": runtime.peer_review_summary,
            "post_execution_qa": runtime.post_execution_qa,
        },
    )


def _maybe_run_fidelity_gate(engine: Any, runtime: Any) -> dict[str, Any] | None:
    """Run the post-implement fidelity gate against the executor's output.

    Returns a dict describing findings when at least one blocking finding
    was raised; returns ``None`` otherwise. Disabled via
    ``WORKFLOW_FIDELITY_GATE=0`` env var so operators can opt out while
    iterating on a flaky heuristic.

    The gate runs heuristic checks against the task_card the planner emitted
    versus the files the executor actually changed. It catches three observed
    drift patterns: (a) the task_name mentions named scope items not present
    in any non-test file; (b) a migration registry references SQL files that
    don't exist on disk; (c) changed test files contain ``not in`` assertions
    whose subject overlaps a planner-required token.

    The WHOLE body is wrapped in a broad try/except: this gate is a quality
    heuristic, it must never crash the autopilot loop on its own bug or on
    an unexpected runtime/engine shape.
    """
    # Default OFF for backward compat with existing test suites that use
    # noop_test_only / phantom changed_files. Operators opt in by setting
    # WORKFLOW_FIDELITY_GATE=1 (or true/on/yes). Once we have task-card
    # metadata that distinguishes real vs phantom executions we can flip
    # the default to ON.
    if str(os.environ.get("WORKFLOW_FIDELITY_GATE", "0")).strip().lower() not in {"1", "true", "on", "yes"}:
        return None
    try:
        from kodawari.autopilot.verify.fidelity_gate import run_fidelity_gate

        task_card = getattr(runtime, "task_card", None) or getattr(engine, "_task_card_payload", None) or {}
        if not isinstance(task_card, dict) or not task_card:
            return None
        changed_files = list(getattr(runtime, "changed_files", None) or [])
        if not changed_files:
            exec_result = getattr(runtime, "execution_result", None)
            if isinstance(exec_result, dict):
                changed_files = list(exec_result.get("changed_files") or [])
        if not changed_files:
            return None
        config = getattr(engine, "config", None)
        if config is None or getattr(config, "project_root", None) is None:
            return None
        project_root = Path(config.project_root)
        # Guard against backend modes that echo phantom changed_files. The
        # ``noop_test_only`` executor returns task_card.files_to_change as
        # its changed_files even though it never wrote anything to disk.
        # Require at least one CHANGED file (not just declared) to actually
        # exist with non-empty content — otherwise the executor didn't
        # produce a real implementation and there's nothing to evaluate.
        real_changed = False
        for rel in changed_files:
            if not str(rel).strip():
                continue
            path = project_root / str(rel)
            try:
                if path.exists() and path.stat().st_size > 0:
                    real_changed = True
                    break
            except OSError:
                continue
        if not real_changed:
            return None
        result = run_fidelity_gate(
            project_root=project_root,
            task_card=task_card,
            changed_files=changed_files,
        )
        if result.passed:
            return None
        return result.to_dict()
    except Exception as exc:  # noqa: BLE001 — gate must never block the loop
        logger.warning("fidelity_gate setup or execution raised: %s; skipping", exc)
        return None


def run_proceed_round(engine: Any, *, runtime: Any, action: Any, round_record: dict[str, Any]) -> dict[str, Any]:
    # P1.7: fidelity gate runs BEFORE the review-evidence validator so a
    # task that under-delivers gets caught even when the LLM reviewer was
    # over-permissive (or skipped entirely on non-first tasks).
    fidelity_block = _maybe_run_fidelity_gate(engine, runtime)
    if fidelity_block is not None and fidelity_block.get("blocking_count"):
        must_fix = [
            f"FIDELITY_GATE [{f['kind']}] {f['message']}"
            for f in fidelity_block.get("findings", [])
            if f.get("severity") == "block"
        ]
        last_error = (
            f"fidelity gate blocked with {fidelity_block.get('blocking_count')} finding(s); "
            f"first: {must_fix[0] if must_fix else 'unknown'}"
        )
        runtime.context.review_feedback.approved = False
        runtime.context.review_feedback.summary = "Fidelity gate blocked — executor under-delivered against task card spec."
        runtime.context.review_feedback.must_fix = list(must_fix)
        runtime.context.review_feedback.blocking_items = list(must_fix)
        runtime.context.review_feedback.gate_recommendation = "REVIEW_FIX_REQUIRED"
        engine.state.add_error(
            last_error,
            phase=Stage.GATE.value,
            action=str(getattr(action, "value", action)),
            category="fidelity",
        )
        engine.state.mark_completed(StopReason.HARD_ERROR, "BLOCKED")
        engine.state.last_stage_status = "fidelity_blocked"
        round_record["stage_status"] = "blocked"
        round_record["last_error"] = last_error
        round_record["details"] = {"fidelity_gate": fidelity_block}
        runtime.round_records.append(update_round_record_outcome(round_record, runtime.context))
        _emit_hook = getattr(engine, "_maybe_emit_hook", None)
        if callable(_emit_hook):
            _emit_hook(
                runtime.hook_events,
                event="post_gate",
                task_id=runtime.task_id,
                task_label=runtime.task_label,
                action=action,
                task_scope=runtime.task_scope,
                details={"status": "fidelity_blocked", "fidelity_gate": fidelity_block},
            )
        return engine._finish_loop(runtime, reason="FIDELITY_GATE_BLOCKED", last_error=last_error, action=action)

    validator = getattr(engine, "_validate_proceed_review_evidence", None)
    if callable(validator):
        evidence = validator(runtime)
        # Cache so _finish_loop compliance reporting can reuse without re-validating.
        runtime.last_proceed_evidence = dict(evidence)
        status = str(evidence.get("status") or "").upper()
        if status == "FAIL":
            last_error = str(evidence.get("blocking_reason") or "review evidence gate blocked")
            engine.state.add_error(
                last_error,
                phase=Stage.PLAN_REVIEW.value,
                action=str(getattr(action, "value", action)),
                category="review",
            )
            engine.state.mark_completed(StopReason.HARD_ERROR, "BLOCKED")
            engine.state.last_stage_status = "review_blocked"
            round_record["stage_status"] = "blocked"
            round_record["last_error"] = last_error
            round_record["details"] = {"review_evidence": evidence}
            runtime.round_records.append(update_round_record_outcome(round_record, runtime.context))
            # Emit post_review hook so observers see the block regardless of call site.
            _emit_hook = getattr(engine, "_maybe_emit_hook", None)
            if callable(_emit_hook):
                _emit_hook(
                    runtime.hook_events,
                    event="post_review",
                    task_id=runtime.task_id,
                    task_label=runtime.task_label,
                    action=action,
                    task_scope=runtime.task_scope,
                    details={"status": "blocked", "review_evidence": dict(evidence)},
            )
            return engine._finish_loop(runtime, reason="OPUS_REVIEW_BLOCKED", last_error=last_error, action=action)
    _persist_review_evidence_artifact(engine, runtime)
    engine.state.current_stage = Stage.GATE
    engine.state.last_stage_status = "ready_for_gate"
    round_record["stage_status"] = "ready"
    round_record["details"] = {
        "message": "handoff to gate",
        "verify_check": dict(runtime.verify_check or {}),
        "gate_check": dict(runtime.gate_check or {}),
        "peer_review_summary": runtime.peer_review_summary,
        "post_execution_qa": runtime.post_execution_qa,
        "architecture_decision_ids": [item.decision_id for item in runtime.context.architecture_decisions],
        "must_fix_remaining": len(runtime.context.review_feedback.must_fix),
        "gate_recommendation": runtime.context.review_feedback.gate_recommendation,
    }
    runtime.round_records.append(update_round_record_outcome(round_record, runtime.context))
    engine._maybe_emit_hook(
        runtime.hook_events,
        event="auto_gate",
        task_id=runtime.task_id,
        task_label=runtime.task_label,
        action=action,
        task_scope=runtime.task_scope,
        details={"status": "ok", "gate_check": dict(runtime.gate_check or {})},
    )
    return engine._finish_loop(runtime, reason="PROCEED_TO_GATE", action=action)

