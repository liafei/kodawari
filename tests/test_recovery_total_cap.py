"""Task-level total recovery attempt cap.

Verifies _executor_recovery_total_attempt_cap honors the env var, the
task card runtime_caps override, and the conservative default.
"""

from __future__ import annotations

from typing import Any

import pytest

from kodawari.autopilot.engine.engine_recovery_mixin import EngineRecoveryMixin


class _StubAdapter:
    config = None


class _StubEngine(EngineRecoveryMixin):
    def __init__(self, task_card: dict[str, Any] | None = None) -> None:
        self._task_card_payload = task_card or {}
        self.adapter = _StubAdapter()


def test_total_cap_default_is_eight() -> None:
    engine = _StubEngine()
    assert engine._executor_recovery_total_attempt_cap() == 8


def test_total_cap_env_var_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKFLOW_RECOVERY_MAX_TOTAL_ATTEMPTS", "12")
    engine = _StubEngine()
    assert engine._executor_recovery_total_attempt_cap() == 12


def test_total_cap_env_var_invalid_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKFLOW_RECOVERY_MAX_TOTAL_ATTEMPTS", "not-a-number")
    engine = _StubEngine()
    assert engine._executor_recovery_total_attempt_cap() == 8


def test_total_cap_card_runtime_caps_used_when_no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WORKFLOW_RECOVERY_MAX_TOTAL_ATTEMPTS", raising=False)
    engine = _StubEngine(task_card={"runtime_caps": {"max_total_recovery_attempts": 5}})
    assert engine._executor_recovery_total_attempt_cap() == 5


def test_total_cap_env_var_wins_over_card(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKFLOW_RECOVERY_MAX_TOTAL_ATTEMPTS", "20")
    engine = _StubEngine(task_card={"runtime_caps": {"max_total_recovery_attempts": 5}})
    assert engine._executor_recovery_total_attempt_cap() == 20


def test_total_cap_minimum_one() -> None:
    """Cap is clamped to at least 1 so a misconfigured 0 cannot stall every task."""
    engine = _StubEngine(task_card={"runtime_caps": {"max_total_recovery_attempts": 0}})
    # 0 falls back to default 8 (not the floor 1) per the helper's `or 8` guard
    assert engine._executor_recovery_total_attempt_cap() == 8
