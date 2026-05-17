"""Recovery decision helpers for the local adapter facade."""

from __future__ import annotations

from pathlib import Path
import shutil
from typing import Any, Callable

from kodawari.autopilot.recovery.executor_recovery import RecoverySynthesizerConfig


RequestRecoveryDecision = Callable[..., tuple[dict[str, Any] | None, str]]
NormalizeRecoveryDecision = Callable[..., dict[str, Any]]


def clean_text(value: Any) -> str:
    return str(value or "").strip()


def _clean_first_truthy(*values: Any) -> str:
    for value in values:
        if value:
            return clean_text(value)
    return ""


def build_recovery_synthesizer_config(
    adapter_config: Any,
    *,
    resolved_reviewer_backend: str,
) -> RecoverySynthesizerConfig:
    backend = _clean_first_truthy(adapter_config.recovery_backend, adapter_config.reviewer_backend)
    if backend in {"", "auto"}:
        backend = resolved_reviewer_backend
    executable = ""
    if backend in {"cli", "mcp", "claude"}:
        executable = _clean_first_truthy(
            adapter_config.recovery_executable_claude,
            adapter_config.reviewer_executable_claude,
            "claude",
        )
    elif backend == "codex":
        executable = _clean_first_truthy(
            adapter_config.recovery_executable_codex,
            adapter_config.reviewer_executable_codex,
            "codex",
        )
    return RecoverySynthesizerConfig(
        backend=backend,
        executable=executable,
        model=_clean_first_truthy(adapter_config.recovery_model, adapter_config.reviewer_model),
        base_url=_clean_first_truthy(
            adapter_config.recovery_base_url,
            adapter_config.reviewer_base_url,
            adapter_config.opus_gateway_base_url,
        ),
        api_key=_clean_first_truthy(
            adapter_config.recovery_api_key,
            adapter_config.reviewer_api_key,
            adapter_config.opus_gateway_api_key,
        ),
        api_format=_clean_first_truthy(
            adapter_config.recovery_api_format,
            adapter_config.reviewer_api_format,
            adapter_config.opus_gateway_api_format,
        ),
        timeout_seconds=max(30, int(adapter_config.recovery_timeout_seconds or 60)),
        max_tokens=max(1024, int(adapter_config.opus_gateway_max_tokens or 4096)),
        reasoning_effort=_clean_first_truthy(adapter_config.recovery_reasoning_effort, "low"),
    )


def recovery_synthesizer_unavailable_reason(config: RecoverySynthesizerConfig) -> str:
    backend = clean_text(config.backend).lower()
    if not backend or backend == "auto":
        return "recovery backend not configured"
    if backend == "api":
        if not clean_text(config.base_url):
            return "api recovery requires base_url"
        if not clean_text(config.api_key):
            return "api recovery requires api_key"
        return ""
    if backend in {"cli", "mcp", "claude", "codex"}:
        executable = clean_text(config.executable)
        if not executable:
            return f"{backend} recovery executable not configured"
        if Path(executable).exists() or shutil.which(executable):
            return ""
        return f"{backend} recovery executable not found: {executable}"
    return f"unsupported recovery backend {backend!r}"


def requested_existing_recovery_files(decision: dict[str, Any], *, allowed_files: list[str]) -> list[str]:
    if str(decision.get("action") or "") != "expand_scope_request":
        return []
    allowed = {str(item).replace("\\", "/").lstrip("/") for item in allowed_files}
    requested: list[str] = []
    for raw in list(decision.get("requested_files") or []):
        path = str(raw or "").replace("\\", "/").lstrip("/")
        if path and path in allowed and path not in requested:
            requested.append(path)
    return requested


def synthesize_executor_recovery_result(
    *,
    task: str,
    context: dict[str, Any],
    must_fix: list[str],
    stall_report: dict[str, Any] | None,
    config: RecoverySynthesizerConfig,
    cwd: Path | None,
    request_decision_fn: RequestRecoveryDecision,
    normalize_decision_fn: NormalizeRecoveryDecision,
) -> dict[str, Any]:
    task_card = dict(context.get("task_card") or {})
    allowed_files = _recovery_allowed_files(context, task_card)
    unavailable = recovery_synthesizer_unavailable_reason(config)
    if unavailable:
        return unavailable_recovery_result(config=config, error=unavailable)
    project_root = _recovery_project_root(context, cwd)
    raw, error = request_decision_fn(
        config,
        task=task,
        task_card=task_card,
        must_fix=must_fix,
        stall_report=stall_report,
        allowed_files=allowed_files,
        recovery_context=_previous_recovery_context(context),
        project_root=project_root,
    )
    if raw is None:
        return blocked_recovery_result(config=config, error=error)
    decision = normalize_decision_fn(raw, allowed_files=allowed_files)
    requested_existing = requested_existing_recovery_files(decision, allowed_files=allowed_files)
    if requested_existing:
        decision = _maybe_retry_for_full_source(
            request_decision_fn,
            normalize_decision_fn,
            config=config,
            task=task,
            task_card=task_card,
            must_fix=must_fix,
            stall_report=stall_report,
            allowed_files=allowed_files,
            context=context,
            project_root=project_root,
            requested_existing=requested_existing,
            initial_decision=decision,
        )
    return {
        "status": "ok",
        "role": "recovery_synthesizer",
        "source": "kodawari.recovery_synthesizer",
        "backend": config.backend,
        "model": clean_text(config.model),
        "decision": decision,
    }


def _recovery_allowed_files(context: dict[str, Any], task_card: dict[str, Any]) -> list[str]:
    raw_files = context.get("task_card_files") or task_card.get("files_to_change") or []
    return [str(item) for item in list(raw_files) if str(item).strip()]


def _recovery_project_root(context: dict[str, Any], cwd: Path | None) -> Path:
    raw_root = context.get("recovery_source_root") or context.get("project_root") or cwd or Path.cwd()
    return Path(str(raw_root)).resolve()


def _previous_recovery_context(
    context: dict[str, Any],
    *,
    retry_reason: str = "",
) -> dict[str, Any]:
    recovery_context = {
        "previous_recovery_decisions": list(context.get("previous_recovery_decisions") or []),
        "previous_execution_result": dict(context.get("previous_execution_result") or {}),
    }
    if retry_reason:
        recovery_context["retry_reason"] = retry_reason
    return recovery_context


def _maybe_retry_for_full_source(
    request_decision_fn: RequestRecoveryDecision,
    normalize_decision_fn: NormalizeRecoveryDecision,
    *,
    config: RecoverySynthesizerConfig,
    task: str,
    task_card: dict[str, Any],
    must_fix: list[str],
    stall_report: dict[str, Any] | None,
    allowed_files: list[str],
    context: dict[str, Any],
    project_root: Path,
    requested_existing: list[str],
    initial_decision: dict[str, Any],
) -> dict[str, Any]:
    raw_retry, retry_error = request_decision_fn(
        config,
        task=task,
        task_card=task_card,
        must_fix=[
            *must_fix,
            "Previous recovery requested full source for files already in scope; full source for those files is now included. Return narrow_patch_plan or abort_with_diagnosis.",
        ],
        stall_report=stall_report,
        allowed_files=allowed_files,
        recovery_context=_previous_recovery_context(
            context,
            retry_reason="full source requested for in-scope files",
        ),
        project_root=project_root,
        full_source_files=requested_existing,
    )
    if raw_retry is not None:
        return normalize_decision_fn(raw_retry, allowed_files=allowed_files)
    if retry_error:
        return {
            "schema_version": "execution.recovery_decision.v1",
            "action": "escalate_to_human",
            "diagnosis": retry_error,
        }
    return initial_decision


def unavailable_recovery_result(*, config: RecoverySynthesizerConfig, error: str) -> dict[str, Any]:
    return {
        "status": "unavailable",
        "role": "recovery_synthesizer",
        "source": "kodawari.recovery_synthesizer",
        "backend": config.backend,
        "model": clean_text(config.model),
        "error": error,
        "decision": {
            "schema_version": "execution.recovery_decision.v1",
            "action": "escalate_to_human",
            "diagnosis": error,
        },
    }


def blocked_recovery_result(*, config: RecoverySynthesizerConfig, error: str) -> dict[str, Any]:
    return {
        "status": "blocked",
        "role": "recovery_synthesizer",
        "source": "kodawari.recovery_synthesizer",
        "backend": config.backend,
        "model": clean_text(config.model),
        "error": error,
        "decision": {
            "schema_version": "execution.recovery_decision.v1",
            "action": "escalate_to_human",
            "diagnosis": error or "recovery synthesizer unavailable",
        },
    }
