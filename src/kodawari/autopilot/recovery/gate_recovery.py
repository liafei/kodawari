"""Deterministic recovery cards for structured runtime gate findings."""

from __future__ import annotations

from pathlib import Path
from typing import Any


GATE_COMPLEXITY_RECOVERY_ACTION = "gate_complexity_refactor"


def build_gate_complexity_recovery(
    *,
    project_root: Path,
    gate_check: dict[str, Any] | None,
    original_card: dict[str, Any] | None,
    task_id: str,
    must_fix: list[str],
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Build a deterministic recovery decision/card for function-metrics gate blocks.

    The gate already knows the file, symbol, metric, actual value, and limit. This
    path keeps that structured evidence intact and avoids sending a simple
    refactor request through the generic executor recovery synthesizer.
    """
    card = dict(original_card or {})
    violations = _function_metric_only_violations(gate_check)
    target_paths = _valid_complexity_targets(project_root=project_root, card=card, violations=violations)
    if not card or not violations or not target_paths:
        return None
    decision = _complexity_recovery_decision(violations=violations, must_fix=must_fix)
    recovery_card = _complexity_recovery_card(
        original_card=card,
        task_id=task_id,
        target_paths=target_paths,
        violations=violations,
        must_fix=must_fix,
    )
    return decision, recovery_card


def _valid_complexity_targets(
    *,
    project_root: Path,
    card: dict[str, Any],
    violations: list[dict[str, Any]],
) -> list[str]:
    allowed = _allowed_write_paths(card)
    target_paths = _unique_paths(str(item["path"]) for item in violations)
    if not target_paths or any(path not in allowed for path in target_paths):
        return []
    if any(not (Path(project_root) / path).exists() for path in target_paths):
        return []
    return target_paths


def _function_metric_only_violations(gate_check: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(gate_check, dict):
        return []
    if str(gate_check.get("total_status") or "").upper() != "BLOCKED":
        return []
    violations: list[dict[str, Any]] = []
    for raw in _gate_violations(gate_check):
        item = _normalize_function_metric_violation(raw)
        if item is None:
            return []
        violations.append(item)
    if not _declared_violation_count_matches(gate_check, actual=len(violations)):
        return []
    return violations


def _gate_violations(gate_check: dict[str, Any]):
    for checker in list(gate_check.get("items") or []):
        if not isinstance(checker, dict):
            continue
        for violation in list(checker.get("violations") or []):
            if isinstance(violation, dict):
                yield violation


def _declared_violation_count_matches(gate_check: dict[str, Any], *, actual: int) -> bool:
    for key in ("total_violations", "blocking_violations"):
        declared = _optional_int(gate_check.get(key))
        if declared is not None:
            return declared == actual
    return True


def _normalize_function_metric_violation(raw: dict[str, Any]) -> dict[str, Any] | None:
    metric = str(raw.get("metric") or "").strip().lower()
    if metric not in {"complexity", "nesting"}:
        return None
    path = _normalize_rel(raw.get("path"))
    symbol = str(raw.get("symbol") or "").strip()
    if not path or not symbol:
        return None
    return {
        "checker": str(raw.get("checker") or "").strip(),
        "path": path,
        "line": _optional_int(raw.get("line")),
        "symbol": symbol,
        "metric": metric,
        "actual": _optional_int(raw.get("actual")),
        "limit": _optional_int(raw.get("limit")),
        "severity": str(raw.get("severity") or "").strip(),
        "message": str(raw.get("message") or "").strip(),
    }


def _complexity_recovery_decision(*, violations: list[dict[str, Any]], must_fix: list[str]) -> dict[str, Any]:
    return {
        "schema_version": "execution.recovery_decision.v1",
        "action": GATE_COMPLEXITY_RECOVERY_ACTION,
        "reason": "rules gate blocked on function metrics only; use deterministic refactor recovery",
        "source": "kodawari.gate_complexity_recovery",
        "must_fix": _string_list(must_fix),
        "gate_violations": [dict(item) for item in violations],
    }


def _complexity_recovery_card(
    *,
    original_card: dict[str, Any],
    task_id: str,
    target_paths: list[str],
    violations: list[dict[str, Any]],
    must_fix: list[str],
) -> dict[str, Any]:
    readonly = _recovery_read_only_files(original_card, writable=target_paths)
    related_tests = _related_tests(original_card, writable=target_paths)
    card: dict[str, Any] = {
        "schema_version": "contract_first.task_card.v1",
        "task_id": f"{task_id}_COMPLEXITY_RECOVERY",
        "task_name": f"Complexity recovery for {task_id}",
        "why_this_layer": "Rules gate recovery card generated from structured complexity violations.",
        "files_to_change": list(target_paths),
        "new_files": [],
        "invariants": _complexity_invariants(original_card),
        "forbidden_changes": _complexity_forbidden_changes(original_card),
        "verify_cmd": str(original_card.get("verify_cmd") or "").strip(),
        "target_symbols": _target_symbols(violations),
        "recovery": {
            "schema_version": "execution.recovery_card.v1",
            "source_action": GATE_COMPLEXITY_RECOVERY_ACTION,
            "must_fix": _string_list(must_fix),
            "reason": "Refactor only the flagged functions until runtime rules gate passes.",
            "gate_violations": [dict(item) for item in violations],
            "instructions": [
                "Extract helpers or flatten nested branches to bring each flagged function metric below the gate limit.",
                "Preserve public behavior, function signature, return shape, exception behavior, and side effects.",
                "Do not change tests or unrelated source files for this recovery.",
                "Run the original scoped verify command after the refactor.",
            ],
        },
    }
    if readonly:
        card["read_only_files"] = readonly
    if related_tests:
        card["related_existing_tests"] = related_tests
    _copy_guidance_fields(original_card, card)
    return card


def _complexity_invariants(original_card: dict[str, Any]) -> list[str]:
    return _unique_strings(
        [
            *_string_list(original_card.get("invariants")),
            "Preserve the flagged function's public signature and externally observable behavior.",
            "Keep existing tests passing; this recovery is a refactor, not a behavior change.",
        ]
    )


def _complexity_forbidden_changes(original_card: dict[str, Any]) -> list[str]:
    return _unique_strings(
        [
            *_string_list(original_card.get("forbidden_changes")),
            "Do not edit tests for a complexity-only gate recovery.",
            "Do not alter external API contracts, schemas, credentials, or configuration.",
        ]
    )


def _copy_guidance_fields(source: dict[str, Any], target: dict[str, Any]) -> None:
    for key in (
        "coverage_hints",
        "api_contracts",
        "test_plan",
        "do_not_change",
        "context_files",
        "requires",
        "read_only_symbols",
    ):
        value = source.get(key)
        if isinstance(value, list):
            target[key] = list(value)


def _recovery_read_only_files(original_card: dict[str, Any], *, writable: list[str]) -> list[str]:
    candidates: list[Any] = []
    candidates.extend(_list_field(original_card, "read_only_files"))
    candidates.extend(_list_field(original_card, "context_files"))
    candidates.extend(_list_field(original_card, "related_existing_tests"))
    candidates.extend(_test_paths(_list_field(original_card, "files_to_change")))
    candidates.extend(_test_paths(_list_field(original_card, "new_files")))
    return _paths_not_writable(candidates, writable=writable)


def _related_tests(original_card: dict[str, Any], *, writable: list[str]) -> list[str]:
    candidates: list[Any] = []
    candidates.extend(_list_field(original_card, "related_existing_tests"))
    candidates.extend(_test_paths(_list_field(original_card, "files_to_change")))
    return _paths_not_writable(candidates, writable=writable)


def _paths_not_writable(values: list[Any], *, writable: list[str]) -> list[str]:
    writable_set = set(writable)
    return _unique_paths(
        item
        for item in values
        if _normalize_rel(item) and _normalize_rel(item) not in writable_set
    )


def _list_field(source: dict[str, Any], key: str) -> list[Any]:
    value = source.get(key)
    return list(value) if isinstance(value, list) else []


def _test_paths(values: list[Any]) -> list[Any]:
    return [item for item in values if _is_test_path(str(item or ""))]


def _target_symbols(violations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    symbols: list[dict[str, Any]] = []
    for item in violations:
        symbols.append(
            {
                "kind": "function",
                "name": str(item.get("symbol") or ""),
                "file": str(item.get("path") or ""),
                "line": item.get("line"),
                "metric": str(item.get("metric") or ""),
                "actual": item.get("actual"),
                "limit": item.get("limit"),
            }
        )
    return symbols


def _allowed_write_paths(card: dict[str, Any]) -> set[str]:
    return {
        path
        for path in _unique_paths(
            [
                *_string_list(card.get("files_to_change")),
                *_string_list(card.get("new_files")),
            ]
        )
        if path
    }


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


def _unique_strings(values: list[str]) -> list[str]:
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


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalize_rel(value: Any) -> str:
    text = str(value or "").replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    if not text or text.startswith("/") or text.startswith("../") or "/../" in text:
        return ""
    return text


def _is_test_path(text: str) -> bool:
    normalized = _normalize_rel(text).lower()
    name = Path(normalized).name
    return normalized.startswith("tests/") or name.startswith("test_") or name.endswith("_test.py")
