"""Minimal instinct store at ``<project_root>/.workflow/instincts.json``.

Why ``.workflow/`` and not ``.claude/memory/``?
The instincts store is kodawari's own error-experience accumulator. It is
model-agnostic — switching from Claude to GPT to any future backend must not
invalidate it. Storing it under ``.claude/`` historically conflated it with
Claude Code's private memory directory; the actual data has nothing to do with
Claude.

Backward-compat: when the new ``.workflow/instincts.json`` does not exist but
the legacy ``.claude/memory/instincts.json`` does, ``load()`` reads from the
legacy path. The next ``save()`` writes to the new path, leaving the legacy
file in place so users can confirm the migration before deleting it manually.
"""

from __future__ import annotations

import json
from pathlib import Path

from kodawari.instincts.models import (
    Instinct,
    InstinctStoreData,
    LearnedInstinct,
    LearningCandidate,
)


STORE_RELATIVE_PATH = Path(".workflow") / "instincts.json"
LEGACY_STORE_RELATIVE_PATH = Path(".claude") / "memory" / "instincts.json"


class InstinctStore:
    def __init__(self, project_root: Path) -> None:
        self.project_root = Path(project_root).resolve()
        self.path = self.project_root / STORE_RELATIVE_PATH
        self.legacy_path = self.project_root / LEGACY_STORE_RELATIVE_PATH

    def exists(self) -> bool:
        """Return True when either the new or the legacy store file exists."""
        return self.path.exists() or self.legacy_path.exists()

    def _read_source(self) -> Path | None:
        if self.path.exists():
            return self.path
        if self.legacy_path.exists():
            return self.legacy_path
        return None

    def load(self) -> InstinctStoreData:
        source = self._read_source()
        if source is None:
            return InstinctStoreData()

        payload = self._load_payload_dict(source)
        instincts = self._parse_instincts(payload.get("instincts", []))
        learning_candidates = self._parse_learning_candidates(payload.get("learning_candidates", []))
        learned_instincts = self._parse_learned_instincts(payload.get("learned_instincts", []))

        return InstinctStoreData(
            schema_version=str(payload.get("schema_version", "v1")),
            instincts=instincts,
            learning_candidates=learning_candidates,
            learned_instincts=learned_instincts,
        )

    def save(self, payload: InstinctStoreData) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(payload.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return self.path

    def _load_payload_dict(self, source: Path) -> dict[str, object]:
        payload = json.loads(source.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("instincts store payload must be a JSON object")
        return payload

    def _parse_instincts(self, rows: object) -> list[Instinct]:
        if not isinstance(rows, list):
            return []
        instincts: list[Instinct] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            instinct = Instinct.from_dict(row)
            if instinct.id and instinct.pattern:
                instincts.append(instinct)
        return instincts

    def _parse_learning_candidates(self, rows: object) -> list[LearningCandidate]:
        if not isinstance(rows, list):
            return []
        candidates: list[LearningCandidate] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            candidate = LearningCandidate.from_dict(row)
            if candidate.id and candidate.signature:
                candidates.append(candidate)
        return candidates

    def _parse_learned_instincts(self, rows: object) -> list[LearnedInstinct]:
        if not isinstance(rows, list):
            return []
        learned: list[LearnedInstinct] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            item = LearnedInstinct.from_dict(row)
            if item.id and item.pattern:
                learned.append(item)
        return learned


__all__ = [
    "InstinctStore",
    "STORE_RELATIVE_PATH",
    "LEGACY_STORE_RELATIVE_PATH",
]
