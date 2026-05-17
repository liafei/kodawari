"""Regression tests: load_task_card and engine._load_contract_json must handle UTF-8 BOM.

Windows tools (PowerShell Set-Content -Encoding utf8) produce UTF-8 BOM files.
Before fix: encoding="utf-8" raised JSONDecodeError silently -> returned None ->
phase guard blocked with "requires an active task card in contract-first mode".
After fix: encoding="utf-8-sig" strips BOM transparently.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from kodawari.autopilot.core.phase_guard import load_task_card, pre_implement_guard


_MINIMAL_CARD = {
    "schema_version": "contract_first.task_card.v1",
    "task_id": "T1",
    "task_name": "test task",
    "why_this_layer": "service",
    "files_to_change": ["backend/service.py"],
    "invariants": ["endpoint must exist"],
    "test_plan": "pytest tests/ -q",
}

_UTF8_BOM = b"\xef\xbb\xbf"


def _write_json(path: Path, payload: dict, *, bom: bool) -> None:
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    with open(path, "wb") as fh:
        if bom:
            fh.write(_UTF8_BOM)
        fh.write(raw)


def test_load_task_card_plain_utf8(tmp_path: Path) -> None:
    p = tmp_path / "card.json"
    _write_json(p, _MINIMAL_CARD, bom=False)
    result = load_task_card(p)
    assert isinstance(result, dict)
    assert result["task_id"] == "T1"


def test_load_task_card_utf8_bom(tmp_path: Path) -> None:
    p = tmp_path / "card.json"
    _write_json(p, _MINIMAL_CARD, bom=True)
    result = load_task_card(p)
    assert isinstance(result, dict), "load_task_card must not return None for UTF-8 BOM files"
    assert result["task_id"] == "T1"


def test_pre_implement_guard_passes_with_bom_card(tmp_path: Path) -> None:
    p = tmp_path / "card.json"
    _write_json(p, _MINIMAL_CARD, bom=True)
    card = load_task_card(p)
    result = pre_implement_guard(
        phase_mode="implement",
        contract_mode="strict",
        task_card=card,
    )
    assert not result.blocked, f"phase guard must not block when card loaded from BOM file: {result.reason}"


def test_pre_implement_guard_blocks_when_card_is_none() -> None:
    result = pre_implement_guard(
        phase_mode="implement",
        contract_mode="strict",
        task_card=None,
    )
    assert result.blocked
    assert "active task card" in result.reason


def test_load_task_card_missing_file(tmp_path: Path) -> None:
    result = load_task_card(tmp_path / "nonexistent.json")
    assert result is None


def test_load_task_card_none_path() -> None:
    assert load_task_card(None) is None


def test_engine_explicit_card_path_must_resolve_no_silent_active_fallback(tmp_path: Path) -> None:
    """When task_card_path is explicitly set but unloadable, engine MUST raise.

    Regression test: previously, if --card pointed to a missing path, the engine
    silently fell back to TASK_CARD_ACTIVE.json, which could belong to a
    different task (e.g. T1) and silently produce the wrong files_to_change for
    the requested task (e.g. T2).
    """
    from kodawari.autopilot.engine import AutopilotConfig, AutopilotEngine

    planning = tmp_path / "planning" / "newsapp"
    planning.mkdir(parents=True)
    # ACTIVE points at T1, but caller asked for a non-existent T_BOGUS path.
    (planning / "TASK_CARD_ACTIVE.json").write_text(
        json.dumps({**_MINIMAL_CARD, "task_id": "T1", "files_to_change": ["a.py"]}),
        encoding="utf-8",
    )
    bogus_path = planning / "TASK_CARD_DOES_NOT_EXIST.json"
    config = AutopilotConfig(
        project_root=tmp_path,
        feature="newsapp",
        task_card_path=bogus_path,
    )
    with pytest.raises(FileNotFoundError, match="silent fallback"):
        AutopilotEngine(config, requirements_text="x")


def test_engine_no_card_path_falls_back_to_active(tmp_path: Path) -> None:
    """When task_card_path is None, ACTIVE fallback is the legacy contract."""
    from kodawari.autopilot.engine import AutopilotConfig, AutopilotEngine

    planning = tmp_path / "planning" / "newsapp"
    planning.mkdir(parents=True)
    (planning / "TASK_CARD_ACTIVE.json").write_text(
        json.dumps({**_MINIMAL_CARD, "task_id": "T_ACTIVE"}),
        encoding="utf-8",
    )
    config = AutopilotConfig(
        project_root=tmp_path,
        feature="newsapp",
        task_card_path=None,
    )
    engine = AutopilotEngine(config, requirements_text="x")
    assert (engine._task_card_payload or {}).get("task_id") == "T_ACTIVE"
