"""Observation helpers for stability-report runtime semantics."""

from __future__ import annotations

import json
from typing import Any


_STOP_REASON_DESCRIPTIONS = {
    "PASS": "全部任务完成",
    "MAX_CYCLES": "达到最大循环次数",
    "TOKEN_BUDGET": "Token 预算耗尽",
    "STUCK": "重复错误 3+ 次",
    "NO_PROGRESS": "无文件变更 3+ 次",
    "HARD_ERROR": "不可恢复错误",
    "USER_INTERRUPT": "用户主动中断",
}


def _coerce_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _compact_context_dict(run: dict[str, Any]) -> dict[str, Any] | None:
    compact = run.get("compact_context")
    return compact if isinstance(compact, dict) else None


def _compact_context_value(compact: dict[str, Any] | None, *keys: str) -> str:
    if compact is None:
        return ""
    for key in keys:
        value = str(compact.get(key) or "").strip()
        if value:
            return value
    return ""


def _compact_note(compact: dict[str, Any] | None) -> str:
    status = _compact_context_value(compact, "runtime_status", "compact_status")
    mode = _compact_context_value(compact, "runtime_mode", "compact_mode")
    instincts = _compact_context_value(compact, "instincts_status")
    parts: list[str] = []
    if status and mode:
        parts.append(f"compact={status}/{mode}")
    elif status:
        parts.append(f"compact={status}")
    if instincts:
        parts.append(f"instincts={instincts}")
    return ", ".join(parts)


def _merged_absorption_note(compact: dict[str, Any] | None) -> str:
    merged_status = compact.get("merged_absorption_status") if isinstance(compact, dict) else {}
    if not isinstance(merged_status, dict) or not merged_status:
        return ""
    pairs = [f"{name}:{merged_status.get(name, '-')}" for name in ("planning_summary", "context_compact", "instincts")]
    return "absorption=" + "/".join(pairs)


def _stop_reason_description(reason: str) -> str:
    return _STOP_REASON_DESCRIPTIONS.get(reason, "-")


def _workflow_chain_final_outcome(run: dict[str, Any]) -> dict[str, Any]:
    chain = run.get("workflow_chain")
    if not isinstance(chain, dict):
        return {}
    outcome = chain.get("final_outcome")
    return dict(outcome) if isinstance(outcome, dict) else {}


def _state_unified_status(state: Any) -> dict[str, Any]:
    if not isinstance(state, dict):
        return {}
    unified = state.get("unified_status")
    return dict(unified) if isinstance(unified, dict) else {}


def _workflow_chain_blocked_note(run: dict[str, Any]) -> str:
    final_outcome = _workflow_chain_final_outcome(run)
    final_status = _coerce_text(final_outcome.get("status")).upper()
    if final_status != "BLOCKED":
        return ""
    return _coerce_text(final_outcome.get("blocking_reason")).strip()[:80]


def _pending_gate_note(run: dict[str, Any]) -> str:
    if _pending_gate_outcome(run) != "ready_for_gate":
        return ""
    return "等待 advisory gate"


def _compact_status_note(run: dict[str, Any]) -> str:
    compact = _compact_context_dict(run)
    return ", ".join(item for item in (_compact_note(compact), _merged_absorption_note(compact)) if item)


def _pass_run_note(state: dict[str, Any], compact_status: str) -> str:
    if _coerce_text(state.get("stop_reason", "")).upper() != "PASS":
        return ""
    return compact_status or "完成"


def _fallback_run_note(state: dict[str, Any], compact_status: str) -> str:
    last_error = _coerce_text(state.get("last_error")).strip()
    return last_error[:80] or compact_status or _stop_reason_description(_coerce_text(state.get("stop_reason", "")).upper())


def summarize_run_note(run: dict[str, Any]) -> str:
    state = run["state"]
    compact_status = _compact_status_note(run)
    for note in (
        _workflow_chain_blocked_note(run),
        _pending_gate_note(run),
        _pass_run_note(state, compact_status),
        _fallback_run_note(state, compact_status),
    ):
        if note:
            return note
    return "-"


def normalize_compact_runtime_key(run: dict[str, Any]) -> str:
    compact = _compact_context_dict(run)
    if compact is None:
        return "missing"
    status = _compact_context_value(compact, "runtime_status", "compact_status").lower()
    mode = _compact_context_value(compact, "runtime_mode", "compact_mode").lower()
    if status and mode:
        return f"{status}/{mode}"
    return status or "unknown"


def normalize_instincts_status_key(run: dict[str, Any]) -> str:
    compact = _compact_context_dict(run)
    if compact is None:
        return "unknown"
    status = str(compact.get("instincts_status") or "").strip().lower()
    if status:
        return status
    loaded = compact.get("instincts_loaded")
    if isinstance(loaded, bool):
        return "loaded" if loaded else "not_loaded"
    return "unknown"


def normalize_round_outcome_key(record: dict[str, Any]) -> str:
    outcome = str(record.get("round_outcome") or "").strip().lower()
    if outcome:
        return outcome
    stage_status = str(record.get("stage_status") or "").strip().lower()
    if stage_status:
        return f"legacy:{stage_status}"
    return "unknown"


def _gate_outcome(run: dict[str, Any]) -> str:
    gate_result = run.get("gate_result")
    if not isinstance(gate_result, dict):
        return ""
    gate_status = str(gate_result.get("total_status", "")).strip().upper()
    if gate_status == "BLOCKED":
        return "blocked_by_gate"
    if gate_status == "PASS":
        # Gate PASS only counts as "pass" when the overall run also succeeded.
        # If the run was stopped by MAX_CYCLES/token-budget/etc., the gate
        # artifact exists but the run outcome must be derived from run state.
        stop_reason = _state_stop_reason(run.get("state"))
        if stop_reason and stop_reason != "PASS":
            return ""
        return "pass"
    return ""


def _workflow_chain_outcome(run: dict[str, Any]) -> str:
    final_outcome = _workflow_chain_final_outcome(run)
    status = _coerce_text(final_outcome.get("status")).upper()
    reason = _coerce_text(final_outcome.get("reason")).strip().lower()
    if status == "PASS":
        return "pass"
    if status == "BLOCKED" and reason in {"advisory_gate_blocked", "task_blocked"}:
        return f"blocked:{reason}"
    return ""


def _pending_gate_outcome(run: dict[str, Any]) -> str:
    final_outcome = _workflow_chain_final_outcome(run)
    status = _coerce_text(final_outcome.get("status")).upper()
    if status == "READY_FOR_GATE":
        return "ready_for_gate"
    if status != "PASS":
        return ""
    unified = _state_unified_status(run.get("state"))
    if not unified or bool(unified.get("is_terminal")):
        return ""
    current_phase = _coerce_text(unified.get("current_phase")).upper()
    if current_phase == "GATE":
        return "ready_for_gate"
    return ""


def _state_outcome(run: dict[str, Any]) -> str:
    state = run.get("state")
    stop_reason = _state_stop_reason(state)
    if stop_reason == "PASS":
        return "pass"
    if stop_reason:
        return f"stopped:{stop_reason.lower()}"
    if _state_unified_is_blocked(state):
        return "blocked"
    return ""


def _state_stop_reason(state: Any) -> str:
    if not isinstance(state, dict):
        return ""
    return _coerce_text(state.get("stop_reason", "")).strip().upper()


def _state_unified_is_blocked(state: Any) -> bool:
    unified = _state_unified_status(state)
    if not unified:
        return False
    return bool(unified.get("is_blocked"))


def _round_outcome_fallback(run: dict[str, Any]) -> str:
    rounds = run.get("rounds")
    if not isinstance(rounds, list):
        return ""
    for record in reversed(rounds):
        if not isinstance(record, dict):
            continue
        outcome = normalize_round_outcome_key(record)
        if outcome == "ready_for_gate":
            return "pass"
        if outcome in {"blocked", "error", "needs_fix"}:
            return f"stopped:{outcome}"
    return ""


def normalize_run_outcome_key(run: dict[str, Any]) -> str:
    return (
        _gate_outcome(run)
        or _pending_gate_outcome(run)
        or _workflow_chain_outcome(run)
        or _state_outcome(run)
        or _round_outcome_fallback(run)
        or "unknown"
    )


def distribution_summary(counts: dict[str, Any]) -> str:
    pairs = sorted((str(key), int(value)) for key, value in counts.items())
    return ", ".join(f"{key}:{value}" for key, value in pairs) or "-"


def round_record_blob(record: dict[str, Any]) -> str:
    return " ".join([str(record.get("last_error") or ""), json.dumps(record.get("details") or {}, ensure_ascii=False)]).lower()
