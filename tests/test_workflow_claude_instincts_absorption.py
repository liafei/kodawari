import json
from pathlib import Path

from kodawari.instincts import (
    Instinct,
    InstinctStore,
    InstinctStoreData,
    ingest_error_event,
    learn_from_globs,
    list_instincts,
    schema_document,
    select_instinct_hints,
)


def _store_path(project_root: Path) -> Path:
    return (project_root / ".workflow" / "instincts.json").resolve()


def test_instincts_learn_from_globs_creates_store_and_lists_items(tmp_path: Path) -> None:
    result = learn_from_globs(
        tmp_path,
        ["planning/*", "src/**/*.py", "planning/*", "  "],
    )

    expected_path = _store_path(tmp_path)
    assert result["inserted"] == 2
    assert result["updated"] == 0
    assert result["patterns"] == ["planning/*", "src/**/*.py"]
    assert result["store_path"] == str(expected_path)
    assert expected_path.exists()

    raw = json.loads(expected_path.read_text(encoding="utf-8"))
    assert raw["schema_version"] == "v1"
    assert len(raw["instincts"]) == 2
    assert schema_document()["schema_version"] == "v1"

    listed = list_instincts(tmp_path)
    assert listed["count"] == 2
    assert listed["include_archived"] is False
    assert {item["pattern"] for item in listed["items"]} == {"planning/*", "src/**/*.py"}


def test_instincts_learn_updates_existing_pattern_and_unarchives(tmp_path: Path) -> None:
    initial = InstinctStoreData(
        instincts=[
            Instinct(
                id="instinct-1",
                pattern="planning/*",
                category="recovery",
                confidence=0.2,
                archived=True,
            )
        ]
    )
    store = InstinctStore(tmp_path)
    store.save(initial)

    result = learn_from_globs(tmp_path, ["planning/*"])
    assert result["inserted"] == 0
    assert result["updated"] == 1

    payload = store.load()
    assert len(payload.instincts) == 1
    item = payload.instincts[0]
    assert item.pattern == "planning/*"
    assert item.archived is False
    assert item.confidence >= 0.6


def test_select_instinct_hints_respects_limit_confidence_and_archived_filter(tmp_path: Path) -> None:
    store = InstinctStore(tmp_path)
    store.save(
        InstinctStoreData(
            instincts=[
                Instinct(id="instinct-1", pattern="a/*", confidence=0.95, archived=False),
                Instinct(id="instinct-2", pattern="b/*", confidence=0.70, archived=False),
                Instinct(id="instinct-3", pattern="c/*", confidence=0.80, archived=True),
                Instinct(id="instinct-4", pattern="d/*", confidence=0.55, archived=False),
            ]
        )
    )

    hints = select_instinct_hints(tmp_path, limit=2, min_confidence=0.6)
    assert len(hints) == 2
    assert [item["pattern"] for item in hints] == ["a/*", "b/*"]

    visible = list_instincts(tmp_path, min_confidence=0.0, include_archived=False)
    assert visible["count"] == 3
    assert all(item["archived"] is False for item in visible["items"])

    with_archived = list_instincts(tmp_path, min_confidence=0.0, include_archived=True)
    assert with_archived["count"] == 4


def test_ingest_error_event_promotes_candidate_to_learned_instinct_after_threshold(tmp_path: Path) -> None:
    for _ in range(3):
        result = ingest_error_event(
            tmp_path,
            {
                "phase": "GATE",
                "action": "RULES_GATE",
                "category": "gate",
                "message": "tests/test_rank_scope.py failed: gate blocked by redline",
            },
            threshold=3,
        )

    assert result["updated"] is True
    assert result["candidate_count"] == 3
    assert result["threshold"] == 3
    assert result["learned_pattern"] == "tests/test_rank_scope.py"

    payload = InstinctStore(tmp_path).load()
    assert payload.learning_candidates
    assert payload.learning_candidates[0].promoted is True
    assert payload.learned_instincts
    assert payload.learned_instincts[0].pattern == "tests/test_rank_scope.py"
    hints = select_instinct_hints(tmp_path, limit=5, min_confidence=0.6)
    assert any(item["pattern"] == "tests/test_rank_scope.py" for item in hints)
