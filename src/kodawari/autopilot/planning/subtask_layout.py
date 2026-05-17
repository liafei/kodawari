"""Canonical subtask directory layout under a feature's planning_dir.

A feature's parent task writes to ``planning/<feature>/``; subtask runners
have historically written into the same directory, which means each
subtask's ``.execution_result.json`` / ``.review_result.json`` /
``REVIEW.md`` overwrites the previous one. Migrate to per-subtask
subdirectories so subtask artifacts cannot collide.

This module is the single source of truth for the layout. Subtask runners
and downstream consumers (delivery report, lane observation, instincts)
should call ``subtask_planning_dir`` instead of inlining the path
construction.
"""

from __future__ import annotations

import re
from pathlib import Path

SUBTASKS_DIRNAME = "_subtasks"

# Subtask IDs are typically uppercase alphanumeric with dots/dashes/underscores.
# Anything outside that set is replaced with `_` so the on-disk path stays safe
# regardless of what the planner emits.
_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9._\-]+")


def sanitize_subtask_id(subtask_id: str) -> str:
    cleaned = _SAFE_ID_RE.sub("_", str(subtask_id or "").strip())
    return cleaned.strip("._-") or "subtask"


def subtask_planning_dir(parent_planning_dir: Path | str, subtask_id: str) -> Path:
    """Return the canonical subdirectory for a subtask's artifacts.

    Layout: ``<parent_planning_dir>/_subtasks/<sanitized_subtask_id>/``.

    The directory is *not* created on disk by this function; callers create
    it lazily when they actually emit an artifact.
    """

    parent = Path(parent_planning_dir).resolve()
    return parent / SUBTASKS_DIRNAME / sanitize_subtask_id(subtask_id)


def is_subtask_planning_dir(planning_dir: Path | str) -> bool:
    """Return True when the given path looks like a subtask subdirectory.

    Used by tooling that needs to distinguish "this is the parent feature dir"
    from "this is a subtask dir under the parent".
    """

    candidate = Path(planning_dir).resolve()
    parent = candidate.parent
    return parent.name == SUBTASKS_DIRNAME


__all__ = [
    "SUBTASKS_DIRNAME",
    "is_subtask_planning_dir",
    "sanitize_subtask_id",
    "subtask_planning_dir",
]
