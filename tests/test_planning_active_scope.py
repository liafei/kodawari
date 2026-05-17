"""Unit tests for active_scope.derive_active_scope (Fix B core).

Background — auto-skip historically passed any not-approved plan straight
to execution, regardless of whether reviewer blockers concerned the active
task or only future tasks in a multi-task epic. Fix B narrows the auto-skip
gate to the *active scope* (active task + its depends_on closure).

These tests pin the derivation contract:
  * single-leaf plans collapse to one active task,
  * multi-leaf plans use a conservative union,
  * TASK_CARD_ACTIVE.json hint takes priority,
  * an explicit hint argument overrides everything.
"""

from __future__ import annotations

import json
from pathlib import Path

from kodawari.autopilot.planning.active_scope import (
    derive_active_scope,
    topological_leaves,
)
from kodawari.autopilot.planning.planning_findings import (
    classify_findings_by_active_scope,
)


def _plan(tasks: list[dict]) -> dict:
    return {"tasks": tasks}


def test_single_leaf_collapses_to_one_active_task() -> None:
    """User's actual TTS-DEG-02 case: leaf with no depends_on, downstream task references it."""
    plan = _plan(
        [
            {"task_id": "TTS-DEG-02", "depends_on": []},
            {"task_id": "TTS-DEG-01", "depends_on": ["TTS-DEG-02"]},
        ]
    )

    out = derive_active_scope(plan_payload=plan)

    assert out["active_task_ids"] == ["TTS-DEG-02"]
    assert out["scope_task_ids"] == {"TTS-DEG-02"}
    assert out["source"] == "topological_first_leaf"


def test_multi_leaf_uses_conservative_union() -> None:
    plan = _plan(
        [
            {"task_id": "A", "depends_on": []},
            {"task_id": "B", "depends_on": []},
            {"task_id": "C", "depends_on": ["A", "B"]},
        ]
    )

    out = derive_active_scope(plan_payload=plan)

    assert sorted(out["active_task_ids"]) == ["A", "B"]
    assert out["scope_task_ids"] == {"A", "B"}
    assert out["source"] == "topological_multi_leaf"


def test_depends_on_closure_pulls_in_upstream_tasks() -> None:
    """Active task with upstream dependencies pulls them into scope.

    A reviewer finding about an upstream task can still concern the active
    task's execution surface (data contract, shared file). The conservative
    rule is to keep upstream in scope.
    """
    plan = _plan(
        [
            {"task_id": "A", "depends_on": []},
            {"task_id": "B", "depends_on": ["A"]},
            {"task_id": "C", "depends_on": []},
        ]
    )

    out = derive_active_scope(plan_payload=plan, hint_task_id="B")

    assert out["active_task_ids"] == ["B"]
    assert out["scope_task_ids"] == {"A", "B"}
    assert out["source"] == "hint"


def test_task_card_active_hint_takes_priority(tmp_path: Path) -> None:
    plan = _plan(
        [
            {"task_id": "TTS-DEG-02", "depends_on": []},
            {"task_id": "TTS-DEG-01", "depends_on": ["TTS-DEG-02"]},
        ]
    )
    planning_dir = tmp_path / "planning" / "feature"
    planning_dir.mkdir(parents=True)
    (planning_dir / "TASK_CARD_ACTIVE.json").write_text(
        json.dumps({"task_id": "TTS-DEG-02"}), encoding="utf-8"
    )

    out = derive_active_scope(plan_payload=plan, planning_dir=planning_dir)

    assert out["active_task_ids"] == ["TTS-DEG-02"]
    assert out["source"] == "task_card_active"


def test_explicit_hint_overrides_task_card_active(tmp_path: Path) -> None:
    plan = _plan(
        [
            {"task_id": "T1", "depends_on": []},
            {"task_id": "T2", "depends_on": []},
        ]
    )
    planning_dir = tmp_path / "planning" / "feature"
    planning_dir.mkdir(parents=True)
    (planning_dir / "TASK_CARD_ACTIVE.json").write_text(
        json.dumps({"task_id": "T1"}), encoding="utf-8"
    )

    out = derive_active_scope(plan_payload=plan, planning_dir=planning_dir, hint_task_id="T2")

    assert out["active_task_ids"] == ["T2"]
    assert out["source"] == "hint"


def test_topological_leaves_for_empty_plan() -> None:
    assert topological_leaves(None) == []
    assert topological_leaves({}) == []
    assert topological_leaves({"tasks": []}) == []


def test_unscoped_when_no_tasks() -> None:
    out = derive_active_scope(plan_payload={"tasks": []})
    assert out["active_task_ids"] == []
    assert out["scope_task_ids"] == set()
    assert out["source"] == "unscoped"


def test_classify_findings_by_active_scope_in_scope() -> None:
    """A finding mentioning the active task is in_scope."""
    findings = [
        {
            "severity": "blocking",
            "category": "scope",
            "description": "TTS-DEG-02 service layer is missing input validation",
            "recommendation": "add validation",
        }
    ]

    in_scope, out_of_scope, unscoped = classify_findings_by_active_scope(
        findings,
        scope_task_ids={"TTS-DEG-02"},
        known_task_ids=["TTS-DEG-02", "TTS-DEG-01"],
    )

    assert len(in_scope) == 1
    assert out_of_scope == []
    assert unscoped == []


def test_classify_findings_by_active_scope_out_of_scope() -> None:
    """A finding mentioning ONLY a future task is out_of_scope."""
    findings = [
        {
            "severity": "blocking",
            "category": "scope",
            "description": "TTS-DEG-01 route layer needs auth header check",
            "recommendation": "add header guard",
        }
    ]

    in_scope, out_of_scope, _unscoped = classify_findings_by_active_scope(
        findings,
        scope_task_ids={"TTS-DEG-02"},
        known_task_ids=["TTS-DEG-02", "TTS-DEG-01"],
    )

    assert in_scope == []
    assert len(out_of_scope) == 1


def test_classify_findings_no_task_mention_treated_as_in_scope() -> None:
    """Conservative: if a finding does not mention any task id, keep it
    blocking. We never silently demote findings whose scope can't be
    determined."""
    findings = [
        {
            "severity": "blocking",
            "category": "completeness",
            "description": "Plan does not anchor canonical PRD path",
            "recommendation": "anchor to PRD",
        }
    ]

    in_scope, out_of_scope, _unscoped = classify_findings_by_active_scope(
        findings,
        scope_task_ids={"TTS-DEG-02"},
        known_task_ids=["TTS-DEG-02", "TTS-DEG-01"],
    )

    assert len(in_scope) == 1
    assert out_of_scope == []


def test_classify_findings_when_plan_has_no_task_ids_returns_all_in_scope() -> None:
    """No way to scope without task ids; treat everything as in_scope (safe)."""
    findings = [
        {"severity": "blocking", "category": "x", "description": "anything"},
    ]

    in_scope, out_of_scope, unscoped = classify_findings_by_active_scope(
        findings,
        scope_task_ids={"T1"},
        known_task_ids=[],
    )

    assert len(in_scope) == 1
    assert out_of_scope == []
    assert unscoped == []
