from __future__ import annotations

import json
from pathlib import Path

import pytest

from kodawari.cli.contract.bridge_types import AutopilotPlanningBridgeError
from kodawari.cli.contract.model_bootstrap import (
    _PlanningInputs,
    _bootstrap_from_fresh_plan,
)


def test_fresh_planning_exception_is_wrapped_for_terminal_finalization(tmp_path: Path) -> None:
    planning_dir = tmp_path / "planning" / "feature"
    planning_dir.mkdir(parents=True)
    inputs = _PlanningInputs(
        planning_mode="contract_first",
        refreshed_inventory={"archetype": "test"},
        resolved_task_direction="plan something",
        current_source={},
        existing_conversation=None,
        conversation_path=planning_dir / "PLANNING_CONVERSATION.json",
    )

    def run_planning_conversation_fn(**_kwargs):
        raise OSError(22, "Invalid argument")

    with pytest.raises(AutopilotPlanningBridgeError) as raised:
        _bootstrap_from_fresh_plan(
            inputs=inputs,
            feature="feature",
            prd_path=None,
            planning_dir=planning_dir,
            project_root=tmp_path,
            steps_run=[],
            artifacts={},
            run_planning_conversation_fn=run_planning_conversation_fn,
            planning_config_from_env_fn=lambda *_args, **_kwargs: object(),
            raise_if_context_scout_awaiting_decision_fn=lambda _payload: None,
        )

    assert raised.value.error_code == "fresh_planning_exception"
    assert raised.value.details["planning_status"] == "error"
    failure = json.loads((planning_dir / ".planning_failure.json").read_text(encoding="utf-8"))
    assert failure["error_code"] == "fresh_planning_exception"
    assert failure["reason"] == "fresh_planning_exception"
    assert failure["error_type"] == "OSError"
