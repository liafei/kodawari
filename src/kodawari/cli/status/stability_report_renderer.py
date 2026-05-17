"""Aggregation and markdown rendering for automation stability reports."""

from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any

from kodawari.cli.gate.root_cause_buckets import classify_root_cause_bucket, ranked_root_causes
from kodawari.cli.status.stability_report_markdown import render_markdown_report
from kodawari.cli.status.stability_report_observation import (
    normalize_compact_runtime_key,
    normalize_instincts_status_key,
    normalize_round_outcome_key,
    normalize_run_outcome_key,
    round_record_blob,
)


SETUP_ERROR_TOKENS = ("error at setup", "failed at setup", "fixture", "scopemismatch")
TASK_TYPE_KEYWORDS: tuple[tuple[str, tuple[str, ...], bool], ...] = (
    ("Backend Bootstrap", ("backend", "bootstrap"), True),
    ("API Endpoint", ("api", "endpoint", "route"), False),
    ("Schema Migration", ("schema", "migration", "table", "column"), False),
    ("Ranking Rules", ("ranking", "rank", "score", "priority", "sort"), False),
)


def _normalize_error_signature(message: str) -> str:
    text = str(message or "").lower().strip()
    if not text:
        return "(empty-error)"
    patterns = [
        (r"\d{4}-\d{2}-\d{2}t\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:z|[+-]\d{2}:\d{2})?", "<ts>"),
        (r"attempt\s+\d+\s*/\s*\d+", "attempt <n>/<n>"),
        (r"attempt\s+\d+", "attempt <n>"),
        (r"line\s+\d+", "line <n>"),
        (r"cycle\s+\d+", "cycle <n>"),
        (r"\b0x[0-9a-f]+\b", "<hex>"),
        (r"\b\d+\b", "<n>"),
    ]
    normalized = text
    for pattern, replacement in patterns:
        normalized = re.sub(pattern, replacement, normalized)
    return re.sub(r"\s+", " ", normalized).strip()[:300]


def _is_blocking_stage_status(value: str) -> bool:
    status = str(value or "").strip().lower()
    blocking = "error" in status or "fail" in status or "blocked" in status
    return bool(status) and (blocking or status in {"setup_error", "verify_failed"})


def _classify_task_type(label: str) -> str:
    text = str(label or "").lower()
    for task_type, keywords, require_all in TASK_TYPE_KEYWORDS:
        if _task_type_matches(text, keywords, require_all):
            return task_type
    return "Other"


def _task_type_matches(text: str, keywords: tuple[str, ...], require_all: bool) -> bool:
    if require_all:
        return all(token in text for token in keywords)
    return any(token in text for token in keywords)


def _safe_pct(numerator: int | float, denominator: int | float) -> float:
    if float(denominator) <= 0:
        return 0.0
    return (float(numerator) / float(denominator)) * 100.0


def _int_or_zero(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _blocking_stage_for_run(run: dict[str, Any]) -> str:
    for record in reversed(run["rounds"]):
        if _is_blocking_stage_status(record.get("stage_status")):
            return str(record.get("stage", "UNKNOWN") or "UNKNOWN").upper()
    return str(run["state"].get("current_stage", "UNKNOWN") or "UNKNOWN").upper()


def _increment_named_counter(counter: dict[str, int], key: str) -> None:
    normalized = str(key or "").strip() or "unknown"
    counter[normalized] = counter.get(normalized, 0) + 1


def _basic_run_totals(runs: list[dict[str, Any]]) -> tuple[int, int, int, int]:
    return (
        len(runs),
        sum(1 for run in runs if str(run["state"].get("stop_reason", "")).upper() == "PASS"),
        sum(int(run["tasks_total"]) for run in runs),
        sum(int(run["tasks_completed"]) for run in runs),
    )


def _build_report_counters(runs: list[dict[str, Any]]) -> dict[str, Any]:
    counters: dict[str, Any] = {
        "stop_reason_counts": {},
        "stage_block_counts": {},
        "issue_counts": {"429 Rate Limit": 0, "VERIFY Setup Error": 0, "Gate Blocked": 0, "Timeout": 0},
        "task_type_stats": _empty_task_type_stats(),
        "normalized_error_counts": {},
        "compact_runtime_counts": {},
        "instincts_status_counts": {},
        "round_outcome_counts": {},
        "run_outcome_counts": {},
        "root_cause_bucket_counts": {},
    }
    for run in runs:
        _update_run_aggregates(
            run=run,
            stop_reason_counts=counters["stop_reason_counts"],
            stage_block_counts=counters["stage_block_counts"],
            issue_counts=counters["issue_counts"],
            task_type_stats=counters["task_type_stats"],
            normalized_error_counts=counters["normalized_error_counts"],
            round_outcome_counts=counters["round_outcome_counts"],
        )
        _increment_named_counter(counters["compact_runtime_counts"], normalize_compact_runtime_key(run))
        _increment_named_counter(counters["instincts_status_counts"], normalize_instincts_status_key(run))
        run_outcome = normalize_run_outcome_key(run)
        _increment_named_counter(counters["run_outcome_counts"], run_outcome)
        _increment_named_counter(counters["root_cause_bucket_counts"], _run_root_cause_bucket(run, run_outcome=run_outcome))
    return counters


def _empty_task_type_stats() -> dict[str, dict[str, float]]:
    return {
        "Backend Bootstrap": {"total": 0.0, "completed": 0.0, "cycles": 0.0, "runs": 0.0},
        "API Endpoint": {"total": 0.0, "completed": 0.0, "cycles": 0.0, "runs": 0.0},
        "Schema Migration": {"total": 0.0, "completed": 0.0, "cycles": 0.0, "runs": 0.0},
        "Ranking Rules": {"total": 0.0, "completed": 0.0, "cycles": 0.0, "runs": 0.0},
        "Other": {"total": 0.0, "completed": 0.0, "cycles": 0.0, "runs": 0.0},
    }


def _update_task_type_stats_for_run(
    task_type_stats: dict[str, dict[str, float]],
    *,
    run: dict[str, Any],
    state: dict[str, Any],
) -> None:
    completed_tasks = state.get("completed_tasks", [])
    completed_labels = set(completed_tasks if isinstance(completed_tasks, list) else [])
    classified_in_run: set[str] = set()
    for task_label in run["tasks"]:
        task_type = _classify_task_type(task_label)
        stats = task_type_stats[task_type]
        stats["total"] += 1
        if task_label in completed_labels:
            stats["completed"] += 1
        if task_type in classified_in_run:
            continue
        stats["cycles"] += float(state.get("cycle", 0) or 0)
        stats["runs"] += 1
        classified_in_run.add(task_type)


def _has_rate_limit(blob: str) -> bool:
    return "429" in blob or "rate limit" in blob


def _has_gate_blocked(blob: str) -> bool:
    return "gate blocked" in blob or ("gate" in blob and "blocked" in blob)


def _has_timeout(blob: str) -> bool:
    return "timeout" in blob


def _has_setup_error(blob: str) -> bool:
    return any(token in blob for token in SETUP_ERROR_TOKENS)


def _update_issue_counts_from_blob(issue_counts: dict[str, int], blob: str) -> None:
    detectors = (
        ("429 Rate Limit", _has_rate_limit),
        ("Gate Blocked", _has_gate_blocked),
        ("Timeout", _has_timeout),
        ("VERIFY Setup Error", _has_setup_error),
    )
    for issue, detector in detectors:
        if detector(blob):
            issue_counts[issue] += 1


def _is_gate_blocked(gate_result: dict[str, Any] | None) -> bool:
    if not gate_result:
        return False
    return str(gate_result.get("total_status", "")).upper() == "BLOCKED"


def _record_stage_block(stage_block_counts: dict[str, int], record: dict[str, Any]) -> None:
    if not _is_blocking_stage_status(record.get("stage_status")):
        return
    key = str(record.get("stage", "UNKNOWN") or "UNKNOWN").upper()
    stage_block_counts[key] = stage_block_counts.get(key, 0) + 1


def _update_round_issue_counts(
    *,
    run: dict[str, Any],
    stage_block_counts: dict[str, int],
    issue_counts: dict[str, int],
    round_outcome_counts: dict[str, int],
) -> None:
    gate_result = run.get("gate_result") if isinstance(run.get("gate_result"), dict) else None
    if _is_gate_blocked(gate_result):
        issue_counts["Gate Blocked"] += 1
    for record in run["rounds"]:
        _record_stage_block(stage_block_counts, record)
        _update_issue_counts_from_blob(issue_counts, round_record_blob(record))
        _increment_named_counter(round_outcome_counts, normalize_round_outcome_key(record))


def _collect_state_messages(state: dict[str, Any]) -> list[str]:
    messages: list[str] = []
    history = state.get("error_history", [])
    if isinstance(history, list):
        messages.extend(str(item) for item in history if str(item).strip())
    last_error = str(state.get("last_error") or "").strip()
    if last_error:
        messages.append(last_error)
    return messages


def _update_normalized_error_counts(
    normalized_error_counts: dict[str, dict[str, Any]],
    *,
    messages: list[str],
    run_id: str,
    stage: str,
) -> None:
    for message in messages:
        signature = _normalize_error_signature(message)
        entry = normalized_error_counts.setdefault(
            signature,
            {"count": 0, "sample": message, "run_id": run_id, "stage": stage},
        )
        entry["count"] += 1


def _build_top_errors(normalized_error_counts: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    ranked = (
        {"signature": key, "count": value["count"], "run_id": value["run_id"], "stage": value["stage"]}
        for key, value in normalized_error_counts.items()
    )
    return sorted(ranked, key=lambda item: item["count"], reverse=True)[:5]


def _error_categories_for_run(run: dict[str, Any]) -> list[str]:
    state = dict(run.get("state") or {})
    raw_events = state.get("error_events")
    categories: list[str] = []
    if not isinstance(raw_events, list):
        return categories
    for item in raw_events:
        if not isinstance(item, dict):
            continue
        category = str(item.get("category") or "").strip().lower()
        if category:
            categories.append(category)
    return categories


def _latest_round_outcome_key(run: dict[str, Any]) -> str:
    for record in reversed(list(run.get("rounds") or [])):
        if not isinstance(record, dict):
            continue
        outcome = normalize_round_outcome_key(record)
        if outcome and outcome != "unknown":
            return outcome
    return ""


def _run_failure_messages(run: dict[str, Any]) -> list[str]:
    state = dict(run.get("state") or {})
    messages = _collect_state_messages(state)
    for record in list(run.get("rounds") or []):
        if not isinstance(record, dict):
            continue
        last_error = str(record.get("last_error") or "").strip()
        if last_error:
            messages.append(last_error)
    gate_result = dict(run.get("gate_result") or {})
    gate_blocking_reason = str(gate_result.get("blocking_reason") or "").strip()
    if gate_blocking_reason:
        messages.append(gate_blocking_reason)
    final_outcome = dict(dict(run.get("workflow_chain") or {}).get("final_outcome") or {})
    final_blocking_reason = str(final_outcome.get("blocking_reason") or "").strip()
    if final_blocking_reason:
        messages.append(final_blocking_reason)
    return messages


def _run_root_cause_bucket(run: dict[str, Any], *, run_outcome: str) -> str:
    state = dict(run.get("state") or {})
    workflow_chain = dict(run.get("workflow_chain") or {})
    gate_result = dict(run.get("gate_result") or {})
    upstream_verify = dict(workflow_chain.get("upstream") or {}).get("verify") or {}
    final_outcome = dict(workflow_chain.get("final_outcome") or {})
    status = str(state.get("final_status") or state.get("stop_reason") or "")
    return classify_root_cause_bucket(
        status=status,
        stop_reason=str(state.get("stop_reason") or ""),
        gate_status=str(gate_result.get("total_status") or ""),
        verify_status=str(dict(upstream_verify).get("status") or ""),
        round_outcome=_latest_round_outcome_key(run),
        run_outcome=run_outcome,
        error_categories=_error_categories_for_run(run),
        failure_messages=_run_failure_messages(run),
        blocking_reason=str(final_outcome.get("blocking_reason") or gate_result.get("blocking_reason") or ""),
        headline=str(state.get("last_error") or ""),
    )


def _build_suggestions(issue_counts: dict[str, int], stop_reason_counts: dict[str, int]) -> list[str]:
    suggestions: list[str] = []
    if issue_counts["VERIFY Setup Error"] > 0:
        suggestions.append("扩充 scoped verify 覆盖面，并持续记录 VERIFY setup 诊断字段。")
    if stop_reason_counts.get("MAX_CYCLES", 0) > 0:
        suggestions.append("对多次触发 MAX_CYCLES 的任务继续拆分，并补充 pattern hints。")
    if issue_counts["429 Rate Limit"] > 0:
        suggestions.append("增加执行退避或降低并发，减轻限流影响。")
    if suggestions:
        return suggestions[:3]
    return ["当前样本整体稳定，继续按周收集 run_id 做趋势对比。"]


def _average_from_runs(runs: list[dict[str, Any]], key: str) -> float:
    if not runs:
        return 0.0
    total = sum(float(run["state"].get(key, 0) or 0) for run in runs)
    return total / float(len(runs))


def _aggregate_subtask_totals(runs: list[dict[str, Any]]) -> tuple[int, int, int]:
    total = sum(int(run["subtasks_total"]) for run in runs)
    done = sum(int(run["subtasks_done"]) for run in runs)
    failed = sum(int(run["subtasks_failed"]) for run in runs)
    return total, done, failed


def _error_category_counts(runs: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for run in runs:
        state = dict(run.get("state") or {})
        raw_events = state.get("error_events")
        if isinstance(raw_events, list) and raw_events:
            _accumulate_error_event_categories(counts, raw_events)
            continue
        fallback = _collect_state_messages(state)
        if fallback:
            counts["runtime"] = counts.get("runtime", 0) + len(fallback)
    return counts


def _accumulate_error_event_categories(counts: dict[str, int], raw_events: list[Any]) -> None:
    for item in raw_events:
        if not isinstance(item, dict):
            continue
        category = str(item.get("category") or "runtime").strip().lower() or "runtime"
        counts[category] = counts.get(category, 0) + 1


def _repeated_failure_rate(runs: list[dict[str, Any]]) -> float:
    total = 0
    repeated = 0
    for run in runs:
        messages = _collect_state_messages(dict(run.get("state") or {}))
        if not messages:
            continue
        total += len(messages)
        signature_counts: dict[str, int] = {}
        for message in messages:
            signature = _normalize_error_signature(message)
            signature_counts[signature] = signature_counts.get(signature, 0) + 1
        repeated += sum(count for count in signature_counts.values() if count > 1)
    return _safe_pct(repeated, total)


def _compact_hit_rate(runs: list[dict[str, Any]]) -> float:
    hits = 0
    for run in runs:
        if isinstance(run.get("semantic_compact"), dict):
            hits += 1
            continue
        if isinstance(run.get("compact_context"), dict):
            hits += 1
    return _safe_pct(hits, len(runs))


def _learned_instinct_hit_rate(runs: list[dict[str, Any]]) -> float:
    hits = sum(1 for run in runs if _run_has_learned_instinct_hit(run))
    return _safe_pct(hits, len(runs))


def _run_has_learned_instinct_hit(run: dict[str, Any]) -> bool:
    semantic = run.get("semantic_compact")
    if isinstance(semantic, dict):
        source = str(semantic.get("verify_target_source") or "").strip().lower()
        if source == "instinct_hints":
            return True
    compact = run.get("compact_context")
    if isinstance(compact, dict):
        hints_count = int(compact.get("instinct_hints_count", 0) or 0)
        if hints_count > 0:
            return True
    for record in list(run.get("rounds") or []):
        if not isinstance(record, dict):
            continue
        details = dict(record.get("details") or {})
        verify_check = details.get("verify_check")
        if isinstance(verify_check, dict):
            source = str(verify_check.get("verify_target_source") or "").strip().lower()
            if source == "instinct_hints":
                return True
    return False


def _setup_recovery_success_rate(runs: list[dict[str, Any]]) -> float:
    attempted = 0
    succeeded = 0
    for run in runs:
        state = dict(run.get("state") or {})
        attempted += int(state.get("verify_setup_recovery_attempted", 0) or 0)
        succeeded += int(state.get("verify_setup_recovery_succeeded", 0) or 0)
    return _safe_pct(succeeded, attempted)


def _stuck_round_limit_distribution(runs: list[dict[str, Any]]) -> dict[str, int]:
    distribution = {"stuck": 0, "round_limit": 0}
    for run in runs:
        state = dict(run.get("state") or {})
        stop_reason = str(state.get("stop_reason") or "").strip().upper()
        last_stage_status = str(state.get("last_stage_status") or "").strip().lower()
        if stop_reason == "STUCK":
            distribution["stuck"] += 1
        if last_stage_status == "round_limit":
            distribution["round_limit"] += 1
            continue
        for record in list(run.get("rounds") or []):
            if not isinstance(record, dict):
                continue
            stage_status = str(record.get("stage_status") or "").strip().lower()
            if stage_status == "round_limit":
                distribution["round_limit"] += 1
                break
    return distribution


def _build_test_params(options: dict[str, Any]) -> str:
    return ", ".join(
        [
            f"task_max_cycles={options.get('task_max_cycles', 'N/A')}",
            f"task_auto_runs={options.get('task_auto_runs', 'N/A')}",
            f"timeout_per_round={options.get('timeout_per_round', 'N/A')}",
        ]
    )


def _update_run_aggregates(
    *,
    run: dict[str, Any],
    stop_reason_counts: dict[str, int],
    stage_block_counts: dict[str, int],
    issue_counts: dict[str, int],
    task_type_stats: dict[str, dict[str, float]],
    normalized_error_counts: dict[str, dict[str, Any]],
    round_outcome_counts: dict[str, int],
) -> None:
    state = run["state"]
    stop_reason = str(state.get("stop_reason", "UNKNOWN") or "UNKNOWN").upper()
    stop_reason_counts[stop_reason] = stop_reason_counts.get(stop_reason, 0) + 1
    _update_task_type_stats_for_run(task_type_stats, run=run, state=state)
    stage = _blocking_stage_for_run(run)
    _update_round_issue_counts(
        run=run,
        stage_block_counts=stage_block_counts,
        issue_counts=issue_counts,
        round_outcome_counts=round_outcome_counts,
    )
    _update_normalized_error_counts(
        normalized_error_counts,
        messages=_collect_state_messages(state),
        run_id=run["run_id"],
        stage=stage,
    )


def _report_avg_task_completion_ratio(*, completed_tasks: int, total_tasks: int, total_runs: int) -> str:
    if total_runs <= 0:
        return "0.00/0.00"
    return f"{completed_tasks / total_runs:.2f}/{total_tasks / total_runs:.2f}"


def _report_average_metrics(runs: list[dict[str, Any]]) -> dict[str, float]:
    avg_review_rounds = 0.0
    if runs:
        avg_review_rounds = sum(_int_or_zero(run.get("review_rounds_used")) for run in runs) / float(len(runs))
    return {
        "avg_cycles": _average_from_runs(runs, "cycle"),
        "avg_tokens": _average_from_runs(runs, "tokens_used"),
        "avg_review_rounds_used": avg_review_rounds,
    }


def _base_report_payload(
    *,
    options: dict[str, Any],
    total_runs: int,
    pass_runs: int,
    total_tasks: int,
    completed_tasks: int,
) -> dict[str, Any]:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "test_params": _build_test_params(options),
        "total_runs": total_runs,
        "completion_rate": _safe_pct(pass_runs, total_runs),
        "avg_task_completion_ratio": _report_avg_task_completion_ratio(
            completed_tasks=completed_tasks,
            total_tasks=total_tasks,
            total_runs=total_runs,
        ),
        "cycle_target": options.get("task_max_cycles"),
        "token_target": options.get("token_budget_target"),
    }


def _report_tail_payload(*, options: dict[str, Any], suggestions: list[str]) -> dict[str, Any]:
    return {
        "suggestions": suggestions,
        "long_term_suggestions": [
            "继续沉淀领域模式，降低自由生成比例。",
            "固定同一批 run 选择规则，按周输出稳定性趋势报告。",
        ],
        "warnings": [str(item) for item in options.get("warnings", [])],
        "project_root": str(options.get("project_root", "")),
        "resolved_planning_dirs": [str(item) for item in options.get("resolved_planning_dirs", [])],
    }


def _report_metric_payload(
    *,
    total_runs: int,
    stop_reason_counts: dict[str, int],
    stage_block_counts: dict[str, int],
    issue_counts: dict[str, int],
    top_errors: list[dict[str, Any]],
    root_cause_bucket_counts: dict[str, int],
    top_root_causes: list[dict[str, Any]],
    task_type_stats: dict[str, dict[str, float]],
    subtask_total: int,
    subtask_done: int,
    subtask_failed: int,
    compact_runtime_counts: dict[str, int],
    instincts_status_counts: dict[str, int],
    round_outcome_counts: dict[str, int],
    run_outcome_counts: dict[str, int],
    error_category_counts: dict[str, int],
    repeated_failure_rate: float,
    compact_hit_rate: float,
    learned_instinct_hit_rate: float,
    setup_recovery_success_rate: float,
    stuck_round_limit_counts: dict[str, int],
) -> dict[str, Any]:
    return {
        "stop_reason_counts": stop_reason_counts,
        "stage_block_counts": stage_block_counts,
        "issue_counts": issue_counts,
        "top_errors": top_errors,
        "root_cause_bucket_counts": dict(root_cause_bucket_counts),
        "top_root_causes": top_root_causes,
        "task_type_stats": task_type_stats,
        "avg_subtasks": (float(subtask_total) / float(total_runs)) if total_runs > 0 else 0.0,
        "subtask_completion_rate": _safe_pct(subtask_done, subtask_total),
        "subtask_failure_rate": _safe_pct(subtask_failed, subtask_total),
        "compact_runtime_counts": dict(compact_runtime_counts),
        "instincts_status_counts": dict(instincts_status_counts),
        "round_outcome_counts": dict(round_outcome_counts),
        "run_outcome_counts": dict(run_outcome_counts),
        "error_category_counts": dict(error_category_counts),
        "repeated_failure_rate": float(repeated_failure_rate),
        "compact_hit_rate": float(compact_hit_rate),
        "learned_instinct_hit_rate": float(learned_instinct_hit_rate),
        "setup_recovery_success_rate": float(setup_recovery_success_rate),
        "stuck_round_limit_counts": dict(stuck_round_limit_counts),
    }


def _build_report_payload(
    *,
    options: dict[str, Any],
    runs: list[dict[str, Any]],
    summary: dict[str, int],
    metric_payload: dict[str, Any],
    suggestions: list[str],
) -> dict[str, Any]:
    payload = _base_report_payload(
        options=options,
        total_runs=summary["total_runs"],
        pass_runs=summary["pass_runs"],
        total_tasks=summary["total_tasks"],
        completed_tasks=summary["completed_tasks"],
    )
    payload.update(_report_average_metrics(runs))
    payload.update(metric_payload)
    payload.update(_report_tail_payload(options=options, suggestions=suggestions))
    return payload


def build_report_data(runs: list[dict[str, Any]], report_options: dict[str, Any] | None = None) -> dict[str, Any]:
    options = dict(report_options or {})
    total_runs, pass_runs, total_tasks, completed_tasks = _basic_run_totals(runs)
    counters = _build_report_counters(runs)
    top_errors = _build_top_errors(counters["normalized_error_counts"])
    top_root_causes = ranked_root_causes(counters["root_cause_bucket_counts"])
    suggestions = _build_suggestions(counters["issue_counts"], counters["stop_reason_counts"])
    subtask_total, subtask_done, subtask_failed = _aggregate_subtask_totals(runs)
    category_counts = _error_category_counts(runs)
    repeated_rate = _repeated_failure_rate(runs)
    compact_rate = _compact_hit_rate(runs)
    instinct_rate = _learned_instinct_hit_rate(runs)
    setup_recovery_rate = _setup_recovery_success_rate(runs)
    stuck_round_limit = _stuck_round_limit_distribution(runs)
    summary = {
        "total_runs": total_runs,
        "pass_runs": pass_runs,
        "total_tasks": total_tasks,
        "completed_tasks": completed_tasks,
    }
    metric_payload = _report_metric_payload(
        total_runs=total_runs,
        stop_reason_counts=counters["stop_reason_counts"],
        stage_block_counts=counters["stage_block_counts"],
        issue_counts=counters["issue_counts"],
        top_errors=top_errors,
        root_cause_bucket_counts=counters["root_cause_bucket_counts"],
        top_root_causes=top_root_causes,
        task_type_stats=counters["task_type_stats"],
        subtask_total=subtask_total,
        subtask_done=subtask_done,
        subtask_failed=subtask_failed,
        compact_runtime_counts=counters["compact_runtime_counts"],
        instincts_status_counts=counters["instincts_status_counts"],
        round_outcome_counts=counters["round_outcome_counts"],
        run_outcome_counts=counters["run_outcome_counts"],
        error_category_counts=category_counts,
        repeated_failure_rate=repeated_rate,
        compact_hit_rate=compact_rate,
        learned_instinct_hit_rate=instinct_rate,
        setup_recovery_success_rate=setup_recovery_rate,
        stuck_round_limit_counts=stuck_round_limit,
    )
    return _build_report_payload(
        options=options,
        runs=runs,
        summary=summary,
        metric_payload=metric_payload,
        suggestions=suggestions,
    )


def render_stability_markdown(
    runs: list[dict[str, Any]],
    report_options: dict[str, Any] | None = None,
) -> str:
    data = build_report_data(runs, report_options)
    return render_markdown_report(data, runs)


__all__ = ["build_report_data", "render_stability_markdown"]

