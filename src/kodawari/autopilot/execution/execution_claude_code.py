"""Native Claude Code execution backend helpers."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import shlex
import shutil
import subprocess
from typing import Any, Mapping
from uuid import uuid4

from kodawari.autopilot.execution.execution_backend import verify_expectation_text
from kodawari.autopilot.execution.execution_prompt_common import (
    render_fix_round_preamble,
    render_scope_risk_warning_lines,
    render_scope_constraint_lines,
)
from kodawari.autopilot.execution.execution_guard import GuardDecision, evaluate_execution_command
from kodawari.autopilot.core.isolated_home import sync_file_mtime_aware
from kodawari.autopilot.core.execution_sentinel import (
    read_sentinel,
    resolve_timeout_seconds,
    sentinel_indicates_verify_passed,
)


logger = logging.getLogger(__name__)
_COMPACT_CONTEXT_FIELDS = ("decisions", "constraints", "recent_errors", "must_fix", "open_questions")


class ClaudeCodePreflightGuardBlocked(RuntimeError):
    """Raised when claude_code override command is blocked before subprocess dispatch."""

    def __init__(self, *, command: str, decision: GuardDecision) -> None:
        super().__init__(decision.message)
        self.command = str(command or "").strip()
        self.decision = decision


def materialize_claude_code_result(
    *,
    config: Any,
    request_path: Path,
    request_payload: dict[str, Any],
) -> dict[str, Any]:
    host_probe = _host_probe(config=config)
    missing_payload = _missing_binary_payload(
        config=config,
        request_payload=request_payload,
        host_probe=host_probe,
    )
    if missing_payload is not None:
        return _attach_host_probe(missing_payload, host_probe=host_probe)
    allowed_files = [str(item) for item in list(request_payload.get("files_to_change") or []) if str(item).strip()]
    project_root = Path(str(request_payload.get("project_root") or "")).resolve()
    planning_dir = _planning_dir(request_payload, project_root=project_root)
    before = _file_hashes(project_root, allowed_files)
    execution_root = _prepare_isolation_workspace(
        planning_dir=planning_dir,
        project_root=project_root,
        allowed_files=allowed_files,
        request_payload=request_payload,
    )

    # Preflight: Node-based Claude CLI lstat's effective user home at startup.
    # We probe the actual child env we are about to launch with, not the host
    # process env, because the executor rewrites HOME/USERPROFILE to an
    # isolated runtime dir before spawning Claude.
    command_override = str(getattr(config, "command", "") or "").strip()
    home_probe: dict[str, Any] | None = None
    if not command_override:
        probe_env = _clean_child_env(execution_root=execution_root)
        home_probe = _probe_home_accessibility(env=probe_env)
        if home_probe.get("status") == "blocked":
            enriched = dict(host_probe)
            enriched["home_probe"] = dict(home_probe)
            remediation = list(home_probe.get("remediation") or ())
            if remediation:
                enriched["remediation"] = remediation
            return _attach_host_probe(
                _home_inaccessible_payload(request_payload, home_probe=home_probe),
                host_probe=enriched,
            )
        # Attach the successful/skipped probe to host_probe for observability.
        host_probe = {**host_probe, "home_probe": dict(home_probe)}

    request_context = _request_context(
        request_payload,
        request_path=request_path,
        execution_root=execution_root,
        host_probe=host_probe,
    )
    try:
        completed = _run_claude_command(
            config=config,
            request_payload=request_context,
            request_path=request_path,
            execution_root=execution_root,
        )
    except ClaudeCodePreflightGuardBlocked as exc:
        logger.warning("claude_code backend preflight guard blocked command override: %s", exc.command)
        return _attach_host_probe(
            _guard_blocked_payload(
            request_payload,
            blocked_command=exc.command,
            decision=exc.decision,
            ),
            host_probe=host_probe,
        )
    except subprocess.TimeoutExpired as exc:
        logger.warning("claude_code backend timed out", exc_info=True)
        # Sentinel-based timeout recovery: if the subprocess managed to write
        # ``.execution_sentinel.json`` with status=verify_passed before being
        # killed, treat it as a recoverable timeout — sync the isolated
        # workspace and report success-from-recovery instead of discarding work.
        sentinel = read_sentinel(execution_root)
        if sentinel_indicates_verify_passed(sentinel):
            logger.info(
                "claude_code: sentinel found after timeout — performing controlled sync "
                "(recovered from timeout)"
            )
            _sync_isolated_workspace_to_project_root(
                project_root=project_root,
                execution_root=execution_root,
                allowed_files=allowed_files,
            )
            recovered_changed_files = _changed_files_from_hashes(
                project_root=project_root,
                allowed_files=allowed_files,
                before=before,
            )
            if recovered_changed_files:
                return _attach_host_probe(
                    _success_payload(
                        request_payload,
                        completed=subprocess.CompletedProcess(
                            args=[],
                            returncode=0,
                            stdout="[recovered from timeout via sentinel]",
                            stderr="",
                        ),
                        changed_files=recovered_changed_files,
                    ),
                    host_probe=host_probe,
                )
        return _attach_host_probe(_timeout_payload(request_payload, exc=exc), host_probe=host_probe)
    except OSError as exc:
        logger.warning("claude_code backend failed to start", exc_info=True)
        return _attach_host_probe(_start_failed_payload(request_payload, exc=exc), host_probe=host_probe)
    if completed.returncode != 0:
        logger.warning("claude_code backend returned non-zero exit code: %s", completed.returncode)
        # Do NOT sync isolation workspace on failure — partial/corrupt edits
        # must not propagate to project_root.  Detect changes in the isolation
        # workspace for diagnostic purposes only.
        changed_files = _changed_files_from_hashes(
            project_root=execution_root,
            allowed_files=allowed_files,
            before=before,
        )
        home_path_for_classify = str((home_probe or {}).get("home") or "") or None
        failure = _failure_payload(
            request_payload,
            completed=completed,
            changed_files=changed_files,
            home_path=home_path_for_classify,
        )
        # Mirror remediation into host_probe so the status.md renderer —
        # which only reads from execution_host_probe — surfaces the hint.
        # Without this, home_access_error failures show remediation on the
        # raw JSON payload but not on STATUS.md, creating a silent gap in
        # the operator view.
        final_host_probe = host_probe
        if (
            failure.get("cli_failure_type") == "home_access_error"
            and failure.get("remediation")
        ):
            final_host_probe = {
                **host_probe,
                "remediation": list(failure["remediation"]),
            }
        return _attach_host_probe(failure, host_probe=final_host_probe)
    # Success path: sync allowed_files back to project_root, then detect changes.
    _sync_isolated_workspace_to_project_root(
        project_root=project_root,
        execution_root=execution_root,
        allowed_files=allowed_files,
    )
    changed_files = _changed_files_from_hashes(
        project_root=project_root,
        allowed_files=allowed_files,
        before=before,
    )
    if not changed_files:
        logger.warning("claude_code backend completed without deterministic changed files")
        return _attach_host_probe(
            _changed_files_missing_payload(request_payload, completed=completed),
            host_probe=host_probe,
        )
    return _attach_host_probe(
        _success_payload(request_payload, completed=completed, changed_files=changed_files),
        host_probe=host_probe,
    )


def _run_claude_command(
    *,
    config: Any,
    request_payload: dict[str, Any],
    request_path: Path,
    execution_root: Path,
) -> subprocess.CompletedProcess[str]:
    project_root = Path(str(request_payload.get("project_root") or "")).resolve()
    command_override = str(getattr(config, "command", "") or "").strip()
    if command_override:
        command = _render_override_command(
            template=command_override,
            project_root=project_root,
            execution_root=execution_root,
            request_payload=request_payload,
            request_path=request_path,
        )
        guard_decision = evaluate_execution_command(command)
        if guard_decision is not None:
            raise ClaudeCodePreflightGuardBlocked(command=command, decision=guard_decision)
        return subprocess.run(
            command,
            shell=True,
            cwd=str(execution_root),
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            timeout=max(30, resolve_timeout_seconds(config, request_payload)),
        )
    prompt = _request_prompt(request_payload)
    executable = _resolved_executable(config)
    executable = _prepare_windows_claude_executable(
        executable=executable,
        execution_root=execution_root,
    )
    model = str(getattr(config, "model", "") or "").strip()
    child_env = _clean_child_env(execution_root=execution_root)
    cmd = _claude_command(executable=executable, model=model)
    completed = subprocess.run(
        cmd,
        input=prompt.encode("utf-8"),
        cwd=str(execution_root),
        env=child_env,
        capture_output=True,
        timeout=max(30, resolve_timeout_seconds(config, request_payload)),
    )
    raw_stdout = getattr(completed, "stdout", b"") or b""
    raw_stderr = getattr(completed, "stderr", b"") or b""
    return subprocess.CompletedProcess(
        args=cmd,
        returncode=getattr(completed, "returncode", 1),
        stdout=raw_stdout.decode("utf-8", errors="replace") if isinstance(raw_stdout, bytes) else str(raw_stdout),
        stderr=raw_stderr.decode("utf-8", errors="replace") if isinstance(raw_stderr, bytes) else str(raw_stderr),
    )


def _request_context(
    request_payload: dict[str, Any],
    *,
    request_path: Path,
    execution_root: Path,
    host_probe: dict[str, Any],
) -> dict[str, Any]:
    payload = dict(request_payload)
    payload["request_path"] = str(request_path)
    payload["execution_root"] = str(execution_root)
    payload["host_probe"] = dict(host_probe or {})
    payload["kernel_compact_context"] = _load_kernel_compact_context(payload)
    return payload


def _build_result_payload(request_payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    from kodawari.autopilot.execution.execution_artifacts import build_execution_result

    kwargs.setdefault("implementer_note", request_payload.get("implementer_note"))
    return build_execution_result(
        feature=str(request_payload.get("feature") or ""),
        task=str(request_payload.get("task") or ""),
        backend="claude_code",
        **kwargs,
    )


def _missing_binary_payload(
    *,
    config: Any,
    request_payload: dict[str, Any],
    host_probe: dict[str, Any],
) -> dict[str, Any] | None:
    command_override = str(getattr(config, "command", "") or "").strip()
    if command_override:
        return None
    if str(host_probe.get("status") or "").strip().lower() == "ready":
        return None
    executable = _resolved_executable(config)
    logger.warning("claude_code backend blocked because claude executable is unavailable")
    return _build_result_payload(
        request_payload,
        status="BLOCKED",
        changed_files=[],
        error_code="CLAUDE_CODE_MISSING",
        blocking_reason=f"claude_code backend requires the '{executable}' executable",
        summary=f"{executable} is unavailable",
    )


def _host_probe(*, config: Any) -> dict[str, Any]:
    command_override = str(getattr(config, "command", "") or "").strip()
    executable = _resolved_executable(config)
    available = _executable_available(executable)
    if command_override:
        return {
            "status": "degraded",
            "surface": "claude_cli",
            "reason": "command_override",
            "executable": executable,
            "executable_available": bool(available),
        }
    return {
        "status": "ready" if available else "blocked",
        "surface": "claude_cli",
        "reason": "" if available else "executable_unavailable",
        "executable": executable,
        "executable_available": bool(available),
    }


def _attach_host_probe(payload: dict[str, Any], *, host_probe: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(payload)
    enriched["host_probe"] = dict(host_probe or {})
    return enriched


def _timeout_payload(request_payload: dict[str, Any], *, exc: subprocess.TimeoutExpired) -> dict[str, Any]:
    return _build_result_payload(
        request_payload,
        status="FAIL",
        changed_files=[],
        stdout_excerpt=_excerpt(getattr(exc, "stdout", "")),
        stderr_excerpt=_excerpt(getattr(exc, "stderr", "")),
        error_code="CLAUDE_CODE_TIMEOUT",
        blocking_reason="claude_code execution timed out",
        summary="claude_code execution timed out",
    )


def _start_failed_payload(request_payload: dict[str, Any], *, exc: OSError) -> dict[str, Any]:
    return _build_result_payload(
        request_payload,
        status="FAIL",
        changed_files=[],
        error_code="CLAUDE_CODE_START_FAILED",
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
        guard_decision=_guard_decision_payload(decision),
    )
    payload["guard_action"] = decision.action
    payload["guard_policy"] = decision.policy
    payload["guard_pattern"] = decision.pattern
    payload["guard_command"] = str(blocked_command or "").strip()
    return payload


def _home_path_needles(home_path: str) -> list[str]:
    """Produce stderr substrings to check against a Windows home path.

    Node's error stringify doubles backslashes (`C:\\\\Users\\\\x`). We
    also try single-backslash and forward-slash forms for robustness.
    """
    if not home_path:
        return []
    norm = home_path.replace("/", "\\")
    variants = {
        norm.replace("\\", "\\\\"),
        norm,
        norm.replace("\\", "/"),
    }
    return [v for v in variants if v]


def _classify_cli_failure(
    stderr: str,
    stdout: str,
    *,
    home_path: str | None = None,
) -> str:
    """Classify failure type from CLI output.

    Recognises Python exception text, Node errno codes (the Claude CLI is
    a Node process — on Windows its filesystem errors surface as
    `code: 'EPERM'` on `lstat`/`open`, not as Python PermissionError), and
    HTTP auth responses.

    When `home_path` is given and the stderr cites a lstat failure whose
    `path:` field matches that exact home path, the result is upgraded
    from the generic ``permission_error`` to ``home_access_error`` so
    operators can distinguish "Claude can't see user home" from any
    other EPERM (e.g. on a repo file or worktree directory). Without
    this precision the same hint would be offered for unrelated lstat
    failures inside the execution workspace.
    """
    combined = (stderr or "") + (stdout or "")
    combined_l = combined.lower()

    if any(p in combined_l for p in ("importerror", "modulenotfounderror")):
        return "import_error"
    if any(p in combined_l for p in ("syntaxerror", "invalid syntax")):
        return "syntax_error"
    if home_path and "lstat" in combined_l:
        for needle in _home_path_needles(home_path):
            if needle and needle in combined:
                return "home_access_error"
    if any(
        p in combined_l
        for p in (
            "permissionerror",
            "permission denied",
            "eperm",
            "eacces",
            "operation not permitted",
        )
    ):
        return "permission_error"
    if any(
        p in combined_l
        for p in ("filenotfounderror", "no such file", "enoent")
    ):
        return "file_not_found"
    if any(p in combined_l for p in ("authentication", "unauthorized", "401", "403")):
        return "auth_error"
    if any(p in combined_l for p in ("traceback", "exception", "error:")):
        return "runtime_error"
    return "unknown_error"


def _failure_payload(
    request_payload: dict[str, Any],
    *,
    completed: subprocess.CompletedProcess[str],
    changed_files: list[str],
    home_path: str | None = None,
) -> dict[str, Any]:
    message = _excerpt(completed.stderr) or _excerpt(completed.stdout) or "claude_code failed"
    failure_type = _classify_cli_failure(
        completed.stderr, completed.stdout, home_path=home_path
    )
    payload = _build_result_payload(
        request_payload,
        status="FAIL",
        changed_files=changed_files,
        stdout_excerpt=_excerpt(completed.stdout),
        stderr_excerpt=_excerpt(completed.stderr),
        returncode=int(completed.returncode),
        artifacts=changed_files,
        error_code="CLAUDE_CODE_FAILED",
        blocking_reason=message,
        summary=message,
    )
    payload["cli_failure_type"] = failure_type
    if failure_type == "home_access_error":
        payload["remediation"] = _home_remediation_hints(home=home_path or "")
    return payload


def _home_inaccessible_payload(
    request_payload: dict[str, Any],
    *,
    home_probe: dict[str, Any],
) -> dict[str, Any]:
    home = str(home_probe.get("home") or "").strip() or "(unknown)"
    error = str(home_probe.get("error") or "").strip() or "access denied"
    message = (
        f"Claude CLI cannot lstat Windows user home ({home}): {error}. "
        "Subprocess launch was skipped."
    )
    payload = _build_result_payload(
        request_payload,
        status="BLOCKED",
        changed_files=[],
        error_code="CLAUDE_CODE_HOME_INACCESSIBLE",
        blocking_reason=message,
        summary=message,
    )
    remediation = list(home_probe.get("remediation") or ())
    if remediation:
        payload["remediation"] = remediation
    return payload


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
        error_code="CLAUDE_CODE_CHANGED_FILES_MISSING",
        blocking_reason="claude_code execution completed without deterministic changed files",
        summary="claude_code did not produce deterministic changed files",
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
        summary="claude_code completed with deterministic changed files",
    )


def _resolved_executable(config: Any) -> str:
    configured = str(getattr(config, "executable", "") or "").strip()
    executable = configured or "claude"
    candidate = Path(executable)
    if candidate.exists():
        return str(candidate)
    resolved = shutil.which(executable)
    if resolved:
        return str(Path(resolved))
    windows_resolved = _resolve_windows_executable(executable)
    if windows_resolved:
        return windows_resolved
    return executable


def _executable_available(executable: str) -> bool:
    candidate = Path(executable)
    if candidate.exists():
        return True
    if shutil.which(executable) is not None:
        return True
    return _resolve_windows_executable(executable) is not None


def _resolve_windows_executable(executable: str) -> str | None:
    if os.name != "nt":
        return None
    normalized = str(executable or "").strip()
    if not normalized:
        return None
    candidate = Path(normalized)
    if candidate.exists():
        return str(candidate)
    candidate_names = [normalized]
    if not candidate.suffix:
        candidate_names.extend(
            f"{normalized}{suffix}"
            for suffix in _windows_pathext_suffixes()
        )
    search_paths = [part.strip('"') for part in os.environ.get("PATH", "").split(os.pathsep) if part.strip()]
    for directory in search_paths:
        base = Path(directory)
        for name in candidate_names:
            resolved = base / name
            if resolved.exists():
                return str(resolved)
    return None


def _backend_runtime_home(*, execution_root: Path) -> Path:
    runtime_home = (execution_root / ".workflow_runtime" / "claude_code" / "home").resolve()
    runtime_home.mkdir(parents=True, exist_ok=True)
    return runtime_home


def _prepare_windows_claude_executable(*, executable: str, execution_root: Path) -> str:
    """Materialize a workspace-local Claude launcher on Windows when possible.

    Some Windows PATH setups resolve `claude` via a system stub that forwards
    back into `%APPDATA%\\npm`, which can trigger `EPERM` before Claude's own
    child env overrides apply. Copying the installed package into the isolated
    workspace gives Node an accessible main-module path.
    """
    if os.name != "nt":
        return executable
    candidate = Path(str(executable or "").strip())
    if candidate.is_absolute():
        normalized = str(candidate).replace("/", "\\").lower()
        known_shim_roots = (
            "\\appdata\\roaming\\npm\\",
            "\\appdata\\local\\npm\\",
            "\\program files\\nodejs\\",
        )
        if not any(marker in normalized for marker in known_shim_roots):
            return executable
    package_root = _resolve_windows_claude_package_root(executable=executable)
    if package_root is None:
        return executable
    launcher_root = (execution_root / ".workflow_runtime" / "claude_code" / "launcher").resolve()
    copied_package = launcher_root / "claude-code"
    cli_js = copied_package / "cli.js"
    wrapper_path = launcher_root / "claude.cmd"
    try:
        launcher_root.mkdir(parents=True, exist_ok=True)
        if not cli_js.exists():
            shutil.copytree(package_root, copied_package, dirs_exist_ok=True)
        wrapper_path.write_text(
            "@echo off\r\n"
            "SETLOCAL\r\n"
            "IF EXIST \"%~dp0node.exe\" (\r\n"
            "  SET \"_prog=%~dp0node.exe\"\r\n"
            ") ELSE (\r\n"
            "  SET \"_prog=node\"\r\n"
            ")\r\n"
            "endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "
            "\"%_prog%\" \"%~dp0claude-code\\cli.js\" %*\r\n",
            encoding="utf-8",
        )
    except OSError:
        logger.debug("failed to materialize workspace-local Claude launcher", exc_info=True)
        return executable
    return str(wrapper_path)


def _resolve_windows_claude_package_root(*, executable: str) -> Path | None:
    if os.name != "nt":
        return None
    name = Path(str(executable or "").strip()).name.lower()
    if not name.startswith("claude"):
        return None
    candidates: list[Path] = []
    appdata = str(os.environ.get("APPDATA") or "").strip()
    if appdata:
        candidates.append(Path(appdata) / "npm" / "node_modules" / "@anthropic-ai" / "claude-code")
    home = _resolve_windows_home()
    if home:
        candidates.append(Path(home) / "AppData" / "Roaming" / "npm" / "node_modules" / "@anthropic-ai" / "claude-code")
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate).lower()
        if key in seen:
            continue
        seen.add(key)
        if (candidate / "cli.js").exists():
            return candidate
    return None


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


_CLAUDE_AUTH_MODE_ENV = "WORKFLOW_CLAUDE_AUTH_MODE"
# host     = copy auth from real user claude home into isolated runtime home (default, suits local dev)
# isolated = do not copy auth; claude starts with no credentials (suits CI/sandbox)


def _claude_auth_mode() -> str:
    raw = os.environ.get(_CLAUDE_AUTH_MODE_ENV, "host").strip().lower()
    return raw if raw in {"host", "isolated"} else "host"


def _sync_claude_auth_to_isolated(runtime_home: Path, *, parent_env: Mapping[str, str]) -> None:
    """Copy Claude CLI credentials + config from real user home into the isolated runtime home.

    The executor overrides USERPROFILE/HOME with an empty isolated directory, so claude
    starts without credentials and fails with "Not logged in · Please run /login".
    Controlled by WORKFLOW_CLAUDE_AUTH_MODE=host (default) or isolated.
    host     — reuses the host machine's claude login (subscription or API key).
    isolated — skips copy; claude starts with no credentials (CI/sandbox use case).

    Resolves the real home from the pre-override parent env so the USERPROFILE we set
    below does not redirect the lookup back into the empty isolated dir.
    """
    parent_home_raw = (
        parent_env.get("USERPROFILE")
        or parent_env.get("HOME")
        or ""
    ).strip()
    candidates: list[Path] = []
    if parent_home_raw:
        candidates.append(Path(parent_home_raw))
    try:
        candidates.append(Path.home())
    except (RuntimeError, OSError):
        pass
    seen: set[str] = set()
    for source_home in candidates:
        key = str(source_home).lower()
        if key in seen:
            continue
        seen.add(key)
        # ~/.claude/.credentials.json — auth token; ~/.claude.json — client config/state.
        # Use mtime-aware sync so a token rotation on the host actually
        # propagates instead of being silently held stale (was a recurrent
        # 401 / "Not logged in" source under the old ``if target.exists():
        # return`` shortcut).
        sync_file_mtime_aware(
            source_home / ".claude" / ".credentials.json",
            runtime_home / ".claude" / ".credentials.json",
            label="claude_executor_credentials",
        )
        sync_file_mtime_aware(
            source_home / ".claude.json",
            runtime_home / ".claude.json",
            label="claude_executor_state",
        )


def _clean_child_env(*, execution_root: Path) -> dict[str, str]:
    """Return a copy of the current env with vars that block nested Claude sessions removed."""
    env = dict(os.environ)
    for key in ("CLAUDECODE", "CLAUDE_CODE_SESSION"):
        env.pop(key, None)
    runtime_home = _backend_runtime_home(execution_root=execution_root)
    if _claude_auth_mode() == "host":
        _sync_claude_auth_to_isolated(runtime_home, parent_env=env)
    _apply_runtime_home_env(env, runtime_home=runtime_home)
    env["CLAUDE_HOME"] = str((runtime_home / ".claude").resolve())
    # Force UTF-8 on Windows to prevent GBK decode errors reading subprocess output
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def _resolve_windows_home(*, env: Mapping[str, str] | None = None) -> str:
    """Resolve the Windows user-home path as seen by a child process.

    Prefers ``USERPROFILE`` (absolute on modern Windows). Falls back to
    ``HOMEDRIVE + HOMEPATH`` because ``HOMEPATH`` alone is typically a
    relative path (e.g. ``\\Users\\liafei``) and unusable on its own.
    Returns the empty string when neither is set.
    """
    source = env if env is not None else os.environ
    userprofile = str(source.get("USERPROFILE") or "").strip()
    if userprofile:
        return userprofile
    drive = str(source.get("HOMEDRIVE") or "").strip()
    path = str(source.get("HOMEPATH") or "").strip()
    if drive and path:
        return drive + path
    return ""


def _home_remediation_hints(*, home: str) -> list[str]:
    shown_home = home or "%USERPROFILE%"
    return [
        (
            "Check Windows Controlled Folder Access (Virus & threat protection "
            f"> Ransomware protection): it can deny Node processes `lstat` on {shown_home}."
        ),
        f"Verify `{shown_home}` exists and the current user has read access.",
        (
            "Reproduce directly in a fresh shell: "
            "node -e \"require('fs').lstatSync(process.env.USERPROFILE)\""
        ),
    ]


def _probe_node_realpath_for_home(*, home: str, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Run a lightweight Node probe that matches Claude CLI startup behavior."""
    node_executable = shutil.which("node")
    if not node_executable:
        return {"status": "skipped", "reason": "node_unavailable"}
    probe_env = dict(os.environ)
    if env is not None:
        probe_env.update({str(key): str(value) for key, value in env.items()})
    probe_env["USERPROFILE"] = str(home)
    try:
        completed = subprocess.run(
            [node_executable, "-e", "require('fs').realpathSync(process.env.USERPROFILE)"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=probe_env,
            timeout=10,
        )
    except subprocess.TimeoutExpired as exc:
        message = _excerpt(getattr(exc, "stderr", "")) or _excerpt(getattr(exc, "stdout", "")) or "node probe timeout"
        return {
            "status": "blocked",
            "home": str(home),
            "error": f"node_home_probe_timeout: {message}",
            "remediation": _home_remediation_hints(home=str(home)),
        }
    except OSError as exc:
        return {
            "status": "blocked",
            "home": str(home),
            "error": f"node_home_probe_start_failed: {exc}",
            "remediation": _home_remediation_hints(home=str(home)),
        }
    if int(getattr(completed, "returncode", 1)) != 0:
        message = _excerpt(completed.stderr) or _excerpt(completed.stdout) or f"node_home_probe_failed_exit_{completed.returncode}"
        return {
            "status": "blocked",
            "home": str(home),
            "error": message,
            "remediation": _home_remediation_hints(home=str(home)),
        }
    return {"status": "ready", "home": str(home)}


def _probe_home_accessibility(*, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Probe whether the child Claude CLI will be able to `lstat` user home.

    Windows-only: Node 20.x fails in ``run_main_module`` with EPERM when the
    home path is protected by Controlled Folder Access or similar ACLs,
    before any cli.js code runs. Probing from our side lets us skip a
    guaranteed-to-fail subprocess and deliver a structured remediation
    hint instead.

    Non-Windows platforms return ``{"status": "skipped"}`` — they do not
    exhibit this failure mode and probing would just add syscall noise.
    """
    if os.name != "nt":
        return {"status": "skipped", "reason": "non_windows"}
    home = _resolve_windows_home(env=env)
    if not home:
        return {
            "status": "blocked",
            "home": "",
            "error": "home_env_missing",
            "remediation": _home_remediation_hints(home=""),
        }
    try:
        os.lstat(home)
    except FileNotFoundError as exc:
        return {
            "status": "blocked",
            "home": home,
            "error": f"FileNotFoundError: {exc}",
            "remediation": _home_remediation_hints(home=home),
        }
    except PermissionError as exc:
        return {
            "status": "blocked",
            "home": home,
            "error": f"PermissionError: {exc}",
            "remediation": _home_remediation_hints(home=home),
        }
    except OSError as exc:
        return {
            "status": "blocked",
            "home": home,
            "error": f"{type(exc).__name__}: {exc}",
            "remediation": _home_remediation_hints(home=home),
        }
    node_probe = _probe_node_realpath_for_home(home=home, env=env)
    if str(node_probe.get("status") or "").strip() == "blocked":
        return node_probe
    ready_payload = {"status": "ready", "home": home}
    if str(node_probe.get("status") or "").strip() == "skipped":
        ready_payload["node_probe"] = node_probe
    return ready_payload


def _windows_pathext_suffixes() -> list[str]:
    raw = os.environ.get("PATHEXT", "")
    suffixes = [suffix.strip() for suffix in raw.split(os.pathsep) if suffix.strip()]
    if suffixes:
        return suffixes
    return [".COM", ".EXE", ".BAT", ".CMD"]


def _sanitize_model_for_cli(value: str) -> str:
    """Guard against values that could be misinterpreted as CLI flags."""
    if not value:
        return ""
    if value.startswith("-") or len(value) > 200 or any(ord(c) < 32 for c in value):
        return ""
    return value


def _claude_command(*, executable: str, model: str = "") -> list[str]:
    cmd = [
        executable,
        "-p",
        "--permission-mode",
        "bypassPermissions",
        "--dangerously-skip-permissions",
    ]
    model = _sanitize_model_for_cli(model)
    if model:
        cmd.extend(["--model", model])
    return cmd


def _request_prompt(request_payload: dict[str, Any]) -> str:
    files = ", ".join(str(item) for item in list(request_payload.get("files_to_change") or []) if str(item).strip())
    invariants = "\n".join(
        f"- {item}" for item in list(request_payload.get("invariants") or []) if str(item).strip()
    )
    task_scope = str(request_payload.get("task_scope") or "").strip()
    verify_cmd = str(request_payload.get("verify_cmd") or "").strip()
    task_id = str(request_payload.get("task_id") or "").strip()
    request_path = str(request_payload.get("request_path") or "").strip()
    execution_root = str(request_payload.get("execution_root") or "").strip()
    host_probe = dict(request_payload.get("host_probe") or {})
    lines = [
        f"Implement task: {str(request_payload.get('task') or '').strip()}",
        f"Feature: {str(request_payload.get('feature') or '').strip()}",
        f"Task ID: {task_id or '(unknown)'}",
        f"Allowed files: {files or '(none supplied)'}",
        (
            "Host probe: "
            f"status={str(host_probe.get('status') or '').strip() or 'unknown'}; "
            f"surface={str(host_probe.get('surface') or '').strip() or 'unknown'}; "
            f"reason={str(host_probe.get('reason') or '').strip() or '(none)'}"
        ),
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
    if execution_root:
        lines.append(f"Execution workspace: {execution_root}")
    if invariants:
        lines.extend(["Invariants:", invariants])
    if verify_cmd:
        lines.append(f"Verify command expectation: {verify_expectation_text(verify_cmd)}")
    compact_lines = _kernel_compact_prompt_lines(request_payload.get("kernel_compact_context"))
    lines.extend(compact_lines)
    lines.append("Modify only the allowed files and leave the workspace in a runnable state.")
    if execution_root:
        lines.append(
            f"When all changes are complete and local verification passes, write the file "
            f"{execution_root}/.execution_sentinel.json with content "
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
    execution_root: Path,
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
        "{project_root}": str(execution_root),
        "{source_project_root}": str(project_root),
        "{execution_root}": str(execution_root),
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


def _planning_dir(request_payload: dict[str, Any], *, project_root: Path) -> Path:
    planning_dir_raw = str(request_payload.get("planning_dir") or "").strip()
    if planning_dir_raw:
        candidate = Path(planning_dir_raw).resolve()
        if candidate.is_relative_to(project_root.resolve()):
            return candidate
        logger.warning("planning_dir %s is outside project_root %s; falling back to default", candidate, project_root)
    return (project_root / "planning").resolve()


def _prepare_isolation_workspace(
    *,
    planning_dir: Path,
    project_root: Path,
    allowed_files: list[str],  # noqa: ARG001 - kept for backward-compatible signature
    request_payload: dict[str, Any],
) -> Path:
    from kodawari.autopilot.execution.execution_isolation import prepare_isolation_workspace
    return prepare_isolation_workspace(
        planning_dir=planning_dir,
        project_root=project_root,
        backend_name="claude_code",
        request_payload=request_payload,
    )


def _sync_isolated_workspace_to_project_root(
    *,
    project_root: Path,
    execution_root: Path,
    allowed_files: list[str],
) -> None:
    from kodawari.autopilot.execution.execution_isolation import sync_isolated_workspace_to_project_root
    sync_isolated_workspace_to_project_root(
        project_root=project_root,
        execution_root=execution_root,
        allowed_files=allowed_files,
    )


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


def _load_kernel_compact_context(request_payload: dict[str, Any]) -> dict[str, Any]:
    planning_dir_raw = str(request_payload.get("planning_dir") or "").strip()
    if not planning_dir_raw:
        return {}
    path = (Path(planning_dir_raw).resolve() / "semantic_compact.json").resolve()
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        logger.warning("claude_code backend failed to parse semantic compact: %s", path, exc_info=True)
        return {}
    if not isinstance(payload, dict):
        return {"_loaded": True}
    compact: dict[str, Any] = {"_loaded": True}
    for field in _COMPACT_CONTEXT_FIELDS:
        values = _compact_context_values(field, payload.get(field))
        if values:
            compact[field] = values
    return compact


def _compact_context_values(field: str, raw: Any) -> list[str]:
    if field == "decisions":
        return _decision_values(raw)
    if field == "recent_errors":
        return _recent_error_values(raw)
    return _string_values(raw)


def _string_values(raw: Any, *, limit: int = 5) -> list[str]:
    values = list(raw) if isinstance(raw, list) else [raw]
    normalized: list[str] = []
    for item in values:
        text = str(item or "").strip()
        if text:
            normalized.append(text)
        if len(normalized) >= limit:
            break
    return normalized


def _decision_values(raw: Any) -> list[str]:
    decisions = list(raw) if isinstance(raw, list) else [raw]
    normalized: list[str] = []
    for item in decisions:
        if isinstance(item, dict):
            decision = str(item.get("decision") or item.get("id") or "").strip()
            rationale = str(item.get("rationale") or "").strip()
            constraints = _string_values(item.get("constraints"), limit=3)
            text = decision or rationale
            if decision and rationale and rationale != decision:
                text = f"{decision} (rationale: {rationale})"
            elif not decision and rationale:
                text = f"rationale: {rationale}"
            if constraints:
                detail = ", ".join(constraints)
                text = f"{text}; constraints: {detail}" if text else f"constraints: {detail}"
        else:
            text = str(item or "").strip()
        if not text:
            continue
        normalized.append(text)
        if len(normalized) >= 5:
            break
    return normalized


def _recent_error_values(raw: Any) -> list[str]:
    errors = list(raw) if isinstance(raw, list) else [raw]
    normalized: list[str] = []
    for item in errors:
        if isinstance(item, dict):
            category = str(item.get("category") or "").strip()
            phase = str(item.get("phase") or "").strip()
            message = str(item.get("message") or "").strip()
            prefix_items = [part for part in (category, phase) if part]
            prefix = f"[{'/'.join(prefix_items)}] " if prefix_items else ""
            text = f"{prefix}{message}".strip()
        else:
            text = str(item or "").strip()
        if not text:
            continue
        normalized.append(text)
        if len(normalized) >= 5:
            break
    return normalized


def _kernel_compact_prompt_lines(compact_context: Any) -> list[str]:
    if not isinstance(compact_context, dict):
        return []
    lines = [
        "Kernel-level compact context injection (from planning_dir/semantic_compact.json, not native host memory):"
    ]
    added = False
    for field in _COMPACT_CONTEXT_FIELDS:
        values = _string_values(compact_context.get(field))
        if not values:
            continue
        added = True
        lines.append(f"{field}:")
        lines.extend(f"- {item}" for item in values)
    if added:
        return lines
    if bool(compact_context.get("_loaded")):
        lines.append("- semantic_compact.json is present, but key compact fields are empty.")
        return lines
    return []


def _guard_decision_payload(decision: GuardDecision) -> dict[str, Any]:
    return {
        "action": decision.action,
        "reason": decision.message,
        "policy": decision.policy,
        "pattern": decision.pattern,
    }


__all__ = ["materialize_claude_code_result"]
