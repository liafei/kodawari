"""Task-run carry-over manifest: closes the sync-leaves-worktree-dirty footgun.

Background
----------
``execution_*::sync_isolated_workspace_to_project_root`` copies the executor's
allowed_files from the isolation workspace back into ``project_root`` so the
downstream verify/gate/review steps can see the diff. The side effect: when
task-run exits (whether PASS, BLOCKED, or FAIL), those files remain dirty in
the user's worktree. The next task-run's worktree preflight sees the dirty
state and refuses to start with ``DIRTY_WORKTREE_BLOCKED`` — even when the
caller is simply retrying the same task after a review-fail loop.

A naive "git commit" at the end of task-run is **not** safe: the user's
worktree may also contain unrelated WIP changes, and a single auto-commit
would mix executor output with user edits. Instead, this module records a
*carry-over manifest* describing exactly which files were produced by the
last task-run and for which task_id. The next preflight then treats the
listed files as "expected dirty from prior task-run" and excludes them from
``core_dirty_files`` when the new run is for the **same** task_id.

Cross-task contamination is intentionally NOT auto-resolved: if T1 leaves
backend/main.py dirty and the user starts T2 (different task_id), the new
preflight still treats main.py as "user-pending changes that need explicit
resolution" — that is the safe default and matches how git itself behaves.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

MANIFEST_FILENAME = ".task_run_manifest.json"
MANIFEST_SCHEMA_VERSION = "task_run_manifest.v1"


def manifest_path(planning_dir: Path) -> Path:
    return planning_dir / MANIFEST_FILENAME


def _normalize_carried_files(files: Iterable[Any]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for item in files or []:
        text = str(item).strip().replace("\\", "/")
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(text)
    return normalized


def _manifest_carried_files_for_task(*, planning_dir: Path, task_id: str) -> list[str]:
    manifest = read_task_run_manifest(planning_dir)
    if not manifest:
        return []
    if str(manifest.get("schema_version") or "") != MANIFEST_SCHEMA_VERSION:
        return []
    if str(manifest.get("task_id") or "").strip() != str(task_id or "").strip():
        return []
    files = manifest.get("carried_files")
    if not isinstance(files, list):
        return []
    return _normalize_carried_files(files)


def write_task_run_manifest(
    *,
    planning_dir: Path,
    task_id: str,
    status: str,
    carried_files: list[str],
) -> None:
    """Write the carry-over manifest at task-run exit.

    Idempotent: overwrites any prior manifest. Failure to write is logged but
    not raised — the manifest is an optimization (avoid spurious DIRTY block on
    retry) and a missing manifest just means the next preflight degrades to
    the conservative default.
    """
    normalized_task_id = str(task_id or "").strip()
    carried = _normalize_carried_files(carried_files or [])
    previous_carried = _manifest_carried_files_for_task(
        planning_dir=planning_dir,
        task_id=normalized_task_id,
    )
    if previous_carried:
        carried = _normalize_carried_files([*previous_carried, *carried])

    payload = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "task_id": normalized_task_id,
        "status": str(status or "").strip(),
        "carried_files": carried,
        "completed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    try:
        path = manifest_path(planning_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        logger.debug("failed to write task-run manifest at %s", planning_dir, exc_info=True)


def read_task_run_manifest(planning_dir: Path) -> dict[str, Any] | None:
    """Return the parsed manifest payload, or ``None`` if missing/invalid."""
    try:
        path = manifest_path(planning_dir)
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        return data
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def carry_over_files_for_task(
    *,
    planning_dir: Path,
    task_id: str,
) -> list[str]:
    """Return files the caller may treat as carry-over from a prior run.

    The carry-over only applies when the prior manifest matches the **same**
    ``task_id``. Cross-task contamination is left for explicit user
    resolution. Returns an empty list when there is no manifest, the schema
    is unknown, or the task_id does not match.
    """
    return _manifest_carried_files_for_task(planning_dir=planning_dir, task_id=task_id)


__all__ = [
    "MANIFEST_FILENAME",
    "MANIFEST_SCHEMA_VERSION",
    "carry_over_files_for_task",
    "manifest_path",
    "read_task_run_manifest",
    "write_task_run_manifest",
]
