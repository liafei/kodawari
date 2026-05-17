"""Stable planning stage profile identifiers.

Profiles describe controller behavior, not model identity. Role-to-model
assignment remains fully delegated to models.yaml / runtime config.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StageProfile:
    profile_id: str
    planner_required: bool
    plan_reviewer_required: bool
    context_scope: str


EPIC_PLAN = StageProfile(
    profile_id="epic_plan",
    planner_required=True,
    plan_reviewer_required=True,
    context_scope="prd_repo_context",
)
TAKE_TASK = StageProfile(
    profile_id="take_task",
    planner_required=False,
    plan_reviewer_required=False,
    context_scope="task_graph_current_card",
)
REVISE_TASK = StageProfile(
    profile_id="revise_task",
    planner_required=True,
    plan_reviewer_required=True,
    context_scope="task_graph_review_feedback",
)
RECOVERY = StageProfile(
    profile_id="recovery",
    planner_required=False,
    plan_reviewer_required=False,
    context_scope="stall_report_current_card",
)

STAGE_PROFILES = {
    item.profile_id: item
    for item in (
        EPIC_PLAN,
        TAKE_TASK,
        REVISE_TASK,
        RECOVERY,
    )
}


__all__ = [
    "EPIC_PLAN",
    "RECOVERY",
    "REVISE_TASK",
    "STAGE_PROFILES",
    "StageProfile",
    "TAKE_TASK",
]
