"""CLI-based reviewer backend that calls a locally logged-in Claude CLI.

This module provides an alternative to the HTTP API gateway in opus_gateway.py.
Instead of requiring an API key, it invokes the ``claude`` CLI as a subprocess,
relying on the user's existing login session.

The two reviewer backends coexist:
  - opus_gateway.py  -> HTTP API + API Key
  - cli_reviewer.py  -> local CLI + logged-in account  (this file)
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any

from kodawari.autopilot.review.peer_review_gateway import (
    build_review_prompt,
    parse_review_content,
)
from kodawari.autopilot.review.bundle import validate_peer_review_response
from kodawari.autopilot.review_runtime_policy import REAL_REVIEW_MODES
from kodawari.autopilot.core.isolated_home import sync_file_mtime_aware
from kodawari.autopilot.core.runtime_paths import reviewer_home
from kodawari.autopilot.core.subprocess_compat import (
    subprocess_text_kwargs,
    windows_safe_command,
)


logger = logging.getLogger(__name__)
"""``REAL_REVIEW_MODES`` is re-exported from ``review_runtime_policy`` —
single source of truth. Historic callers that imported from this module
or from ``gateways.cli`` keep working via the re-export below."""


@dataclass
class CliReviewerConfig:
    """Configuration for the Claude CLI reviewer backend.

    This backend only supports the ``claude`` CLI. Codex CLI uses a different
    command interface and is not supported here.
    """
    executable: str = "claude"
    timeout_seconds: int = 120
    max_tokens: int = 4096
    retry_attempts: int = 1
    model: str = ""


def request_cli_review(
    config: CliReviewerConfig,
    *,
    task: str,
    context: dict[str, Any],
    changed_files: list[str],
    review_iteration: int,
    review_bundle: dict[str, Any] | None = None,
    project_root: Path | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """Request a code review via a locally installed CLI (no API key needed).

    Returns ``(payload, "")`` on success or ``(None, error_message)`` on failure.
    """
    prompt = build_review_prompt(
        task=task,
        context=context,
        changed_files=changed_files,
        review_iteration=review_iteration,
        review_bundle=review_bundle,
        reviewer_capability="bundle_only",
    )
    executable = _resolved_executable(config)
    if not _is_claude_executable(executable):
        return None, f"cli reviewer only supports claude CLI, got: {executable}"
    if not _executable_available(executable):
        return None, f"cli reviewer executable not found: {executable}"

    retries = max(1, int(config.retry_attempts or 1))
    errors: list[str] = []
    for _ in range(retries):
        payload, error = _run_cli_review(config, executable=executable, prompt=prompt, project_root=project_root)
        if payload is not None:
            return payload, ""
        errors.append(error)
        if not _is_retryable_error(error):
            break
    return None, errors[-1] if errors else "unknown error"


def cli_reviewer_available(config: CliReviewerConfig | None = None) -> bool:
    """Check whether the CLI reviewer executable is reachable and compatible.

    Only the ``claude`` CLI is supported. Other executables (e.g. ``codex``)
    use incompatible command-line arguments and will be rejected.
    """
    resolved = config or CliReviewerConfig()
    executable = _resolved_executable(resolved)
    if not _is_claude_executable(executable):
        return False
    return _executable_available(executable)


def _is_claude_executable(executable: str) -> bool:
    """Return True if the executable name looks like a claude CLI binary."""
    name = Path(executable).stem.lower()
    return name == "claude" or name.startswith("claude-") or name.startswith("claude_")


def _run_cli_review(
    config: CliReviewerConfig,
    *,
    executable: str,
    prompt: str,
    project_root: Path | None = None,
) -> tuple[dict[str, Any] | None, str]:
    command = _build_command(executable=executable, config=config)
    cwd = str(project_root.resolve()) if project_root is not None else None
    child_env = _reviewer_child_env(project_root=project_root)
    try:
        run_kwargs = subprocess_text_kwargs(
            input=prompt,
            timeout=max(30, int(config.timeout_seconds or 120)),
            cwd=cwd,
            env=child_env,
        )
        completed = subprocess.run(command, **run_kwargs)
    except subprocess.TimeoutExpired:
        return None, "cli reviewer timed out"
    except OSError as exc:
        return None, f"cli reviewer failed to start: {exc}"

    if completed.returncode != 0:
        stderr_excerpt = _excerpt(completed.stderr)
        stdout_excerpt = _excerpt(completed.stdout)
        logger.warning("cli reviewer failed stdout: %s", stdout_excerpt)
        return None, f"cli reviewer exited with code {completed.returncode}: {stderr_excerpt or stdout_excerpt}"

    content = _extract_content(completed.stdout)
    if not content:
        return None, "cli reviewer returned empty output"

    return parse_review_content(content, fallback_error="cli reviewer response missing review json")


def _build_command(
    *,
    executable: str,
    config: CliReviewerConfig,
) -> list[str]:
    """Build claude CLI command. Prompt is passed via stdin, not as argument."""
    cmd = windows_safe_command(
        executable,
        "-p",
        "--output-format", "json",
        "--max-turns", "1",
    )
    model = _sanitize_model_for_cli(str(config.model or "").strip())
    if model:
        cmd.extend(["--model", model])
    return cmd


def _sanitize_model_for_cli(value: str) -> str:
    """Guard against values that could be misinterpreted as CLI flags."""
    if not value:
        return ""
    if value.startswith("-"):
        return ""
    if len(value) > 200:
        return ""
    if any(ord(c) < 32 for c in value):
        return ""
    return value


def _extract_content(stdout: str) -> str:
    """Extract the review text from CLI stdout.

    The ``claude --output-format json`` mode wraps output in a JSON envelope
    with a ``result`` field.  We try to unwrap that first; if the output is
    not in envelope form we return it as-is for downstream JSON parsing.
    """
    text = str(stdout or "").strip()
    if not text:
        return ""
    try:
        envelope = json.loads(text)
        if isinstance(envelope, dict):
            result = envelope.get("result") or envelope.get("content") or envelope.get("text")
            if isinstance(result, str):
                return result
            if isinstance(result, list):
                parts = [
                    str(item.get("text") or "")
                    for item in result
                    if isinstance(item, dict) and str(item.get("type") or "").lower() == "text"
                ]
                joined = "\n".join(p for p in parts if p)
                if joined:
                    return joined
            return text
    except (json.JSONDecodeError, TypeError):
        pass
    return text


def _resolved_executable(config: CliReviewerConfig) -> str:
    configured = str(config.executable or "").strip() or "claude"
    candidate = Path(configured)
    if candidate.exists():
        return str(candidate)
    resolved = shutil.which(configured)
    if resolved:
        return str(Path(resolved))
    if os.name == "nt":
        windows_resolved = _resolve_windows_executable(configured)
        if windows_resolved:
            return windows_resolved
    return configured


def _executable_available(executable: str) -> bool:
    candidate = Path(executable)
    if candidate.exists():
        return True
    if shutil.which(executable) is not None:
        return True
    if os.name == "nt":
        return _resolve_windows_executable(executable) is not None
    return False


def _resolve_windows_executable(executable: str) -> str | None:
    normalized = str(executable or "").strip()
    if not normalized:
        return None
    candidate = Path(normalized)
    if candidate.exists():
        return str(candidate)
    candidate_names = [normalized]
    if not candidate.suffix:
        pathext = os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD")
        candidate_names.extend(
            f"{normalized}{suffix.strip()}"
            for suffix in pathext.split(os.pathsep)
            if suffix.strip()
        )
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        base = Path(directory.strip('"'))
        for name in candidate_names:
            resolved = base / name
            if resolved.exists():
                return str(resolved)
    return None


def _reviewer_workspace_root(project_root: Path | None) -> Path:
    if project_root is not None:
        return project_root.resolve()
    return Path.cwd().resolve()


def _reviewer_home(project_root: Path | None) -> Path:
    configured = str(os.environ.get("WORKFLOW_REVIEWER_CLAUDE_HOME") or "").strip()
    if configured:
        return Path(configured).resolve()
    return reviewer_home(_reviewer_workspace_root(project_root), "claude")


def _sync_claude_credentials_to_reviewer_home(reviewer_home: Path, *, parent_env: dict[str, str]) -> None:
    """Sync the real user's Claude credentials into the isolated reviewer HOME.

    Claude reads two files from $HOME (which we override to reviewer_home):
      - $HOME/.claude/.credentials.json — OAuth token; without this the CLI
        exits with "Not logged in 🚫 Please run /login".
      - $HOME/.claude.json — client config / state (model preferences, recent
        sessions, etc.). Missing this can manifest as half-broken sessions.

    Both use mtime-aware sync — token rotations on the host actually propagate
    instead of being silently held stale.
    """
    parent_claude_home_raw = parent_env.get("CLAUDE_HOME", "")
    parent_user_home_raw = parent_env.get("USERPROFILE", "") or parent_env.get("HOME", "")

    # Source candidates for the .claude directory (where .credentials.json lives).
    claude_dir_candidates: list[Path] = []
    if parent_claude_home_raw:
        claude_dir_candidates.append(Path(parent_claude_home_raw))
    claude_dir_candidates.append(Path.home() / ".claude")

    # Source candidates for the user-home directory (where .claude.json lives).
    user_home_candidates: list[Path] = []
    if parent_user_home_raw:
        user_home_candidates.append(Path(parent_user_home_raw))
    user_home_candidates.append(Path.home())

    # Sync .credentials.json -> reviewer_home/.claude/.credentials.json
    for source_dir in claude_dir_candidates:
        source = source_dir / ".credentials.json"
        if source.exists():
            sync_file_mtime_aware(
                source,
                reviewer_home / ".claude" / ".credentials.json",
                label="claude_reviewer_credentials",
            )
            break

    # Sync .claude.json -> reviewer_home/.claude.json (client config/state)
    for source_dir in user_home_candidates:
        source = source_dir / ".claude.json"
        if source.exists():
            sync_file_mtime_aware(
                source,
                reviewer_home / ".claude.json",
                label="claude_reviewer_state",
            )
            break


def _reviewer_child_env(*, project_root: Path | None) -> dict[str, str]:
    env = dict(os.environ)
    reviewer_home = _reviewer_home(project_root)
    tmp_dir = (reviewer_home / "tmp").resolve()
    reviewer_home.mkdir(parents=True, exist_ok=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    _sync_claude_credentials_to_reviewer_home(reviewer_home, parent_env=env)
    env["CLAUDE_HOME"] = str(reviewer_home)
    env["HOME"] = str(reviewer_home)
    env["USERPROFILE"] = str(reviewer_home)
    env["TMP"] = str(tmp_dir)
    env["TEMP"] = str(tmp_dir)
    return env


def _is_retryable_error(error: str) -> bool:
    text = str(error or "").strip().lower()
    return "timed out" in text or "timeout" in text


def _excerpt(value: Any, *, max_lines: int = 6) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return "\n".join(text.splitlines()[-max_lines:]).strip()


# --- MCP mode -------------------------------------------------------------

def request_mcp_review(
    config: CliReviewerConfig,
    *,
    task: str,
    context: dict[str, Any],
    changed_files: list[str],
    review_iteration: int,
    review_bundle: dict[str, Any] | None = None,
    project_root: Path | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """Request a review via CLI + MCP server for structured context exchange.

    Launches a lightweight MCP server alongside the CLI so the AI model can
    pull review context via ``get_review_bundle`` and submit results via
    ``submit_review`` — instead of receiving everything in one large prompt.

    Returns ``(payload, "")`` on success or ``(None, error_message)`` on failure.
    """
    executable = _resolved_executable(config)
    if not _is_claude_executable(executable):
        return None, f"mcp reviewer only supports claude CLI, got: {executable}"
    if not _executable_available(executable):
        return None, f"mcp reviewer executable not found: {executable}"

    import shutil as _shutil
    import tempfile
    tmp_dir = Path(tempfile.mkdtemp(prefix="wf_mcp_review_"))
    try:
        return _run_mcp_review_in_tmpdir(
            config, executable=executable, tmp_dir=tmp_dir,
            task=task, context=context, changed_files=changed_files,
            review_iteration=review_iteration, review_bundle=review_bundle,
            project_root=project_root,
        )
    finally:
        try:
            _shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            logger.debug("mcp reviewer failed to clean up temp dir: %s", tmp_dir)


def _run_mcp_review_in_tmpdir(
    config: CliReviewerConfig,
    *,
    executable: str,
    tmp_dir: Path,
    task: str,
    context: dict[str, Any],
    changed_files: list[str],
    review_iteration: int,
    review_bundle: dict[str, Any] | None,
    project_root: Path | None = None,
) -> tuple[dict[str, Any] | None, str]:
    bundle_path = tmp_dir / "review_bundle.json"
    result_path = tmp_dir / "review_result.json"
    mcp_config_path = tmp_dir / "mcp_config.json"

    full_prompt = build_review_prompt(
        task=task, context=context, changed_files=changed_files,
        review_iteration=review_iteration, review_bundle=review_bundle,
        reviewer_capability="bundle_only",
    )
    bundle_payload = dict(review_bundle or {})
    bundle_payload["prompt"] = full_prompt
    bundle_payload["task"] = task
    bundle_payload["changed_files"] = list(changed_files)
    bundle_payload["review_iteration"] = review_iteration
    bundle_path.write_text(json.dumps(bundle_payload, ensure_ascii=False), encoding="utf-8")

    server_module = "kodawari.autopilot.review.mcp_review_server"
    mcp_config = {
        "mcpServers": {
            "workflow-review": {
                "command": _python_executable(),
                "args": ["-m", server_module, "--bundle-path", str(bundle_path), "--result-path", str(result_path)],
            }
        }
    }
    mcp_config_path.write_text(json.dumps(mcp_config, ensure_ascii=False), encoding="utf-8")

    mcp_prompt = (
        "You are a peer code reviewer. "
        "Your only evidence source is the review bundle retrieved via get_review_bundle. "
        "You have no filesystem access. "
        "First call the get_review_bundle tool to retrieve the review context. "
        "Then analyze the code changes and produce a structured review. "
        "Finally call the submit_review tool with your review JSON containing: "
        "approved, summary, must_fix, should_fix, blocking_items, severity, "
        "score, target_score, min_dimension_score, gate_recommendation, evidence."
    )
    command = [executable, "-p", "--output-format", "json", "--max-turns", "3", "--mcp-config", str(mcp_config_path)]
    cwd = str(project_root.resolve()) if project_root is not None else None
    child_env = _reviewer_child_env(project_root=project_root)

    try:
        completed = subprocess.run(
            command, input=mcp_prompt, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=max(60, int(config.timeout_seconds or 120)),
            cwd=cwd,
            env=child_env,
        )
    except subprocess.TimeoutExpired:
        return None, "mcp reviewer timed out"
    except OSError as exc:
        return None, f"mcp reviewer failed to start: {exc}"

    if result_path.exists():
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
            if isinstance(result, dict):
                return parse_review_content(
                    json.dumps(result, ensure_ascii=False), fallback_error="mcp review result invalid",
                )
        except (json.JSONDecodeError, OSError):
            logger.warning("mcp reviewer result file unreadable: %s", result_path)

    if completed.returncode != 0:
        stderr_excerpt = _excerpt(completed.stderr)
        return None, f"mcp reviewer exited with code {completed.returncode}: {stderr_excerpt}"

    content = _extract_content(completed.stdout)
    if not content:
        return None, "mcp reviewer returned empty output"
    return parse_review_content(content, fallback_error="mcp reviewer response missing review json")


def _python_executable() -> str:
    """Return the current Python interpreter path."""
    return sys.executable


__all__ = [
    "CliReviewerConfig",
    "cli_reviewer_available",
    "request_cli_review",
    "request_mcp_review",
]

