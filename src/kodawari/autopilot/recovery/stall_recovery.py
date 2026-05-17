"""Deterministic recovery cards for executor stall patterns."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from kodawari.autopilot.core.task_modes import verification_only_allows_empty_files
from kodawari.autopilot.recovery.executor_recovery import build_scope_expansion_recovery_card


NO_WRITE_STALL_RECOVERY_ACTION = "executor_no_write_stall_retry"
TOOL_CALL_LIMIT_RECOVERY_ACTION = "executor_tool_call_limit_retry"
SCOPE_DRIFT_RECOVERY_ACTION = "executor_scope_drift_retry"
_TOOL_LIMIT_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*) called too many times for ([^\s]+)")


def build_no_write_stall_recovery(
    *,
    project_root: Path,
    original_card: dict[str, Any] | None,
    task_id: str,
    must_fix: list[str],
    stall_report: dict[str, Any] | None = None,
    execution_result: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Build a deterministic write-first retry card for no-write executor stalls."""
    card = dict(original_card or {})
    report = _stall_report(stall_report=stall_report, execution_result=execution_result)
    if not card or not _is_plain_no_write_stall(report):
        return None
    target_paths = _valid_write_targets(project_root=project_root, card=card)
    if not target_paths:
        return None
    decision = _no_write_stall_decision(report=report, must_fix=must_fix)
    recovery_card = _no_write_stall_card(
        original_card=card,
        task_id=task_id,
        target_paths=target_paths,
        must_fix=must_fix,
        report=report,
    )
    return decision, recovery_card


def build_tool_call_limit_recovery(
    *,
    project_root: Path,
    original_card: dict[str, Any] | None,
    task_id: str,
    must_fix: list[str],
    stall_report: dict[str, Any] | None = None,
    execution_result: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Build a deterministic recovery card for repeated same-path tool calls."""
    card = dict(original_card or {})
    report = _stall_report(stall_report=stall_report, execution_result=execution_result)
    if _normalized_code(report.get("error_code") or report.get("reason")) != "MAX_SAME_TOOL_CALLS_PER_PATH":
        return None
    tool_name, repeated_path = _structured_tool_limit(report)
    if not tool_name or not repeated_path:
        evidence = _execution_error_text(report)
        tool_name, repeated_path = _tool_limit(evidence)
    target_paths = _valid_write_targets(project_root=project_root, card=card)
    if not card or not tool_name or repeated_path not in target_paths:
        return None
    decision = _tool_limit_decision(
        tool_name=tool_name,
        repeated_path=repeated_path,
        must_fix=must_fix,
    )
    recovery_card = _tool_limit_card(
        original_card=card,
        task_id=task_id,
        target_paths=target_paths,
        tool_name=tool_name,
        repeated_path=repeated_path,
        must_fix=must_fix,
    )
    return decision, recovery_card


def build_scope_drift_recovery(
    *,
    project_root: Path,
    original_card: dict[str, Any] | None,
    task_id: str,
    must_fix: list[str],
    affected_paths: list[str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Build a deterministic scope-expansion card from FailureEvent.affected_paths.

    Mirrors the synthesizer-driven expand_scope_request decision but skips the
    LLM round-trip. Triggers when the failure event names files outside the
    original card's writable scope, which is the structural signal that the
    task as scoped cannot land the must_fix items.
    """
    card = dict(original_card or {})
    if not card:
        return None
    if verification_only_allows_empty_files(card):
        return None
    drift = _scope_drift_paths(card=card, affected_paths=affected_paths, project_root=project_root)
    if not drift:
        return None
    decision = {
        "action": "expand_scope_request",
        "reason": "deterministic scope drift: must_fix references files outside the original card scope",
        "requested_files": list(drift),
        "source_action": SCOPE_DRIFT_RECOVERY_ACTION,
    }
    recovery_card = build_scope_expansion_recovery_card(
        original_card=card,
        decision=decision,
        task_id=task_id,
        must_fix=must_fix,
        project_root=project_root,
    )
    if recovery_card is None:
        return None
    decision_payload = {
        "schema_version": "execution.recovery_decision.v1",
        "action": SCOPE_DRIFT_RECOVERY_ACTION,
        "reason": decision["reason"],
        "source": "kodawari.scope_drift_recovery",
        "must_fix": _string_list(must_fix),
        "requested_files": list(drift),
    }
    return decision_payload, recovery_card


def _scope_drift_paths(
    *,
    card: dict[str, Any],
    affected_paths: list[str] | None,
    project_root: Path,
) -> list[str]:
    paths = _string_list(affected_paths)
    if not paths:
        return []
    in_scope = {item.lower() for item in _valid_write_targets(project_root=project_root, card=card)}
    drift: list[str] = []
    seen: set[str] = set()
    root = Path(project_root).resolve()
    for raw in paths:
        normalized = _normalize_rel(raw)
        if not normalized:
            continue
        if normalized.lower() in in_scope:
            continue
        try:
            absolute = (root / normalized).resolve()
            absolute.relative_to(root)
        except (OSError, ValueError):
            continue
        if not absolute.exists():
            continue
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        drift.append(normalized)
    return drift


def _stall_report(
    *,
    stall_report: dict[str, Any] | None,
    execution_result: dict[str, Any] | None,
) -> dict[str, Any]:
    if isinstance(stall_report, dict) and stall_report:
        return dict(stall_report)
    if isinstance(execution_result, dict):
        nested = execution_result.get("stall_report")
        if isinstance(nested, dict):
            return dict(nested)
        if _normalized_code(execution_result.get("error_code") or execution_result.get("reason")):
            return {
                "error_code": execution_result.get("error_code") or execution_result.get("reason"),
                "reason": execution_result.get("reason") or execution_result.get("error_code"),
                "blocking_reason": execution_result.get("blocking_reason"),
                "summary": execution_result.get("summary"),
                "error": execution_result.get("error"),
                "tool_call_limit": execution_result.get("tool_call_limit"),
                "counters": {},
                "patch_plan": {},
                "read_scope_exhausted": execution_result.get("read_scope_exhausted"),
            }
    return {}


def _structured_tool_limit(report: dict[str, Any]) -> tuple[str, str]:
    raw = report.get("tool_call_limit")
    if not isinstance(raw, dict):
        return "", ""
    tool_name = str(raw.get("tool") or "").strip()
    repeated_path = str(raw.get("path") or "").strip().replace("\\", "/")
    while repeated_path.startswith("./"):
        repeated_path = repeated_path[2:]
    return tool_name, repeated_path


def _is_plain_no_write_stall(report: dict[str, Any]) -> bool:
    code = _normalized_code(report.get("error_code") or report.get("reason"))
    if code not in {
        "EXECUTOR_STALLED_NO_WRITE_PROGRESS",
        "EXECUTOR_STALLED_PATCH_PLAN_REQUIRED",
        "EXECUTOR_STALLED_FRAGMENTED_READS",
    }:
        return False
    counters = report.get("counters") if isinstance(report.get("counters"), dict) else {}
    # Fragmented-read stalls are by definition "many reads, no writes"; their
    # stall report may not carry no_write_iterations because the read-window
    # cap fires first. For other stall codes we still require the no-write
    # counter to be high enough to justify a write-first retry card.
    if code != "EXECUTOR_STALLED_FRAGMENTED_READS" and _int_value(counters.get("no_write_iterations")) < 3:
        return False
    if _int_value(counters.get("patch_apply_failures")) > 0:
        return False
    if bool(report.get("read_scope_exhausted")):
        return False
    patch_plan = report.get("patch_plan") if isinstance(report.get("patch_plan"), dict) else {}
    if _int_value(patch_plan.get("total")) > 0:
        return False
    return True


def _valid_write_targets(*, project_root: Path, card: dict[str, Any]) -> list[str]:
    root = Path(project_root).resolve()
    targets = _unique_paths(
        [
            *_string_list(card.get("files_to_change")),
            *_string_list(card.get("new_files")),
        ]
    )
    valid: list[str] = []
    for path in targets:
        try:
            (root / path).resolve().relative_to(root)
        except (OSError, ValueError):
            continue
        valid.append(path)
    return valid


def _no_write_stall_decision(*, report: dict[str, Any], must_fix: list[str]) -> dict[str, Any]:
    return {
        "schema_version": "execution.recovery_decision.v1",
        "action": NO_WRITE_STALL_RECOVERY_ACTION,
        "reason": "executor stalled after repeated reads without writes; use deterministic write-first retry",
        "source": "kodawari.no_write_stall_recovery",
        "must_fix": _string_list(must_fix),
        "stall_counters": dict(report.get("counters") or {}) if isinstance(report.get("counters"), dict) else {},
    }


def _no_write_stall_card(
    *,
    original_card: dict[str, Any],
    task_id: str,
    target_paths: list[str],
    must_fix: list[str],
    report: dict[str, Any],
) -> dict[str, Any]:
    previous_context = _previous_recovery_context(original_card)
    merged_must_fix = _unique_strings([
        *_string_list(must_fix),
        *_string_list(previous_context.get("must_fix")),
    ])
    card: dict[str, Any] = {
        "schema_version": "contract_first.task_card.v1",
        "task_id": f"{task_id}_NO_WRITE_STALL_RECOVERY",
        "task_name": f"No-write stall recovery for {task_id}",
        "why_this_layer": "Executor recovery card generated from deterministic no-write stall telemetry.",
        "files_to_change": list(target_paths),
        "new_files": _new_file_targets(original_card, target_paths),
        "invariants": _stall_invariants(original_card),
        "forbidden_changes": _stall_forbidden_changes(original_card),
        "verify_cmd": str(original_card.get("verify_cmd") or "").strip(),
        "recovery": {
            "schema_version": "execution.recovery_card.v1",
            "source_action": NO_WRITE_STALL_RECOVERY_ACTION,
            "must_fix": merged_must_fix,
            "reason": "Retry with a write-first plan because the previous executor run only read context.",
            "stall_counters": dict(report.get("counters") or {}) if isinstance(report.get("counters"), dict) else {},
            "instructions": [
                "Do not spend another full round re-reading the same context.",
                "If previous_recovery_context is present, act on that concrete failure before generic cleanup.",
                "In the first write step, create or patch one scoped files_to_change path.",
                "Prefer creating planned new files before editing router registration.",
                "After the first write, continue with the original task invariants and scoped verify command.",
            ],
        },
    }
    if previous_context:
        card["recovery"]["previous_recovery_context"] = previous_context
    _copy_list_fields(original_card, card)
    return card


def _previous_recovery_context(original_card: dict[str, Any]) -> dict[str, Any]:
    raw = original_card.get("recovery")
    if not isinstance(raw, dict):
        return {}
    context: dict[str, Any] = {}
    for key in (
        "source_action",
        "reason",
        "must_fix",
        "failed_tests",
        "missing_names",
        "collection_error_files",
        "instructions",
    ):
        value = raw.get(key)
        if isinstance(value, list):
            copied = _string_list(value)
            if copied:
                context[key] = copied
        elif isinstance(value, str) and value.strip():
            context[key] = value.strip()
    return context


def _tool_limit_decision(*, tool_name: str, repeated_path: str, must_fix: list[str]) -> dict[str, Any]:
    return {
        "schema_version": "execution.recovery_decision.v1",
        "action": TOOL_CALL_LIMIT_RECOVERY_ACTION,
        "reason": "executor hit same-path tool call limit; use deterministic consolidated-edit retry",
        "source": "kodawari.tool_call_limit_recovery",
        "must_fix": _string_list(must_fix),
        "tool_call_limit": {
            "tool": tool_name,
            "path": repeated_path,
        },
    }


def _tool_limit_card(
    *,
    original_card: dict[str, Any],
    task_id: str,
    target_paths: list[str],
    tool_name: str,
    repeated_path: str,
    must_fix: list[str],
) -> dict[str, Any]:
    card: dict[str, Any] = {
        "schema_version": "contract_first.task_card.v1",
        "task_id": f"{task_id}_TOOL_LIMIT_RECOVERY",
        "task_name": f"Tool-call limit recovery for {task_id}",
        "why_this_layer": "Executor recovery card generated from deterministic same-path tool-call guard.",
        "files_to_change": list(target_paths),
        "new_files": _new_file_targets(original_card, target_paths),
        "invariants": _tool_limit_invariants(original_card),
        "forbidden_changes": _tool_limit_forbidden_changes(original_card),
        "verify_cmd": str(original_card.get("verify_cmd") or "").strip(),
        "recovery": {
            "schema_version": "execution.recovery_card.v1",
            "source_action": TOOL_CALL_LIMIT_RECOVERY_ACTION,
            "must_fix": _string_list(must_fix),
            "reason": "Retry with consolidated edits instead of repeated same-path tool calls.",
            "tool_call_limit": {
                "tool": tool_name,
                "path": repeated_path,
            },
            "instructions": [
                f"Do not keep issuing small {tool_name} calls against {repeated_path}.",
                "Read the current file once, then apply one consolidated replacement or full-file write when allowed.",
                "Prioritize the active must_fix items over unrelated test polish.",
                "Finish only after deterministic changed files exist and scoped verify passes.",
            ],
        },
    }
    _copy_list_fields(original_card, card)
    return card


def _stall_invariants(original_card: dict[str, Any]) -> list[str]:
    return _unique_strings(
        [
            *_string_list(original_card.get("invariants")),
            "Begin recovery by writing at least one scoped file before additional broad exploration.",
            "Preserve the original task contract, invariants, and verify command.",
        ]
    )


def _stall_forbidden_changes(original_card: dict[str, Any]) -> list[str]:
    return _unique_strings(
        [
            *_string_list(original_card.get("forbidden_changes")),
            "Do not expand scope beyond files_to_change for a no-write stall retry.",
            "Do not delete tests or weaken assertions to avoid implementation work.",
        ]
    )


def _tool_limit_invariants(original_card: dict[str, Any]) -> list[str]:
    return _unique_strings(
        [
            *_string_list(original_card.get("invariants")),
            "Consolidate repeated same-file edits into a single clear patch.",
            "Preserve the original task contract, review must-fix items, and scoped verify command.",
        ]
    )


def _tool_limit_forbidden_changes(original_card: dict[str, Any]) -> list[str]:
    return _unique_strings(
        [
            *_string_list(original_card.get("forbidden_changes")),
            "Do not keep making unrelated polish edits after a same-path tool-call limit.",
            "Do not bypass the original review must-fix items.",
        ]
    )


def _execution_error_text(execution_result: dict[str, Any]) -> str:
    return "\n".join(
        str(execution_result.get(key) or "")
        for key in ("blocking_reason", "summary", "reason", "error")
        if execution_result.get(key)
    )


def _tool_limit(text: str) -> tuple[str, str]:
    match = _TOOL_LIMIT_RE.search(text)
    if not match:
        return "", ""
    return match.group(1), _normalize_rel(match.group(2))


def _copy_list_fields(source: dict[str, Any], target: dict[str, Any]) -> None:
    for key in (
        "allowed_test_mutations",
        "api_contracts",
        "context_files",
        "coverage_hints",
        "do_not_change",
        "read_only_files",
        "read_only_symbols",
        "related_existing_tests",
        "requires",
        "review_focus",
        "test_plan",
    ):
        value = source.get(key)
        if isinstance(value, list):
            target[key] = list(value)
        elif key == "test_plan" and isinstance(value, str):
            target[key] = value


def _new_file_targets(original_card: dict[str, Any], target_paths: list[str]) -> list[str]:
    new_files = set(_string_list(original_card.get("new_files")))
    return [path for path in target_paths if path in new_files]


def _normalized_code(value: Any) -> str:
    return str(value or "").strip().upper()


def _int_value(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _unique_paths(values) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        normalized = _normalize_rel(raw)
        if not normalized:
            continue
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(normalized)
    return out


def _unique_strings(values) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        text = str(raw or "").strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def _string_list(raw: Any) -> list[str]:
    return [str(item) for item in list(raw or []) if str(item).strip()]


def _normalize_rel(value: Any) -> str:
    text = str(value or "").replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    if not text or text.startswith("/") or text.startswith("../") or "/../" in text:
        return ""
    if len(text) >= 3 and text[1:3] == ":/":
        return ""
    return text
