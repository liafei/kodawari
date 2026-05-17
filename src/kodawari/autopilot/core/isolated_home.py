"""Shared helpers for syncing real user credentials into isolated subprocess HOMEs.

Both the codex executor/reviewer and the claude executor/reviewer run their
backends in an isolated HOME (workspace-relative ``.workflow/<...>_home``) so
that subprocess state does not pollute the user's real ``~/.codex`` or
``~/.claude``. To make those subprocesses authenticated, we copy a small set of
credential files from the user's real HOME into the isolated HOME on every
launch.

The naive ``if target.exists(): return`` pattern was a recurring footgun: once a
token was synced, it never refreshed, so a token rotation on the host produced
silent ``401`` / "Not logged in" errors inside the isolated subprocess. The
correct pattern is **mtime-aware copy** — only skip when the cached copy is at
least as fresh as the source.

This module exposes a single primitive ``sync_file_mtime_aware`` plus a
convenience wrapper for the "try a list of source candidates" idiom that both
codex and claude sync paths use.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)


def sync_file_mtime_aware(source: Path, target: Path, *, label: str = "") -> bool:
    """Copy ``source`` -> ``target`` if source is newer than target (or target missing).

    Returns ``True`` when a copy actually happened, ``False`` otherwise (target
    already fresh, source missing, copy failed, etc.).

    Replaces patterns like ``if target.exists(): return`` that silently held
    stale tokens. Idempotent and safe to call on every subprocess launch.
    """
    try:
        if not source.exists():
            return False
        if source.resolve() == target.resolve():
            return False
        if target.exists() and source.stat().st_mtime <= target.stat().st_mtime:
            return False
    except OSError:
        return False
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        if label:
            logger.debug("synced %s: %s -> %s", label, source, target)
        else:
            logger.debug("synced %s -> %s", source, target)
        return True
    except OSError:
        logger.debug("failed to sync %s -> %s", source, target, exc_info=True)
        return False


def sync_first_present_source(
    *,
    target: Path,
    source_candidates: Iterable[Path],
    label: str = "",
) -> bool:
    """Try each candidate path in order; copy from the first that exists.

    Mirrors the codex/claude pattern of preferring an explicit env-var-pointed
    HOME before falling back to ``~/.codex`` / ``~/.claude``. Stops after the
    first matching source — even if that source's copy is skipped (mtime
    fresh) — because falling through to a different source would be wrong.

    Returns ``True`` if a copy happened, ``False`` if all candidates were
    skipped or missing.
    """
    for source in source_candidates:
        if not source.exists():
            continue
        return sync_file_mtime_aware(source, target, label=label)
    return False


__all__ = [
    "sync_file_mtime_aware",
    "sync_first_present_source",
]
