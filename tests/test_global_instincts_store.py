"""PR4: cross-project ``GlobalInstinctStore`` end-to-end behavior.

What this file pins:
- Path resolution honors ``WORKFLOW_INSTINCTS_GLOBAL_PATH`` so tests never
  touch the real ``~/.kodawari/`` directory.
- ``upsert_learned`` inserts new rows and merges duplicates without
  downgrading confidence.
- Concurrent writers cannot corrupt the store (atomic write + path lock).
- Project-level promotion publishes a portable LearnedInstinct to global
  once confidence crosses ``GLOBAL_PROMOTION_CONFIDENCE_THRESHOLD``.
- Repo-specific patterns (high confidence but no portable error_code) stay
  project-local — they MUST NOT leak to the global store.
- ``select_instinct_hints`` merges global hints in; project rows win on
  signature/pattern conflict.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from kodawari.instincts import (
    GLOBAL_PROMOTION_CONFIDENCE_THRESHOLD,
    GLOBAL_STORE_ENV_VAR,
    GlobalInstinctStore,
    LearnedInstinct,
    is_portable_learned_instinct,
    select_instinct_hints,
)
from kodawari.instincts.engine import ingest_error_event
from kodawari.instincts.storage import InstinctStore


@pytest.fixture(autouse=True)
def _isolate_global_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect every global-store access in this test module to tmp_path."""
    target = tmp_path / "global_instincts.json"
    monkeypatch.setenv(GLOBAL_STORE_ENV_VAR, str(target))
    return target


def _make_event(message: str, *, run_id: str, error_code: str = "", category: str = "implement") -> dict:
    return {
        "message": message,
        "category": category,
        "phase": "IMPLEMENT",
        "action": "IMPLEMENT",
        "run_id": run_id,
        "error_code": error_code,
    }


def _portable_learned(signature: str = "boom", confidence: float = 0.9) -> LearnedInstinct:
    return LearnedInstinct(
        id=f"learned-{signature}",
        signature=signature,
        pattern="src/auth/refresh_token.py",
        category="external_gateway",
        confidence=confidence,
        count=6,
        source="error_learning",
        explanation="Promoted",
        first_seen="2026-01-01T00:00:00Z",
        last_seen="2026-04-01T00:00:00Z",
        archived=False,
        metadata={"error_code": "REVIEW_GATEWAY_BLOCKED", "backend": "claude"},
    )


def test_global_store_path_honors_env_override(tmp_path: Path, _isolate_global_path: Path) -> None:
    store = GlobalInstinctStore()
    assert store.path == _isolate_global_path
    assert not store.exists()


def test_upsert_inserts_new_signature(_isolate_global_path: Path) -> None:
    store = GlobalInstinctStore()
    result = store.upsert_learned(_portable_learned())

    assert result["decision"] == "inserted"
    assert _isolate_global_path.exists()
    payload = json.loads(_isolate_global_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "v1"
    assert len(payload["learned_instincts"]) == 1
    assert payload["learned_instincts"][0]["signature"] == "boom"


def test_upsert_merges_duplicate_signature_without_downgrade(_isolate_global_path: Path) -> None:
    store = GlobalInstinctStore()
    store.upsert_learned(_portable_learned(confidence=0.92))
    # Lower-confidence incoming row must NOT lower the stored value.
    result = store.upsert_learned(_portable_learned(confidence=0.80))

    assert result["decision"] == "merged"
    rows = store.load_learned()
    assert len(rows) == 1
    assert rows[0].confidence == pytest.approx(0.92)


def test_concurrent_upserts_keep_store_valid(_isolate_global_path: Path) -> None:
    """Race two writers with distinct signatures and verify both land."""
    store = GlobalInstinctStore()

    def _writer(sig: str) -> None:
        store.upsert_learned(_portable_learned(signature=sig, confidence=0.9))

    threads = [threading.Thread(target=_writer, args=(f"sig_{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    rows = store.load_learned()
    signatures = {row.signature for row in rows}
    assert signatures == {f"sig_{i}" for i in range(8)}


def test_corrupt_global_store_does_not_crash_reader(_isolate_global_path: Path) -> None:
    _isolate_global_path.parent.mkdir(parents=True, exist_ok=True)
    _isolate_global_path.write_text("{not-json", encoding="utf-8")
    # Must NOT raise.
    assert GlobalInstinctStore().load_learned() == []


def test_portable_judgment_requires_portable_error_code() -> None:
    portable_li = _portable_learned()
    portable_li.metadata = {"error_code": "CODEX_CLI_TIMEOUT"}
    portable_li.category = "runtime"
    assert is_portable_learned_instinct(portable_li) is True

    repo_specific = _portable_learned()
    repo_specific.metadata = {"error_code": ""}  # no structured code
    repo_specific.pattern = "tests/test_ranking.py"
    repo_specific.category = "implement"
    assert is_portable_learned_instinct(repo_specific) is False

    # High confidence + portable category but unrecognised code → not portable.
    weird_code = _portable_learned()
    weird_code.metadata = {"error_code": "SOMETHING_REPO_SPECIFIC"}
    weird_code.category = "runtime"
    assert is_portable_learned_instinct(weird_code) is False


def test_project_promotion_publishes_portable_to_global(
    tmp_path: Path,
    _isolate_global_path: Path,
) -> None:
    """Run a project ingest until the LearnedInstinct's confidence crosses
    the global threshold; the global store must have the entry."""
    project_root = tmp_path / "project_a"
    msg = "codex_cli execution timed out"
    # PR2.5 distinct-run semantics: each run_id only counts once.
    # Threshold 3 → confidence 0.75 at first promote. We need confidence
    # ≥ 0.85 to trigger global promotion → 0.75 + 4*0.03 = 0.87 → 7 distinct runs.
    for i in range(7):
        ingest_error_event(
            project_root,
            _make_event(msg, run_id=f"run_{i}", error_code="CODEX_CLI_TIMEOUT", category="runtime"),
        )

    global_store = GlobalInstinctStore()
    rows = global_store.load_learned()
    assert len(rows) == 1
    assert rows[0].metadata.get("error_code") == "CODEX_CLI_TIMEOUT"
    assert rows[0].confidence >= GLOBAL_PROMOTION_CONFIDENCE_THRESHOLD


def test_project_promotion_keeps_non_portable_local(
    tmp_path: Path,
    _isolate_global_path: Path,
) -> None:
    """A high-confidence pattern with no portable error_code stays local."""
    project_root = tmp_path / "project_b"
    msg = "tests/test_ranking.py keeps failing"
    for i in range(7):
        # Category 'implement' is always-learnable but is NOT in
        # _PORTABLE_CATEGORIES, so even with a fake error_code this MUST
        # stay project-local.
        ingest_error_event(
            project_root,
            _make_event(msg, run_id=f"run_{i}", error_code="", category="implement"),
        )

    project_payload = InstinctStore(project_root).load()
    assert any(
        li.confidence >= GLOBAL_PROMOTION_CONFIDENCE_THRESHOLD
        for li in project_payload.learned_instincts
    )
    # Global store stays empty.
    assert GlobalInstinctStore().load_learned() == []
    assert not _isolate_global_path.exists()


def test_select_instinct_hints_merges_global_into_project(
    tmp_path: Path,
    _isolate_global_path: Path,
) -> None:
    project_root = tmp_path / "project_c"
    # Seed the global store directly with a portable hint.
    GlobalInstinctStore().upsert_learned(_portable_learned(signature="global_only", confidence=0.91))

    hints = select_instinct_hints(project_root, limit=10, min_confidence=0.5)
    # Project store is empty; the merged result still surfaces the global row.
    assert any(row.get("scope") == "global" and row.get("signature") == "global_only" for row in hints)


def test_project_hints_win_on_signature_conflict(
    tmp_path: Path,
    _isolate_global_path: Path,
) -> None:
    project_root = tmp_path / "project_d"
    # Seed global with one row.
    GlobalInstinctStore().upsert_learned(_portable_learned(signature="boom", confidence=0.95))
    # Promote the same signature locally with lower confidence — project wins
    # because the local repo knows its own failure shape best.
    msg = "boom"
    for i in range(3):
        ingest_error_event(
            project_root,
            _make_event(msg, run_id=f"run_{i}", error_code="CODEX_CLI_TIMEOUT", category="runtime"),
        )
    hints = select_instinct_hints(project_root, limit=10, min_confidence=0.5)
    boom_rows = [row for row in hints if row.get("signature") == "boom"]
    # Only the project-scoped row survives the merge.
    assert len(boom_rows) == 1
    assert boom_rows[0].get("scope") == "project"
