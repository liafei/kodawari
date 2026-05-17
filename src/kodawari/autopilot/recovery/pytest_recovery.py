"""Deterministic recovery cards for pytest collection failures."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any


PYTEST_COLLECTION_NAMEERROR_RECOVERY_ACTION = "pytest_collection_nameerror_fix"
PYTEST_VERIFY_FAILURE_RECOVERY_ACTION = "pytest_verify_failure_fix"

_NAMEERROR_RE = re.compile(r"NameError:\s+name ['\"]([^'\"]+)['\"] is not defined")
_REL_PY_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_./\\-])((?:[A-Za-z0-9_.-]+[\\/])+[A-Za-z0-9_.-]+\.py)(?::\d+)?"
)
_FAILED_TEST_RE = re.compile(r"(?m)^(?:FAILED|ERROR)\s+([^\s]+\.py::[^\s]+)")


def build_pytest_collection_nameerror_recovery(
    *,
    project_root: Path,
    original_card: dict[str, Any] | None,
    task_id: str,
    must_fix: list[str],
    execution_result: dict[str, Any] | None = None,
    collection_errors: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Build a deterministic recovery card for in-scope pytest collection NameError.

    This intentionally handles only collection-time NameError in task-writable
    files. Assertion failures and out-of-scope collection errors still go to the
    normal executor recovery path.
    """
    card = dict(original_card or {})
    evidence = _collection_evidence_text(must_fix=must_fix, execution_result=execution_result)
    structured_names, structured_paths = _structured_collection_errors(collection_errors)
    names = structured_names or _missing_names(evidence)
    paths = structured_paths or _collection_error_paths(evidence)
    target_paths = _valid_collection_targets(project_root=project_root, card=card, paths=paths)
    if not card or not names or not target_paths:
        return None
    if not structured_names and not _looks_like_collection_error(evidence):
        return None
    decision = _nameerror_recovery_decision(
        names=names,
        target_paths=target_paths,
        must_fix=must_fix,
    )
    recovery_card = _nameerror_recovery_card(
        original_card=card,
        task_id=task_id,
        target_paths=target_paths,
        names=names,
        must_fix=must_fix,
    )
    return decision, recovery_card


def build_pytest_verify_failure_recovery(
    *,
    project_root: Path,
    original_card: dict[str, Any] | None,
    task_id: str,
    must_fix: list[str],
    execution_result: dict[str, Any] | None = None,
    verify_check: dict[str, Any] | None = None,
    collection_errors: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Build a deterministic recovery card for ordinary scoped pytest failures.

    Collection-time NameError has a narrower recovery card. This detector handles
    assertion/runtime pytest failures by forcing the executor into targeted patch
    mode instead of letting stale no-write stall evidence consume the attempt.
    """
    card = dict(original_card or {})
    evidence = _verify_evidence_text(must_fix=must_fix, execution_result=execution_result, verify_check=verify_check)
    if not card or collection_errors or _looks_like_collection_error(evidence):
        return None
    if not _looks_like_pytest_verify_failure(evidence):
        return None
    target_paths = _valid_verify_targets(project_root=project_root, card=card)
    if not target_paths:
        return None
    failed_tests = _failed_tests(evidence)
    decision = _verify_failure_decision(
        failed_tests=failed_tests,
        target_paths=target_paths,
        must_fix=must_fix,
    )
    recovery_card = _verify_failure_card(
        original_card=card,
        task_id=task_id,
        target_paths=target_paths,
        failed_tests=failed_tests,
        must_fix=must_fix,
    )
    return decision, recovery_card


def _collection_evidence_text(
    *,
    must_fix: list[str],
    execution_result: dict[str, Any] | None,
) -> str:
    parts = [str(item) for item in must_fix if str(item).strip()]
    if isinstance(execution_result, dict):
        for key in ("blocking_reason", "reason", "summary", "error"):
            value = execution_result.get(key)
            if value:
                parts.append(str(value))
        for key in ("verify_summary", "verify_check"):
            value = execution_result.get(key)
            if isinstance(value, dict):
                parts.extend(
                    str(item)
                    for item in (
                        value.get("stdout_excerpt"),
                        value.get("stdout"),
                        value.get("stderr_excerpt"),
                        value.get("stderr"),
                    )
                    if item
                )
    return "\n".join(parts)


def _verify_evidence_text(
    *,
    must_fix: list[str],
    execution_result: dict[str, Any] | None,
    verify_check: dict[str, Any] | None,
) -> str:
    parts = [str(item) for item in must_fix if str(item).strip()]
    for source in (execution_result, verify_check):
        if not isinstance(source, dict):
            continue
        for key in ("blocking_reason", "reason", "summary", "error", "stdout_excerpt", "stdout", "stderr_excerpt", "stderr"):
            value = source.get(key)
            if value:
                parts.append(str(value))
        for key in ("verify_summary", "verify_check"):
            value = source.get(key)
            if isinstance(value, dict):
                parts.extend(
                    str(item)
                    for item in (
                        value.get("stdout_excerpt"),
                        value.get("stdout"),
                        value.get("stderr_excerpt"),
                        value.get("stderr"),
                        value.get("summary"),
                        value.get("error"),
                    )
                    if item
                )
    return "\n".join(parts)


def _looks_like_collection_error(text: str) -> bool:
    lowered = text.lower()
    return "error collecting" in lowered or "error at setup" in lowered


def _looks_like_pytest_verify_failure(text: str) -> bool:
    lowered = text.lower()
    if "error collecting" in lowered:
        return False
    return bool(_FAILED_TEST_RE.search(text)) or any(
        marker in text
        for marker in (
            "E   AssertionError",
            "E   TypeError",
            "E   ValueError",
            "E   KeyError",
            "short test summary info",
        )
    )


def _failed_tests(text: str) -> list[str]:
    return _unique_strings(_FAILED_TEST_RE.findall(text))


def _missing_names(text: str) -> list[str]:
    return _unique_strings(_NAMEERROR_RE.findall(text))


def _collection_error_paths(text: str) -> list[str]:
    return _unique_paths(_REL_PY_PATH_RE.findall(text))


def _structured_collection_errors(collection_errors: list[dict[str, Any]] | None) -> tuple[list[str], list[str]]:
    names: list[str] = []
    paths: list[str] = []
    for item in list(collection_errors or []):
        if not isinstance(item, dict):
            continue
        if str(item.get("exc_type") or "").strip() not in {"", "NameError"}:
            continue
        name = str(item.get("name") or "").strip()
        path = str(item.get("file") or item.get("path") or "").strip().replace("\\", "/")
        if name:
            names.append(name)
        if path:
            paths.append(path)
    return _unique_strings(names), _unique_paths(paths)


def _valid_collection_targets(
    *,
    project_root: Path,
    card: dict[str, Any],
    paths: list[str],
) -> list[str]:
    allowed = _allowed_write_paths(card)
    targets = [path for path in paths if path in allowed]
    if not targets:
        return []
    return [path for path in _unique_paths(targets) if (Path(project_root) / path).exists()]


def _valid_verify_targets(*, project_root: Path, card: dict[str, Any]) -> list[str]:
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


def _nameerror_recovery_decision(
    *,
    names: list[str],
    target_paths: list[str],
    must_fix: list[str],
) -> dict[str, Any]:
    return {
        "schema_version": "execution.recovery_decision.v1",
        "action": PYTEST_COLLECTION_NAMEERROR_RECOVERY_ACTION,
        "reason": "pytest collection failed on an in-scope NameError; use deterministic collection recovery",
        "source": "kodawari.pytest_collection_nameerror_recovery",
        "must_fix": _string_list(must_fix),
        "pytest_nameerrors": [
            {
                "name": name,
                "paths": list(target_paths),
            }
            for name in names
        ],
    }


def _verify_failure_decision(
    *,
    failed_tests: list[str],
    target_paths: list[str],
    must_fix: list[str],
) -> dict[str, Any]:
    return {
        "schema_version": "execution.recovery_decision.v1",
        "action": PYTEST_VERIFY_FAILURE_RECOVERY_ACTION,
        "reason": "scoped pytest failed after executor changes; use deterministic targeted patch retry",
        "source": "kodawari.pytest_verify_failure_recovery",
        "must_fix": _string_list(must_fix),
        "failed_tests": list(failed_tests),
        "target_files": list(target_paths),
    }


def _nameerror_recovery_card(
    *,
    original_card: dict[str, Any],
    task_id: str,
    target_paths: list[str],
    names: list[str],
    must_fix: list[str],
) -> dict[str, Any]:
    card: dict[str, Any] = {
        "schema_version": "contract_first.task_card.v1",
        "task_id": f"{task_id}_PYTEST_NAMEERROR_RECOVERY",
        "task_name": f"Pytest collection NameError recovery for {task_id}",
        "why_this_layer": "Executor recovery card generated from deterministic pytest collection evidence.",
        "files_to_change": list(target_paths),
        "new_files": _new_file_targets(original_card, target_paths),
        "invariants": _nameerror_invariants(original_card),
        "forbidden_changes": _nameerror_forbidden_changes(original_card),
        "verify_cmd": str(original_card.get("verify_cmd") or "").strip(),
        "recovery": {
            "schema_version": "execution.recovery_card.v1",
            "source_action": PYTEST_COLLECTION_NAMEERROR_RECOVERY_ACTION,
            "must_fix": _string_list(must_fix),
            "reason": "Fix only the pytest collection NameError before retrying scoped verification.",
            "missing_names": list(names),
            "collection_error_files": list(target_paths),
            "instructions": [
                "Define, import, or move the missing symbol so pytest can collect the scoped tests.",
                "Keep the task's behavior assertions intact.",
                "Do not delete or relax tests to make collection pass.",
                "Run the original scoped verify command after the collection fix.",
            ],
        },
    }
    readonly = _read_only_files(original_card, writable=target_paths)
    related_tests = _related_tests(original_card, writable=target_paths)
    if readonly:
        card["read_only_files"] = readonly
    if related_tests:
        card["related_existing_tests"] = related_tests
    _copy_guidance_fields(original_card, card)
    return card


def _verify_failure_card(
    *,
    original_card: dict[str, Any],
    task_id: str,
    target_paths: list[str],
    failed_tests: list[str],
    must_fix: list[str],
) -> dict[str, Any]:
    card: dict[str, Any] = {
        "schema_version": "contract_first.task_card.v1",
        "task_id": f"{task_id}_PYTEST_VERIFY_RECOVERY",
        "task_name": f"Pytest verify recovery for {task_id}",
        "why_this_layer": "Executor recovery card generated from deterministic scoped pytest failure evidence.",
        "files_to_change": list(target_paths),
        "new_files": _new_file_targets(original_card, target_paths),
        "invariants": _verify_failure_invariants(original_card),
        "forbidden_changes": _verify_failure_forbidden_changes(original_card),
        "verify_cmd": str(original_card.get("verify_cmd") or "").strip(),
        "recovery": {
            "schema_version": "execution.recovery_card.v1",
            "source_action": PYTEST_VERIFY_FAILURE_RECOVERY_ACTION,
            "must_fix": _string_list(must_fix),
            "reason": "Fix the failing scoped pytest assertions/runtime errors before retrying broad exploration.",
            "failed_tests": list(failed_tests),
            "instructions": [
                "Start from the failing pytest assertion or exception and patch the scoped implementation/test target directly.",
                "Do not repeat broad reads that were already completed in the previous executor attempt.",
                "Do not relax or delete tests unless allowed_test_mutations explicitly permits that exact mutation.",
                "Run the original scoped verify command after the targeted patch.",
            ],
        },
    }
    for key in ("read_only_files", "related_existing_tests"):
        value = original_card.get(key)
        if isinstance(value, list):
            card[key] = list(value)
    _copy_guidance_fields(original_card, card)
    return card


def _nameerror_invariants(original_card: dict[str, Any]) -> list[str]:
    return _unique_strings(
        [
            *_string_list(original_card.get("invariants")),
            "Fix pytest collection before changing behavior assertions.",
            "Define, import, or move the missing symbol so scoped pytest can collect tests.",
            "Preserve the task's behavioral contract and existing scoped tests.",
        ]
    )


def _nameerror_forbidden_changes(original_card: dict[str, Any]) -> list[str]:
    return _unique_strings(
        [
            *_string_list(original_card.get("forbidden_changes")),
            "Do not broaden scope beyond the pytest collection error files.",
            "Do not relax or delete assertions to make collection pass.",
        ]
    )


def _verify_failure_invariants(original_card: dict[str, Any]) -> list[str]:
    return _unique_strings(
        [
            *_string_list(original_card.get("invariants")),
            "Treat scoped pytest output as the active recovery target.",
            "Patch a files_to_change/new_files target before additional broad exploration.",
            "Preserve the task's behavior contract and original verify command.",
        ]
    )


def _verify_failure_forbidden_changes(original_card: dict[str, Any]) -> list[str]:
    return _unique_strings(
        [
            *_string_list(original_card.get("forbidden_changes")),
            "Do not delete, skip, or weaken failing tests unless allowed_test_mutations explicitly permits it.",
            "Do not expand scope beyond files_to_change/new_files for a pytest verify retry.",
        ]
    )


def _copy_guidance_fields(source: dict[str, Any], target: dict[str, Any]) -> None:
    for key in (
        "allowed_test_mutations",
        "api_contracts",
        "context_files",
        "coverage_hints",
        "do_not_change",
        "read_only_symbols",
        "requires",
        "review_focus",
        "test_plan",
    ):
        value = source.get(key)
        if isinstance(value, list):
            target[key] = list(value)


def _new_file_targets(original_card: dict[str, Any], target_paths: list[str]) -> list[str]:
    new_files = set(_string_list(original_card.get("new_files")))
    return [path for path in target_paths if path in new_files]


def _read_only_files(original_card: dict[str, Any], *, writable: list[str]) -> list[str]:
    candidates: list[Any] = []
    candidates.extend(_list_field(original_card, "read_only_files"))
    candidates.extend(_list_field(original_card, "context_files"))
    candidates.extend(_list_field(original_card, "related_existing_tests"))
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


def _list_field(source: dict[str, Any], key: str) -> list[Any]:
    value = source.get(key)
    return list(value) if isinstance(value, list) else []


def _test_paths(values: list[Any]) -> list[Any]:
    return [item for item in values if _is_test_path(str(item or ""))]


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
    text = str(value or "").replace("\\", "/").strip().strip("'\"`.,;:()[]")
    while text.startswith("./"):
        text = text[2:]
    if not text or text.startswith("/") or text.startswith("../") or "/../" in text:
        return ""
    if re.match(r"^[A-Za-z]:/", text):
        return ""
    return text


def _is_test_path(text: str) -> bool:
    normalized = _normalize_rel(text).lower()
    name = Path(normalized).name
    return normalized.startswith("tests/") or name.startswith("test_") or name.endswith("_test.py")
