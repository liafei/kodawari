"""Structured recovery failure events.

This module normalizes executor, verify, and gate artifacts into one small
shape so recovery detectors do not scrape human-facing log wording.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any


_NAMEERROR_RE = re.compile(r"NameError:\s+name ['\"]([^'\"]+)['\"] is not defined")
_REL_PY_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_./\\-])((?:[A-Za-z0-9_.-]+[\\/])+[A-Za-z0-9_.-]+\.py)(?::\d+)?"
)
_TOOL_LIMIT_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*) called too many times for ([^\s]+)")


@dataclass(frozen=True)
class ToolCallLimit:
    tool: str = ""
    path: str = ""
    count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"tool": self.tool, "path": self.path, "count": int(self.count)}


@dataclass(frozen=True)
class CollectionError:
    file: str = ""
    exc_type: str = ""
    name: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"file": self.file, "exc_type": self.exc_type, "name": self.name}


@dataclass(frozen=True)
class FailureEvent:
    phase: str
    error_code: str
    detector_hint: str = ""
    severity: str = "blocking"
    evidence: str = ""
    affected_paths: list[str] = field(default_factory=list)
    tool_call_limit: ToolCallLimit | None = None
    collection_errors: list[CollectionError] = field(default_factory=list)
    counters: dict[str, Any] = field(default_factory=dict)
    retryable: bool = False
    stall_report: dict[str, Any] = field(default_factory=dict)
    execution_result: dict[str, Any] = field(default_factory=dict)
    verify_check: dict[str, Any] = field(default_factory=dict)
    gate_check: dict[str, Any] = field(default_factory=dict)
    verify_passed: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "phase": self.phase,
            "error_code": self.error_code,
            "detector_hint": self.detector_hint,
            "severity": self.severity,
            "evidence": self.evidence,
            "affected_paths": list(self.affected_paths),
            "collection_errors": [item.to_dict() for item in self.collection_errors],
            "counters": dict(self.counters),
            "retryable": bool(self.retryable),
            "verify_passed": bool(self.verify_passed),
        }
        if self.tool_call_limit is not None:
            payload["tool_call_limit"] = self.tool_call_limit.to_dict()
        return payload


def build_failure_event(
    *,
    stall_report: dict[str, Any] | None = None,
    execution_result: dict[str, Any] | None = None,
    verify_check: dict[str, Any] | None = None,
    gate_check: dict[str, Any] | None = None,
    must_fix: list[str] | None = None,
    verify_passed: bool = False,
) -> FailureEvent:
    report = dict(stall_report or {}) if isinstance(stall_report, dict) else {}
    execution = dict(execution_result or {}) if isinstance(execution_result, dict) else {}
    if not report and isinstance(execution.get("stall_report"), dict):
        report = dict(execution.get("stall_report") or {})
    verify = dict(verify_check or {}) if isinstance(verify_check, dict) else {}
    gate = dict(gate_check or {}) if isinstance(gate_check, dict) else {}
    evidence = _evidence_text(
        must_fix=must_fix or [],
        stall_report=report,
        execution_result=execution,
        verify_check=verify,
        gate_check=gate,
    )
    error_code = _error_code(report=report, execution_result=execution, verify_check=verify, gate_check=gate)
    collection_errors = _collection_errors(evidence)
    tool_call_limit = _tool_call_limit(report=report, execution_result=execution, evidence=evidence)
    paths = _affected_paths(
        collection_errors=collection_errors,
        tool_call_limit=tool_call_limit,
        gate_check=gate,
        evidence=evidence,
        execution_result=execution,
    )
    counters = dict(report.get("counters") or {}) if isinstance(report.get("counters"), dict) else {}
    return FailureEvent(
        phase=_phase(error_code=error_code, verify_check=verify, gate_check=gate),
        error_code=error_code,
        detector_hint=_detector_hint(error_code=error_code, tool_call_limit=tool_call_limit, collection_errors=collection_errors),
        evidence=evidence,
        affected_paths=paths,
        tool_call_limit=tool_call_limit,
        collection_errors=collection_errors,
        counters=counters,
        retryable=_retryable(error_code),
        stall_report=report,
        execution_result=execution,
        verify_check=verify,
        gate_check=gate,
        verify_passed=bool(verify_passed),
    )


def _error_code(
    *,
    report: dict[str, Any],
    execution_result: dict[str, Any],
    verify_check: dict[str, Any],
    gate_check: dict[str, Any],
) -> str:
    execution_code = _first_code(execution_result.get("error_code"), execution_result.get("reason"))
    if execution_code.startswith("VERIFY_"):
        return execution_code
    if _verify_failed(verify_check):
        return "VERIFY_FAILED"
    if str(gate_check.get("total_status") or "").upper() == "BLOCKED":
        return "GATE_BLOCKED"
    if execution_code:
        return execution_code
    report_code = _first_code(report.get("error_code"), report.get("reason"))
    if report_code:
        return report_code
    return ""


def _phase(*, error_code: str, verify_check: dict[str, Any], gate_check: dict[str, Any]) -> str:
    if error_code == "GATE_BLOCKED" or str(gate_check.get("total_status") or "").upper() == "BLOCKED":
        return "gate"
    if error_code.startswith("VERIFY_") or _verify_failed(verify_check):
        return "verify"
    return "executor"


def _retryable(error_code: str) -> bool:
    return error_code in {
        "EXECUTOR_STALLED_NO_WRITE_PROGRESS",
        "EXECUTOR_STALLED_BUDGET_PRESSURE",
        "EXECUTOR_STALLED_PATCH_FAILURES",
        "EXECUTOR_STALLED_PATCH_PLAN_REQUIRED",
        "EXECUTOR_STALLED_REDUNDANT_READS",
        "EXECUTOR_STALLED_REPEATED_SEARCH",
        "MAX_SAME_TOOL_CALLS_PER_PATH",
        "MAX_TOOL_CALLS_PER_RESPONSE",
        "MAX_TOOL_ITERATIONS",
        "NO_PROGRESS_ABORTED",
        "VERIFY_FAILED",
        "VERIFY_FAILED_RETRYABLE",
        "GATE_BLOCKED",
    }


def _detector_hint(
    *,
    error_code: str,
    tool_call_limit: ToolCallLimit | None,
    collection_errors: list[CollectionError],
) -> str:
    if error_code == "EXECUTOR_STALLED_NO_WRITE_PROGRESS":
        return "no_write_stall"
    if error_code == "EXECUTOR_STALLED_PATCH_PLAN_REQUIRED":
        return "no_write_stall"
    if error_code == "MAX_SAME_TOOL_CALLS_PER_PATH" or tool_call_limit is not None:
        return "same_path_tool_limit"
    if collection_errors:
        return "pytest_collection_nameerror"
    if error_code in {"VERIFY_FAILED", "VERIFY_FAILED_RETRYABLE"}:
        return "pytest_verify_failure"
    if error_code == "GATE_BLOCKED":
        return "gate_complexity"
    return ""


def _first_code(*values: Any) -> str:
    for value in values:
        code = str(value or "").strip().upper()
        if code:
            return code
    return ""


def _verify_failed(verify_check: dict[str, Any]) -> bool:
    if not isinstance(verify_check, dict) or not verify_check:
        return False
    if verify_check.get("passed") is False:
        return True
    status = str(verify_check.get("status") or verify_check.get("total_status") or "").strip().upper()
    return status in {"FAIL", "FAILED", "BLOCKED", "ERROR"}


def _evidence_text(
    *,
    must_fix: list[str],
    stall_report: dict[str, Any],
    execution_result: dict[str, Any],
    verify_check: dict[str, Any],
    gate_check: dict[str, Any],
) -> str:
    parts = [str(item) for item in must_fix if str(item).strip()]
    for source in (execution_result, stall_report, verify_check, gate_check):
        for key in (
            "blocking_reason",
            "reason",
            "summary",
            "error",
            "error_message",
            "stdout_excerpt",
            "stdout",
            "stderr_excerpt",
            "stderr",
        ):
            value = source.get(key)
            if value:
                parts.append(str(value))
        for nested_key in ("verify_summary", "verify_check"):
            nested = source.get(nested_key)
            if isinstance(nested, dict):
                for key in ("stdout_excerpt", "stdout", "stderr_excerpt", "stderr", "summary", "error"):
                    value = nested.get(key)
                    if value:
                        parts.append(str(value))
    return "\n".join(parts)


def _tool_call_limit(
    *,
    report: dict[str, Any],
    execution_result: dict[str, Any],
    evidence: str,
) -> ToolCallLimit | None:
    for source in (report, execution_result):
        raw = source.get("tool_call_limit")
        if isinstance(raw, dict):
            tool = str(raw.get("tool") or "").strip()
            path = _normalize_path(raw.get("path"))
            count = _to_int(raw.get("count"), 0)
            if tool and path:
                return ToolCallLimit(tool=tool, path=path, count=count)
    match = _TOOL_LIMIT_RE.search(evidence)
    if match:
        return ToolCallLimit(tool=match.group(1), path=_normalize_path(match.group(2)), count=0)
    return None


def _collection_errors(text: str) -> list[CollectionError]:
    lowered = text.lower()
    if "error collecting" not in lowered and "error at setup" not in lowered:
        return []
    names = _unique(_NAMEERROR_RE.findall(text))
    paths = _unique(_normalize_path(path) for path in _REL_PY_PATH_RE.findall(text))
    if not names or not paths:
        return []
    return [
        CollectionError(file=path, exc_type="NameError", name=name)
        for path in paths
        for name in names
    ]


def _affected_paths(
    *,
    collection_errors: list[CollectionError],
    tool_call_limit: ToolCallLimit | None,
    gate_check: dict[str, Any],
    evidence: str,
    execution_result: dict[str, Any] | None = None,
) -> list[str]:
    paths: list[str] = []
    # Structured infeasibility signal carries its missing_preconditions
    # directly — these are the highest-fidelity affected paths available.
    execution = dict(execution_result or {}) if isinstance(execution_result, dict) else {}
    for raw in list(execution.get("missing_preconditions") or []):
        text = str(raw or "").strip()
        if text:
            paths.append(text)
    infeasibility = execution.get("infeasibility_report") if isinstance(execution.get("infeasibility_report"), dict) else {}
    for raw in list(infeasibility.get("missing_preconditions") or []):
        text = str(raw or "").strip()
        if text:
            paths.append(text)
    paths.extend(item.file for item in collection_errors if item.file)
    if tool_call_limit is not None and tool_call_limit.path:
        paths.append(tool_call_limit.path)
    for checker in list(gate_check.get("items") or []):
        if not isinstance(checker, dict):
            continue
        for violation in list(checker.get("violations") or []):
            if isinstance(violation, dict):
                paths.append(_normalize_path(violation.get("path")))
    paths.extend(_normalize_path(path) for path in _REL_PY_PATH_RE.findall(evidence))
    return _unique(path for path in paths if path)


def _normalize_path(value: Any) -> str:
    path = str(value or "").strip().replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


def _to_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _unique(values) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        item = str(raw or "").strip()
        if not item:
            continue
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


__all__ = ["CollectionError", "FailureEvent", "ToolCallLimit", "build_failure_event"]
