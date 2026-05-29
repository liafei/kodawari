"""Guarded OpenAI-compatible tool-use execution backend."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import time
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest

from kodawari.autopilot.core.repo_path_guard import (
    DEFAULT_MAX_READ_BYTES,
    guard_repo_read_path,
    guard_repo_write_path,
)
from kodawari.autopilot.core.http_safety import RedirectBlocked, SafeRedirectHandler
from kodawari.autopilot.core.prompt_profiles import (
    render_learned_prompt_lesson_text,
    nudge_policy_for_model,
    render_prompt_profile_text,
)
from kodawari.autopilot.core.secret_redactor import redact_jsonable, redact_secret_text
from kodawari.autopilot.core.task_modes import verification_only_allows_empty_files
from kodawari.autopilot.execution import tool_use_prompt as _tool_use_prompt
from kodawari.autopilot.execution import tool_use_result as _tool_use_result
from kodawari.autopilot.execution import tool_use_stall as _tool_use_stall
from kodawari.autopilot.execution.tool_use_stall import StallDetector
from kodawari.autopilot.execution import tool_use_transport as _tool_use_transport
from kodawari.autopilot.execution.tool_use_common import (
    _cap,
    _changed_files_from_hashes,
    _copy_ignore,
    _dedupe_paths,
    _file_hash,
    _file_hashes,
    _is_test_path,
    _looks_like_repo_context_path,
    _looks_secret,
    _normalize_rel,
    _replacement_texts_for_content,
    _task_id_tokens,
    _text_count_with_line_ending_variants,
)
from kodawari.autopilot.execution.tool_use_types import OpenAIToolUseExecutionError
from kodawari.autopilot.execution.tool_use_runtime import ToolUseRuntime
from kodawari.autopilot.execution.verify_execution import VERIFY_TIMEOUT_SECONDS, maybe_execute_verify_command


OPENAI_TOOL_USE_BACKEND = "openai_tool_use"
FULL_FILE_PROTOCOL = _tool_use_prompt.FULL_FILE_PROTOCOL
EXACT_STR_REPLACE_PROTOCOL = _tool_use_prompt.EXACT_STR_REPLACE_PROTOCOL
TOOL_MANIFEST_VERSION = "execution.tool_manifest.v1"
TOOL_CALL_LOG_FILENAME = ".execution_tool_calls.jsonl"
PATCH_ATTEMPTS_FILENAME = ".execution_patch_attempts.jsonl"
READ_SCOPE_WIDEN_FILENAME = ".execution_read_scope_widen.jsonl"
STALL_REPORT_FILENAME = ".execution_stall_report.json"
FULL_FILE_TOOL_MANIFEST_V1 = _tool_use_prompt.FULL_FILE_TOOL_MANIFEST_V1
PATCH_TOOL_MANIFEST_V1 = _tool_use_prompt.PATCH_TOOL_MANIFEST_V1
TOOL_MANIFEST_V2 = _tool_use_prompt.TOOL_MANIFEST_V2
_SKIP_TOP_LEVEL = {".git", ".venv", "node_modules", ".tox", ".workflow", ".workflow_runtime"}
_UNSUPPORTED_REQUEST_CAPABILITIES = {
    "shell.exec",
    "bash",
    "repo.move_file",
    "repo.rename_file",
    "file.move",
    "file.rename",
    "patch.emit",
    "patch.apply",
    "replace_in_file",
    "apply_patch",
}
_UNSUPPORTED_REQUEST_MARKERS = (
    "replace_in_file",
    "apply_patch",
    "search/replace",
    "search-replace",
    "patch protocol",
    "move_file",
    "rename_file",
    "move file",
    "rename file",
    "shell.exec",
    "run shell",
    "bash -c",
    "run bash",
    "use bash",
)


_RECOVERABLE_READ_SCOPE_ERRORS = {
    "PATH_OUT_OF_SCOPE",
    "PATH_GUARD_BLOCKED",
    "DIR_OUT_OF_SCOPE",
    "READ_FAILED",
}
_READ_ONLY_TOOLS = {"list_files_in_dir", "read_file", "read_file_partial", "get_file_hash", "search_file"}
_INTERNAL_TOOL_NAME_KEY = _tool_use_prompt.INTERNAL_TOOL_NAME_KEY
_INTERNAL_TARGET_PATH_KEY = _tool_use_prompt.INTERNAL_TARGET_PATH_KEY
_STALL_ERROR_CODES = {
    "EXECUTOR_STALLED_BUDGET_PRESSURE",
    "EXECUTOR_STALLED_CONTEXT_OVERFLOW",
    "EXECUTOR_STALLED_FRAGMENTED_READS",
    "EXECUTOR_STALLED_NO_WRITE_PROGRESS",
    "EXECUTOR_STALLED_PATCH_PLAN_REQUIRED",
    "EXECUTOR_STALLED_PATCH_FAILURES",
    "EXECUTOR_STALLED_REDUNDANT_READS",
    "EXECUTOR_STALLED_REPEATED_SEARCH",
    "TASK_BLOCKED_BY_PRECONDITION",
    "MAX_SAME_TOOL_CALLS_PER_PATH",
    "READ_SCOPE_EXHAUSTED",
}
_PATCH_ACTION_TOOLS = {"str_replace", "write_new_file", "delete_file", "apply_patch_plan_item", "finish_execution"}
_SAFE_ROOT_READ_NAMES = {
    "README.md",
    "pyproject.toml",
    "pytest.ini",
    "setup.cfg",
    "tox.ini",
    "package.json",
    "tsconfig.json",
}
_SAFE_CONTEXT_FILE_NAMES = {
    "conftest.py",
    "db.py",
    "database.py",
    "schema.py",
    "db_schema.py",
    "schemas.py",
}
_SAFE_CONTEXT_SUFFIXES = ("_schema.py", "_schemas.py", "_models.py", "_types.py")


def _tool_path_arg(arguments: dict[str, Any]) -> str:
    return str(arguments.get("path") or arguments.get("dir") or "").strip()


def _recoverable_tool_error(name: str, code: str) -> bool:
    return name in _READ_ONLY_TOOLS and code in _RECOVERABLE_READ_SCOPE_ERRORS

def materialize_openai_tool_use_result(
    *,
    config: Any,
    request_path: Path,
    request_payload: dict[str, Any],
) -> dict[str, Any]:
    runtime: ToolUseRuntime | None = None
    try:
        task_card = request_payload.get("task_card")
        if (
            not [str(item).strip() for item in list(request_payload.get("files_to_change") or []) if str(item).strip()]
            and verification_only_allows_empty_files(
                request_payload,
                task_card if isinstance(task_card, dict) else None,
            )
        ):
            return _materialize_verification_only_result(config=config, request_payload=request_payload)
        runtime = _build_runtime(config=config, request_path=request_path, request_payload=request_payload)
        _apply_recovery_card_action_only_mode(runtime, request_payload)
        runtime.write_tool_manifest()
        preexisting_result = _maybe_accept_preexisting_task_state(runtime)
        if preexisting_result is not None:
            runtime.write_tool_manifest()
            payload = _success_payload(runtime, preexisting_result)
            runtime.cleanup_success()
            return payload
        auto_result = _maybe_auto_apply_recovery_patch_plan(runtime)
        if auto_result is not None:
            runtime.write_tool_manifest()
            payload = _success_payload(runtime, auto_result)
            runtime.cleanup_success()
            return payload
        result = _run_tool_loop(runtime)
        runtime.write_tool_manifest()
        payload = _success_payload(runtime, result)
        runtime.cleanup_success()
        return payload
    except OpenAIToolUseExecutionError as exc:
        interrupted_payload = _auto_finish_after_interruption(runtime, exc) if runtime is not None else None
        if interrupted_payload is not None:
            return interrupted_payload
        if runtime is not None and _is_stall_error_code(exc.code):
            runtime.write_stall_report(error_code=exc.code, message=exc.message, iteration=_stall_iteration(runtime))
        if runtime is not None:
            runtime.write_tool_manifest()
        return _blocked_payload(config, request_payload, error_code=exc.code, message=exc.message, runtime=runtime)
    except Exception as exc:
        return _blocked_payload(config, request_payload, error_code="OPENAI_TOOL_USE_ERROR", message=str(exc), runtime=runtime)


def _verification_only_verify_cmd(request_payload: dict[str, Any]) -> str:
    task_card = request_payload.get("task_card")
    if isinstance(task_card, dict):
        for container in (task_card, task_card.get("execution_constraints")):
            if not isinstance(container, dict):
                continue
            command = str(container.get("verify_cmd") or container.get("test_plan") or "").strip()
            if command:
                return command
    return str(request_payload.get("verify_cmd") or "").strip()


def _verification_only_evidence_paths(request_payload: dict[str, Any]) -> list[str]:
    task_card = request_payload.get("task_card")
    if not isinstance(task_card, dict):
        return []
    paths: list[str] = []
    for field in ("related_existing_tests", "read_only_files", "do_not_change"):
        for raw in list(task_card.get(field) or []):
            path = str(raw or "").strip().replace("\\", "/")
            if path and path not in paths:
                paths.append(path)
    return paths


def _materialize_verification_only_result(*, config: Any, request_payload: dict[str, Any]) -> dict[str, Any]:
    project_root = Path(str(request_payload.get("project_root") or "")).resolve()
    verify_cmd = _verification_only_verify_cmd(request_payload)
    if not verify_cmd:
        return _blocked_payload(
            config,
            request_payload,
            error_code="VERIFY_CMD_MISSING",
            message="verification-only task requires verify_cmd",
            runtime=None,
        )
    verify = maybe_execute_verify_command(
        project_root=project_root,
        feature=str(request_payload.get("feature") or ""),
        task_label=str(request_payload.get("task") or ""),
        verify_cmd=verify_cmd,
        changed_files=_verification_only_evidence_paths(request_payload),
        timeout_seconds=_cap(config, "verify_timeout_seconds", VERIFY_TIMEOUT_SECONDS),
    )
    if verify and bool(verify.get("passed")):
        payload = _tool_use_result.build_execution_result(
            feature=str(request_payload.get("feature") or ""),
            task=str(request_payload.get("task") or ""),
            backend=OPENAI_TOOL_USE_BACKEND,
            status="PASS",
            changed_files=[],
            artifacts=[],
            summary="openai_tool_use verification-only task passed scoped verify.",
            execution_protocol=str(getattr(config, "execution_protocol", "") or request_payload.get("execution_protocol") or ""),
            implementer_note={
                "claimed_intent": "Verify existing implementation without code edits.",
                "claimed_invariants_preserved": [
                    str(item) for item in list(request_payload.get("invariants") or []) if str(item).strip()
                ],
                "claimed_risks": [],
            },
        )
        payload["verify_summary"] = redact_jsonable(verify)
        payload["verification_only_noop"] = True
        return redact_jsonable(payload)
    blocking_reason = "verification-only verify command did not pass"
    if isinstance(verify, dict):
        blocking_reason = str(
            verify.get("blocking_reason")
            or verify.get("summary")
            or blocking_reason
        ).strip()
    payload = _blocked_payload(
        config,
        request_payload,
        error_code="VERIFY_FAILED",
        message=blocking_reason,
        runtime=None,
    )
    payload["verify_summary"] = redact_jsonable(verify or {})
    payload["verification_only_noop"] = True
    return payload


def _maybe_auto_apply_recovery_patch_plan(runtime: ToolUseRuntime) -> dict[str, Any] | None:
    if runtime.execution_protocol() != EXACT_STR_REPLACE_PROTOCOL:
        return None
    if _cap(runtime.config, "auto_apply_recovery_patch_plan", 1) <= 0:
        return None
    task_card = runtime.request_payload.get("task_card")
    if not isinstance(task_card, dict):
        return None
    recovery = task_card.get("recovery")
    if not isinstance(recovery, dict):
        return None
    if str(recovery.get("source_action") or "").strip() != "narrow_patch_plan":
        return None
    if not runtime._patch_plan():
        return None
    apply_result = runtime.apply_recovery_patch_plan()
    failures = list(apply_result.get("failed") or []) if isinstance(apply_result, dict) else []
    if failures and not runtime.changed_paths:
        first = dict(failures[0])
        raise OpenAIToolUseExecutionError(
            str(first.get("error_code") or "PATCH_PLAN_APPLY_FAILED"),
            str(first.get("error") or "runtime failed to apply recovery patch plan"),
        )
    if failures and not str(runtime.request_payload.get("verify_cmd") or "").strip():
        first = dict(failures[0])
        raise OpenAIToolUseExecutionError(
            str(first.get("error_code") or "PATCH_PLAN_PARTIAL_WITHOUT_VERIFY"),
            str(first.get("error") or "recovery patch plan applied only partially and no verify_cmd is available"),
        )
    runtime.finished_summary = "Runtime auto-applied executor recovery patch plan."
    finish_result = runtime._finish()
    if finish_result.get("status") == "FINISHED":
        finish_result = dict(finish_result)
        finish_result["auto_applied_recovery_patch_plan"] = True
        return finish_result
    if failures:
        raise OpenAIToolUseExecutionError(
            "PATCH_PLAN_PARTIAL_VERIFY_FAILED",
            (
                _patch_plan_failure_message(failures)
                + "; verify after partial recovery patch failed: "
                + _finish_result_message(finish_result, fallback="recovery patch plan verify failed")
            ),
        )
    raise OpenAIToolUseExecutionError(
        str(finish_result.get("status") or "VERIFY_FAILED_RETRYABLE"),
        _finish_result_message(finish_result, fallback="recovery patch plan verify failed"),
    )


def _maybe_accept_preexisting_task_state(runtime: ToolUseRuntime) -> dict[str, Any] | None:
    if not _preexisting_task_acceptance_enabled(runtime):
        return None
    verify_cmd = str(runtime.request_payload.get("verify_cmd") or "").strip()
    if not verify_cmd:
        return None
    changed_files = [item for item in runtime.allowed_files if (runtime.project_root / item).is_file()]
    if set(changed_files) != set(runtime.allowed_files):
        return None
    verify = maybe_execute_verify_command(
        project_root=runtime.project_root,
        feature=str(runtime.request_payload.get("feature") or ""),
        task_label=str(runtime.request_payload.get("task") or ""),
        verify_cmd=verify_cmd,
        changed_files=changed_files,
        timeout_seconds=_cap(runtime.config, "verify_timeout_seconds", VERIFY_TIMEOUT_SECONDS),
    )
    if not verify or not bool(verify.get("passed")):
        return None
    runtime.finished_summary = "Runtime accepted pre-existing task state after scoped verify passed."
    return {
        "ok": True,
        "status": "FINISHED",
        "changed_files": changed_files,
        "verify_summary": redact_jsonable(verify),
        "preexisting_task_state_accepted": True,
    }


def _preexisting_task_acceptance_enabled(runtime: ToolUseRuntime) -> bool:
    if str(os.environ.get("WORKFLOW_DISABLE_PREEXISTING_RECOVERY_ACCEPTANCE") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return False
    task_card = runtime.request_payload.get("task_card")
    if not isinstance(task_card, dict):
        return False
    recovery = task_card.get("recovery")
    if isinstance(recovery, dict) and bool(str(recovery.get("source_action") or "").strip()):
        return True
    new_files = _dedupe_paths([str(item) for item in list(task_card.get("new_files") or [])])
    if not new_files:
        return False
    declared = _dedupe_paths(
        [str(item) for item in list(task_card.get("files_to_change") or [])]
        or [str(item) for item in list(runtime.allowed_files or [])]
    )
    if not declared:
        return False
    return set(new_files).issubset(set(declared))


def _patch_plan_failure_message(failures: list[Any]) -> str:
    parts: list[str] = []
    for raw in failures[:3]:
        if not isinstance(raw, dict):
            continue
        parts.append(
            "patch_plan item failed"
            f" id={str(raw.get('id') or '')}"
            f" path={str(raw.get('path') or '')}"
            f" code={str(raw.get('error_code') or '')}"
            f" error={str(raw.get('error') or '')}"
        )
    if not parts:
        return "recovery patch plan partially failed"
    omitted = len(failures) - len(parts)
    if omitted > 0:
        parts.append(f"{omitted} additional patch_plan failure(s) omitted")
    return "; ".join(parts)


def _apply_recovery_card_action_only_mode(runtime: ToolUseRuntime, request_payload: dict[str, Any]) -> None:
    """Flip ``runtime.action_only_mode=True`` when the recovery card
    propagated through ``request_payload["task_card"]`` carries the
    ``action_only_on_start`` flag.

    This fires when engine_implementation_mixin recognised a
    no_write_stall recovery card (detector_name=="no_write_stall") and
    tagged the card with the flag before submitting the execute
    request. Tool schemas are rebuilt every iteration via
    _tool_schemas_for_runtime, so flipping the flag here drops read
    tools from the very first chat turn — preventing mimo (or any
    weaker-instruction-following model) from re-entering the same
    read loop that triggered the original stall.

    Pulled out as a helper so unit tests can exercise the call site
    directly without spinning up the HTTP gateway / chat client.
    """
    task_card = request_payload.get("task_card") or {}
    if not isinstance(task_card, dict):
        return
    if not bool(task_card.get("action_only_on_start")):
        return
    runtime.action_only_mode = True
    runtime.action_only_reason = str(
        task_card.get("action_only_reason") or "recovery_card_action_only"
    )


def _build_runtime(*, config: Any, request_path: Path, request_payload: dict[str, Any]) -> ToolUseRuntime:
    project_root = Path(str(request_payload.get("project_root") or "")).resolve()
    planning_dir = Path(str(request_payload.get("planning_dir") or project_root)).resolve()
    allowed = _normalize_allowed_files(project_root, list(request_payload.get("files_to_change") or []))
    if not allowed:
        raise OpenAIToolUseExecutionError("ALLOWED_FILES_MISSING", "openai_tool_use executor requires task files_to_change")
    read_only = _normalize_read_only_files(project_root, request_payload, writable_files=allowed)
    protocol = str(
        getattr(config, "execution_protocol", "")
        or request_payload.get("execution_protocol")
        or FULL_FILE_PROTOCOL
    ).strip().lower().replace("-", "_")
    unsupported = _openai_tool_use_unsuitable_reasons(request_payload, execution_protocol=protocol)
    if unsupported:
        raise OpenAIToolUseExecutionError(
            "OPENAI_TOOL_USE_TASK_UNSUITABLE",
            "openai_tool_use cannot safely execute this task: " + "; ".join(unsupported),
        )
    return ToolUseRuntime(
        config=config,
        request_path=request_path,
        request_payload=request_payload,
        project_root=project_root,
        planning_dir=planning_dir,
        allowed_files=allowed,
        read_only_files=read_only,
    )


@dataclass
class _ToolCallOutcome:
    finish_result: dict[str, Any] | None
    write_progress: bool
    observation_progress: bool
    tool_result_message: dict[str, Any] | None


@dataclass
class _PostChatOutcome:
    body: dict[str, Any] | None
    waf_retry_count: int
    waf_compact_mode: bool
    should_continue: bool = False


def _process_single_tool_call(
    runtime: ToolUseRuntime,
    call: Any,
    iteration: int,
    detector: Any,
) -> _ToolCallOutcome:
    tool_name, arguments, tool_call_id = _tool_call_parts(call)
    before_changed = set(runtime.changed_paths)
    try:
        detector.record_tool_call(tool_name, arguments)
        result = runtime.execute_tool(tool_name, arguments)
    except OpenAIToolUseExecutionError as exc:
        if _recoverable_tool_error(tool_name, exc.code):
            result = {
                "ok": False,
                "status": "TOOL_ERROR",
                "error_code": exc.code,
                "error": exc.message,
                "instruction": "Do not request this path again. Continue using only files_to_change, or adjust the in-scope implementation/test.",
            }
            runtime.log_tool_call(
                iteration=iteration,
                tool_call_id=tool_call_id,
                name=tool_name,
                arguments=arguments,
                result=result,
                error_code=exc.code,
                error_message=exc.message,
            )
            return _ToolCallOutcome(
                finish_result=None,
                write_progress=False,
                observation_progress=False,
                tool_result_message=_tool_result_message(tool_call_id, tool_name, result),
            )
        runtime.log_tool_call(
            iteration=iteration,
            tool_call_id=tool_call_id,
            name=tool_name,
            arguments=arguments,
            error_code=exc.code,
            error_message=exc.message,
        )
        raise
    runtime.log_tool_call(
        iteration=iteration,
        tool_call_id=tool_call_id,
        name=tool_name,
        arguments=arguments,
        result=result,
    )
    detector.record_tool_result(tool_name, result)
    tool_result_message = _tool_result_message(tool_call_id, tool_name, result)
    if runtime.finish_seen and result.get("status") == "FINISHED":
        return _ToolCallOutcome(
            finish_result=dict(result),
            write_progress=False,
            observation_progress=False,
            tool_result_message=tool_result_message,
        )
    write_progress = set(runtime.changed_paths) != before_changed or tool_name == "finish_execution"
    observation_progress = False
    if not write_progress:
        observation_progress = _tool_observation_made_progress(runtime, tool_name, result)
    if tool_name in {"read_file", "read_file_partial"}:
        path = str(result.get("path") or arguments.get("path") or "")
        if path:
            window_count = int(runtime.read_progress_window_counts.get(path.replace("\\", "/"), 0))
            detector.record_fragmented_read(path=path, window_count=window_count)
    return _ToolCallOutcome(
        finish_result=None,
        write_progress=write_progress,
        observation_progress=observation_progress,
        tool_result_message=tool_result_message,
    )


def _tool_loop_endpoint_and_key(runtime: ToolUseRuntime) -> tuple[str, str]:
    endpoint = _chat_completions_endpoint(_base_url(runtime.config), api_format=str(getattr(runtime.config, "api_format", "") or ""))
    api_key = os.environ.get(str(getattr(runtime.config, "api_key_env", "") or ""), "")
    if not api_key:
        raise OpenAIToolUseExecutionError("API_KEY_MISSING", "openai_tool_use executor api_key_env is missing or empty")
    return endpoint, api_key


def _enforce_tool_loop_iteration_budget(
    runtime: ToolUseRuntime,
    *,
    started: float,
    iteration: int,
    last_progress_iteration: int,
) -> None:
    if time.monotonic() - started > _cap(runtime.config, "max_wall_clock_seconds", 1800):
        raise OpenAIToolUseExecutionError("MAX_WALL_CLOCK_SECONDS", "openai_tool_use executor exceeded wall-clock budget")
    if iteration - last_progress_iteration > _cap(runtime.config, "max_no_progress_iterations", 5):
        raise OpenAIToolUseExecutionError("NO_PROGRESS_ABORTED", "openai_tool_use executor made no write/finish progress")


def _post_tool_loop_chat(
    runtime: ToolUseRuntime,
    *,
    endpoint: str,
    api_key: str,
    payload: dict[str, Any],
    messages: list[dict[str, Any]],
    waf_retry_count: int,
) -> _PostChatOutcome:
    try:
        body = _post_chat(
            endpoint=endpoint,
            api_key=api_key,
            payload=payload,
            timeout_seconds=_http_timeout_seconds(runtime.config),
            max_retries=_cap(runtime.config, "max_http_retries", 2),
            api_format=str(getattr(runtime.config, "api_format", "") or "openai_chat"),
        )
    except OpenAIToolUseExecutionError as exc:
        if exc.code == "HTTP_WAF_BLOCKED" and waf_retry_count < _cap(runtime.config, "max_waf_retries", 1):
            messages.append({"role": "user", "content": _waf_retry_instruction(runtime)})
            return _PostChatOutcome(
                body=None,
                waf_retry_count=waf_retry_count + 1,
                waf_compact_mode=True,
                should_continue=True,
            )
        raise
    return _PostChatOutcome(body=body, waf_retry_count=waf_retry_count, waf_compact_mode=False)


def _record_tool_loop_token_spend(
    detector: Any,
    *,
    body: dict[str, Any],
    payload: dict[str, Any],
    token_spend_reported: int,
    token_spend_estimated: int,
    iteration: int,
) -> tuple[int, int, bool]:
    reported_delta = _reported_usage_tokens(body)
    estimated_delta = _estimated_payload_tokens(payload)
    token_spend_reported += reported_delta
    token_spend_estimated += max(reported_delta, estimated_delta)
    cache_hit, cache_miss = _reported_cache_tokens(body)
    if cache_hit or cache_miss:
        detector.record_prompt_cache(hit=cache_hit, miss=cache_miss)
    crossed_soft_budget = detector.record_token_spend(
        reported=token_spend_reported,
        estimated=token_spend_estimated,
        iteration=iteration,
    )
    detector.enforce_hard_budget()
    return token_spend_reported, token_spend_estimated, crossed_soft_budget


def _message_tool_calls(body: dict[str, Any], runtime: ToolUseRuntime) -> tuple[dict[str, Any], list[Any]]:
    message = _first_message(body)
    calls = message.get("tool_calls")
    if not isinstance(calls, list) or not calls:
        raise OpenAIToolUseExecutionError("TOOL_CALLS_MISSING", "model did not call an execution tool")
    if len(calls) > _cap(runtime.config, "max_tool_calls_per_response", 8):
        raise OpenAIToolUseExecutionError("MAX_TOOL_CALLS_PER_RESPONSE", "model emitted too many tool calls")
    return message, calls


def _append_budget_pressure_instruction(
    messages: list[dict[str, Any]],
    runtime: ToolUseRuntime,
    *,
    crossed_soft_budget: bool,
) -> None:
    if crossed_soft_budget:
        messages.append({"role": "user", "content": _budget_pressure_instruction(runtime)})


def _no_write_threshold(runtime: ToolUseRuntime) -> int:
    threshold = _cap(runtime.config, "max_no_write_iterations", 12)
    if runtime.stall_detector.budget_pressure:
        threshold = min(threshold, _cap(runtime.config, "max_no_write_iterations_under_budget_pressure", 2))
    return max(1, threshold)


def _runtime_cap(config: Any, key: str) -> int | None:
    caps = getattr(config, "runtime_caps", None)
    if not isinstance(caps, dict) or key not in caps:
        return None
    try:
        value = int(caps.get(key) or 0)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _executor_nudge_policy(runtime: ToolUseRuntime) -> dict[str, int]:
    return nudge_policy_for_model(
        project_root=runtime.project_root,
        model=str(getattr(runtime.config, "model", "") or ""),
        transport_name=str(getattr(runtime.config, "transport_name", "") or ""),
        driver=str(getattr(runtime.config, "backend", "") or ""),
    )


def _write_progress_nudge_iteration(runtime: ToolUseRuntime) -> int:
    explicit = _runtime_cap(runtime.config, "write_progress_nudge_iteration")
    if explicit is not None:
        return explicit
    policy = _executor_nudge_policy(runtime)
    return int(policy.get("write_progress_nudge_iteration") or policy.get("no_write_after_iter") or 4)


def _missing_writable_remind_every(runtime: ToolUseRuntime) -> int:
    return int(_executor_nudge_policy(runtime).get("missing_writable_remind_every") or 0)


def _should_send_write_progress_nudge(
    runtime: ToolUseRuntime,
    *,
    iteration: int,
    last_sent_iteration: int,
) -> bool:
    if runtime.changed_paths or runtime.deleted_paths:
        return False
    threshold = _no_write_threshold(runtime)
    configured = _write_progress_nudge_iteration(runtime)
    trigger = max(1, min(configured, max(1, threshold - 2)))
    if last_sent_iteration <= 0:
        return int(iteration) >= trigger
    reminder = _missing_writable_remind_every(runtime)
    return reminder > 0 and int(iteration) - int(last_sent_iteration) >= reminder


def _patch_plan_required_iteration(runtime: ToolUseRuntime) -> int:
    explicit = _runtime_cap(runtime.config, "patch_plan_required_iteration")
    if explicit is not None:
        return explicit
    threshold = _no_write_threshold(runtime)
    return max(1, min(_write_progress_nudge_iteration(runtime) + 2, max(1, threshold - 1)))


def _max_patch_plan_required_read_iterations(runtime: ToolUseRuntime) -> int:
    explicit = _runtime_cap(runtime.config, "max_patch_plan_required_read_iterations")
    return explicit if explicit is not None else 2


def _should_require_patch_plan(
    runtime: ToolUseRuntime,
    *,
    iteration: int,
    last_sent_iteration: int,
) -> bool:
    if runtime.changed_paths or runtime.deleted_paths:
        return False
    return last_sent_iteration <= 0 and int(iteration) >= _patch_plan_required_iteration(runtime)


def _append_patch_plan_required_instruction(
    messages: list[dict[str, Any]],
    runtime: ToolUseRuntime,
    *,
    iteration: int,
) -> None:
    messages.append(
        {
            "role": "user",
            "content": _patch_plan_required_instruction(runtime, iteration=iteration),
        }
    )


def _patch_plan_required_instruction(runtime: ToolUseRuntime, *, iteration: int) -> str:
    missing = json.dumps(_tool_use_prompt.missing_writable_files(runtime), ensure_ascii=False)
    return (
        f"Patch-plan discipline is now required after {int(iteration)} read/search iteration(s) without writes. "
        "Stop broad context gathering. In the next response, either call str_replace/write_new_file/"
        "apply_patch_plan_item for an in-scope file, or call finish_execution with the exact blocker. "
        f"Missing writable files that require write_new_file: {missing}. "
        "Only one final targeted read/hash is acceptable if it is immediately needed for exact old_text."
    )


def _tool_call_names(calls: list[Any]) -> list[str]:
    names: list[str] = []
    for call in calls:
        if not isinstance(call, dict):
            continue
        function = call.get("function")
        if isinstance(function, dict):
            name = str(function.get("name") or "").strip()
            if name:
                names.append(name)
    return names


def _has_patch_action(calls: list[Any]) -> bool:
    return any(name in _PATCH_ACTION_TOOLS for name in _tool_call_names(calls))


def _enforce_patch_plan_required_progress(
    runtime: ToolUseRuntime,
    *,
    patch_plan_required: bool,
    calls: list[Any],
    read_only_iterations: int,
    observation_progress: bool,
) -> int:
    if not patch_plan_required or runtime.changed_paths or runtime.deleted_paths:
        return 0
    if _has_patch_action(calls):
        return 0
    if observation_progress:
        return 0
    next_count = int(read_only_iterations) + 1
    if next_count > _max_patch_plan_required_read_iterations(runtime):
        raise OpenAIToolUseExecutionError(
            "EXECUTOR_STALLED_PATCH_PLAN_REQUIRED",
            "executor kept reading/searching without new context after patch-plan-required mode; recovery must write a scoped patch or finish with blocker",
        )
    return next_count


def _action_only_checkpoint_instruction(runtime: ToolUseRuntime, *, error_code: str, message: str, iteration: int) -> str:
    active_tools = json.dumps(runtime.active_tools(), ensure_ascii=False)
    missing = json.dumps(_tool_use_prompt.missing_writable_files(runtime), ensure_ascii=False)
    return (
        f"Executor decision checkpoint at tool iteration {int(iteration)} after {error_code}: {message}. "
        "Stop exploratory reads/searches/listing now; the next response must use only the action-only tools "
        f"currently available: {active_tools}. "
        f"Missing writable files that may need write_new_file: {missing}. "
        "Apply a patch-plan item, call str_replace/write_new_file/delete_file if available for an in-scope edit, "
        "call declare_task_infeasible for a true missing precondition, or call finish_execution if the task is complete. "
        "Do not request read_file, read_file_partial, search_file, glob, or directory-listing tools."
    )


def _maybe_enter_action_only_checkpoint(
    runtime: ToolUseRuntime,
    messages: list[dict[str, Any]],
    exc: OpenAIToolUseExecutionError,
    *,
    iteration: int,
) -> bool:
    if runtime.action_only_checkpoint_attempts >= 1 or runtime.action_only_mode:
        return False
    if exc.code not in _STALL_ERROR_CODES and not str(exc.code or "").startswith("EXECUTOR_STALLED"):
        return False
    runtime.action_only_checkpoint_attempts += 1
    runtime.action_only_mode = True
    runtime.action_only_reason = exc.message
    runtime.action_only_error_code = exc.code
    messages.append(
        {
            "role": "user",
            "content": _action_only_checkpoint_instruction(
                runtime,
                error_code=exc.code,
                message=exc.message,
                iteration=iteration,
            ),
        }
    )
    return True


def _append_write_progress_nudge(
    messages: list[dict[str, Any]],
    runtime: ToolUseRuntime,
    *,
    iteration: int,
) -> None:
    messages.append(
        {
            "role": "user",
            "content": _write_progress_instruction(runtime, iteration=iteration, threshold=_no_write_threshold(runtime)),
        }
    )


def _auto_finish_after_no_write_stall(runtime: ToolUseRuntime, exc: OpenAIToolUseExecutionError) -> dict[str, Any]:
    if not runtime.changed_paths:
        raise exc
    runtime.finished_summary = (
        runtime.finished_summary
        or "Runtime auto-finished after write progress stalled before finish_execution."
    )
    finish_result = runtime._finish()
    if finish_result.get("status") == "FINISHED":
        finish_result = dict(finish_result)
        finish_result["auto_finished"] = True
        return finish_result
    raise OpenAIToolUseExecutionError(
        str(finish_result.get("status") or exc.code),
        _finish_result_message(finish_result, fallback=exc.message),
    ) from exc


def _run_tool_loop(runtime: ToolUseRuntime) -> dict[str, Any]:
    endpoint, api_key = _tool_loop_endpoint_and_key(runtime)
    messages = [
        {"role": "system", "content": _system_prompt(runtime.execution_protocol(), runtime=runtime)},
        {"role": "user", "content": _user_prompt(runtime)},
    ]
    detector = runtime.stall_detector
    token_spend_reported = 0
    token_spend_estimated = 0
    last_progress_iteration = 0
    waf_retry_count = 0
    waf_compact_mode = False
    last_write_progress_nudge_iteration = 0
    last_patch_plan_required_iteration = 0
    patch_plan_required = False
    patch_plan_required_read_iterations = 0
    started = time.monotonic()
    for iteration in range(1, _cap(runtime.config, "max_tool_iterations", 30) + 1):
        _enforce_tool_loop_iteration_budget(
            runtime,
            started=started,
            iteration=iteration,
            last_progress_iteration=last_progress_iteration,
        )
        payload = {
            "model": str(getattr(runtime.config, "model", "") or ""),
            "messages": _messages_for_payload(messages, runtime.config, compact_all=waf_compact_mode, runtime=runtime),
            "tools": _tool_schemas_for_runtime(runtime),
            "tool_choice": "auto",
            "temperature": 0,
            "stream": False,
        }
        post_chat = _post_tool_loop_chat(
            runtime,
            endpoint=endpoint,
            api_key=api_key,
            payload=payload,
            messages=messages,
            waf_retry_count=waf_retry_count,
        )
        waf_retry_count = post_chat.waf_retry_count
        if post_chat.should_continue:
            waf_compact_mode = post_chat.waf_compact_mode
            continue
        token_spend_reported, token_spend_estimated, crossed_soft_budget = _record_tool_loop_token_spend(
            detector,
            body=dict(post_chat.body or {}),
            payload=payload,
            token_spend_reported=token_spend_reported,
            token_spend_estimated=token_spend_estimated,
            iteration=iteration,
        )
        _append_budget_pressure_instruction(messages, runtime, crossed_soft_budget=crossed_soft_budget)
        message, calls = _message_tool_calls(dict(post_chat.body or {}), runtime)
        assistant_entry: dict[str, Any] = {"role": "assistant", "content": message.get("content") or "", "tool_calls": calls}
        reasoning_content = message.get("reasoning_content")
        if reasoning_content:
            # Required by DeepSeek-style reasoning models when echoing assistant turn back in history.
            assistant_entry["reasoning_content"] = reasoning_content
        messages.append(assistant_entry)
        iteration_observation_progress = False
        for call in calls:
            outcome = _process_single_tool_call(runtime, call, iteration, detector)
            if outcome.finish_result is not None:
                if outcome.tool_result_message is not None:
                    messages.append(outcome.tool_result_message)
                return outcome.finish_result
            if outcome.write_progress:
                detector.record_write_progress(iteration)
            if outcome.write_progress or outcome.observation_progress:
                last_progress_iteration = iteration
            if outcome.observation_progress:
                iteration_observation_progress = True
                detector.record_observation_progress(iteration)
            if outcome.tool_result_message is not None:
                messages.append(outcome.tool_result_message)
        try:
            patch_plan_required_read_iterations = _enforce_patch_plan_required_progress(
                runtime,
                patch_plan_required=patch_plan_required,
                calls=calls,
                read_only_iterations=patch_plan_required_read_iterations,
                observation_progress=iteration_observation_progress,
            )
        except OpenAIToolUseExecutionError as exc:
            if not runtime.changed_paths and _maybe_enter_action_only_checkpoint(runtime, messages, exc, iteration=iteration):
                patch_plan_required_read_iterations = 0
                continue
            return _auto_finish_after_no_write_stall(runtime, exc)
        if _should_require_patch_plan(
            runtime,
            iteration=iteration,
            last_sent_iteration=last_patch_plan_required_iteration,
        ):
            _append_patch_plan_required_instruction(messages, runtime, iteration=iteration)
            last_patch_plan_required_iteration = iteration
            patch_plan_required = True
            patch_plan_required_read_iterations = 0
        if _should_send_write_progress_nudge(
            runtime,
            iteration=iteration,
            last_sent_iteration=last_write_progress_nudge_iteration,
        ):
            _append_write_progress_nudge(messages, runtime, iteration=iteration)
            last_write_progress_nudge_iteration = iteration
        try:
            detector.enforce_read_discipline()
            detector.enforce_no_write_progress(iteration)
        except OpenAIToolUseExecutionError as exc:
            if not runtime.changed_paths and _maybe_enter_action_only_checkpoint(runtime, messages, exc, iteration=iteration):
                continue
            return _auto_finish_after_no_write_stall(runtime, exc)
    raise OpenAIToolUseExecutionError("MAX_TOOL_ITERATIONS", "openai_tool_use executor exceeded max_tool_iterations")


def _tool_result_message(tool_call_id: str, tool_name: str, result: dict[str, Any]) -> dict[str, Any]:
    # P1-#6: record the target path on read-style tool results so the
    # compaction pass can keep the latest source-of-truth for any file the
    # executor is currently editing. Without this, the LLM loses the file
    # body it was about to rewrite as soon as the compaction window slides.
    msg = {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "content": json.dumps(result, ensure_ascii=False),
        _INTERNAL_TOOL_NAME_KEY: tool_name,
    }
    target_path = ""
    if isinstance(result, dict):
        candidate = result.get("path")
        if isinstance(candidate, str) and candidate:
            target_path = candidate
    if target_path:
        msg[_INTERNAL_TARGET_PATH_KEY] = target_path
    return msg


def _messages_for_payload(
    messages: list[dict[str, Any]],
    config: Any,
    *,
    compact_all: bool = False,
    runtime: Any = None,
) -> list[dict[str, Any]]:
    return _tool_use_prompt.messages_for_payload(
        messages, config, compact_all=compact_all, cap_fn=_cap, runtime=runtime
    )


def _waf_retry_instruction(runtime: ToolUseRuntime) -> str:
    return _tool_use_prompt.waf_retry_instruction(runtime)


def _budget_pressure_instruction(runtime: ToolUseRuntime) -> str:
    return _tool_use_prompt.budget_pressure_instruction(runtime)


def _write_progress_instruction(runtime: ToolUseRuntime, *, iteration: int, threshold: int) -> str:
    return _tool_use_prompt.write_progress_instruction(runtime, iteration=iteration, threshold=threshold)


def _compact_tool_result_content(content: str, tool_name: str) -> str:
    return _tool_use_prompt.compact_tool_result_content(content, tool_name)


def _tool_observation_made_progress(runtime: ToolUseRuntime, tool_name: str, result: dict[str, Any]) -> bool:
    return _tool_use_result.tool_observation_made_progress(runtime, tool_name, result)


def _success_payload(runtime: ToolUseRuntime, result: dict[str, Any]) -> dict[str, Any]:
    return _tool_use_result.success_payload(runtime, result)


def _auto_finish_after_interruption(runtime: ToolUseRuntime, exc: OpenAIToolUseExecutionError) -> dict[str, Any] | None:
    return _tool_use_result.auto_finish_after_interruption(runtime, exc)


def _finish_result_message(finish_result: dict[str, Any], *, fallback: str) -> str:
    return _tool_use_result.finish_result_message(finish_result, fallback=fallback)


def _blocked_payload(config: Any, request_payload: dict[str, Any], *, error_code: str, message: str, runtime: ToolUseRuntime | None) -> dict[str, Any]:
    return _tool_use_result.blocked_payload(
        config,
        request_payload,
        error_code=error_code,
        message=message,
        runtime=runtime,
    )


def _post_chat(
    *,
    endpoint: str,
    api_key: str,
    payload: dict[str, Any],
    timeout_seconds: int,
    max_retries: int = 0,
    api_format: str = "openai_chat",
) -> dict[str, Any]:
    return _tool_use_transport.post_chat(
        endpoint=endpoint,
        api_key=api_key,
        payload=payload,
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
        api_format=api_format,
    )


def _http_timeout_seconds(config: Any) -> int:
    return _tool_use_transport.http_timeout_seconds(config, cap_fn=_cap)


def _is_context_overflow_http_error(status_code: int, body: str) -> bool:
    return _tool_use_transport.is_context_overflow_http_error(status_code, body)


def _is_waf_http_error(status_code: int, body: str) -> bool:
    return _tool_use_transport.is_waf_http_error(status_code, body)


def _tool_call_parts(call: Any) -> tuple[str, dict[str, Any], str]:
    if not isinstance(call, dict):
        raise OpenAIToolUseExecutionError("INVALID_TOOL_CALL", "tool call is not an object")
    call_id = str(call.get("id") or "")
    function = call.get("function")
    if not isinstance(function, dict):
        raise OpenAIToolUseExecutionError("INVALID_TOOL_CALL", "tool call missing function")
    name = str(function.get("name") or "").strip()
    raw_args = function.get("arguments")
    if not isinstance(raw_args, str):
        raise OpenAIToolUseExecutionError("INVALID_TOOL_CALL", "tool call arguments must be JSON string")
    try:
        args = json.loads(raw_args or "{}")
    except json.JSONDecodeError as exc:
        raise OpenAIToolUseExecutionError("INVALID_TOOL_CALL", "tool call arguments are not valid JSON") from exc
    if not isinstance(args, dict):
        raise OpenAIToolUseExecutionError("INVALID_TOOL_CALL", "tool call arguments must be an object")
    return name, args, call_id


def _tool_schemas(execution_protocol: str) -> list[dict[str, Any]]:
    return _tool_use_prompt.tool_schemas(execution_protocol)


def _tool_schemas_for_runtime(runtime: ToolUseRuntime) -> list[dict[str, Any]]:
    allowed = set(runtime.active_tools())
    return [
        schema
        for schema in _tool_schemas(runtime.execution_protocol())
        if isinstance(schema, dict)
        and isinstance(schema.get("function"), dict)
        and str(schema["function"].get("name") or "") in allowed
    ]


def _tool(name: str, description: str, properties: dict[str, Any], *, required: list[str] | None = None) -> dict[str, Any]:
    return _tool_use_prompt.tool(name, description, properties, required=required)


def _system_prompt(execution_protocol: str, runtime: ToolUseRuntime | None = None) -> str:
    profile_text = ""
    if runtime is not None:
        profile_chunks = [
            render_prompt_profile_text(
                project_root=runtime.project_root,
                role="executor",
                model=str(getattr(runtime.config, "model", "") or ""),
                transport_name=str(getattr(runtime.config, "transport_name", "") or ""),
                driver=str(getattr(runtime.config, "backend", "") or ""),
            ),
            render_learned_prompt_lesson_text(
                project_root=runtime.project_root,
                role="executor",
                model=str(getattr(runtime.config, "model", "") or ""),
                transport_name=str(getattr(runtime.config, "transport_name", "") or ""),
                driver=str(getattr(runtime.config, "backend", "") or ""),
            ),
        ]
        profile_text = "\n\n".join(chunk for chunk in profile_chunks if chunk)
    return _tool_use_prompt.system_prompt(execution_protocol, profile_text=profile_text)


def _user_prompt(runtime: ToolUseRuntime) -> str:
    return _tool_use_prompt.user_prompt(runtime)


def _request_for_prompt(request: dict[str, Any]) -> dict[str, Any]:
    return _tool_use_prompt.request_for_prompt(request)


def _compact_task_card_for_prompt(task_card: dict[str, Any]) -> None:
    _tool_use_prompt.compact_task_card_for_prompt(task_card)


def _compact_prompt_text_list(items: Any, *, max_items: int, limit: int) -> list[str]:
    return _tool_use_prompt.compact_prompt_text_list(items, max_items=max_items, limit=limit)


def _compact_prompt_text(text: str, *, limit: int) -> str:
    return _tool_use_prompt.compact_prompt_text(text, limit=limit)


def _summarize_patch_plan_for_prompt(items: list[Any]) -> list[dict[str, Any]]:
    return _tool_use_prompt.summarize_patch_plan_for_prompt(items)


def _first_message(body: dict[str, Any]) -> dict[str, Any]:
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise OpenAIToolUseExecutionError("INVALID_RESPONSE", "endpoint response missing choices")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise OpenAIToolUseExecutionError("INVALID_RESPONSE", "endpoint response missing message")
    return message


def _usage_tokens(body: dict[str, Any], payload: dict[str, Any]) -> int:
    return max(_reported_usage_tokens(body), _estimated_payload_tokens(payload))


def _reported_usage_tokens(body: dict[str, Any]) -> int:
    usage = body.get("usage")
    if isinstance(usage, dict):
        try:
            return int(usage.get("total_tokens") or 0)
        except (TypeError, ValueError):
            pass
    return 0


def _reported_cache_tokens(body: dict[str, Any]) -> tuple[int, int]:
    """Extract OpenAI-compatible prompt cache tokens (DeepSeek, Qwen, etc.).

    Returns (hit, miss). Both 0 when the provider does not report cache stats
    (vanilla OpenAI, mimo, fakes) — observation-only, never raises.
    """
    usage = body.get("usage")
    if not isinstance(usage, dict):
        return 0, 0
    try:
        hit = int(usage.get("prompt_cache_hit_tokens") or 0)
    except (TypeError, ValueError):
        hit = 0
    try:
        miss = int(usage.get("prompt_cache_miss_tokens") or 0)
    except (TypeError, ValueError):
        miss = 0
    return max(0, hit), max(0, miss)


def _estimated_payload_tokens(payload: dict[str, Any]) -> int:
    return max(1, len(json.dumps(payload, ensure_ascii=False)) // 4)


def _openai_tool_use_unsuitable_reasons(request_payload: dict[str, Any], *, execution_protocol: str = FULL_FILE_PROTOCOL) -> list[str]:
    reasons: list[str] = []
    caps = {str(item).strip().lower() for item in list(request_payload.get("capabilities") or []) if str(item).strip()}
    task_card = request_payload.get("task_card")
    if isinstance(task_card, dict):
        caps.update(str(item).strip().lower() for item in list(task_card.get("capabilities") or []) if str(item).strip())
    unsupported_capabilities = set(_UNSUPPORTED_REQUEST_CAPABILITIES)
    if execution_protocol == EXACT_STR_REPLACE_PROTOCOL:
        unsupported_capabilities -= {"patch.emit", "patch.apply", "replace_in_file", "apply_patch"}
    unsupported_caps = sorted(caps & unsupported_capabilities)
    if unsupported_caps:
        reasons.append(f"unsupported capabilities requested: {unsupported_caps}")
    text_parts = [
        request_payload.get("requested_action"),
        request_payload.get("task_requirements"),
        request_payload.get("task_scope"),
        request_payload.get("surface"),
    ]
    if isinstance(task_card, dict):
        text_parts.extend([task_card.get("title"), task_card.get("summary"), task_card.get("description")])
    lowered = "\n".join(str(item or "").lower() for item in text_parts)
    unsupported_markers = set(_UNSUPPORTED_REQUEST_MARKERS)
    if execution_protocol == EXACT_STR_REPLACE_PROTOCOL:
        unsupported_markers -= {"replace_in_file", "apply_patch", "search/replace", "search-replace", "patch protocol"}
    marker_hits = sorted({marker for marker in unsupported_markers if marker in lowered})
    if marker_hits:
        reasons.append(f"task text asks for unsupported executor operations: {marker_hits}")
    return reasons


def _summarize_tool_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return _tool_use_result.summarize_tool_payload(payload)


def _normalize_allowed_files(project_root: Path, files: list[Any]) -> list[str]:
    out: list[str] = []
    for raw in files:
        text = _normalize_rel(str(raw or ""))
        if not text or text in out:
            continue
        guard = guard_repo_write_path(project_root=project_root, path=text)
        if not guard.allowed:
            raise OpenAIToolUseExecutionError("PATH_GUARD_BLOCKED", f"{text}: {guard.reason}")
        out.append(text)
    return out


def _normalize_read_only_files(
    project_root: Path,
    request_payload: dict[str, Any],
    *,
    writable_files: list[str],
) -> list[str]:
    task_card = request_payload.get("task_card")
    if not isinstance(task_card, dict):
        task_card = {}
    candidates: list[Any] = []
    for key in (
        "read_only_files",
        "context_files",
        "related_existing_tests",
        "do_not_change",
        "forbidden_changes",
    ):
        value = task_card.get(key)
        if isinstance(value, list):
            candidates.extend(value)
    for item in list(task_card.get("requires") or []):
        if isinstance(item, dict):
            for key in ("path", "file", "source"):
                candidates.append(item.get(key))
    writable = set(writable_files)
    out: list[str] = []
    for raw in candidates:
        text = _normalize_rel(str(raw or ""))
        if not text or text in writable or text in out or not _looks_like_repo_context_path(text):
            continue
        target = project_root / text
        if not target.exists():
            continue
        guard = guard_repo_read_path(project_root=project_root, path=text, require_file=False)
        if not guard.allowed:
            continue
        out.append(text)
    return out


def _base_url(config: Any) -> str:
    return _tool_use_transport.base_url(config)


def _chat_completions_endpoint(base_url: str, *, api_format: str) -> str:
    return _tool_use_transport.chat_completions_endpoint(base_url, api_format=api_format)


def _safe_http_body(exc: urlerror.HTTPError) -> str:
    return _tool_use_transport.safe_http_body(exc)


def _is_stall_error_code(code: str) -> bool:
    return str(code or "").strip().upper() in _STALL_ERROR_CODES


def _stall_iteration(runtime: ToolUseRuntime) -> int:
    values = [
        int(item.get("iteration") or 0)
        for item in _recent_tool_calls(runtime.tool_log_path(), limit=1, run_id=runtime.run_id)
        if isinstance(item, dict)
    ]
    return max(values, default=0)


def _recent_tool_calls(path: Path, *, limit: int, run_id: str = "") -> list[dict[str, Any]]:
    return _tool_use_stall.recent_tool_calls(path, limit=limit, run_id=run_id)


__all__ = ["EXACT_STR_REPLACE_PROTOCOL", "FULL_FILE_PROTOCOL", "OPENAI_TOOL_USE_BACKEND", "PATCH_ATTEMPTS_FILENAME", "PATCH_TOOL_MANIFEST_V1", "READ_SCOPE_WIDEN_FILENAME", "STALL_REPORT_FILENAME", "StallDetector", "TOOL_MANIFEST_V2", "TOOL_MANIFEST_VERSION", "materialize_openai_tool_use_result"]
