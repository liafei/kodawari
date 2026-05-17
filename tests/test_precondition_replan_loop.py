"""Plan-time readiness BLOCK now feeds the planner one retry round.

The orchestrator used to break the planning loop the first time
``evaluate_plan_execution_readiness`` reported BLOCKED. With the new
behaviour the readiness finding is surfaced as a blocking review payload
and the planner gets another round to insert a prereq schema/migration
task. Stubborn streaks (same missing preconditions twice in a row) still
escalate.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from kodawari.autopilot.planning import planning_orchestrator
from kodawari.autopilot.planning.planning_orchestrator import PlanningConfig


def _patch_planning_context(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        planning_orchestrator,
        "collect_planning_context",
        lambda **_kwargs: {
            "schema_version": "planning.context.v1",
            "task_direction": "",
            "project_root": "",
            "claude_md": "",
            "task_plans": "",
            "dev_status": "",
            "prd_coverage": "",
            "prd_excerpt": "",
            "readme_excerpt": "",
            "recent_commits": "",
            "uncommitted_changes": "",
            "precondition_replan_hint": {},
            "repo_inventory_summary": {"archetype": "", "code_roots": [], "capabilities": []},
            "repo_manifest": {"files": []},
            "snippets": [],
            "instinct_risk_zones": {},
            "telemetry_summary": {},
            "lane_stability": {},
            "failing_baseline": {},
            "git_head": "",
            "file_manifest": {},
        },
    )


def _write_schema(tmp_path: Path) -> None:
    sql = tmp_path / "backend" / "db" / "schema.sql"
    sql.parent.mkdir(parents=True, exist_ok=True)
    sql.write_text("CREATE TABLE events (event_id TEXT PRIMARY KEY);\n", encoding="utf-8")


def _task(task_id: str, files: list[str], requires: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "task_name": task_id,
        "why_this_layer": "test",
        "files_to_change": files,
        "new_files": files,
        "invariants": [],
        "test_plan": "pytest -q",
        "requires": requires,
    }


def _plan(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": "contract_first.task_graph.v1",
        "task_direction": "test",
        "tasks": tasks,
        "module_boundaries": [],
        "verify_recipes": [],
        "approval_points": [],
        "execution_constraints": {},
        "confidence": "medium",
        "confidence_issues": [],
    }


def test_planner_stubbornness_still_escalates_after_two_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If round 2 emits the same plan with the same missing preconditions,
    the loop must escalate instead of looping forever."""

    _patch_planning_context(monkeypatch)
    _write_schema(tmp_path)

    same_plan = _plan(
        tasks=[
            _task(
                "T1",
                files=["src/x.py"],
                requires=[{"kind": "field", "name": "events.missing", "source": "existing"}],
            )
        ]
    )
    monkeypatch.setattr(planning_orchestrator, "generate_plan", lambda **_kwargs: (same_plan, ""))
    monkeypatch.setattr(planning_orchestrator, "review_plan", lambda **_kwargs: (None, "should not run"))

    planning_dir = tmp_path / "planning" / "feat"
    result = planning_orchestrator.run_planning_conversation(
        config=PlanningConfig(max_rounds=5, decision_policy="strict-gate"),
        project_root=tmp_path,
        planning_dir=planning_dir,
        task_direction="add missing field consumer",
        repo_inventory={"archetype": "fastapi_api"},
        feature="feat",
    )

    assert result.status == "precondition_blocked"
    assert len(result.rounds) == 2
    assert result.escalation is not None
    assert result.escalation["gate_reason"] == "blocked_by_precondition"
