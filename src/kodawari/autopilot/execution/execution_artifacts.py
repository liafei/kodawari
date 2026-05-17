"""Execution backend artifacts and bridge helpers."""

from __future__ import annotations

from contextlib import contextmanager
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

from jsonschema import Draft7Validator

from kodawari.autopilot.execution.execution_backend import (
    ALLOWED_EXECUTION_BACKENDS,
    CLAUDE_CODE_BACKEND,
    CODEX_CLI_BACKEND,
    EXTERNAL_CLI_BACKEND,
    MANUAL_BACKEND,
    NOOP_TEST_ONLY_BACKEND,
    ExecutionBackendConfig,
    execution_backend_capability_truth,
    execution_backend_capabilities,
    execution_backend_descriptor,
    ExecutionBackendInvocation,
    resolve_execution_backend as resolve_execution_backend_name,
    run_registered_execution_backend,
)
from kodawari.autopilot.execution.execution_guard import GuardDecision, evaluate_execution_command
from kodawari.autopilot.core.secret_redactor import redact_jsonable
from kodawari.infra.io_atomic import atomic_write_json, load_json_dict, path_lock


EXECUTION_REQUEST_SCHEMA_VERSION = "execution.request.v1"
EXECUTION_RESULT_SCHEMA_VERSION = "execution.result.v1"
EXECUTION_REQUEST_FILENAME = ".execution_request.json"
EXECUTION_RESULT_FILENAME = ".execution_result.json"

_SCHEMA_CACHE: dict[str, dict[str, Any]] = {}
logger = logging.getLogger(__name__)


class ExecutionArtifactError(ValueError):
    """Raised when execution request/result payloads are invalid."""


class ExecutionRunLockBusy(RuntimeError):
    """Raised when another executor run owns the project-level execution lock."""


def _schema_path(name: str) -> Path:
    return Path(__file__).resolve().parents[2] / "schemas" / "runtime" / f"{name}.schema.json"


def _schema(name: str) -> dict[str, Any]:
    cached = _SCHEMA_CACHE.get(name)
    if cached is not None:
        return cached
    path = _schema_path(name)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"invalid schema payload: {path}")
    _SCHEMA_CACHE[name] = payload
    return payload


def _validate(name: str, payload: dict[str, Any]) -> dict[str, Any]:
    validator = Draft7Validator(_schema(name))
    errors = sorted(validator.iter_errors(payload), key=lambda item: list(item.path))
    if errors:
        details = []
        for error in errors:
            field = ".".join(str(item) for item in error.path) or "<root>"
            details.append(f"{field}: {error.message}")
        raise ExecutionArtifactError("; ".join(details))
    return payload


def is_test_environment() -> bool:
    # No-fake-run policy Fix 0a: previously also checked
    # ``"pytest" in sys.modules`` which trips production-like sessions
    # whenever pytest is importable (VS Code Python test explorer, tox,
    # nox, coverage all leave pytest in sys.modules in long-lived
    # shells). The noop_test_only backend should only activate when the
    # operator explicitly opts in via PYTEST_CURRENT_TEST (pytest itself)
    # or WORKFLOW_SDK_TEST_MODE=1.
    return bool(
        os.getenv("PYTEST_CURRENT_TEST")
        or os.getenv("WORKFLOW_SDK_TEST_MODE") == "1"
    )


def resolve_execution_backend(value: str) -> str:
    try:
        return resolve_execution_backend_name(value, test_environment=is_test_environment())
    except ValueError as exc:
        raise ExecutionArtifactError(str(exc)) from exc


def build_execution_request(
    *,
    feature: str,
    task: str,
    context: dict[str, Any],
    backend: str,
    command: str,
    allowed_files: list[str],
    guard_decision: dict[str, Any] | None = None,
    execution_protocol: str = "",
) -> dict[str, Any]:
    normalized_backend = str(backend or "").strip()
    payload = {
        "schema_version": EXECUTION_REQUEST_SCHEMA_VERSION,
        "feature": str(feature or "").strip(),
        "task": str(task or "").strip(),
        "backend": normalized_backend,
        "backend_capabilities": execution_backend_capabilities(normalized_backend),
        "backend_capability_truth": execution_backend_capability_truth(normalized_backend),
        "executor_command": str(command or "").strip(),
        "guard_decision": dict(guard_decision or {}),
        "project_root": str(context.get("project_root") or "").strip(),
        "planning_dir": str(context.get("planning_dir") or "").strip(),
        "task_id": str(context.get("task_id") or "").strip(),
        "requested_action": str(context.get("requested_action") or "").strip(),
        "review_round": int(context.get("review_round", 0) or 0),
        "attempt": int(context.get("attempt", 1) or 1),
        "files_to_change": [str(item) for item in list(allowed_files or []) if str(item).strip()],
        "invariants": [str(item) for item in list(context.get("task_invariants") or []) if str(item).strip()],
        "task_card": dict(context.get("task_card") or {}),
        "task_scope": str(context.get("task_scope") or "").strip(),
        "task_requirements": str(context.get("requirements") or "").strip()[:8192],
        "verify_cmd": str(context.get("verify_cmd") or "").strip(),
        "archetype": str(context.get("archetype") or "").strip(),
        "capabilities": [str(item) for item in list(context.get("capabilities") or []) if str(item).strip()],
        "surface": str(context.get("surface") or "").strip(),
        "must_fix": [str(item) for item in list(context.get("must_fix") or []) if str(item).strip()],
        "scope_risk_warnings": [
            str(item)
            for item in list(context.get("scope_risk_warnings") or [])
            if str(item).strip()
        ][:8],
        "execution_timeout_hint": str(context.get("execution_timeout_hint") or "").strip().lower() or None,
    }
    protocol = str(execution_protocol or context.get("execution_protocol") or "").strip()
    if protocol:
        payload["execution_protocol"] = protocol
    normalized_note = _normalize_implementer_note(context.get("implementer_note"))
    if normalized_note:
        payload["implementer_note"] = normalized_note
    return _validate("execution_request", payload)


def build_execution_result(
    *,
    feature: str,
    task: str,
    backend: str,
    status: str,
    changed_files: list[str] | None = None,
    stdout_excerpt: str = "",
    stderr_excerpt: str = "",
    returncode: int | None = None,
    artifacts: list[str] | None = None,
    error_code: str = "",
    blocking_reason: str = "",
    summary: str = "",
    backend_capabilities: dict[str, Any] | None = None,
    guard_decision: dict[str, Any] | None = None,
    implementer_note: dict[str, Any] | None = None,
    execution_protocol: str = "",
) -> dict[str, Any]:
    normalized_backend = str(backend or "").strip()
    payload = {
        "schema_version": EXECUTION_RESULT_SCHEMA_VERSION,
        "feature": str(feature or "").strip(),
        "task": str(task or "").strip(),
        "backend": normalized_backend,
        "backend_capabilities": dict(backend_capabilities or execution_backend_capabilities(normalized_backend)),
        "backend_capability_truth": execution_backend_capability_truth(normalized_backend),
        "guard_decision": dict(guard_decision or {}),
        "status": str(status or "").strip().upper() or "UNKNOWN",
        "changed_files": [str(item) for item in list(changed_files or []) if str(item).strip()],
        "stdout_excerpt": str(stdout_excerpt or "")[:4000],
        "stderr_excerpt": str(stderr_excerpt or "")[:4000],
        "returncode": returncode,
        "artifacts": [str(item) for item in list(artifacts or []) if str(item).strip()],
        "error_code": str(error_code or "").strip(),
        "blocking_reason": str(blocking_reason or "").strip(),
        "summary": str(summary or "").strip(),
    }
    protocol = str(execution_protocol or "").strip()
    if protocol:
        payload["execution_protocol"] = protocol
    normalized_note = _normalize_implementer_note(implementer_note)
    if normalized_note:
        payload["implementer_note"] = normalized_note
    return _validate("execution_result", payload)


def _normalize_implementer_note(raw: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    claimed_intent = str(raw.get("claimed_intent") or "").strip()
    claimed_invariants_preserved = [
        str(item).strip()
        for item in list(raw.get("claimed_invariants_preserved") or [])
        if str(item).strip()
    ]
    claimed_risks = [
        str(item).strip()
        for item in list(raw.get("claimed_risks") or [])
        if str(item).strip()
    ]
    if not any([claimed_intent, claimed_invariants_preserved, claimed_risks]):
        return {}
    return {
        "claimed_intent": claimed_intent,
        "claimed_invariants_preserved": claimed_invariants_preserved,
        "claimed_risks": claimed_risks,
    }


def write_execution_request(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_json(path, _validate("execution_request", redact_jsonable(dict(payload))))


def write_execution_result(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_json(path, _validate("execution_result", redact_jsonable(dict(payload))))


def load_execution_result(path: Path) -> dict[str, Any]:
    payload = load_json_dict(path, required=True, quarantine_on_error=True)
    if payload is None:
        raise ExecutionArtifactError(f"execution result missing: {path}")
    return _validate("execution_result", dict(payload))


def _load_existing_execution_result(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return load_execution_result(path)
    except ExecutionArtifactError:
        return {}


def _noop_changed_files(request: dict[str, Any]) -> list[str]:
    candidates = [str(item) for item in list(request.get("files_to_change") or []) if str(item).strip()]
    if candidates:
        return candidates
    task_card = dict(request.get("task_card") or {})
    return [str(item) for item in list(task_card.get("files_to_change") or []) if str(item).strip()]


def _adapter_result_from_execution(payload: dict[str, Any], *, request_path: Path, result_path: Path) -> dict[str, Any]:
    normalized_status = str(payload.get("status") or "").strip().upper()
    mode = str(payload.get("backend") or "").strip().lower() or "unknown"
    backend_capabilities = dict(payload.get("backend_capabilities") or {})
    execution_guard = {
        "action": str(payload.get("guard_action") or "").strip(),
        "policy": str(payload.get("guard_policy") or "").strip(),
        "pattern": str(payload.get("guard_pattern") or "").strip(),
        "command": str(payload.get("guard_command") or "").strip(),
        "decision": dict(payload.get("guard_decision") or {}),
    }
    backend_truth = dict(payload.get("backend_capability_truth") or {})
    if normalized_status in {"PASS", "DONE", "SUCCESS"}:
        return {
            "status": "done",
            "changes": list(payload.get("changed_files") or []),
            "mode": mode,
            "execution_backend": mode,
            "execution_backend_capabilities": backend_capabilities,
            "execution_backend_capability_truth": backend_truth,
            "execution_guard": execution_guard,
            "execution_result": dict(payload),
            "execution_artifacts": {
                EXECUTION_REQUEST_FILENAME: str(request_path),
                EXECUTION_RESULT_FILENAME: str(result_path),
            },
        }
    if normalized_status in {"BLOCKED", "MISSING", "WAITING"}:
        return {
            "status": "blocked",
            "reason": str(payload.get("error_code") or "EXECUTION_BLOCKED"),
            "blocking_reason": str(payload.get("blocking_reason") or payload.get("summary") or "execution backend blocked"),
            "mode": mode,
            "execution_backend": mode,
            "execution_backend_capabilities": backend_capabilities,
            "execution_backend_capability_truth": backend_truth,
            "execution_guard": execution_guard,
            "execution_result": dict(payload),
            "execution_artifacts": {
                EXECUTION_REQUEST_FILENAME: str(request_path),
                EXECUTION_RESULT_FILENAME: str(result_path),
            },
        }
    return {
        "status": "error",
        "error": str(payload.get("blocking_reason") or payload.get("summary") or "execution backend failed"),
        "mode": mode,
        "execution_backend": mode,
        "execution_backend_capabilities": backend_capabilities,
        "execution_backend_capability_truth": backend_truth,
        "execution_guard": execution_guard,
        "execution_result": dict(payload),
        "execution_artifacts": {
            EXECUTION_REQUEST_FILENAME: str(request_path),
            EXECUTION_RESULT_FILENAME: str(result_path),
        },
    }


def _external_cli_env(*, request_path: Path, result_path: Path, feature: str, task: str, backend: str) -> dict[str, str]:
    return {
        "WORKFLOW_EXECUTION_REQUEST_PATH": str(request_path),
        "WORKFLOW_EXECUTION_RESULT_PATH": str(result_path),
        "WORKFLOW_AUTOMATION_STAGE": "implement",
        "WORKFLOW_FEATURE": str(feature or ""),
        "WORKFLOW_TASK": str(task or ""),
        "WORKFLOW_EXECUTOR_BACKEND": str(backend or ""),
    }


def run_execution_backend(
    *,
    config: ExecutionBackendConfig,
    task: str,
    context: dict[str, Any],
    allowed_files: list[str],
) -> dict[str, Any]:
    backend = resolve_execution_backend(config.backend)
    request_path = config.planning_dir / EXECUTION_REQUEST_FILENAME
    result_path = config.planning_dir / EXECUTION_RESULT_FILENAME
    guard_decision = _evaluate_dispatch_guard(backend=backend, command=config.command)
    request_payload = build_execution_request(
        feature=config.feature,
        task=task,
        context=context,
        backend=backend,
        command=config.command,
        allowed_files=allowed_files,
        guard_decision=_guard_payload(guard_decision),
        execution_protocol=str(config.execution_protocol or ""),
    )
    write_execution_request(request_path, request_payload)
    previous_result = _load_existing_execution_result(result_path)
    invocation = ExecutionBackendInvocation(
        config=config,
        task=task,
        context=dict(context),
        allowed_files=list(allowed_files),
        request_path=request_path,
        result_path=result_path,
        request_payload=dict(request_payload),
    )
    if not backend:
        return _blocked_backend_result(
            reason="EXECUTOR_BACKEND_MISSING",
            blocking_reason="executor backend is not configured; provide --executor-backend and a real executor path",
            mode="unconfigured",
            backend_name="",
            request_path=request_path,
        )
    if guard_decision is not None:
        payload = build_execution_result(
            feature=config.feature,
            task=task,
            backend=backend,
            status="BLOCKED",
            changed_files=[],
            error_code=guard_decision.error_code,
            blocking_reason=guard_decision.message,
            summary=guard_decision.message,
            guard_decision=_guard_payload(guard_decision),
            implementer_note=request_payload.get("implementer_note"),
        )
        payload["guard_action"] = guard_decision.action
        payload["guard_policy"] = guard_decision.policy
        payload["guard_pattern"] = guard_decision.pattern
        payload["guard_command"] = str(config.command or "").strip()
        write_execution_result(result_path, payload)
        return _adapter_result_from_execution(payload, request_path=request_path, result_path=result_path)
    if backend == NOOP_TEST_ONLY_BACKEND:
        return _noop_backend_result(
            config=config,
            task=task,
            request_path=request_path,
            result_path=result_path,
            request_payload=request_payload,
        )
    if backend == MANUAL_BACKEND:
        return _manual_backend_result(request_path=request_path, result_path=result_path, backend=backend)
    try:
        with _project_execution_lock(config=config, backend=backend):
            payload = run_registered_execution_backend(backend, invocation=invocation)
            if payload is not None:
                payload.setdefault("guard_decision", _guard_payload(guard_decision))
                payload = _resume_existing_execution_result(
                    payload=payload,
                    previous_payload=previous_result,
                    request_payload=request_payload,
                )
                payload = _accept_explicit_idempotent_noop_result(
                    payload=payload,
                    request_payload=request_payload,
                )
                write_execution_result(result_path, payload)
                return _adapter_result_from_execution(payload, request_path=request_path, result_path=result_path)
            if backend == EXTERNAL_CLI_BACKEND:
                return _external_cli_backend_result(
                    config=config,
                    task=task,
                    backend=backend,
                    request_path=request_path,
                    result_path=result_path,
                )
    except ExecutionRunLockBusy as exc:
        payload = build_execution_result(
            feature=config.feature,
            task=task,
            backend=backend,
            status="BLOCKED",
            changed_files=[],
            error_code="EXECUTION_RUN_LOCK_BUSY",
            blocking_reason=str(exc),
            summary="another execution run is active for this project",
            guard_decision=_guard_payload(guard_decision),
            implementer_note=request_payload.get("implementer_note"),
        )
        write_execution_result(result_path, payload)
        return _adapter_result_from_execution(payload, request_path=request_path, result_path=result_path)
    descriptor = execution_backend_descriptor(backend)
    if not descriptor.implemented:
        return _blocked_backend_result(
            reason="EXECUTOR_BACKEND_PLACEHOLDER",
            blocking_reason=(
                f"executor backend '{backend}' is a {descriptor.maturity} placeholder and has no runtime runner yet"
            ),
            mode=backend,
            backend_name=backend,
            request_path=request_path,
        )
    return _blocked_backend_result(
        reason="EXECUTOR_BACKEND_RUNNER_MISSING",
        blocking_reason=f"executor backend '{backend}' has no registered runtime runner",
        mode=backend,
        backend_name=backend,
        request_path=request_path,
    )


@contextmanager
def _project_execution_lock(*, config: ExecutionBackendConfig, backend: str):
    if backend in {NOOP_TEST_ONLY_BACKEND, MANUAL_BACKEND}:
        yield
        return
    root = Path(config.project_root or Path.cwd()).resolve()
    lock_path = root / ".workflow" / ".execution_run.lock"
    timeout = max(30.0, min(float(config.timeout_seconds or 600), 300.0))
    lock_ctx = path_lock(lock_path, timeout_seconds=timeout, stale_after_seconds=timeout)
    try:
        lock_ctx.__enter__()
    except ValueError as exc:
        raise ExecutionRunLockBusy(str(exc)) from exc
    try:
        yield
    finally:
        lock_ctx.__exit__(None, None, None)


def _evaluate_dispatch_guard(*, backend: str, command: str) -> GuardDecision | None:
    configured_command = str(command or "").strip()
    if not configured_command:
        return None
    # Guard commands only for backends that execute user-provided command templates.
    if backend not in {EXTERNAL_CLI_BACKEND, CODEX_CLI_BACKEND, CLAUDE_CODE_BACKEND}:
        return None
    return evaluate_execution_command(configured_command)


def _guard_payload(decision: GuardDecision | None) -> dict[str, Any]:
    if decision is None:
        return {}
    return {
        "action": decision.action,
        "reason": decision.message,
        "policy": decision.policy,
        "pattern": decision.pattern,
    }


def _blocked_backend_result(
    *,
    reason: str,
    blocking_reason: str,
    mode: str,
    backend_name: str,
    request_path: Path,
) -> dict[str, Any]:
    return {
        "status": "blocked",
        "reason": reason,
        "blocking_reason": blocking_reason,
        "mode": mode,
        "execution_backend": str(backend_name or "").strip(),
        "execution_backend_capabilities": execution_backend_capabilities(backend_name),
        "execution_artifacts": {EXECUTION_REQUEST_FILENAME: str(request_path)},
    }


def _noop_backend_result(
    *,
    config: ExecutionBackendConfig,
    task: str,
    request_path: Path,
    result_path: Path,
    request_payload: dict[str, Any],
) -> dict[str, Any]:
    if not is_test_environment():
        return _blocked_backend_result(
            reason="NOOP_EXECUTOR_FORBIDDEN",
            blocking_reason="noop_test_only backend is only allowed in tests",
            mode=NOOP_TEST_ONLY_BACKEND,
            backend_name=NOOP_TEST_ONLY_BACKEND,
            request_path=request_path,
        )
    payload = build_execution_result(
        feature=config.feature,
        task=task,
        backend=NOOP_TEST_ONLY_BACKEND,
        status="PASS",
        changed_files=_noop_changed_files(request_payload),
        artifacts=_noop_changed_files(request_payload),
        summary="noop_test_only executor materialized deterministic test execution result.",
        implementer_note=request_payload.get("implementer_note"),
    )
    write_execution_result(result_path, payload)
    return _adapter_result_from_execution(payload, request_path=request_path, result_path=result_path)


def _manual_backend_result(*, request_path: Path, result_path: Path, backend: str) -> dict[str, Any]:
    if result_path.exists():
        payload = load_execution_result(result_path)
        return _adapter_result_from_execution(payload, request_path=request_path, result_path=result_path)
    return _blocked_backend_result(
        reason="EXECUTION_RESULT_MISSING",
        blocking_reason="manual execution backend requires a valid .execution_result.json artifact before rerun",
        mode=backend,
        backend_name=backend,
        request_path=request_path,
    )


def _external_cli_backend_result(
    *,
    config: ExecutionBackendConfig,
    task: str,
    backend: str,
    request_path: Path,
    result_path: Path,
) -> dict[str, Any]:
    command = str(config.command or "").strip()
    if not command:
        return _blocked_backend_result(
            reason="EXECUTOR_COMMAND_MISSING",
            blocking_reason="external_cli backend requires --executor-command",
            mode=backend,
            backend_name=backend,
            request_path=request_path,
        )
    env = _external_cli_process_env(
        request_path=request_path,
        result_path=result_path,
        feature=config.feature,
        task=task,
        backend=backend,
    )
    # Clear any stale result from a prior run before dispatching. Otherwise
    # a subprocess that fails to write its own .execution_result.json would
    # silently inherit the previous task's status/task/changed_files.
    try:
        result_path.unlink()
    except FileNotFoundError:
        pass
    try:
        run = subprocess.run(
            command,
            shell=True,
            cwd=str(config.project_root),
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            timeout=max(1, int(config.timeout_seconds)),
            env=env,
        )
    except subprocess.TimeoutExpired:
        logger.warning("external_cli executor timed out")
        return {
            "status": "error",
            "error": "executor command timed out",
            "mode": backend,
            "execution_backend": backend,
            "execution_backend_capabilities": execution_backend_capabilities(backend),
            "execution_artifacts": {EXECUTION_REQUEST_FILENAME: str(request_path)},
        }
    return _external_cli_completed_result(
        run=run,
        backend=backend,
        request_path=request_path,
        result_path=result_path,
        expected_feature=config.feature,
        expected_task=task,
    )


def _external_cli_process_env(
    *,
    request_path: Path,
    result_path: Path,
    feature: str,
    task: str,
    backend: str,
) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        _external_cli_env(
            request_path=request_path,
            result_path=result_path,
            feature=feature,
            task=task,
            backend=backend,
        )
    )
    return env


def _external_cli_completed_result(
    *,
    run: subprocess.CompletedProcess[str],
    backend: str,
    request_path: Path,
    result_path: Path,
    expected_feature: str = "",
    expected_task: str = "",
) -> dict[str, Any]:
    if not result_path.exists():
        return _external_cli_missing_result(
            run=run,
            backend=backend,
            request_path=request_path,
        )
    payload = load_execution_result(result_path)
    actual_feature = str(payload.get("feature") or "").strip()
    actual_task = str(payload.get("task") or "").strip()
    feature_mismatch = bool(expected_feature) and bool(actual_feature) and actual_feature != expected_feature.strip()
    task_mismatch = bool(expected_task) and bool(actual_task) and actual_task != expected_task.strip()
    if feature_mismatch or task_mismatch:
        logger.warning(
            "external_cli result identity mismatch (expected feature=%r task=%r; got feature=%r task=%r)",
            expected_feature,
            expected_task,
            actual_feature,
            actual_task,
        )
        return _external_cli_stale_result(
            run=run,
            backend=backend,
            request_path=request_path,
            expected_feature=expected_feature,
            expected_task=expected_task,
            actual_feature=actual_feature,
            actual_task=actual_task,
        )
    if not str(payload.get("stdout_excerpt") or "").strip() and str(run.stdout or "").strip():
        payload["stdout_excerpt"] = str(run.stdout).strip()[:4000]
    if not str(payload.get("stderr_excerpt") or "").strip() and str(run.stderr or "").strip():
        payload["stderr_excerpt"] = str(run.stderr).strip()[:4000]
    if payload.get("returncode") is None:
        payload["returncode"] = int(run.returncode)
    write_execution_result(result_path, payload)
    return _adapter_result_from_execution(payload, request_path=request_path, result_path=result_path)


def _external_cli_missing_result(
    *,
    run: subprocess.CompletedProcess[str],
    backend: str,
    request_path: Path,
) -> dict[str, Any]:
    if run.returncode != 0:
        logger.warning("external_cli executor failed without result artifact: %s", run.returncode)
        return {
            "status": "error",
            "error": (run.stderr or run.stdout or "executor command failed").strip(),
            "mode": backend,
            "returncode": run.returncode,
            "execution_backend": backend,
            "execution_backend_capabilities": execution_backend_capabilities(backend),
            "execution_artifacts": {EXECUTION_REQUEST_FILENAME: str(request_path)},
        }
    return _blocked_backend_result(
        reason="EXECUTION_RESULT_MISSING",
        blocking_reason="executor command completed without writing .execution_result.json",
        mode=backend,
        backend_name=backend,
        request_path=request_path,
    )


def _external_cli_stale_result(
    *,
    run: subprocess.CompletedProcess[str],
    backend: str,
    request_path: Path,
    expected_feature: str,
    expected_task: str,
    actual_feature: str,
    actual_task: str,
) -> dict[str, Any]:
    blocking_reason = (
        "executor command left a stale .execution_result.json: "
        f"expected feature={expected_feature!r} task={expected_task!r}; "
        f"got feature={actual_feature!r} task={actual_task!r}"
    )
    if run.returncode != 0:
        return {
            "status": "error",
            "error": (run.stderr or run.stdout or blocking_reason).strip(),
            "mode": backend,
            "returncode": run.returncode,
            "execution_backend": backend,
            "execution_backend_capabilities": execution_backend_capabilities(backend),
            "execution_artifacts": {EXECUTION_REQUEST_FILENAME: str(request_path)},
        }
    return _blocked_backend_result(
        reason="EXECUTION_RESULT_STALE",
        blocking_reason=blocking_reason,
        mode=backend,
        backend_name=backend,
        request_path=request_path,
    )


def _resume_existing_execution_result(
    *,
    payload: dict[str, Any],
    previous_payload: dict[str, Any],
    request_payload: dict[str, Any],
) -> dict[str, Any]:
    if not _can_resume_existing_execution_result(
        payload=payload,
        previous_payload=previous_payload,
        request_payload=request_payload,
    ):
        return payload
    backend = str(payload.get("backend") or previous_payload.get("backend") or "").strip()
    changed_files = _resume_changed_files(previous_payload=previous_payload, request_payload=request_payload)
    resumed = build_execution_result(
        feature=str(request_payload.get("feature") or previous_payload.get("feature") or ""),
        task=str(request_payload.get("task") or previous_payload.get("task") or ""),
        backend=backend,
        status="PASS",
        changed_files=changed_files,
        stdout_excerpt=str(payload.get("stdout_excerpt") or previous_payload.get("stdout_excerpt") or ""),
        stderr_excerpt=str(payload.get("stderr_excerpt") or previous_payload.get("stderr_excerpt") or ""),
        returncode=payload.get("returncode", previous_payload.get("returncode")),
        artifacts=changed_files,
        summary=f"{backend or 'executor'} rerun reused existing deterministic task changes",
        backend_capabilities=dict(
            payload.get("backend_capabilities")
            or previous_payload.get("backend_capabilities")
            or execution_backend_capabilities(backend)
        ),
        guard_decision=dict(payload.get("guard_decision") or previous_payload.get("guard_decision") or {}),
        implementer_note=dict(
            payload.get("implementer_note")
            or previous_payload.get("implementer_note")
            or request_payload.get("implementer_note")
            or {}
        ),
    )
    host_probe = dict(payload.get("host_probe") or previous_payload.get("host_probe") or {})
    if host_probe:
        resumed["host_probe"] = host_probe
    return resumed


def _accept_explicit_idempotent_noop_result(
    *,
    payload: dict[str, Any],
    request_payload: dict[str, Any],
) -> dict[str, Any]:
    if not _env_flag("WORKFLOW_ACCEPT_IDEMPOTENT_NOOP"):
        return payload
    error_code = str(payload.get("error_code") or "").strip().upper()
    if not error_code.endswith("CHANGED_FILES_MISSING"):
        return payload
    if str(payload.get("status") or "").strip().upper() != "BLOCKED":
        return payload
    if int(payload.get("returncode", 0) or 0) != 0:
        return payload
    changed_files = [
        str(item).strip().replace("\\", "/")
        for item in list(request_payload.get("files_to_change") or [])
        if str(item).strip()
    ]
    if not changed_files:
        return payload
    backend = str(payload.get("backend") or request_payload.get("backend") or "").strip()
    accepted = build_execution_result(
        feature=str(request_payload.get("feature") or payload.get("feature") or ""),
        task=str(request_payload.get("task") or payload.get("task") or ""),
        backend=backend,
        status="PASS",
        changed_files=changed_files,
        stdout_excerpt=str(payload.get("stdout_excerpt") or ""),
        stderr_excerpt=str(payload.get("stderr_excerpt") or ""),
        returncode=payload.get("returncode"),
        artifacts=changed_files,
        summary=f"{backend or 'executor'} accepted explicit idempotent no-op using planned files",
        backend_capabilities=dict(payload.get("backend_capabilities") or execution_backend_capabilities(backend)),
        guard_decision=dict(payload.get("guard_decision") or {}),
        implementer_note=dict(payload.get("implementer_note") or request_payload.get("implementer_note") or {}),
    )
    host_probe = dict(payload.get("host_probe") or {})
    if host_probe:
        accepted["host_probe"] = host_probe
    accepted["idempotent_noop_accepted"] = True
    return accepted


def _env_flag(name: str) -> bool:
    return str(os.environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _can_resume_existing_execution_result(
    *,
    payload: dict[str, Any],
    previous_payload: dict[str, Any],
    request_payload: dict[str, Any],
) -> bool:
    error_code = str(payload.get("error_code") or "").strip().upper()
    if not error_code.endswith("CHANGED_FILES_MISSING"):
        return False
    if str(payload.get("status") or "").strip().upper() != "BLOCKED":
        return False
    if int(payload.get("returncode", 0) or 0) != 0:
        return False
    return bool(_resume_changed_files(previous_payload=previous_payload, request_payload=request_payload))


def _resume_changed_files(
    *,
    previous_payload: dict[str, Any],
    request_payload: dict[str, Any],
) -> list[str]:
    allowed_files = {
        str(item).strip().replace("\\", "/")
        for item in list(request_payload.get("files_to_change") or [])
        if str(item).strip()
    }
    feature = str(request_payload.get("feature") or "").strip()
    task = str(request_payload.get("task") or "").strip()
    backend = str(request_payload.get("backend") or "").strip()
    if str(previous_payload.get("status") or "").strip().upper() == "PASS":
        if (not feature or str(previous_payload.get("feature") or "").strip() == feature) and (
            not task or str(previous_payload.get("task") or "").strip() == task
        ) and (not backend or str(previous_payload.get("backend") or "").strip() == backend):
            previous_changed = [
                str(item).strip().replace("\\", "/")
                for item in list(previous_payload.get("changed_files") or [])
                if str(item).strip()
            ]
            if previous_changed and set(previous_changed).issubset(allowed_files):
                return previous_changed
    planning_dir = Path(str(request_payload.get("planning_dir") or "")).resolve()
    if planning_dir.exists():
        state_payload = load_json_dict(planning_dir / ".autopilot_state.json", required=False) or {}
        if str(state_payload.get("feature") or "").strip() == str(request_payload.get("feature") or "").strip():
            state_changed = [
                str(item).strip().replace("\\", "/")
                for item in list(state_payload.get("changed_files") or [])
                if str(item).strip()
            ]
            if state_changed and set(state_changed).issubset(allowed_files):
                return state_changed
    return []


__all__ = [
    "ALLOWED_EXECUTION_BACKENDS",
    "EXECUTION_REQUEST_FILENAME",
    "EXECUTION_REQUEST_SCHEMA_VERSION",
    "EXECUTION_RESULT_FILENAME",
    "EXECUTION_RESULT_SCHEMA_VERSION",
    "ExecutionArtifactError",
    "ExecutionBackendConfig",
    "EXTERNAL_CLI_BACKEND",
    "CODEX_CLI_BACKEND",
    "MANUAL_BACKEND",
    "NOOP_TEST_ONLY_BACKEND",
    "build_execution_request",
    "build_execution_result",
    "is_test_environment",
    "load_execution_result",
    "resolve_execution_backend",
    "run_execution_backend",
    "write_execution_request",
    "write_execution_result",
]

