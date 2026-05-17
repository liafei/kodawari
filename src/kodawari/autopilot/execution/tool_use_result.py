"""Result and progress helpers for the tool-use executor."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from kodawari.autopilot.core.secret_redactor import redact_jsonable, redact_secret_text
from kodawari.autopilot.execution.execution_artifacts import build_execution_result
from kodawari.autopilot.execution.tool_use_types import OpenAIToolUseExecutionError

OPENAI_TOOL_USE_BACKEND = "openai_tool_use"
TOOL_CALL_LOG_FILENAME = ".execution_tool_calls.jsonl"
PATCH_ATTEMPTS_FILENAME = ".execution_patch_attempts.jsonl"
READ_SCOPE_WIDEN_FILENAME = ".execution_read_scope_widen.jsonl"
STALL_REPORT_FILENAME = ".execution_stall_report.json"
INFEASIBILITY_REPORT_FILENAME = ".execution_infeasibility.json"
AUTO_FINISH_INTERRUPTION_CODES = {
    "HTTP_ERROR",
    "HTTP_WAF_BLOCKED",
    "MAX_TOOL_ITERATIONS",
    "MAX_WALL_CLOCK_SECONDS",
    "NO_PROGRESS_ABORTED",
    "TOOL_CALLS_MISSING",
}


def tool_observation_made_progress(runtime: Any, tool_name: str, result: dict[str, Any]) -> bool:
    if not bool(result.get("ok")):
        return False
    checker = _PROGRESS_CHECKERS.get(tool_name)
    if checker is not None:
        return checker(runtime, result)
    if tool_name not in {"read_file", "read_file_partial"}:
        return False
    return _read_progress(runtime, result)


def _hash_progress(runtime: Any, result: dict[str, Any]) -> bool:
    path = str(result.get("path") or "")
    digest = str(result.get("sha256") or "")
    if not path or not digest:
        return False
    key = f"{path}\0{digest}"
    if key in runtime.observed_hashes:
        return False
    runtime.observed_hashes.add(key)
    return True


def _search_progress(runtime: Any, result: dict[str, Any]) -> bool:
    path = str(result.get("path") or "")
    query = str(result.get("query") or "")
    digest = str(result.get("sha256") or "")
    key = f"search\0{path}\0{query}\0{digest}\0{result.get('match_count_returned')}"
    if not path or not query or key in runtime.observed_hashes:
        return False
    runtime.observed_hashes.add(key)
    return True


def _read_progress(runtime: Any, result: dict[str, Any]) -> bool:
    path = str(result.get("path") or "")
    if not path:
        return False
    offset = _read_offset(result)
    content_bytes = _read_content_bytes(result)
    if content_bytes <= 0:
        return False
    end = max(0, offset) + content_bytes
    previous = runtime.read_progress_ends.get(path, 0)

    counts = getattr(runtime, "read_progress_window_counts", None)
    if counts is None:
        counts = {}
        setattr(runtime, "read_progress_window_counts", counts)
    totals = getattr(runtime, "read_progress_total_bytes", None)
    if totals is None:
        totals = {}
        setattr(runtime, "read_progress_total_bytes", totals)
    counts[path] = int(counts.get(path, 0)) + 1
    totals[path] = int(totals.get(path, 0)) + content_bytes

    window_key = _read_window_key(path=path, offset=offset, content_bytes=content_bytes, result=result)
    windows = getattr(runtime, "read_progress_windows", None)
    if windows is None:
        windows = set()
        setattr(runtime, "read_progress_windows", windows)

    file_size = _read_file_size_hint(result)
    extends_max = end > previous
    duplicate_window = window_key in windows
    if not duplicate_window:
        windows.add(window_key)
    runtime.read_progress_ends[path] = max(previous, end)

    # Block "sliding window theatrics": once cumulative reads of this path
    # have covered ~70% of its known size (or accumulated >= 1.5x the file's
    # observable size), small new windows do not earn observation_progress.
    # The model already has the file in context — it should be writing.
    coverage_ratio = _coverage_ratio(end_max=runtime.read_progress_ends[path], file_size=file_size)
    re_read_ratio = _re_read_ratio(total=totals[path], file_size=file_size, end_max=runtime.read_progress_ends[path])
    saturated = coverage_ratio >= 0.7 or re_read_ratio >= 1.5
    if saturated and not extends_max:
        return False
    if duplicate_window:
        if not extends_max:
            return False
        return True
    return True


def _read_file_size_hint(result: dict[str, Any]) -> int:
    """Best-effort total file size from the read result."""

    for key in ("file_size", "total_bytes", "size", "size_bytes"):
        raw = result.get(key)
        if raw is None:
            continue
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return 0


def _coverage_ratio(*, end_max: int, file_size: int) -> float:
    if file_size <= 0:
        return 0.0
    return min(1.0, max(0.0, end_max / file_size))


def _re_read_ratio(*, total: int, file_size: int, end_max: int) -> float:
    """Total bytes pulled / known size (fall back to highest end seen)."""

    denominator = max(1, file_size if file_size > 0 else end_max)
    return total / denominator


def _read_window_key(*, path: str, offset: int, content_bytes: int, result: dict[str, Any]) -> str:
    digest = str(result.get("content_sha256") or result.get("sha256") or "")
    return f"{path}\0{max(0, offset)}\0{max(0, content_bytes)}\0{digest}"


def _read_offset(result: dict[str, Any]) -> int:
    try:
        return int(result.get("offset") or 0)
    except (TypeError, ValueError):
        return 0


def _read_content_bytes(result: dict[str, Any]) -> int:
    try:
        content_bytes = int(result.get("content_bytes") or 0)
    except (TypeError, ValueError):
        content_bytes = 0
    if content_bytes > 0:
        return content_bytes
    content = result.get("content")
    if isinstance(content, str):
        return len(content.encode("utf-8", errors="replace"))
    return 0


def _complexity_check_progress(_runtime: Any, result: dict[str, Any]) -> bool:
    """check_complexity counts as progress only when it returns a numeric result.

    Otherwise the model could spam it on non-Python files to game the stall
    timer. The tool returns ``ok=True`` with a ``violations`` list (possibly
    empty) on real Python files; non-applicable paths return ``ok=False`` with
    an explanatory ``error_code`` and DO NOT count as progress.
    """
    return bool(result.get("ok")) and "violations" in result


_PROGRESS_CHECKERS: dict[str, Any] = {
    "get_file_hash": _hash_progress,
    "search_file": _search_progress,
    "list_patch_plan": lambda _runtime, _result: True,
    # P1-#4: ``check_complexity`` is observation_progress when it returns a
    # real measurement. This unblocks self-checking refactors that today
    # would trip ``max_no_progress_iterations`` while the model is
    # legitimately verifying its own complexity numbers before submitting.
    "check_complexity": _complexity_check_progress,
}


def success_payload(runtime: Any, result: dict[str, Any]) -> dict[str, Any]:
    changed = [str(item) for item in list(result.get("changed_files") or []) if str(item).strip()]
    artifacts = changed + [".execution_tool_manifest.json", TOOL_CALL_LOG_FILENAME]
    if runtime.patch_log_path().exists():
        artifacts.append(PATCH_ATTEMPTS_FILENAME)
    if hasattr(runtime, "read_scope_widen_log_path") and runtime.read_scope_widen_log_path().exists():
        artifacts.append(READ_SCOPE_WIDEN_FILENAME)
    payload = build_execution_result(
        feature=str(runtime.request_payload.get("feature") or ""),
        task=str(runtime.request_payload.get("task") or ""),
        backend=OPENAI_TOOL_USE_BACKEND,
        status="PASS",
        changed_files=changed,
        artifacts=artifacts,
        summary=runtime.finished_summary or "openai_tool_use executor completed",
        execution_protocol=runtime.execution_protocol(),
        implementer_note={
            "claimed_intent": runtime.finished_summary,
            "claimed_invariants_preserved": [str(item) for item in list(runtime.request_payload.get("invariants") or [])],
            "claimed_risks": [],
        },
    )
    payload["verify_summary"] = redact_jsonable(result.get("verify_summary") or {})
    payload["tool_manifest"] = runtime.tool_manifest()
    return redact_jsonable(payload)


def auto_finish_after_interruption(runtime: Any, exc: OpenAIToolUseExecutionError) -> dict[str, Any] | None:
    if exc.code not in AUTO_FINISH_INTERRUPTION_CODES:
        return None
    if not runtime.changed_paths and not runtime.deleted_paths:
        return None
    runtime.finished_summary = (
        runtime.finished_summary
        or f"Runtime auto-finished existing scratch changes after executor interruption: {exc.code}."
    )
    try:
        finish_result = runtime._finish()
    except OpenAIToolUseExecutionError as finish_exc:
        return blocked_payload(
            runtime.config,
            runtime.request_payload,
            error_code=finish_exc.code,
            message=finish_exc.message,
            runtime=runtime,
        )
    if finish_result.get("status") == "FINISHED":
        finish_result = dict(finish_result)
        finish_result["auto_finished"] = True
        finish_result["interruption_code"] = exc.code
        payload = success_payload(runtime, finish_result)
        runtime.cleanup_success()
        return payload
    return blocked_payload(
        runtime.config,
        runtime.request_payload,
        error_code=str(finish_result.get("status") or exc.code),
        message=finish_result_message(finish_result, fallback=exc.message),
        runtime=runtime,
    )


def finish_result_message(finish_result: dict[str, Any], *, fallback: str) -> str:
    verify_summary = finish_result.get("verify_summary")
    if isinstance(verify_summary, dict):
        for key in ("blocking_reason", "summary"):
            value = str(verify_summary.get(key) or "").strip()
            if value:
                return value
    for key in ("error", "blocking_reason", "summary"):
        value = str(finish_result.get(key) or "").strip()
        if value:
            return value
    return str(fallback or "").strip()


def blocked_payload(config: Any, request_payload: dict[str, Any], *, error_code: str, message: str, runtime: Any | None) -> dict[str, Any]:
    payload = build_execution_result(
        feature=str(request_payload.get("feature") or ""),
        task=str(request_payload.get("task") or ""),
        backend=OPENAI_TOOL_USE_BACKEND,
        status="BLOCKED",
        changed_files=[],
        error_code=error_code,
        blocking_reason=redact_secret_text(message),
        summary=redact_secret_text(message),
        artifacts=[],
        execution_protocol=str(getattr(config, "execution_protocol", "") or request_payload.get("execution_protocol") or ""),
        implementer_note=request_payload.get("implementer_note"),
    )
    if runtime is not None:
        payload["execution_protocol"] = runtime.execution_protocol()
        payload["tool_manifest"] = runtime.tool_manifest()
        payload["scratch_root"] = str(runtime.scratch_root)
        if runtime.tool_log_path().exists():
            payload["artifacts"] = sorted(set(list(payload.get("artifacts") or []) + [TOOL_CALL_LOG_FILENAME]))
        if runtime.patch_log_path().exists():
            payload["artifacts"] = sorted(set(list(payload.get("artifacts") or []) + [PATCH_ATTEMPTS_FILENAME]))
        if hasattr(runtime, "read_scope_widen_log_path") and runtime.read_scope_widen_log_path().exists():
            payload["artifacts"] = sorted(set(list(payload.get("artifacts") or []) + [READ_SCOPE_WIDEN_FILENAME]))
        if runtime.stall_report_path().exists():
            payload["artifacts"] = sorted(set(list(payload.get("artifacts") or []) + [STALL_REPORT_FILENAME]))
            try:
                payload["stall_report"] = json.loads(runtime.stall_report_path().read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload["stall_report"] = {"schema_version": "execution.stall_report.v1", "error_code": error_code}
        infeasibility_path = runtime.planning_dir / INFEASIBILITY_REPORT_FILENAME
        if infeasibility_path.exists():
            payload["artifacts"] = sorted(set(list(payload.get("artifacts") or []) + [INFEASIBILITY_REPORT_FILENAME]))
            try:
                infeasibility = json.loads(infeasibility_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                infeasibility = {"schema_version": "execution.infeasibility.v1", "error_code": error_code}
            payload["infeasibility_report"] = infeasibility
            payload["missing_preconditions"] = list(infeasibility.get("missing_preconditions") or [])
    return redact_jsonable(payload)


def summarize_tool_payload(payload: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in dict(payload or {}).items():
        text_key = str(key)
        if text_key == "content" and isinstance(value, str):
            encoded = value.encode("utf-8", errors="replace")
            out["content_bytes"] = len(encoded)
            out["content_sha256"] = hashlib.sha256(encoded).hexdigest()
            continue
        if text_key == "verify_summary" and isinstance(value, dict):
            out[text_key] = {
                "status": str(value.get("status") or ""),
                "passed": bool(value.get("passed")),
                "summary": str(value.get("summary") or value.get("blocking_reason") or "")[:1000],
            }
            continue
        if isinstance(value, str) and len(value) > 1000:
            out[text_key] = value[:1000] + "...<truncated>"
            out[f"{text_key}_bytes"] = len(value.encode("utf-8", errors="replace"))
            continue
        out[text_key] = value
    return out
