"""Tests for deterministic model-plan consistency checks."""

from __future__ import annotations

from typing import Any

from kodawari.autopilot.planning.planning_consistency import (
    finding_task_ids,
    validate_plan_consistency,
    validate_plan_revision,
)


def _task(task_id: str, **overrides: Any) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "task_name": f"Task {task_id}",
        "layer_owner": "service",
        "surface": "rest_api",
        "files_to_change": [f"{task_id.lower()}.py"],
        "new_files": [f"{task_id.lower()}.py"],
        "coverage_hints": [],
        "approach": "do scoped work",
        "invariants": ["no regression"],
        "test_plan": "pytest -q",
        "verify_cmd": "pytest -q",
        "depends_on": [],
        "forbidden_changes": [],
        "provides": [],
        "requires": [],
        "api_contracts": [],
        **overrides,
    }


def _plan(*tasks: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    return {
        "summary": "plan",
        "business_outcome": "outcome",
        "out_of_scope": [],
        "source_of_truth": [],
        "source_of_truth_canonical": [],
        "path_type": "write",
        "layers": ["service"],
        "coverage_hints": [],
        "module_boundaries": [],
        "verify_recipes": [],
        "approval_points": [],
        "execution_constraints": {},
        "confidence": "high",
        "confidence_issues": [],
        "tasks": list(tasks),
        "risks": [],
        "change_log": [],
        **overrides,
    }


def test_api_contract_conflict_is_structural() -> None:
    plan = _plan(
        _task(
            "T07",
            api_contracts=[
                {
                    "method": "GET",
                    "endpoint": "/events/{id}/social",
                    "response_shape": {"kol_comments": "list"},
                }
            ],
        ),
        _task(
            "T08",
            api_contracts=[
                {
                    "method": "GET",
                    "endpoint": "/events/{id}/social",
                    "response_shape": {"x": "list", "reddit": "list"},
                }
            ],
        ),
    )

    errors = validate_plan_consistency(plan)

    assert any("api_contracts conflict for GET /events/{id}/social" in item for item in errors)


def test_field_requirement_without_provider_is_structural() -> None:
    plan = _plan(
        _task(
            "T08",
            requires=[{"kind": "field", "name": "social_thread_snapshots.cluster_id"}],
        )
    )

    errors = validate_plan_consistency(plan)

    assert any("T08 requires field 'social_thread_snapshots.cluster_id'" in item for item in errors)
    assert any("no task provides it" in item for item in errors)


def test_field_requirement_must_depend_on_provider() -> None:
    plan = _plan(
        _task(
            "T06",
            provides=[{"kind": "field", "name": "social_thread_snapshots.cluster_id"}],
        ),
        _task(
            "T08",
            requires=[{"kind": "field", "name": "social_thread_snapshots.cluster_id"}],
        ),
    )

    errors = validate_plan_consistency(plan)

    assert any("does not depend on a provider task" in item for item in errors)


def test_field_requirement_accepts_dependency_provider() -> None:
    plan = _plan(
        _task(
            "T06",
            provides=[{"kind": "field", "name": "social_thread_snapshots.cluster_id"}],
        ),
        _task(
            "T08",
            depends_on=["T06"],
            requires=[{"kind": "field", "name": "social_thread_snapshots.cluster_id"}],
        ),
    )

    assert validate_plan_consistency(plan) == []


def test_revision_rejects_silent_task_rewrite() -> None:
    previous = _plan(_task("T1", approach="old approach"))
    current = _plan(_task("T1", approach="new approach"))

    errors = validate_plan_revision(previous_plan=previous, current_plan=current)

    assert any("change_log must be non-empty" in item for item in errors)
    assert any("change_log missing modified task T1" in item for item in errors)


def test_revision_accepts_declared_task_change() -> None:
    previous = _plan(_task("T1", approach="old approach"))
    current = _plan(
        _task("T1", approach="new approach"),
        change_log=[
            {
                "task_id": "T1",
                "fields": ["approach"],
                "reason": "Addresses previous finding for T1.",
            }
        ],
    )

    errors = validate_plan_revision(
        previous_plan=previous,
        current_plan=current,
        previous_findings=[
            {
                "severity": "blocking",
                "description": "T1 needs a clearer implementation approach",
            }
        ],
    )

    assert errors == []


def test_revision_accepts_change_log_full_task_id_when_finding_uses_short_prefix() -> None:
    previous = _plan(_task("T101-followup-links-schema", approach="old approach"))
    current = _plan(
        _task("T101-followup-links-schema", approach="new approach"),
        change_log=[
            {
                "task_id": "T101-followup-links-schema",
                "fields": ["approach"],
                "reason": "Addresses previous finding for T101.",
            }
        ],
    )

    errors = validate_plan_revision(
        previous_plan=previous,
        current_plan=current,
        previous_findings=[
            {
                "severity": "blocking",
                "description": "T101 needs a clearer implementation approach",
            }
        ],
    )

    assert errors == []


def test_revision_rejects_change_log_outside_explicit_finding_task() -> None:
    previous = _plan(_task("T1"), _task("T2", approach="old approach"))
    current = _plan(
        _task("T1"),
        _task("T2", approach="new approach"),
        change_log=[
            {
                "task_id": "T2",
                "fields": ["approach"],
                "reason": "Opportunistic rewrite.",
            }
        ],
    )

    errors = validate_plan_revision(
        previous_plan=previous,
        current_plan=current,
        previous_findings=[
            {
                "severity": "blocking",
                "description": "T1 is missing route coverage",
            }
        ],
    )

    assert any("targets T2" in item and "T1" in item for item in errors)


def test_revision_accepts_new_prereq_task_inserted_in_response_to_precondition_hint() -> None:
    """Auto-replan triggered by readiness BLOCK lets the planner insert a
    new prerequisite task (typically T0_*). Reviewer findings cannot pre-name
    such a task — the hint is the upstream signal. Without the exemption,
    plan-reviewer rejects the necessary expansion and the loop deadlocks."""

    previous = _plan(_task("T1"))
    current = _plan(
        _task("T0_ADD_ENGAGEMENT_SCORE", task_name="Add column"),
        _task("T1"),
        change_log=[
            {
                "task_id": "T0_ADD_ENGAGEMENT_SCORE",
                "fields": ["tasks"],
                "reason": "Inserted prerequisite migration to satisfy missing column.",
            }
        ],
    )
    hint = {
        "missing_field_preconditions": ["social_thread_snapshots.engagement_score"],
        "missing_symbol_preconditions": [],
    }

    errors_without_hint = validate_plan_revision(
        previous_plan=previous,
        current_plan=current,
        previous_findings=[{"severity": "blocking", "description": "T1 is incomplete"}],
    )
    errors_with_hint = validate_plan_revision(
        previous_plan=previous,
        current_plan=current,
        previous_findings=[{"severity": "blocking", "description": "T1 is incomplete"}],
        precondition_replan_hint=hint,
    )

    assert any("T0_ADD_ENGAGEMENT_SCORE" in item for item in errors_without_hint)
    assert not any("T0_ADD_ENGAGEMENT_SCORE" in item for item in errors_with_hint)


def test_revision_hint_does_not_exempt_unrelated_new_tasks() -> None:
    """The hint exemption applies only to tasks that respond to the missing
    precondition (T0_* prefix or task body referencing a missing field). An
    arbitrary new task that has nothing to do with the hint must still be
    reviewer-justified."""

    previous = _plan(_task("T1"))
    current = _plan(
        _task("T_RANDOM", task_name="Unrelated rewrite"),
        _task("T1"),
        change_log=[
            {
                "task_id": "T_RANDOM",
                "fields": ["tasks"],
                "reason": "Why not.",
            }
        ],
    )
    hint = {
        "missing_field_preconditions": ["social_thread_snapshots.engagement_score"],
        "missing_symbol_preconditions": [],
    }

    errors = validate_plan_revision(
        previous_plan=previous,
        current_plan=current,
        previous_findings=[{"severity": "blocking", "description": "T1 is incomplete"}],
        precondition_replan_hint=hint,
    )
    assert any("T_RANDOM" in item for item in errors)


def test_finding_task_ids_ignores_natural_language_capital_t() -> None:
    findings = [{"description": "That issue with T093 should be fixed. The plan needs T001."}]

    assert finding_task_ids(findings) == {"T001", "T093"}


def test_finding_task_ids_handles_compound_ids() -> None:
    findings = [{"description": "See T093-FIX-01 and T002_helper."}]

    assert finding_task_ids(findings) == {"T002_helper", "T093-FIX-01"}
