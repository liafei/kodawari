"""Codex CLI reviewer backend.

Calls the locally installed ``codex`` CLI as a subprocess to perform peer
review.  Unlike the Claude CLI path in ``cli_reviewer.py``, Codex does not
support ``--output-format json``, so this module extracts JSON from the
plain-text output.

The reviewer backends coexist:
  - opus_gateway.py   -> HTTP API + API Key
  - cli_reviewer.py   -> Claude CLI + logged-in account
  - codex_reviewer.py  -> Codex CLI + logged-in account  (this file)
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from kodawari.autopilot.core.json_extractor import extract_json_object_text
from kodawari.autopilot.review.peer_review_gateway import build_review_prompt, parse_review_content
from kodawari.autopilot.core.isolated_home import sync_first_present_source
from kodawari.autopilot.core.runtime_paths import reviewer_home
from kodawari.autopilot.core.subprocess_compat import (
    subprocess_text_kwargs,
    windows_safe_command,
)


logger = logging.getLogger(__name__)


@dataclass
class CodexReviewerConfig:
    """Configuration for the Codex CLI reviewer.

    This backend only supports the ``codex`` CLI.
    """

    executable: str = "codex"
    timeout_seconds: int = 180
    retry_attempts: int = 1
    model: str = ""


def request_codex_review(
    config: CodexReviewerConfig,
    *,
    task: str,
    context: dict[str, Any],
    changed_files: list[str],
    review_iteration: int,
    review_bundle: dict[str, Any] | None = None,
    project_root: Path | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """Request a code review via a locally installed Codex CLI.

    Returns ``(payload, "")`` on success or ``(None, error_message)`` on failure.
    """
    prompt = build_review_prompt(
        task=task,
        context=context,
        changed_files=changed_files,
        review_iteration=review_iteration,
        review_bundle=review_bundle,
        reviewer_capability="local_repo_read",
    )
    executable = _resolved_executable(config)
    if not _is_codex_executable(executable):
        return None, f"codex reviewer only supports codex CLI, got: {executable}"
    if not _executable_available(executable):
        return None, f"codex reviewer executable not found: {executable}"

    retries = max(1, int(config.retry_attempts or 1))
    errors: list[str] = []
    for _ in range(retries):
        payload, error = _run_codex_review(config, executable=executable, prompt=prompt, project_root=project_root)
        if payload is not None:
            return payload, ""
        errors.append(error)
        if not _is_retryable_error(error):
            break
    return None, errors[-1] if errors else "unknown error"


def codex_reviewer_available(config: CodexReviewerConfig | None = None) -> bool:
    """Check whether the Codex CLI executable is reachable and compatible."""
    resolved = config or CodexReviewerConfig()
    executable = _resolved_executable(resolved)
    if not _is_codex_executable(executable):
        return False
    return _executable_available(executable)


def _is_codex_executable(executable: str) -> bool:
    """Return True if the executable name looks like a codex CLI binary."""
    name = Path(executable).stem.lower()
    return name == "codex" or name.startswith("codex-") or name.startswith("codex_")


def _run_codex_review(
    config: CodexReviewerConfig,
    *,
    executable: str,
    prompt: str,
    project_root: Path | None = None,
) -> tuple[dict[str, Any] | None, str]:
    command = _build_codex_review_command(executable=executable, model=str(config.model or "").strip())
    cwd = str(project_root.resolve()) if project_root is not None else None
    child_env = _reviewer_child_env(project_root=project_root)
    try:
        run_kwargs = subprocess_text_kwargs(
            input=prompt,
            timeout=max(30, int(config.timeout_seconds or 180)),
            cwd=cwd,
            env=child_env,
        )
        completed = subprocess.run(command, **run_kwargs)
    except subprocess.TimeoutExpired:
        return None, "codex reviewer timed out"
    except OSError as exc:
        return None, f"codex reviewer failed to start: {exc}"

    if completed.returncode != 0:
        stderr_excerpt = _excerpt(completed.stderr)
        return None, f"codex reviewer exited with code {completed.returncode}: {stderr_excerpt}"

    content = _extract_codex_content(completed.stdout)
    if not content:
        return None, "codex reviewer returned empty output"

    return parse_review_content(content, fallback_error="codex reviewer response missing review json")


def _build_codex_review_command(*, executable: str, model: str = "") -> list[str]:
    """Build codex CLI command.  Prompt is passed via stdin.

    ``--sandbox read-only`` ensures the reviewer process cannot write files,
    install packages, or run network requests — it may only read the workspace.
    """
    cmd = windows_safe_command(
        executable,
        "exec",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
    )
    model = _sanitize_model_for_cli(model)
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


def _extract_codex_content(stdout: str) -> str:
    """Extract review JSON from Codex CLI plain-text output.

    Codex does not support ``--output-format json``, so the review JSON may
    be embedded in conversational text.  Extraction strategy:
      1. Try the entire output as JSON.
      2. Try extracting from a fenced ``json`` code block.
      3. Find the outermost ``{ ... }`` and return that substring.
      4. Return raw text as fallback (downstream will attempt its own parse).
    """
    text = str(stdout or "").strip()
    if not text:
        return ""
    extracted = extract_json_object_text(text)
    return extracted or text


_FENCED_JSON_RE = re.compile(r"```json\s*\n(.*?)\n\s*```", re.DOTALL)


def _extract_fenced_json(text: str) -> str:
    """Extract content from the first ```json ... ``` block."""
    match = _FENCED_JSON_RE.search(text)
    if match:
        return match.group(1).strip()
    return ""


def _is_json_object(text: str) -> bool:
    try:
        return isinstance(json.loads(text), dict)
    except (json.JSONDecodeError, ValueError):
        return False


def _resolved_executable(config: CodexReviewerConfig) -> str:
    configured = str(config.executable or "").strip()
    executable = configured or "codex"
    if Path(executable).exists():
        return str(Path(executable))
    resolved = shutil.which(executable)
    if resolved:
        return str(Path(resolved))
    # Windows: npm 全局包不一定在子进程 PATH 里，主动探测常见安装位置
    if os.name == "nt":
        candidate = _windows_npm_find(executable)
        if candidate:
            return candidate
    return executable


def _windows_npm_find(name: str) -> str | None:
    """在 Windows 常见 npm 全局目录里查找可执行文件。"""
    appdata = os.environ.get("APPDATA", "")
    search_dirs = []
    if appdata:
        search_dirs.append(Path(appdata) / "npm")
    # nvm / volta / fnm 等版本管理器的常见路径
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


def _reviewer_workspace_root(project_root: Path | None) -> Path:
    if project_root is not None:
        return project_root.resolve()
    return Path.cwd().resolve()


def _reviewer_home(project_root: Path | None) -> Path:
    configured = str(os.environ.get("WORKFLOW_REVIEWER_CODEX_HOME") or "").strip()
    if configured:
        return Path(configured).resolve()
    return reviewer_home(_reviewer_workspace_root(project_root), "codex")


_CODEX_AUTH_MODE_ENV = "WORKFLOW_CODEX_AUTH_MODE"


def _codex_auth_mode() -> str:
    raw = os.environ.get(_CODEX_AUTH_MODE_ENV, "host").strip().lower()
    return raw if raw in {"host", "isolated"} else "host"


def _sync_codex_auth_to_isolated(isolated_codex_home: Path, *, parent_env: dict[str, str]) -> None:
    """Copy auth.json from the real user codex home into the reviewer's isolated home.

    Mirrors the executor-side fix in execution_codex_cli.py: the reviewer
    overrides CODEX_HOME to a per-project workspace dir, so codex starts
    without credentials and review-time API calls return 401 unless we
    pre-seed auth.json from the parent (host) codex home.

    Uses the shared mtime-aware sync primitive (token rotations on the host
    propagate into the isolated copy instead of being silently held stale).
    """
    target = isolated_codex_home / "auth.json"
    parent_codex_home_raw = parent_env.get("CODEX_HOME", "")
    candidate_dirs: list[Path] = []
    if parent_codex_home_raw:
        candidate_dirs.append(Path(parent_codex_home_raw))
    candidate_dirs.append(Path.home() / ".codex")
    sync_first_present_source(
        target=target,
        source_candidates=[d / "auth.json" for d in candidate_dirs],
        label="codex_reviewer_auth",
    )


def _reviewer_child_env(*, project_root: Path | None) -> dict[str, str]:
    env = dict(os.environ)
    reviewer_home = _reviewer_home(project_root)
    tmp_dir = (reviewer_home / "tmp").resolve()
    reviewer_home.mkdir(parents=True, exist_ok=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    if _codex_auth_mode() == "host":
        _sync_codex_auth_to_isolated(reviewer_home, parent_env=env)
    env["CODEX_HOME"] = str(reviewer_home)
    env["HOME"] = str(reviewer_home)
    env["USERPROFILE"] = str(reviewer_home)
    env["TMP"] = str(tmp_dir)
    env["TEMP"] = str(tmp_dir)
    return env


def _executable_available(executable: str) -> bool:
    if Path(executable).exists():
        return True
    return shutil.which(executable) is not None


def _is_retryable_error(error: str) -> bool:
    text = str(error or "").strip().lower()
    return "timeout" in text or "timed out" in text


def _excerpt(value: Any, *, max_lines: int = 8) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return "\n".join(text.splitlines()[-max_lines:]).strip()


__all__ = [
    "CodexReviewerConfig",
    "codex_reviewer_available",
    "request_codex_review",
]

