"""Shared execution isolation workspace helpers.

Both claude_code and codex_cli backends can run inside a per-task copy of
the project under `planning_dir/.parallel_workers/<backend>/<slug>-<hex>/`.
After execution, only `allowed_files` (a.k.a. `files_to_change`) are synced
back to project_root. This gives:

- **Directory-level isolation**: parallel tasks cannot clobber each other's
  in-flight edits because they run in independent copies.
- **Failure attribution**: on backend failure the workspace stays on disk,
  making it obvious what the backend actually attempted.
- **Scope enforcement**: only allowed_files propagate back; edits to files
  outside `files_to_change` stay trapped in the isolation workspace.
- **Secret hygiene**: known secret/key files are not copied into isolation
  workspaces.

The isolation is **directory-level**, not `git worktree`. See
[docs/CAPABILITY_MAP.md](../../docs/CAPABILITY_MAP.md) for the distinction.
"""

from __future__ import annotations

import fnmatch
import logging
import shutil
from pathlib import Path
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)


_IGNORE_COPY_PATTERNS = ("__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache")
_SKIP_TOP_LEVEL = frozenset({".git", ".venv", "node_modules", ".tox", ".android-studio", ".idea", ".gradle"})
_SECRET_PATH_PATTERNS = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.jks",
    "*.keystore",
    "id_rsa*",
    "id_ed25519*",
    ".npmrc",
    ".pypirc",
    "credentials.json",
    "token.json",
)


def _slug(task_id: str) -> str:
    normalized = str(task_id or "task").strip() or "task"
    slug = "".join(char if char.isalnum() else "-" for char in normalized).strip("-").lower()
    return slug or "task"


def prepare_isolation_workspace(
    *,
    planning_dir: Path,
    project_root: Path,
    backend_name: str,
    request_payload: dict[str, Any],
) -> Path:
    """Allocate a per-task isolation directory under planning_dir.

    Returns the workspace path (caller runs the backend with cwd=workspace).
    """
    slug = _slug(str(request_payload.get("task_id") or ""))
    workspace = (
        planning_dir / ".parallel_workers" / backend_name / f"{slug}-{uuid4().hex[:8]}"
    ).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    _copy_project_snapshot(
        project_root=project_root,
        planning_dir=planning_dir,
        workspace=workspace,
    )
    return workspace


def _copy_project_snapshot(
    *,
    project_root: Path,
    planning_dir: Path,
    workspace: Path,
) -> None:
    ignore = _copytree_ignore
    for source in project_root.iterdir():
        if _skip_workspace_copy(source=source, planning_dir=planning_dir):
            continue
        if source.is_symlink():
            # Never dereference top-level links into isolation snapshots.
            # This avoids copying content from outside project_root.
            continue
        if _looks_like_secret_name(source.name):
            continue
        target = workspace / source.name
        if source.is_dir():
            shutil.copytree(
                source,
                target,
                symlinks=True,
                dirs_exist_ok=True,
                ignore=ignore,
            )
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _skip_workspace_copy(*, source: Path, planning_dir: Path) -> bool:
    resolved_source = source.resolve()
    resolved_planning_dir = planning_dir.resolve()
    if resolved_source == resolved_planning_dir:
        return True
    if resolved_planning_dir.is_relative_to(resolved_source):
        return True
    return source.name in _SKIP_TOP_LEVEL


def _looks_like_secret_name(name: str) -> bool:
    lowered = str(name or "").strip().lower()
    if not lowered:
        return False
    return any(fnmatch.fnmatchcase(lowered, pattern) for pattern in _SECRET_PATH_PATTERNS)


def _copytree_ignore(_dir: str, names: list[str]) -> set[str]:
    ignored: set[str] = set()
    directory = Path(_dir)
    for name in names:
        candidate = directory / name
        if candidate.is_symlink():
            ignored.add(name)
            continue
        if name in _IGNORE_COPY_PATTERNS:
            ignored.add(name)
            continue
        if _looks_like_secret_name(name):
            ignored.add(name)
    return ignored


def _resolve_within_root(*, root: Path, relative: str) -> Path | None:
    text = str(relative or "").strip().replace("\\", "/")
    if not text:
        return None
    candidate = (root / text).resolve()
    if not candidate.is_relative_to(root.resolve()):
        return None
    return candidate


def sync_isolated_workspace_to_project_root(
    *,
    project_root: Path,
    execution_root: Path,
    allowed_files: list[str],
) -> None:
    """Copy allowed_files from the isolation workspace back to project_root.

    Files outside allowed_files are NOT synced — that's the scope lock.
    If an allowed file was deleted inside the workspace, it is also deleted
    from project_root (propagate intentional removals).
    """
    from kodawari.autopilot.core.permission_policy import is_path_blocked_for_write

    for relative in allowed_files:
        if is_path_blocked_for_write(relative):
            logger.warning("sync skipped permission-blocked path: %s", relative)
            continue
        source = _resolve_within_root(root=execution_root, relative=relative)
        target = _resolve_within_root(root=project_root, relative=relative)
        if source is None or target is None:
            continue
        if source.exists() and source.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            continue
        if target.exists() and target.is_file():
            target.unlink()


__all__ = [
    "prepare_isolation_workspace",
    "sync_isolated_workspace_to_project_root",
]

