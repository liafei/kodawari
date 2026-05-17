"""declare_task_infeasible tool + task_infeasibility recovery detector.

Real-run regression: when the executor hits a structural precondition gap
(missing schema column / module / API), it used to dress the result up
with pytest.xfail to make verify "PASS" — which the reviewer correctly
rejected as inadmissible evidence. The dedicated tool gives the executor
a first-class infeasibility signal; the registry routes it to a stop
decision instead of a retry card.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kodawari.autopilot.execution.tool_use_prompt import (
    FULL_FILE_TOOL_MANIFEST_V1,
    PATCH_TOOL_MANIFEST_V1,
    tool_schemas,
)
from kodawari.autopilot.recovery.failure_event import FailureEvent
from kodawari.autopilot.recovery.registry import RecoveryContext, route_deterministic_recovery


def test_manifest_v1_lists_infeasibility_tool() -> None:
    assert "declare_task_infeasible" in PATCH_TOOL_MANIFEST_V1
    assert "declare_task_infeasible" in FULL_FILE_TOOL_MANIFEST_V1


def test_tool_schema_advertises_required_fields() -> None:
    schemas = tool_schemas("exact_str_replace_v1")
    by_name = {s["function"]["name"]: s for s in schemas}
    assert "declare_task_infeasible" in by_name
    spec = by_name["declare_task_infeasible"]["function"]
    assert "infeasible_reason" in spec["parameters"]["properties"]
    assert "missing_preconditions" in spec["parameters"]["properties"]
    assert "infeasible_reason" in spec["parameters"]["required"]
    assert "missing_preconditions" in spec["parameters"]["required"]


def test_registry_routes_task_infeasibility_to_stop_decision(tmp_path: Path) -> None:
    event = FailureEvent(
        phase="implement",
        error_code="TASK_BLOCKED_BY_PRECONDITION",
        affected_paths=["social_thread_snapshots.crawl_provider_kind"],
        evidence="task infeasible: missing schema column",
    )
    match = route_deterministic_recovery(
        RecoveryContext(
            project_root=tmp_path,
            original_card={"task_id": "T093", "files_to_change": ["tests/test_x.py"]},
            task_id="T093",
            must_fix=["scope precondition fix"],
            event=event,
        )
    )
    assert match is not None
    assert match.name == "task_infeasibility"
    # Detector returns no card — the engine must finish the loop instead of
    # retrying since no executor work can make the missing column appear.
    assert match.card == {}
    assert match.decision["action"] == "task_blocked_by_precondition"
    assert match.decision["missing_preconditions"] == ["social_thread_snapshots.crawl_provider_kind"]


def test_registry_does_not_match_for_other_error_codes(tmp_path: Path) -> None:
    """task_infeasibility must NOT short-circuit retryable failures."""

    event = FailureEvent(
        phase="implement",
        error_code="VERIFY_FAILED_RETRYABLE",
        affected_paths=["tests/test_x.py"],
    )
    match = route_deterministic_recovery(
        RecoveryContext(
            project_root=tmp_path,
            original_card={"task_id": "T093", "files_to_change": ["tests/test_x.py"]},
            task_id="T093",
            must_fix=["fix verify"],
            event=event,
        )
    )
    # Either no match or matches a different detector — but never task_infeasibility.
    if match is not None:
        assert match.name != "task_infeasibility"


def test_failure_event_pulls_missing_preconditions_from_execution_result() -> None:
    """When the executor's BLOCKED payload carries an infeasibility_report,
    build_failure_event surfaces missing_preconditions as affected_paths
    so the detector chain can route them deterministically."""

    from kodawari.autopilot.recovery.failure_event import build_failure_event

    event = build_failure_event(
        execution_result={
            "status": "BLOCKED",
            "error_code": "TASK_BLOCKED_BY_PRECONDITION",
            "missing_preconditions": ["social_thread_snapshots.crawl_provider_kind"],
            "infeasibility_report": {
                "schema_version": "execution.infeasibility.v1",
                "infeasible_reason": "column does not exist",
                "missing_preconditions": ["social_thread_snapshots.crawl_provider_kind"],
            },
        },
        must_fix=["fix me"],
    )
    assert event.error_code == "TASK_BLOCKED_BY_PRECONDITION"
    assert "social_thread_snapshots.crawl_provider_kind" in event.affected_paths
