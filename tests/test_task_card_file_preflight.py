"""Unit tests for the minimum task-card file preflight.

Locks 2026-04-23 Item 7 of the 8-vulnerability closeout: refuse to start
the executor when files_to_change entries do not exist and are not declared
as new_files.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from kodawari.autopilot.planning.task_card_file_preflight import (
    preflight_enabled,
    run_file_preflight,
)


def _card(**overrides) -> dict:
    base = {
        "schema_version": "contract_first.task_card.v1",
        "task_id": "T1",
        "task_name": "demo",
        "why_this_layer": "service",
        "files_to_change": [],
        "invariants": ["x"],
        "test_plan": "run tests",
    }
    base.update(overrides)
    return base


def test_preflight_passes_when_all_files_exist(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("# a\n", encoding="utf-8")
    (tmp_path / "src" / "b.py").write_text("# b\n", encoding="utf-8")
    card = _card(files_to_change=["src/a.py", "src/b.py"])
    report = run_file_preflight(card, tmp_path)
    assert report.blocked is False
    assert report.issues == ()


def test_preflight_blocks_on_missing_source_file(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "existing.py").write_text("# ok\n", encoding="utf-8")
    card = _card(files_to_change=["src/existing.py", "src/typo_name.py"])
    report = run_file_preflight(card, tmp_path)
    assert report.blocked is True
    assert len(report.issues) == 1
    assert report.issues[0].kind == "missing_source"
    assert report.issues[0].path == "src/typo_name.py"


def test_preflight_suggests_possible_matches(tmp_path: Path) -> None:
    (tmp_path / "backend" / "routes").mkdir(parents=True)
    (tmp_path / "backend" / "routes" / "hot_channel_routes.py").write_text("# ok\n", encoding="utf-8")
    card = _card(files_to_change=["backend/routes/hot_routes.py"])
    report = run_file_preflight(card, tmp_path)
    assert report.blocked is True
    match_paths = {m.replace("\\", "/") for m in report.issues[0].possible_matches}
    assert "backend/routes/hot_channel_routes.py" in match_paths


def test_preflight_passes_when_missing_file_is_declared_new(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "existing.py").write_text("# ok\n", encoding="utf-8")
    card = _card(
        files_to_change=["src/existing.py", "src/to_create.py"],
        new_files=["src/to_create.py"],
    )
    report = run_file_preflight(card, tmp_path)
    assert report.blocked is False


def test_preflight_blocks_when_new_file_already_exists(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "already_here.py").write_text("# existing\n", encoding="utf-8")
    card = _card(
        files_to_change=["src/already_here.py"],
        new_files=["src/already_here.py"],
    )
    report = run_file_preflight(card, tmp_path)
    assert report.blocked is True
    assert report.issues[0].kind == "new_file_already_exists"


def test_preflight_blocks_when_new_files_not_subset(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("# a\n", encoding="utf-8")
    card = _card(
        files_to_change=["src/a.py"],
        new_files=["src/outside_scope.py"],
    )
    report = run_file_preflight(card, tmp_path)
    assert report.blocked is True
    kinds = {issue.kind for issue in report.issues}
    assert "new_files_not_subset" in kinds


def test_preflight_blocks_path_traversal_outside_project_root(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside_demo_file.py"
    outside.write_text("# outside\n", encoding="utf-8")
    card = _card(files_to_change=["../outside_demo_file.py"])
    report = run_file_preflight(card, tmp_path)
    assert report.blocked is True
    assert report.issues[0].kind == "path_outside_project_root"
    outside.unlink(missing_ok=True)


def test_preflight_env_var_can_disable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKFLOW_CONTRACT_PREFLIGHT", "0")
    card = _card(files_to_change=["src/does_not_exist.py"])
    report = run_file_preflight(card, tmp_path)
    assert report.blocked is False
    assert report.skipped is True
    assert preflight_enabled() is False


def test_preflight_default_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WORKFLOW_CONTRACT_PREFLIGHT", raising=False)
    assert preflight_enabled() is True


def test_preflight_empty_files_to_change_passes(tmp_path: Path) -> None:
    card = _card(files_to_change=[])
    report = run_file_preflight(card, tmp_path)
    assert report.blocked is False
    assert report.issues == ()


def test_v1_1_blocks_empty_verify_cmd(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("def ok():\n    return True\n", encoding="utf-8")
    card = _card(
        schema_version="contract_first.task_card.v1.1",
        files_to_change=["src/a.py"],
        verify_cmd="",
    )
    report = run_file_preflight(card, tmp_path)
    assert report.blocked is True
    assert "invalid_verify_cmd" in {issue.kind for issue in report.issues}


def test_v1_1_blocks_large_file_without_target_symbols(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    big_file = tmp_path / "src" / "big.py"
    big_file.write_text("".join(f"# line {i}\n" for i in range(900)), encoding="utf-8")
    card = _card(
        schema_version="contract_first.task_card.v1.1",
        files_to_change=["src/big.py"],
        verify_cmd="pytest -q",
    )
    report = run_file_preflight(card, tmp_path)
    assert report.blocked is True
    assert "large_file_requires_target_symbols" in {issue.kind for issue in report.issues}


def test_v1_1_deep_exempt_large_file_emits_warning_not_block(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    big_file = tmp_path / "src" / "big.py"
    big_file.write_text("".join(f"# line {i}\n" for i in range(900)), encoding="utf-8")
    card = _card(
        schema_version="contract_first.task_card.v1.1",
        files_to_change=["src/big.py"],
        verify_cmd="pytest -q",
        budget_tier="deep",
        do_not_change=["ranking weights"],
        scout_report={"user_acknowledged_partial_symbol_map": True},
    )
    report = run_file_preflight(card, tmp_path)
    assert report.blocked is False
    assert report.issues == ()
    assert "large_file_symbol_map_deep_exempt" in {item.kind for item in report.warnings}


def test_v1_1_passes_large_file_when_symbol_is_declared_and_resolved(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    big_file = tmp_path / "src" / "big.py"
    big_file.write_text(
        "class Svc:\n    def run(self):\n        return 1\n" + "".join(f"# line {i}\n" for i in range(850)),
        encoding="utf-8",
    )
    card = _card(
        schema_version="contract_first.task_card.v1.1",
        files_to_change=["src/big.py"],
        verify_cmd="pytest -q",
        target_symbols=[{"file": "src/big.py", "kind": "method", "class": "Svc", "name": "run"}],
    )
    report = run_file_preflight(card, tmp_path)
    assert report.blocked is False
    assert report.issues == ()


def test_v1_1_blocks_symbol_not_found(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("def existing():\n    return True\n", encoding="utf-8")
    card = _card(
        schema_version="contract_first.task_card.v1.1",
        files_to_change=["src/a.py"],
        verify_cmd="pytest -q",
        target_symbols=[{"file": "src/a.py", "kind": "function", "name": "missing"}],
    )
    report = run_file_preflight(card, tmp_path)
    assert report.blocked is True
    assert "symbol_not_found" in {issue.kind for issue in report.issues}


def test_v1_1_blocks_unauthorized_mutation(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("def existing():\n    return True\n", encoding="utf-8")
    card = _card(
        schema_version="contract_first.task_card.v1.1",
        files_to_change=["src/a.py"],
        verify_cmd="pytest -q",
        behavior_changes=[{"id": "display_count", "from": "5", "to": "4", "scope": "display only"}],
        allowed_test_mutations=[
            {
                "file": "tests/test_outside.py",
                "match_kind": "literal_assert",
                "old_pattern": "assert len(items) == 5",
                "new_pattern": "assert len(items) == 4",
                "behavior_change_id": "wrong_id",
            }
        ],
    )
    report = run_file_preflight(card, tmp_path)
    assert report.blocked is True
    assert "unauthorized_mutation" in {issue.kind for issue in report.issues}


def test_v1_1_blocks_stale_freshness_hash(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("def existing():\n    return True\n", encoding="utf-8")
    card = _card(
        schema_version="contract_first.task_card.v1.1",
        files_to_change=["src/a.py"],
        verify_cmd="pytest -q",
        freshness={
            "source_file_hashes": [
                {"path": "src/a.py", "sha256": "not-the-real-hash", "line_count": 2},
            ]
        },
    )
    report = run_file_preflight(card, tmp_path)
    assert report.blocked is True
    assert "stale_task_card" in {issue.kind for issue in report.issues}
