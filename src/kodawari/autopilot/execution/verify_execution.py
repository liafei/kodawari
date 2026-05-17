"""Command-backed verify helpers for the merged workflow runtime."""

from __future__ import annotations

from pathlib import Path
import shlex
import subprocess
import sys
from typing import Any


DEFAULT_VERIFY_CMD = "pytest -q"
VERIFY_TIMEOUT_SECONDS = 120


def maybe_execute_verify_command(
    *,
    project_root: Path,
    feature: str,
    task_label: str,
    verify_cmd: str,
    changed_files: list[str],
    timeout_seconds: int | None = None,
    verify_targets: list[str] | None = None,
    verify_target_source: str = "",
) -> dict[str, Any] | None:
    mode = _execution_mode(
        project_root=project_root,
        verify_cmd=verify_cmd,
        changed_files=changed_files,
        verify_targets=verify_targets,
        verify_target_source=verify_target_source,
    )
    if not mode:
        return None
    return _run_verify_command(
        project_root=project_root,
        feature=feature,
        task_label=task_label,
        verify_cmd=verify_cmd,
        changed_files=changed_files,
        timeout_seconds=_normalize_timeout_seconds(timeout_seconds),
    )


def _execution_mode(
    *,
    project_root: Path,
    verify_cmd: str,
    changed_files: list[str],
    verify_targets: list[str] | None = None,
    verify_target_source: str = "",
) -> str:
    """Decide whether to actually shell out to verify_cmd.

    Skip rules — return "" (no run):
    - empty verify_cmd
    - empty changed_files (nothing to verify)
    - default verify_cmd AND no test targets resolvable AND no tests/ directory

    Run rules:
    - explicit verify_cmd (user / planner overrode default) → "explicit"
    - resolved verify_targets non-empty (verify_targeting found scoped tests) → "detected"
    - default verify_cmd, no targets, but a tests/ directory exists → "broad"
      (project has tests but our heuristic couldn't map them to changed_files;
      better to run the broad suite than silently skip)

    Background: the previous gate (line 38-44, pre-fix) skipped the broad-default
    case AND the no-derived-target case. T4-style code-only rounds whose paired
    test lived under a different name (test_api.py for app/main.py) silently
    skipped — exactly the regression-check scenario we want to verify.
    """
    normalized = str(verify_cmd or "").strip()
    if not normalized:
        return ""
    if not list(changed_files or []):
        return ""
    if normalized != DEFAULT_VERIFY_CMD:
        return "explicit"
    if list(verify_targets or []):
        return "detected"
    source = str(verify_target_source or "").strip().lower()
    if source and source != "default":
        return "detected"
    # Last resort for default pytest -q with no resolved targets: run when a
    # tests/ directory exists. This avoids silently passing on a project that
    # DOES have tests but our naming heuristic missed.
    if _project_has_tests_directory(project_root):
        return "broad"
    return ""


def _project_has_tests_directory(project_root: Path) -> bool:
    root = Path(project_root).resolve()
    for candidate in ("tests", "test"):
        path = root / candidate
        if path.is_dir() and any(path.iterdir()):
            return True
    return False


def _existing_verify_targets(project_root: Path, changed_files: list[str]) -> list[Path]:
    return [path for path in _existing_changed_paths(project_root, changed_files) if _looks_like_test_path(path)]


def _existing_changed_paths(project_root: Path, changed_files: list[str]) -> list[Path]:
    resolved: list[Path] = []
    root = Path(project_root).resolve()
    for raw in changed_files:
        candidate = (root / str(raw)).resolve()
        if candidate.exists():
            resolved.append(candidate)
    return sorted({path for path in resolved})


def _looks_like_test_path(path: Path) -> bool:
    name = path.name.lower()
    return name.startswith("test_") or "tests" in {part.lower() for part in path.parts}


def _run_verify_command(
    *,
    project_root: Path,
    feature: str,
    task_label: str,
    verify_cmd: str,
    changed_files: list[str],
    timeout_seconds: int,
) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            _command_payload(project_root=project_root, verify_cmd=verify_cmd),
            cwd=str(Path(project_root).resolve()),
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            timeout=timeout_seconds,
            shell=_command_uses_shell(project_root=project_root, verify_cmd=verify_cmd),
        )
    except subprocess.TimeoutExpired as exc:
        return _timeout_payload(
            feature=feature,
            task_label=task_label,
            verify_cmd=verify_cmd,
            changed_files=changed_files,
            timeout_seconds=timeout_seconds,
            exc=exc,
        )
    except OSError as exc:
        return _error_payload(
            feature=feature,
            task_label=task_label,
            verify_cmd=verify_cmd,
            changed_files=changed_files,
            timeout_seconds=timeout_seconds,
            summary=str(exc),
        )
    return _completed_payload(
        feature=feature,
        task_label=task_label,
        verify_cmd=verify_cmd,
        changed_files=changed_files,
        timeout_seconds=timeout_seconds,
        completed=completed,
    )


def _normalize_timeout_seconds(value: int | None) -> int:
    try:
        parsed = int(value or VERIFY_TIMEOUT_SECONDS)
    except (TypeError, ValueError):
        parsed = VERIFY_TIMEOUT_SECONDS
    return max(1, parsed)


def _strip_outer_quotes(token: str) -> str:
    """Remove a single outer pair of matching quotes left in place by
    ``shlex.split(..., posix=False)``. Leaves inner content untouched so
    boolean expressions like ``foo and bar`` stay intact as one argv entry.
    """
    if len(token) >= 2 and token[0] == token[-1] and token[0] in ('"', "'"):
        return token[1:-1]
    return token


def _command_payload(*, project_root: Path, verify_cmd: str) -> list[str] | str:
    normalized = str(verify_cmd or "").strip()
    if normalized.startswith("pytest "):
        # Use shlex.split so quoted arguments (e.g. `-k "foo and bar"`) stay
        # as a single argv entry. Plain .split() would break the -k boolean
        # expression and pytest would treat `and`/`bar` as positional files,
        # reporting "ERROR: file or directory not found: and".
        # posix=False keeps Windows backslashes in paths intact.
        try:
            parts = shlex.split(normalized, posix=False)
        except ValueError:
            # Malformed quoting — fall back to whitespace split so we still try
            # to run something rather than crashing here.
            parts = normalized.split()
        # Strip shlex's preserved quote characters (posix=False keeps them).
        parts = [_strip_outer_quotes(p) for p in parts]
        return [sys.executable, "-m", "pytest", *parts[1:]]
    if normalized == "pytest":
        return [sys.executable, "-m", "pytest"]
    command_file = _verify_command_file_candidate(project_root=project_root, verify_cmd=normalized)
    if command_file is not None:
        lowered = command_file.suffix.lower()
        if lowered in {".cmd", ".bat"}:
            return ["cmd", "/c", str(command_file)]
        if lowered == ".ps1":
            return ["powershell", "-File", str(command_file)]
        return [str(command_file)]
    return normalized


def _command_uses_shell(*, project_root: Path, verify_cmd: str) -> bool:
    return isinstance(_command_payload(project_root=project_root, verify_cmd=verify_cmd), str)


def _verify_command_file_candidate(*, project_root: Path, verify_cmd: str) -> Path | None:
    normalized = str(verify_cmd or "").strip()
    if not normalized:
        return None
    candidate = Path(normalized)
    if not candidate.is_absolute():
        candidate = Path(project_root).resolve() / candidate
    if candidate.is_file():
        return candidate.resolve()
    return None


def _timeout_payload(
    *,
    feature: str,
    task_label: str,
    verify_cmd: str,
    changed_files: list[str],
    timeout_seconds: int,
    exc: subprocess.TimeoutExpired,
) -> dict[str, Any]:
    summary = f"Verify command timed out after {int(exc.timeout or timeout_seconds)}s"
    return _base_payload(
        feature=feature,
        task_label=task_label,
        verify_cmd=verify_cmd,
        changed_files=changed_files,
        timeout_seconds=timeout_seconds,
        status="BLOCKED",
        summary=summary,
        command_executed=True,
        returncode=None,
        stdout_excerpt=_excerpt(getattr(exc, "stdout", None)),
        stderr_excerpt=_excerpt(getattr(exc, "stderr", None)),
    )


def _error_payload(
    *,
    feature: str,
    task_label: str,
    verify_cmd: str,
    changed_files: list[str],
    timeout_seconds: int,
    summary: str,
) -> dict[str, Any]:
    return _base_payload(
        feature=feature,
        task_label=task_label,
        verify_cmd=verify_cmd,
        changed_files=changed_files,
        timeout_seconds=timeout_seconds,
        status="BLOCKED",
        summary=summary,
        command_executed=True,
        returncode=None,
        stdout_excerpt="",
        stderr_excerpt="",
    )


def _completed_payload(
    *,
    feature: str,
    task_label: str,
    verify_cmd: str,
    changed_files: list[str],
    timeout_seconds: int,
    completed: subprocess.CompletedProcess[str],
) -> dict[str, Any]:
    status = "PASS" if completed.returncode == 0 else "BLOCKED"
    max_lines = 8 if status == "PASS" else 80
    summary = _completed_summary(completed, status=status, max_lines=max_lines)
    return _base_payload(
        feature=feature,
        task_label=task_label,
        verify_cmd=verify_cmd,
        changed_files=changed_files,
        timeout_seconds=timeout_seconds,
        status=status,
        summary=summary,
        command_executed=True,
        returncode=completed.returncode,
        stdout_excerpt=_excerpt(completed.stdout, max_lines=max_lines),
        stderr_excerpt=_excerpt(completed.stderr, max_lines=max_lines),
    )


def _completed_summary(completed: subprocess.CompletedProcess[str], *, status: str, max_lines: int = 8) -> str:
    for output in (completed.stderr, completed.stdout):
        excerpt = _excerpt(output, max_lines=max_lines)
        if excerpt:
            return excerpt
    return "Verify command passed" if status == "PASS" else f"Verify command failed with exit code {completed.returncode}"


def _base_payload(
    *,
    feature: str,
    task_label: str,
    verify_cmd: str,
    changed_files: list[str],
    timeout_seconds: int,
    status: str,
    summary: str,
    command_executed: bool,
    returncode: int | None,
    stdout_excerpt: str,
    stderr_excerpt: str,
) -> dict[str, Any]:
    return {
        "feature": feature,
        "task_label": task_label,
        "status": status,
        "passed": status == "PASS",
        "mode": "command",
        "source": "verify_command",
        "verify_cmd": verify_cmd,
        "timeout_seconds": timeout_seconds,
        "artifacts": list(changed_files),
        "summary": summary,
        "blocking_reason": "" if status == "PASS" else summary,
        "command_executed": command_executed,
        "returncode": returncode,
        "stdout_excerpt": stdout_excerpt,
        "stderr_excerpt": stderr_excerpt,
    }


def _excerpt(value: Any, *, max_lines: int = 8) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return "\n".join(text.splitlines()[-max_lines:]).strip()
