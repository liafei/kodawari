"""Tests for _resolve_gate_from_autopilot_rounds fallback in delivery_evidence."""

from __future__ import annotations

import json
from pathlib import Path

from kodawari.cli.delivery_evidence import _resolve_gate_from_autopilot_rounds


def _write_rounds(planning_dir: Path, records: list[dict]) -> None:
    planning_dir.mkdir(parents=True, exist_ok=True)
    lines = "\n".join(json.dumps(r, ensure_ascii=False) for r in records)
    (planning_dir / ".autopilot_rounds.jsonl").write_text(lines, encoding="utf-8")


def test_returns_none_when_no_rounds_file(tmp_path: Path) -> None:
    result = _resolve_gate_from_autopilot_rounds(tmp_path)
    assert result is None


def test_returns_pass_from_rules_gate_round(tmp_path: Path) -> None:
    _write_rounds(tmp_path, [
        {
            "stage": "RULES_GATE",
            "details": {"gate_check": {"total_status": "PASS"}},
        }
    ])
    result = _resolve_gate_from_autopilot_rounds(tmp_path)
    assert result is not None
    assert result["status"] == "PASS"
    assert result["gate_status"] == "PASS"
    assert result["source"] == ".autopilot_rounds.jsonl"


def test_returns_pass_from_proceed_to_gate_round(tmp_path: Path) -> None:
    _write_rounds(tmp_path, [
        {
            "stage": "PROCEED_TO_GATE",
            "details": {"gate_check": {"total_status": "PASS"}},
        }
    ])
    result = _resolve_gate_from_autopilot_rounds(tmp_path)
    assert result is not None
    assert result["status"] == "PASS"


def test_returns_none_when_no_gate_stage(tmp_path: Path) -> None:
    _write_rounds(tmp_path, [
        {"stage": "CODEX_IMPLEMENT", "details": {}},
        {"stage": "VERIFY", "details": {}},
    ])
    result = _resolve_gate_from_autopilot_rounds(tmp_path)
    assert result is None


def test_returns_fail_when_gate_status_blocked(tmp_path: Path) -> None:
    _write_rounds(tmp_path, [
        {
            "stage": "RULES_GATE",
            "details": {"gate_check": {"total_status": "BLOCKED", "blocking_reason": "lints fail"}},
        }
    ])
    result = _resolve_gate_from_autopilot_rounds(tmp_path)
    assert result is not None
    assert result["status"] == "FAIL"
    assert result["gate_status"] == "BLOCKED"
    assert "lints fail" in result["reason"]


def test_returns_none_when_gate_check_missing(tmp_path: Path) -> None:
    _write_rounds(tmp_path, [
        {"stage": "RULES_GATE", "details": {}},
    ])
    result = _resolve_gate_from_autopilot_rounds(tmp_path)
    assert result is None


def test_uses_last_rules_gate_round_when_multiple(tmp_path: Path) -> None:
    # A later PASS round supersedes an earlier FAIL (gate was retried and fixed).
    _write_rounds(tmp_path, [
        {"stage": "RULES_GATE", "details": {"gate_check": {"total_status": "BLOCKED"}}},
        {"stage": "RULES_GATE", "details": {"gate_check": {"total_status": "PASS"}}},
    ])
    result = _resolve_gate_from_autopilot_rounds(tmp_path)
    assert result is not None
    assert result["status"] == "PASS"


def test_fail_result_does_not_propagate_as_pass(tmp_path: Path) -> None:
    # Regression guard: previously the fallback silently fell through on
    # non-PASS gate, masking a real FAIL as "gate result unavailable".
    _write_rounds(tmp_path, [
        {"stage": "RULES_GATE", "details": {"gate_check": {"total_status": "FAIL"}}},
    ])
    result = _resolve_gate_from_autopilot_rounds(tmp_path)
    assert result is not None
    assert result["status"] == "FAIL"
