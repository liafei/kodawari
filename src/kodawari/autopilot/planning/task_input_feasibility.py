"""Task-direction → repo-surface feasibility precheck (Layer D).

Runs once before round 1 of ``run_planning_conversation``. Detects the
"input is fundamentally unfeasible" case the planner-reviewer dance has no
legal way to close — typically a ``test-only`` task targeting a route that
does not yet exist in the repo. Without this precheck the planner is
forced to invent a plan that closes against a fictional surface, the
reviewer keeps blocking, and the loop runs out of rounds; downstream
self_repair then misclassifies the failure as a deterministic
contradiction.

Hard signal: the task names a concrete API route token (e.g.
``/api/v1/events/{id}/social``) and the repo manifest contains no file
referencing the route's subject nouns.

Soft signal: the task wording is *test-only* — it asks for tests/coverage/
regression and contains no production-change verbs.

Both signals must fire to trip ``status=INFEASIBLE``. Single-signal cases
fall through to the normal planner path so this precheck does not block
legitimate test-coverage work whose target *does* exist in the repo.
"""

from __future__ import annotations

import os
import re
from typing import Any


_ROUTE_TOKEN_RE = re.compile(
    r"/api/v\d+(?:/[A-Za-z0-9_\-]+|/\{[A-Za-z0-9_]+\})+",
    re.IGNORECASE,
)
_PATH_PIECE_RE = re.compile(r"[A-Za-z0-9_\-]+")
_GENERIC_PATH_NOUNS = {
    "api",
    "v1",
    "v2",
    "v3",
    "id",
    "uuid",
    "slug",
}
_TEST_INTENT_MARKERS = (
    "regression test",
    "regression coverage",
    "regression-only",
    "test coverage",
    "补回归",
    "补测试",
    "test-only",
    "tests-only",
    "only tests",
    "only adds tests",
    "purely a test",
    "test addition",
    "add tests for",
    "add test for",
    "add a test",
    "add a regression test",
    "write tests for",
)
_PRODUCTION_INTENT_MARKERS = (
    "implement",
    "create handler",
    "create route",
    "add route",
    "wire up",
    "wire it",
    "build the",
    "ship",
    "introduce a new",
    "create a new",
    "new route",
    "new endpoint",
    "new handler",
    "new service",
)


def _route_subject_tokens(route: str) -> set[str]:
    pieces = _PATH_PIECE_RE.findall(route.lower())
    return {
        piece
        for piece in pieces
        if len(piece) >= 3
        and piece not in _GENERIC_PATH_NOUNS
        and not piece.startswith("v")  # /v1/ /v2/ etc
    }


def _detect_intent(task_direction: str) -> str:
    text = task_direction.lower()
    test_hit = any(marker in text for marker in _TEST_INTENT_MARKERS)
    production_hit = any(marker in text for marker in _PRODUCTION_INTENT_MARKERS)
    if test_hit and not production_hit:
        return "test_only"
    if production_hit and not test_hit:
        return "production"
    return "ambiguous"


def _route_subject_present_in_manifest(
    *,
    subject_tokens: set[str],
    file_manifest: dict[str, list[str]],
) -> bool:
    """Return True when at least one subject token appears in any manifest path.

    file_manifest is a basename → list[paths] map (built by
    ``planning_context.build_file_manifest``). We scan paths because a token
    like ``social`` may be in ``backend/api/v1/services/social_thread_service.py``
    even when no basename equals ``social``.
    """
    if not subject_tokens:
        return True
    for paths in file_manifest.values():
        for path in paths:
            lowered = path.lower()
            if any(token in lowered for token in subject_tokens):
                return True
    return False


def evaluate_task_input_feasibility(
    *,
    task_direction: str,
    file_manifest: dict[str, list[str]],
) -> dict[str, Any]:
    """Return ``{"status": "OK" | "INFEASIBLE", ...}`` shape.

    INFEASIBLE entries carry:
      - ``reason``: short string code for telemetry
      - ``missing_surfaces``: list of route tokens that didn't resolve
      - ``detected_intent``: ``"test_only"``
      - ``finding``: blocking finding ready to attach to escalation
    """
    if _is_disabled():
        return {"status": "OK", "reason": "precheck_disabled", "detected_intent": ""}
    direction = (task_direction or "").strip()
    if not direction:
        return {"status": "OK", "reason": "empty_task_direction", "detected_intent": ""}
    routes = list(dict.fromkeys(_ROUTE_TOKEN_RE.findall(direction)))
    if not routes:
        return {"status": "OK", "reason": "no_route_token", "detected_intent": _detect_intent(direction)}
    intent = _detect_intent(direction)
    if intent != "test_only":
        return {
            "status": "OK",
            "reason": "intent_not_test_only",
            "detected_intent": intent,
            "routes": routes,
        }
    missing: list[str] = []
    for route in routes:
        subject_tokens = _route_subject_tokens(route)
        if not subject_tokens:
            continue
        if not _route_subject_present_in_manifest(
            subject_tokens=subject_tokens,
            file_manifest=file_manifest,
        ):
            missing.append(route)
    if not missing:
        return {
            "status": "OK",
            "reason": "all_routes_resolved",
            "detected_intent": intent,
            "routes": routes,
        }
    finding = {
        "severity": "blocking",
        "category": "task_shape_infeasible",
        "description": (
            "Task asks for tests/regression coverage of routes the repo does "
            f"not implement: {missing}. Adding tests for a non-existent surface "
            "has no legal closure path — the planner cannot satisfy 'test-only' "
            "AND 'cover the new route' simultaneously."
        ),
        "recommendation": (
            "Either (a) widen the task to also implement the missing route(s) "
            "before adding tests, or (b) re-anchor the task to an existing "
            "surface, or (c) confirm the route should be created and update "
            "the task direction to 'implement + test' before re-planning."
        ),
        "source": "task_input_feasibility",
    }
    return {
        "status": "INFEASIBLE",
        "reason": "missing_route_surface",
        "detected_intent": intent,
        "routes": routes,
        "missing_surfaces": missing,
        "finding": finding,
    }


def _is_disabled() -> bool:
    raw = os.environ.get("WORKFLOW_TASK_INPUT_FEASIBILITY_PRECHECK", "")
    return raw.strip().lower() in {"0", "false", "off", "no"}


def build_infeasibility_escalation(feasibility: dict[str, Any]) -> dict[str, Any]:
    """Build the escalation payload the orchestrator writes when the precheck
    trips. Kept here (not in the orchestrator) so changes to the precheck
    surface are localized.
    """
    finding = dict(feasibility.get("finding") or {})
    missing_surfaces = list(feasibility.get("missing_surfaces") or [])
    detected_intent = str(feasibility.get("detected_intent") or "").strip()
    return {
        "gate_reason": "task_input_infeasible_surface",
        "termination_reason": "task_input_infeasible_surface",
        "conflict_category": "task_input",
        "unresolved_findings": [finding] if finding else [],
        "missing_surfaces": missing_surfaces,
        "detected_intent": detected_intent,
        "planner_position": "",
        "reviewer_position": "",
        "suggested_human_questions": [
            "Should the missing route surface(s) be implemented before adding "
            "regression tests, or should the task be re-anchored to an "
            "existing surface?",
            f"Confirm the route surface(s): {missing_surfaces}",
        ],
    }


__all__ = [
    "build_infeasibility_escalation",
    "evaluate_task_input_feasibility",
]
