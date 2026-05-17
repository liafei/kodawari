"""Shared root-cause bucket inference for lane/operator observability."""

from __future__ import annotations

from typing import Any, NamedTuple


ROOT_CAUSE_BUCKET_LABELS: dict[str, str] = {
    "stable_pass": "Stable pass",
    "env_missing": "Environment missing",
    "rate_limit": "Rate limited",
    "timeout": "Timeout",
    "external_gateway": "External gateway or network",
    "gate_blocked": "Gate blocked",
    "verify_setup": "Verify setup failure",
    "verify_failure": "Verify failure",
    "task_blocked": "Task blocked",
    "ready_for_gate": "Ready for gate",
    "max_cycles": "Max cycles",
    "no_progress": "No progress",
    "stuck": "Repeated failure / stuck",
    "review_feedback": "Review feedback",
    "implementation_error": "Implementation error",
    "flaky_failure": "Flaky failure",
    "runtime_error": "Runtime error",
    "unknown": "Unknown",
}

_MISSING_ENV_TOKENS = (
    "required integration environment is incomplete",
    "missing environment",
    "missing env",
    "workflow_opus_api_key",
    "workflow_opus_gateway",
)
_RATE_LIMIT_TOKENS = ("429", "rate limit", "too many requests")
_TIMEOUT_TOKENS = ("timeout", "timed out", "deadline exceeded")
_EXTERNAL_GATEWAY_TOKENS = (
    "gateway",
    "connection refused",
    "name or service not known",
    "service unavailable",
    "temporarily unavailable",
    "dns",
    "ssl",
    "tls",
    "proxy",
    "socket",
    "network is unreachable",
)
_GATE_BLOCKED_TOKENS = ("gate blocked", "advisory gate", "quality gate", "blocking_violations", "blocking violations")
_VERIFY_SETUP_TOKENS = ("error at setup", "failed at setup", "fixture", "scopemismatch", "setup_error", "verify setup")
_VERIFY_FAILURE_TOKENS = ("assertionerror", "assertion failed", "verify_failed", "verification failed")
_TASK_BLOCKED_TOKENS = ("task blocked", "blocked:task_blocked", "blocked by task", "task_blocked")
_MAX_CYCLES_TOKENS = ("max_cycles", "max cycles", "cycle limit", "round_limit")
_NO_PROGRESS_TOKENS = ("no_progress", "no progress", "no file changes")
_STUCK_TOKENS = ("stuck", "repeated error")
_REVIEW_TOKENS = ("changes requested", "review rejected", "review blocked")
_CLASSIFICATION_BUCKET_HINTS: dict[str, str] = {
    "lane.stable_pass": "stable_pass",
    "lane_pass": "stable_pass",
    "lane.integration_env_missing": "env_missing",
    "integration_env_missing": "env_missing",
    "lane.integration_env_missing_fail_closed": "env_missing",
    "integration_env_missing_fail_closed": "env_missing",
    "lane.flaky_failure": "flaky_failure",
    "lane_flaky": "flaky_failure",
}


def _s(v: object, upper: bool = False) -> str:
    result = str(v or "").strip()
    return result.upper() if upper else result.lower()


def _nonempty_lower(items: list[object] | tuple[object, ...] | set[object] | None) -> list[str]:
    return [t for t in (_s(item) for item in list(items or [])) if t]


def _build_message_blob(
    failure_messages: list[str] | tuple[str, ...] | None,
    blocking_reason: str,
    headline: str,
) -> str:
    parts = [*list(failure_messages or []), blocking_reason, headline]
    return " | ".join(t for t in (_s(p) for p in parts) if t)


class _Ctx(NamedTuple):
    classification_key: str
    classification_bucket: str
    status_key: str
    stop_reason_key: str
    gate_status_key: str
    verify_status_key: str
    round_outcome_key: str
    run_outcome_key: str
    categories: frozenset[str]
    missing: list[str]
    message_blob: str


def _make_ctx(
    classification_id: str,
    status: str,
    stop_reason: str,
    gate_status: str,
    verify_status: str,
    round_outcome: str,
    run_outcome: str,
    error_categories: list[str] | tuple[str, ...] | set[str] | None,
    missing_env: list[str] | tuple[str, ...] | set[str] | None,
    failure_messages: list[str] | tuple[str, ...] | None,
    blocking_reason: str,
    headline: str,
) -> _Ctx:
    classification_key = _s(classification_id)
    return _Ctx(
        classification_key=classification_key,
        classification_bucket=_CLASSIFICATION_BUCKET_HINTS.get(classification_key, ""),
        status_key=_s(status, upper=True),
        stop_reason_key=_s(stop_reason, upper=True),
        gate_status_key=_s(gate_status, upper=True),
        verify_status_key=_s(verify_status, upper=True),
        round_outcome_key=_s(round_outcome),
        run_outcome_key=_s(run_outcome),
        categories=frozenset(_nonempty_lower(error_categories)),
        missing=_nonempty_lower(missing_env),
        message_blob=_build_message_blob(failure_messages, blocking_reason, headline),
    )


def _check_stable_pass(ctx: _Ctx) -> str | None:
    if ctx.classification_key == "lane.stable_pass" or ctx.classification_bucket == "stable_pass":
        return "stable_pass"
    if ctx.status_key == "PASS" and ctx.gate_status_key != "BLOCKED" and not ctx.run_outcome_key.startswith("blocked"):
        return "stable_pass"
    return None


def _check_ready_for_gate(ctx: _Ctx) -> str | None:
    return "ready_for_gate" if ctx.run_outcome_key == "ready_for_gate" else None


def _check_env_missing(ctx: _Ctx) -> str | None:
    if ctx.missing or ctx.classification_key in {"lane.integration_env_missing", "lane.integration_env_missing_fail_closed"}:
        return "env_missing"
    if ctx.classification_bucket == "env_missing" or _contains_any(ctx.message_blob, _MISSING_ENV_TOKENS):
        return "env_missing"
    return None


def _check_rate_limit(ctx: _Ctx) -> str | None:
    return "rate_limit" if _contains_any(ctx.message_blob, _RATE_LIMIT_TOKENS) else None


def _check_timeout(ctx: _Ctx) -> str | None:
    return "timeout" if _contains_any(ctx.message_blob, _TIMEOUT_TOKENS) else None


def _check_external_gateway(ctx: _Ctx) -> str | None:
    if "external_gateway" in ctx.categories or _contains_any(ctx.message_blob, _EXTERNAL_GATEWAY_TOKENS):
        return "external_gateway"
    return None


def _check_gate_blocked(ctx: _Ctx) -> str | None:
    if ctx.gate_status_key == "BLOCKED" or "gate" in ctx.categories or ctx.run_outcome_key == "blocked_by_gate":
        return "gate_blocked"
    return "gate_blocked" if _contains_any(ctx.message_blob, _GATE_BLOCKED_TOKENS) else None


def _check_verify_setup(ctx: _Ctx) -> str | None:
    if "setup" in ctx.categories or _contains_any(ctx.message_blob, _VERIFY_SETUP_TOKENS):
        return "verify_setup"
    return None


def _check_verify_failure(ctx: _Ctx) -> str | None:
    if "verify" in ctx.categories and (
        ctx.verify_status_key == "FAIL"
        or ctx.round_outcome_key.endswith("verify_failed")
        or ctx.status_key in {"FAIL", "BLOCKED", "HARD_ERROR"}
    ):
        return "verify_failure"
    return "verify_failure" if _contains_any(ctx.message_blob, _VERIFY_FAILURE_TOKENS) else None


def _check_task_blocked(ctx: _Ctx) -> str | None:
    if ctx.run_outcome_key == "blocked:task_blocked" or _contains_any(ctx.message_blob, _TASK_BLOCKED_TOKENS):
        return "task_blocked"
    return None


def _check_max_cycles(ctx: _Ctx) -> str | None:
    if ctx.stop_reason_key == "MAX_CYCLES" or ctx.run_outcome_key == "stopped:max_cycles":
        return "max_cycles"
    return "max_cycles" if _contains_any(ctx.message_blob, _MAX_CYCLES_TOKENS) else None


def _check_no_progress(ctx: _Ctx) -> str | None:
    if ctx.stop_reason_key == "NO_PROGRESS" or ctx.run_outcome_key == "stopped:no_progress":
        return "no_progress"
    return "no_progress" if _contains_any(ctx.message_blob, _NO_PROGRESS_TOKENS) else None


def _check_stuck(ctx: _Ctx) -> str | None:
    if ctx.stop_reason_key == "STUCK" or ctx.run_outcome_key == "stopped:stuck":
        return "stuck"
    return "stuck" if _contains_any(ctx.message_blob, _STUCK_TOKENS) else None


def _check_review_feedback(ctx: _Ctx) -> str | None:
    if "review" in ctx.categories or _contains_any(ctx.message_blob, _REVIEW_TOKENS):
        return "review_feedback"
    return None


def _check_implementation_error(ctx: _Ctx) -> str | None:
    return "implementation_error" if "implement" in ctx.categories else None


def _check_flaky_or_bucket(ctx: _Ctx) -> str | None:
    if ctx.classification_key == "lane.flaky_failure":
        return "flaky_failure"
    return ctx.classification_bucket or None


def _check_runtime_error(ctx: _Ctx) -> str | None:
    has_failure_signal = any([
        bool(ctx.message_blob),
        bool(ctx.categories),
        ctx.status_key in {"FAIL", "BLOCKED", "HARD_ERROR", "SKIP"},
        bool(ctx.run_outcome_key and ctx.run_outcome_key != "unknown"),
        bool(ctx.round_outcome_key and ctx.round_outcome_key != "unknown"),
    ])
    if ctx.classification_key in {"lane.consistent_failure", "lane.unclassified"} or has_failure_signal:
        return "runtime_error"
    return None


_BUCKET_CHAIN = (
    _check_stable_pass,
    _check_ready_for_gate,
    _check_env_missing,
    _check_rate_limit,
    _check_timeout,
    _check_external_gateway,
    _check_gate_blocked,
    _check_verify_setup,
    _check_verify_failure,
    _check_task_blocked,
    _check_max_cycles,
    _check_no_progress,
    _check_stuck,
    _check_review_feedback,
    _check_implementation_error,
    _check_flaky_or_bucket,
    _check_runtime_error,
)


def root_cause_bucket_label(bucket: str) -> str:
    return ROOT_CAUSE_BUCKET_LABELS.get(str(bucket or "").strip(), "Unknown")


def ranked_root_causes(
    counts: dict[str, Any],
    *,
    limit: int = 5,
    include_stable: bool = False,
) -> list[dict[str, Any]]:
    rows = [
        (str(bucket).strip(), int(value))
        for bucket, value in counts.items()
        if str(bucket).strip() and int(value) > 0 and (include_stable or str(bucket).strip() != "stable_pass")
    ]
    ranked = sorted(rows, key=lambda item: (-item[1], item[0]))
    return [
        {"bucket": bucket, "label": root_cause_bucket_label(bucket), "count": count}
        for bucket, count in ranked[:limit]
    ]


def classify_root_cause_bucket(
    *,
    classification_id: str = "",
    status: str = "",
    stop_reason: str = "",
    gate_status: str = "",
    verify_status: str = "",
    round_outcome: str = "",
    run_outcome: str = "",
    error_categories: list[str] | tuple[str, ...] | set[str] | None = None,
    missing_env: list[str] | tuple[str, ...] | set[str] | None = None,
    failure_messages: list[str] | tuple[str, ...] | None = None,
    blocking_reason: str = "",
    headline: str = "",
) -> str:
    ctx = _make_ctx(
        classification_id, status, stop_reason, gate_status, verify_status,
        round_outcome, run_outcome, error_categories, missing_env,
        failure_messages, blocking_reason, headline,
    )
    for checker in _BUCKET_CHAIN:
        result = checker(ctx)
        if result is not None:
            return result
    return "unknown"


def _contains_any(blob: str, tokens: tuple[str, ...]) -> bool:
    return any(token in blob for token in tokens)


__all__ = ["ROOT_CAUSE_BUCKET_LABELS", "classify_root_cause_bucket", "ranked_root_causes", "root_cause_bucket_label"]
