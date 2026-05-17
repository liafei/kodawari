"""Behavior pin: ``_run_task_card`` stamps ``engine.state.run_id`` BEFORE
calling ``run_collaboration_loop``.

Without the stamp, every ErrorEvent emitted during the in-flight task-run
would carry an empty run_id, breaking distinct-run learning downstream.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import pytest

from kodawari.cli.contract import contract_first_cmd


class _FakeState:
    def __init__(self) -> None:
        self.run_id = ""


class _FakeEngine:
    """Minimal stand-in that records the state.run_id observed at the moment
    ``run_collaboration_loop`` is invoked."""

    def __init__(self, *, config: Any, requirements_text: Any) -> None:
        del config, requirements_text
        self.state = _FakeState()
        self.run_id_at_loop_start: str | None = None

    def run_collaboration_loop(
        self,
        *,
        task_label: str,
        task_scope: str,
        enable_peer_review: bool,
    ) -> dict[str, Any]:
        del task_label, task_scope, enable_peer_review
        # Snapshot run_id at the moment the loop starts; any later mutation
        # by other code paths must NOT change this captured value.
        self.run_id_at_loop_start = self.state.run_id
        return {"reason": "PROCEED_TO_GATE", "rounds": []}


def _build_args(tmp_path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        project_root=str(tmp_path),
        feature="feat",
        requirements_file=None,
        verify_cmd="pytest -q",
        max_cycles=2,
        token_budget=10000,
        executor_backend="",
        executor_command="",
        self_review_backend="",
        self_review_command="",
        contract_mode="warn",
        phase_mode="implement",
        strict_scope=False,
        card=str(tmp_path / "TASK_CARD.json"),
        real_peer_review=False,
        require_real_peer_review=False,
        opus_reviewer_backend="",
        executor_model="",
        reviewer_backend="",
        reviewer_model="",
        reviewer_api_format="",
        reviewer_base_url="",
        peer_review_max_tokens=2048,
        rollback_on_failure=False,
        max_verify_retries=2,
        no_enable_peer_review=False,
    )


def test_run_task_card_stamps_run_id_before_loop_starts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, _FakeEngine] = {}

    def _capture(*, config: Any, requirements_text: Any) -> _FakeEngine:
        engine = _FakeEngine(config=config, requirements_text=requirements_text)
        captured["engine"] = engine
        return engine

    monkeypatch.setattr(contract_first_cmd, "AutopilotEngine", _capture)
    args = _build_args(tmp_path)
    card = {
        "task_id": "T1",
        "task_name": "Stamp run_id",
        "files_to_change": ["src/x.py"],
        "test_plan": "pytest -q",
    }

    result = contract_first_cmd._run_task_card(
        args,
        card=card,
        card_path=tmp_path / "TASK_CARD_T1.json",
        run_id="run_xyz_42",
    )

    engine = captured["engine"]
    assert engine.run_id_at_loop_start == "run_xyz_42", (
        "run_id must be stamped on engine.state BEFORE the collaboration loop "
        "runs, otherwise in-flight ErrorEvents would carry an empty run_id."
    )
    # Sanity: the loop ran and returned its fake result.
    assert result["reason"] == "PROCEED_TO_GATE"


def test_run_task_card_without_run_id_does_not_overwrite_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backward compat: callers that omit run_id must not blank a state that
    might have been populated upstream."""
    captured: dict[str, _FakeEngine] = {}

    def _capture(*, config: Any, requirements_text: Any) -> _FakeEngine:
        engine = _FakeEngine(config=config, requirements_text=requirements_text)
        engine.state.run_id = "preexisting_run"
        captured["engine"] = engine
        return engine

    monkeypatch.setattr(contract_first_cmd, "AutopilotEngine", _capture)
    args = _build_args(tmp_path)
    card = {
        "task_id": "T1",
        "task_name": "no run_id",
        "files_to_change": [],
        "test_plan": "",
    }

    contract_first_cmd._run_task_card(
        args,
        card=card,
        card_path=tmp_path / "TASK_CARD_T1.json",
    )

    assert captured["engine"].run_id_at_loop_start == "preexisting_run"
