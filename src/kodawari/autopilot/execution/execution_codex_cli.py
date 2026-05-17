"""Native Codex CLI execution backend helpers."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
from typing import Any

from kodawari.autopilot.execution.execution_backend import verify_expectation_text
from kodawari.autopilot.execution.execution_prompt_common import (
    render_fix_round_preamble,
    render_scope_risk_warning_lines,
    render_scope_constraint_lines,
)
from kodawari.autopilot.execution.execution_guard import GuardDecision, evaluate_execution_command
from kodawari.autopilot.execution.execution_isolation import (
    prepare_isolation_workspace,
    sync_isolated_workspace_to_project_root,
)
from kodawari.autopilot.core.isolated_home import sync_first_present_source
from kodawari.autopilot.core.subprocess_compat import (
    subprocess_text_kwargs,
    windows_safe_command,
)
from kodawari.autopilot.core.execution_sentinel import (
    SENTINEL_FILENAME,
    TIMEOUT_HINT_MAP,
    read_sentinel,
    resolve_timeout_seconds,
    sentinel_indicates_verify_passed,
    sentinel_path,
)

logger = logging.getLogger(__name__)


class CodexCliPreflightGuardBlocked(RuntimeError):
    """Raised when codex_cli override command is blocked before subprocess dispatch."""

    def __init__(self, *, command: str, decision: GuardDecision) -> None:
        super().__init__(decision.message)
        self.command = str(command or "").strip()
        self.decision = decision


def _codex_isolation_enabled(config: Any) -> bool:
    """codex_cli isolation is enabled by default (opt-out).

    Disabled when WorkflowConfig / executor config explicitly sets
    `isolation_workspace=False`, or when env WORKFLOW_CODEX_ISOLATION is
    falsy ("0", "false", "no", "off").  Any other value (including unset)
    defaults to enabled so every codex run is sandboxed automatically.
    """
    import os as _os
    explicit = getattr(config, "isolation_workspace", None)
    if explicit is not None:
        return bool(explicit)
    env_val = str(_os.environ.get("WORKFLOW_CODEX_ISOLATION", "")).strip().lower()
    if env_val in {"0", "false", "no", "off"}:
        return False
    return True  # default on


def _codex_planning_dir(request_payload: dict[str, Any], *, project_root: Path) -> Path:
    planning_dir_raw = str(request_payload.get("planning_dir") or "").strip()
    if planning_dir_raw:
        candidate = Path(planning_dir_raw).resolve()
        if candidate.is_relative_to(project_root.resolve()):
            return candidate
        logger.warning("planning_dir %s is outside project_root %s; falling back to default", candidate, project_root)
    return (project_root / "planning").resolve()


_SENTINEL_FILENAME = SENTINEL_FILENAME
_TIMEOUT_HINT_MAP = TIMEOUT_HINT_MAP


def _resolve_timeout_seconds(config: Any, request_payload: dict[str, Any]) -> int:
    return resolve_timeout_seconds(config, request_payload)


def _sentinel_path(execution_root: Path) -> Path:
    return sentinel_path(execution_root)


def _read_sentinel(execution_root: Path) -> dict[str, Any] | None:
    return read_sentinel(execution_root)


def _subprocess_text_kwargs(*, timeout_seconds: int) -> dict[str, Any]:
    return subprocess_text_kwargs(timeout=max(30, int(timeout_seconds or 600)))


def _backend_runtime_home(*, project_root: Path) -> Path:
    runtime_home = (project_root / ".workflow_runtime" / "codex_cli" / "home").resolve()
    runtime_home.mkdir(parents=True, exist_ok=True)
    return runtime_home


def _apply_runtime_home_env(env: dict[str, str], *, runtime_home: Path) -> None:
    home_text = str(runtime_home)
    (runtime_home / "tmp").mkdir(parents=True, exist_ok=True)
    (runtime_home / ".config").mkdir(parents=True, exist_ok=True)
    (runtime_home / ".cache").mkdir(parents=True, exist_ok=True)
    (runtime_home / ".state").mkdir(parents=True, exist_ok=True)
    (runtime_home / "AppData" / "Roaming").mkdir(parents=True, exist_ok=True)
    (runtime_home / "AppData" / "Local").mkdir(parents=True, exist_ok=True)

    env["HOME"] = home_text
    env["USERPROFILE"] = home_text
    drive, tail = os.path.splitdrive(home_text)
    if drive:
        env["HOMEDRIVE"] = drive
    if tail:
        normalized_tail = tail if tail.startswith(("\\", "/")) else f"\\{tail}"
        env["HOMEPATH"] = normalized_tail.replace("/", "\\")
    env["XDG_CONFIG_HOME"] = str((runtime_home / ".config").resolve())
    env["XDG_CACHE_HOME"] = str((runtime_home / ".cache").resolve())
    env["XDG_STATE_HOME"] = str((runtime_home / ".state").resolve())
    env["APPDATA"] = str((runtime_home / "AppData" / "Roaming").resolve())
    env["LOCALAPPDATA"] = str((runtime_home / "AppData" / "Local").resolve())
    env["TMP"] = str((runtime_home / "tmp").resolve())
    env["TEMP"] = str((runtime_home / "tmp").resolve())


_CODEX_AUTH_MODE_ENV = "WORKFLOW_CODEX_AUTH_MODE"
# host     = copy auth from real user codex home into isolated dir (default, suits local dev)
# isolated = do not copy auth; codex starts with no credentials (suits CI/sandbox)


def _codex_auth_mode() -> str:
    raw = os.environ.get(_CODEX_AUTH_MODE_ENV, "host").strip().lower()
    return raw if raw in {"host", "isolated"} else "host"


def _codex_child_env(*, project_root: Path) -> dict[str, str]:
    env = dict(os.environ)
    runtime_home = _backend_runtime_home(project_root=project_root)
    _apply_runtime_home_env(env, runtime_home=runtime_home)
    codex_home = (runtime_home / ".codex").resolve()
    codex_home.mkdir(parents=True, exist_ok=True)
    if _codex_auth_mode() == "host":
        _sync_codex_auth_to_isolated(codex_home, parent_env=env)
    env["CODEX_HOME"] = str(codex_home)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def _sync_codex_auth_to_isolated(isolated_codex_home: Path, *, parent_env: dict[str, str]) -> None:
    """Copy auth.json from the real user codex home into the isolated home.

    Controlled by WORKFLOW_CODEX_AUTH_MODE=host (default) or isolated.
    host     — reuses the host machine's codex login (VSCode session or CLI login).
    isolated — skips copy; codex starts with no credentials (CI/sandbox use case).

    Uses the shared mtime-aware sync primitive so token refreshes on the host
    actually propagate into the isolated copy.
    """
    target = isolated_codex_home / "auth.json"
    # Prefer explicit CODEX_HOME from parent env (before our override).
    # Fall back to the OS user home .codex directory.
    parent_codex_home_raw = parent_env.get("CODEX_HOME", "")
    candidate_dirs: list[Path] = []
    if parent_codex_home_raw:
        candidate_dirs.append(Path(parent_codex_home_raw))
    candidate_dirs.append(Path.home() / ".codex")
    sync_first_present_source(
        target=target,
        source_candidates=[d / "auth.json" for d in candidate_dirs],
        label="codex_executor_auth",
    )


def materialize_codex_cli_result(
    *,
    config: Any,
    request_path: Path,
    request_payload: dict[str, Any],
) -> dict[str, Any]:
    missing_payload = _missing_binary_payload(config=config, request_payload=request_payload)
    if missing_payload is not None:
        return missing_payload
    allowed_files = [str(item) for item in list(request_payload.get("files_to_change") or []) if str(item).strip()]
    project_root = Path(str(request_payload.get("project_root") or "")).resolve()
    before = _file_hashes(project_root, allowed_files)

    # Phase B: opt-in isolation workspace. When enabled, codex runs inside a
    # per-task copy of project_root; only allowed_files are synced back on
    # successful exit. On non-zero exit the workspace is left behind for
    # debugging (no partial writes leak into project_root).
    isolation_enabled = _codex_isolation_enabled(config)
    execution_root = project_root
    workspace_path: Path | None = None
    if isolation_enabled:
        planning_dir = _codex_planning_dir(request_payload, project_root=project_root)
        workspace_path = prepare_isolation_workspace(
            planning_dir=planning_dir,
            project_root=project_root,
            backend_name="codex_cli",
            request_payload=request_payload,
        )
        execution_root = workspace_path

    request_context = _request_context(request_payload, request_path=request_path)
    effective_request_path = request_path
    if workspace_path is not None:
        effective_request_path = workspace_path / ".execution_request.json"
        # Update both project_root AND request_path in the context BEFORE
        # writing to disk.  The subprocess reads request_path from the JSON
        # file itself; if it still points to the original planning dir the
        # subprocess will write to project_root, bypassing the isolation boundary.
        request_context["project_root"] = str(workspace_path)
        request_context["request_path"] = str(effective_request_path)
        effective_request_path.write_text(
            json.dumps(request_context, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    timeout_seconds = _resolve_timeout_seconds(config, request_payload)
    try:
        completed = _run_codex_command(
            config=config,
            request_payload=request_context,
            request_path=effective_request_path,
            timeout_seconds=timeout_seconds,
        )
    except CodexCliPreflightGuardBlocked as exc:
        logger.warning("codex_cli backend preflight guard blocked command override: %s", exc.command)
        return _guard_blocked_payload(request_payload, blocked_command=exc.command, decision=exc.decision)
    except subprocess.TimeoutExpired as exc:
        logger.warning("codex_cli backend timed out after %ss", timeout_seconds)
        # Check if executor wrote a completion sentinel before being killed.
        # If present with status=verify_passed, treat as recoverable timeout and sync.
        sentinel = _read_sentinel(execution_root)
        if sentinel and sentinel.get("status") == "verify_passed" and workspace_path is not None:
            logger.info("codex_cli: sentinel found after timeout — performing controlled sync")
            sync_isolated_workspace_to_project_root(
                project_root=project_root,
                execution_root=execution_root,
                allowed_files=allowed_files,
            )
            changed_files = _changed_files_from_hashes(
                project_root=project_root,
                allowed_files=allowed_files,
                before=before,
            )
            if changed_files:
                return _success_payload(
                    request_payload,
                    completed=subprocess.CompletedProcess(args=[], returncode=0, stdout="[recovered from timeout via sentinel]", stderr=""),
                    changed_files=changed_files,
                )
        return _timeout_payload(request_payload, exc=exc)
    except OSError as exc:
        logger.warning("codex_cli backend failed to start", exc_info=True)
        return _start_failed_payload(request_payload, exc=exc)

    # On success only, propagate allowed_files from the workspace back into
    # project_root so downstream deterministic changed_files detection sees them.
    if workspace_path is not None and completed.returncode == 0:
        sync_isolated_workspace_to_project_root(
            project_root=project_root,
            execution_root=execution_root,
            allowed_files=allowed_files,
        )

    changed_files = _changed_files_from_hashes(
        project_root=project_root,
        allowed_files=allowed_files,
        before=before,
    )
    if completed.returncode != 0:
        logger.warning("codex_cli backend returned non-zero exit code: %s", completed.returncode)
        return _failure_payload(request_payload, completed=completed, changed_files=changed_files)
    if not changed_files:
        logger.warning("codex_cli backend completed without deterministic changed files")
        return _changed_files_missing_payload(request_payload, completed=completed)
    return _success_payload(request_payload, completed=completed, changed_files=changed_files)


def _run_codex_command(
    *,
    config: Any,
    request_payload: dict[str, Any],
    request_path: Path,
    timeout_seconds: int | None = None,
) -> subprocess.CompletedProcess[str]:
    project_root = Path(str(request_payload.get("project_root") or "")).resolve()
    effective_timeout = timeout_seconds if timeout_seconds is not None else int(getattr(config, "timeout_seconds", 600) or 600)
    command_override = str(getattr(config, "command", "") or "").strip()
    if command_override:
        command = _render_override_command(
            template=command_override,
            project_root=project_root,
            request_payload=request_payload,
            request_path=request_path,
        )
        guard_decision = evaluate_execution_command(command)
        if guard_decision is not None:
            raise CodexCliPreflightGuardBlocked(command=command, decision=guard_decision)
        return subprocess.run(
            command,
            shell=True,
            cwd=str(project_root),
            **_subprocess_text_kwargs(timeout_seconds=effective_timeout),
        )
    prompt = _request_prompt(request_payload)
    executable = _resolved_executable(config)
    model = str(getattr(config, "model", "") or "").strip()
    child_env = _codex_child_env(project_root=project_root)
    return subprocess.run(
        _codex_command(executable=executable, project_root=project_root, model=model),
        input=prompt,
        cwd=str(project_root),
        env=child_env,
        **_subprocess_text_kwargs(timeout_seconds=effective_timeout),
    )


def _request_context(request_payload: dict[str, Any], *, request_path: Path) -> dict[str, Any]:
    payload = dict(request_payload)
    payload["request_path"] = str(request_path)
    return payload


def _build_result_payload(request_payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    from kodawari.autopilot.execution.execution_artifacts import build_execution_result

    kwargs.setdefault("implementer_note", request_payload.get("implementer_note"))
    return build_execution_result(
        feature=str(request_payload.get("feature") or ""),
        task=str(request_payload.get("task") or ""),
        backend="codex_cli",
        **kwargs,
    )


def _missing_binary_payload(*, config: Any, request_payload: dict[str, Any]) -> dict[str, Any] | None:
    command_override = str(getattr(config, "command", "") or "").strip()
    if command_override:
        return None
    executable = _resolved_executable(config)
    if _executable_available(executable):
        return None
    logger.warning("codex_cli backend blocked because codex binary is unavailable")
    return _build_result_payload(
        request_payload,
        status="BLOCKED",
        changed_files=[],
        error_code="CODEX_CLI_MISSING",
        blocking_reason=f"codex_cli backend requires the '{executable}' executable",
        summary=f"{executable} is unavailable",
    )


def _timeout_payload(request_payload: dict[str, Any], *, exc: subprocess.TimeoutExpired) -> dict[str, Any]:
    return _build_result_payload(
        request_payload,
        status="FAIL",
        changed_files=[],
        stdout_excerpt=_excerpt(getattr(exc, "stdout", "")),
        stderr_excerpt=_excerpt(getattr(exc, "stderr", "")),
        error_code="CODEX_CLI_TIMEOUT",
        blocking_reason="codex_cli execution timed out",
        summary="codex_cli execution timed out",
    )


def _start_failed_payload(request_payload: dict[str, Any], *, exc: OSError) -> dict[str, Any]:
    return _build_result_payload(
        request_payload,
        status="FAIL",
        changed_files=[],
        error_code="CODEX_CLI_START_FAILED",
        blocking_reason=str(exc),
        summary=str(exc),
    )


def _guard_blocked_payload(
    request_payload: dict[str, Any],
    *,
    blocked_command: str,
    decision: GuardDecision,
) -> dict[str, Any]:
    payload = _build_result_payload(
        request_payload,
        status="BLOCKED",
        changed_files=[],
        error_code=decision.error_code,
        blocking_reason=decision.message,
        summary=decision.message,
    )
    payload["guard_action"] = decision.action
    payload["guard_policy"] = decision.policy
    payload["guard_pattern"] = decision.pattern
    payload["guard_command"] = str(blocked_command or "").strip()
    payload["guard_decision"] = _guard_decision_payload(decision)
    return payload


def _failure_payload(
    request_payload: dict[str, Any],
    *,
    completed: subprocess.CompletedProcess[str],
    changed_files: list[str],
) -> dict[str, Any]:
    message = _excerpt(completed.stderr) or _excerpt(completed.stdout) or "codex_cli failed"
    return _build_result_payload(
        request_payload,
        status="FAIL",
        changed_files=changed_files,
        stdout_excerpt=_excerpt(completed.stdout),
        stderr_excerpt=_excerpt(completed.stderr),
        returncode=int(completed.returncode),
        artifacts=changed_files,
        error_code="CODEX_CLI_FAILED",
        blocking_reason=message,
        summary=message,
    )


def _changed_files_missing_payload(
    request_payload: dict[str, Any],
    *,
    completed: subprocess.CompletedProcess[str],
) -> dict[str, Any]:
    return _build_result_payload(
        request_payload,
        status="BLOCKED",
        changed_files=[],
        stdout_excerpt=_excerpt(completed.stdout),
        stderr_excerpt=_excerpt(completed.stderr),
        returncode=int(completed.returncode),
        error_code="CODEX_CLI_CHANGED_FILES_MISSING",
        blocking_reason="codex_cli execution completed without deterministic changed files",
        summary="codex_cli did not produce deterministic changed files",
    )


def _success_payload(
    request_payload: dict[str, Any],
    *,
    completed: subprocess.CompletedProcess[str],
    changed_files: list[str],
) -> dict[str, Any]:
    return _build_result_payload(
        request_payload,
        status="PASS",
        changed_files=changed_files,
        stdout_excerpt=_excerpt(completed.stdout),
        stderr_excerpt=_excerpt(completed.stderr),
        returncode=int(completed.returncode),
        artifacts=changed_files,
        summary="codex_cli completed with deterministic changed files",
    )


def _resolved_executable(config: Any) -> str:
    executable = str(getattr(config, "executable", "") or "codex").strip()
    if not executable:
        executable = "codex"
    candidate = Path(executable)
    if candidate.exists():
        return str(candidate)
    resolved = shutil.which(executable)
    if resolved:
        return str(Path(resolved))
    # Windows: npm 全局包不一定在子进程 PATH 里，主动探测常见安装位置
    if os.name == "nt":
        found = _windows_npm_find(executable)
        if found:
            return found
    return executable


def _windows_npm_find(name: str) -> str | None:
    """在 Windows 常见 npm 全局目录里查找可执行文件。"""
    appdata = os.environ.get("APPDATA", "")
    search_dirs = []
    if appdata:
        search_dirs.append(Path(appdata) / "npm")
    localappdata = os.environ.get("LOCALAPPDATA", "")
    if localappdata:
        search_dirs += [
            Path(localappdata) / "nvm",
            Path(localappdata) / "Volta" / "bin",
        ]
    for d in search_dirs:
        for suffix in ("", ".cmd", ".ps1", ".exe"):
            candidate = d / (name + suffix)
            if candidate.exists():
                return str(candidate)
    return None


def _executable_available(executable: str) -> bool:
    candidate = Path(executable)
    if candidate.exists():
        return True
    return shutil.which(executable) is not None


def _sanitize_model_for_cli(value: str) -> str:
    """Guard against values that could be misinterpreted as CLI flags."""
    if not value:
        return ""
    if value.startswith("-") or len(value) > 200 or any(ord(c) < 32 for c in value):
        return ""
    return value


def _codex_command(*, executable: str, project_root: Path, model: str = "") -> list[str]:
    """Build codex CLI command. Prompt is passed via stdin (not argv)."""
    cmd = [
        *_codex_program(executable),
        "exec",
        "--skip-git-repo-check",
        "--dangerously-bypass-approvals-and-sandbox",
        "--cd",
        str(project_root),
    ]
    model = _sanitize_model_for_cli(model)
    if model:
        cmd.extend(["--model", model])
    return cmd


def _codex_program(executable: str) -> list[str]:
    path = Path(executable)
    if path.suffix.lower() == ".py" and path.exists():
        return [sys.executable, str(path)]
    # Windows .cmd/.bat shims need cmd.exe wrapping; shared helper handles both.
    return windows_safe_command(str(path))


def _request_prompt(request_payload: dict[str, Any]) -> str:
    files = ", ".join(str(item) for item in list(request_payload.get("files_to_change") or []) if str(item).strip())
    invariants = "\n".join(
        f"- {item}" for item in list(request_payload.get("invariants") or []) if str(item).strip()
    )
    task_scope = str(request_payload.get("task_scope") or "").strip()
    verify_cmd = str(request_payload.get("verify_cmd") or "").strip()
    archetype = str(request_payload.get("archetype") or "").strip()
    capabilities = ", ".join(str(item) for item in list(request_payload.get("capabilities") or []) if str(item).strip())
    surface = str(request_payload.get("surface") or "").strip()
    task_id = str(request_payload.get("task_id") or "").strip()
    request_path = str(request_payload.get("request_path") or "").strip()
    lines = [
        f"Implement task: {str(request_payload.get('task') or '').strip()}",
        f"Feature: {str(request_payload.get('feature') or '').strip()}",
        f"Task ID: {task_id or '(unknown)'}",
        f"Archetype: {archetype or '(unknown)'}",
        f"Capabilities: {capabilities or '(none)'}",
        f"Surface: {surface or '(unknown)'}",
        f"Allowed files: {files or '(none supplied)'}",
    ]
    if task_scope:
        lines.append(f"Scope: {task_scope}")
    task_requirements = str(request_payload.get("task_requirements") or "").strip()
    if task_requirements:
        lines.append(f"Requirements:\n{task_requirements}")
    risk_warning_lines = render_scope_risk_warning_lines(request_payload)
    if risk_warning_lines:
        lines.extend(risk_warning_lines)
    if request_path:
        lines.append(f"Request path: {request_path}")
    if invariants:
        lines.extend(["Invariants:", invariants])
    if verify_cmd:
        lines.append(f"Verify command expectation: {verify_expectation_text(verify_cmd)}")
    lines.append("Modify only the allowed files and leave the workspace in a runnable state.")
    sentinel_path = str(request_payload.get("request_path") or "").strip()
    if sentinel_path:
        sentinel_dir = str(Path(sentinel_path).parent)
        lines.append(
            f"When all changes are complete and local verification passes, write the file "
            f"{sentinel_dir}/.execution_sentinel.json with content "
            f'{{\"status\": \"verify_passed\"}} to signal successful completion. '
            f"This allows the parent process to recover from timeout if the task finishes "
            f"just after the deadline."
        )
    scope_lines = render_scope_constraint_lines(request_payload)
    if scope_lines:
        lines.extend(scope_lines)
    preamble = render_fix_round_preamble(request_payload)
    return "\n".join(preamble + lines)


def _render_override_command(
    *,
    template: str,
    project_root: Path,
    request_payload: dict[str, Any],
    request_path: Path,
) -> str:
    task_card = dict(request_payload.get("task_card") or {})
    # Path values are operator-controlled (CLI args), safe to inline unquoted.
    # Payload-sourced values (task, files, archetype, surface) come from AI
    # planner output and MUST be shell-escaped to prevent injection.
    values = {
        "{task}": shlex.quote(str(request_payload.get("task") or "")),
        "{files}": shlex.quote(",".join(str(item) for item in list(request_payload.get("files_to_change") or []) if str(item).strip())),
        "{project_root}": str(project_root),
        "{archetype}": shlex.quote(str(task_card.get("archetype") or "")),
        "{surface}": shlex.quote(str(task_card.get("surface") or "")),
        "{request_path}": str(request_path),
    }
    rendered = str(template)
    for placeholder, value in values.items():
        rendered = rendered.replace(placeholder, value)
    return rendered


def _file_hashes(project_root: Path, allowed_files: list[str]) -> dict[str, str]:
    hashes: dict[str, str] = {}
    root = project_root.resolve()
    for relative in allowed_files:
        candidate = _resolve_allowed_candidate(project_root=root, relative=relative)
        if candidate is None:
            hashes[relative] = ""
            continue
        if candidate.exists() and candidate.is_file():
            hashes[relative] = _sha256(candidate)
            continue
        hashes[relative] = ""
    return hashes


def _changed_files_from_hashes(
    *,
    project_root: Path,
    allowed_files: list[str],
    before: dict[str, str],
) -> list[str]:
    changed: list[str] = []
    root = project_root.resolve()
    for relative in allowed_files:
        candidate = _resolve_allowed_candidate(project_root=root, relative=relative)
        if candidate is None:
            continue
        after = _sha256(candidate) if candidate.exists() and candidate.is_file() else ""
        if before.get(relative, "") != after:
            changed.append(relative.replace("\\", "/"))
    return changed


def _resolve_allowed_candidate(*, project_root: Path, relative: str) -> Path | None:
    normalized = str(relative or "").strip().replace("\\", "/")
    if not normalized:
        return None
    candidate = (project_root / normalized).resolve()
    if not candidate.is_relative_to(project_root):
        return None
    return candidate


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _excerpt(value: Any, *, max_lines: int = 8) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return "\n".join(text.splitlines()[-max_lines:]).strip()


def _guard_decision_payload(decision: GuardDecision) -> dict[str, Any]:
    return {
        "action": decision.action,
        "reason": decision.message,
        "policy": decision.policy,
        "pattern": decision.pattern,
    }


__all__ = ["materialize_codex_cli_result"]

