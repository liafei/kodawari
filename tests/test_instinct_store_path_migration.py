"""Path migration tests for ``InstinctStore``.

The store moved from ``<root>/.claude/memory/instincts.json`` to
``<root>/.workflow/instincts.json``. These tests pin three behaviors:

1. New writes land at the new path.
2. A legacy-only on-disk file is still readable.
3. After a save, the new file appears alongside the legacy file (the legacy
   file is **not** deleted automatically — operators confirm migration first).
"""

from __future__ import annotations

import json
from pathlib import Path

from kodawari.instincts.models import (
    Instinct,
    InstinctStoreData,
    LearnedInstinct,
)
from kodawari.instincts.storage import (
    LEGACY_STORE_RELATIVE_PATH,
    STORE_RELATIVE_PATH,
    InstinctStore,
)


def _legacy_payload() -> dict:
    return {
        "schema_version": "v1",
        "instincts": [
            {
                "id": "inst_legacy_1",
                "pattern": "tests/test_*.py",
                "category": "verify",
                "confidence": 0.7,
                "archived": False,
            }
        ],
        "learning_candidates": [],
        "learned_instincts": [
            {
                "id": "learned_legacy_1",
                "signature": "legacy-sig",
                "pattern": "tests/test_legacy*.py",
                "category": "verify",
                "confidence": 0.85,
                "count": 5,
                "source": "error_learning",
                "explanation": "",
                "first_seen": "2026-01-01T00:00:00Z",
                "last_seen": "2026-01-02T00:00:00Z",
                "archived": False,
                "metadata": {},
            }
        ],
    }


def test_new_save_lands_at_dot_workflow(tmp_path: Path) -> None:
    store = InstinctStore(tmp_path)
    payload = InstinctStoreData(
        instincts=[Instinct(id="x", pattern="src/**/*.py", category="recovery", confidence=0.6)],
    )
    written = store.save(payload)

    expected = (tmp_path / STORE_RELATIVE_PATH).resolve()
    assert written.resolve() == expected
    assert expected.exists()
    # Legacy path must not be created on a fresh project.
    assert not (tmp_path / LEGACY_STORE_RELATIVE_PATH).exists()


def test_load_falls_back_to_legacy_path(tmp_path: Path) -> None:
    legacy_file = tmp_path / LEGACY_STORE_RELATIVE_PATH
    legacy_file.parent.mkdir(parents=True, exist_ok=True)
    legacy_file.write_text(json.dumps(_legacy_payload()), encoding="utf-8")

    data = InstinctStore(tmp_path).load()

    # Legacy data is parsed correctly even though the new path does not exist.
    assert len(data.learned_instincts) == 1
    learned = data.learned_instincts[0]
    assert isinstance(learned, LearnedInstinct)
    assert learned.signature == "legacy-sig"
    assert not (tmp_path / STORE_RELATIVE_PATH).exists()


def test_save_after_legacy_load_writes_new_path_without_deleting_legacy(tmp_path: Path) -> None:
    legacy_file = tmp_path / LEGACY_STORE_RELATIVE_PATH
    legacy_file.parent.mkdir(parents=True, exist_ok=True)
    legacy_file.write_text(json.dumps(_legacy_payload()), encoding="utf-8")

    store = InstinctStore(tmp_path)
    data = store.load()
    written = store.save(data)

    assert written == tmp_path / STORE_RELATIVE_PATH
    # Migration is non-destructive: legacy file stays put.
    assert legacy_file.exists()


def test_exists_returns_true_for_legacy_only_state(tmp_path: Path) -> None:
    legacy_file = tmp_path / LEGACY_STORE_RELATIVE_PATH
    legacy_file.parent.mkdir(parents=True, exist_ok=True)
    legacy_file.write_text(json.dumps(_legacy_payload()), encoding="utf-8")

    assert InstinctStore(tmp_path).exists() is True


def test_new_path_takes_precedence_over_legacy_when_both_exist(tmp_path: Path) -> None:
    legacy_file = tmp_path / LEGACY_STORE_RELATIVE_PATH
    legacy_file.parent.mkdir(parents=True, exist_ok=True)
    legacy_file.write_text(json.dumps(_legacy_payload()), encoding="utf-8")

    new_payload = {
        "schema_version": "v1",
        "instincts": [],
        "learning_candidates": [],
        "learned_instincts": [
            {
                "id": "learned_new_1",
                "signature": "new-sig",
                "pattern": "tests/test_new*.py",
                "category": "verify",
                "confidence": 0.9,
                "count": 6,
                "source": "error_learning",
                "explanation": "",
                "first_seen": "2026-04-01T00:00:00Z",
                "last_seen": "2026-04-02T00:00:00Z",
                "archived": False,
                "metadata": {},
            }
        ],
    }
    new_file = tmp_path / STORE_RELATIVE_PATH
    new_file.parent.mkdir(parents=True, exist_ok=True)
    new_file.write_text(json.dumps(new_payload), encoding="utf-8")

    data = InstinctStore(tmp_path).load()
    assert len(data.learned_instincts) == 1
    assert data.learned_instincts[0].signature == "new-sig"
