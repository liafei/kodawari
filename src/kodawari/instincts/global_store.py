"""Cross-project ``GlobalInstinctStore``.

The project-level store at ``<project_root>/.workflow/instincts.json`` is a
bag-of-experience that is invisible to every other project. As soon as a
high-confidence ``LearnedInstinct`` (e.g. ``codex_cli execution timed out``
seen across many runs) gets promoted, we want every other kodawari
project on the same machine to benefit on day one — that's the whole point
of "the workflow learned this".

Design points
=============

* **Path** — defaults to ``~/.kodawari/instincts.json``. Override with
  the ``WORKFLOW_INSTINCTS_GLOBAL_PATH`` env var (tests use this to redirect
  to a tmp_path; never let production tests touch the real home dir).
* **Schema** — only ``learned_instincts`` is stored globally. Candidates and
  manual ``instincts`` stay project-local; only fully-promoted, portable
  patterns belong cross-project.
* **Concurrency** — multiple projects may run autopilot in parallel and
  promote at the same time. We use ``infra.io_atomic.path_lock`` for
  exclusive writes and ``atomic_write_json`` for crash-safe replace; a torn
  write across processes is impossible.
* **Failure mode** — if the global store is unreadable / corrupt / locked
  too long, the project-level path keeps working. Never let global writes
  block local promotion.
* **Portable ≠ this class's job** — the policy decision "should this
  LearnedInstinct enter the global store" lives in
  ``instincts.engine.is_portable_learned_instinct``. This module only
  provides storage.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from kodawari.infra.io_atomic import (
    CorruptArtifactError,
    atomic_write_json,
    path_lock,
)
from kodawari.instincts.models import LearnedInstinct


logger = logging.getLogger(__name__)


GLOBAL_STORE_ENV_VAR = "WORKFLOW_INSTINCTS_GLOBAL_PATH"
GLOBAL_STORE_DEFAULT_FILENAME = "instincts.json"
GLOBAL_STORE_DEFAULT_DIRNAME = ".kodawari"


def resolve_global_store_path() -> Path:
    """Return the path the global store should use right now.

    Resolution order:
      1. ``WORKFLOW_INSTINCTS_GLOBAL_PATH`` env var (absolute path).
      2. ``~/.kodawari/instincts.json``.
    """
    override = os.environ.get(GLOBAL_STORE_ENV_VAR, "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / GLOBAL_STORE_DEFAULT_DIRNAME / GLOBAL_STORE_DEFAULT_FILENAME).resolve()


class GlobalInstinctStore:
    """Cross-project store for promoted, portable ``LearnedInstinct`` rows."""

    SCHEMA_VERSION = "v1"

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path).resolve() if path is not None else resolve_global_store_path()

    def exists(self) -> bool:
        return self.path.exists()

    def load_learned(self) -> list[LearnedInstinct]:
        """Return the stored learned instincts; empty list on any failure."""
        if not self.path.exists():
            return []
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError:
            logger.warning("global instincts store read failed", exc_info=True)
            return []
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning(
                "global instincts store has invalid JSON; ignoring until next write",
                exc_info=True,
            )
            return []
        if not isinstance(payload, dict):
            return []
        rows = payload.get("learned_instincts", [])
        if not isinstance(rows, list):
            return []
        learned: list[LearnedInstinct] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                item = LearnedInstinct.from_dict(row)
            except Exception:  # noqa: BLE001 - defensive: never let a bad row crash readers
                logger.warning("global instincts row failed to parse; skipping", exc_info=True)
                continue
            if item.id and item.pattern:
                learned.append(item)
        return learned

    def upsert_learned(self, learned: LearnedInstinct) -> dict[str, str]:
        """Merge ``learned`` into the global store under a path lock.

        Merge rule on signature collision: never downgrade an existing
        higher-confidence row; take the max of confidence and count, prefer
        the incoming pattern only when the existing row has none.
        """
        try:
            with path_lock(self.path, timeout_seconds=5.0):
                existing = self._read_unlocked()
                by_sig: dict[str, LearnedInstinct] = {
                    item.signature: item for item in existing if item.signature
                }
                current = by_sig.get(learned.signature)
                if current is None:
                    existing.append(learned)
                    decision = "inserted"
                else:
                    current.confidence = max(float(current.confidence), float(learned.confidence))
                    current.count = max(int(current.count), int(learned.count))
                    if learned.last_seen:
                        current.last_seen = learned.last_seen
                    if learned.pattern and not current.pattern:
                        current.pattern = learned.pattern
                    if learned.category and not current.category:
                        current.category = learned.category
                    if learned.metadata:
                        merged_metadata = dict(current.metadata)
                        for key, value in learned.metadata.items():
                            merged_metadata.setdefault(key, value)
                        current.metadata = merged_metadata
                    if learned.source and not current.source:
                        current.source = learned.source
                    current.archived = False
                    decision = "merged"
                payload = {
                    "schema_version": self.SCHEMA_VERSION,
                    "learned_instincts": [item.to_dict() for item in existing],
                }
                # use_lock=False because path_lock is already held.
                atomic_write_json(self.path, payload, use_lock=False)
                return {"decision": decision, "store_path": str(self.path)}
        except (ValueError, OSError, CorruptArtifactError):
            logger.warning("global instincts upsert failed; project store still updated", exc_info=True)
            return {"decision": "skipped", "store_path": str(self.path)}

    def _read_unlocked(self) -> list[LearnedInstinct]:
        """Same parse as ``load_learned`` but assumes the lock is held; reads
        the on-disk state we are about to merge into."""
        if not self.path.exists():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("global instincts store unreadable inside lock", exc_info=True)
            return []
        if not isinstance(payload, dict):
            return []
        rows = payload.get("learned_instincts", [])
        if not isinstance(rows, list):
            return []
        out: list[LearnedInstinct] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                item = LearnedInstinct.from_dict(row)
            except Exception:  # noqa: BLE001
                continue
            if item.id and item.pattern:
                out.append(item)
        return out


__all__ = [
    "GLOBAL_STORE_ENV_VAR",
    "GlobalInstinctStore",
    "resolve_global_store_path",
]
