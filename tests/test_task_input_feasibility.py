"""Tests for task_input_feasibility precheck (Layer D)."""

from __future__ import annotations

import pytest

from kodawari.autopilot.planning.task_input_feasibility import (
    evaluate_task_input_feasibility,
)


def _manifest_with(*paths: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for path in paths:
        basename = path.rsplit("/", 1)[-1]
        out.setdefault(basename, []).append(path)
    return out


def test_test_only_task_with_missing_route_surface_is_infeasible() -> None:
    """The user's T100C-style incident: test-only regression for a route the
    repo doesn't implement. Precheck must trip."""
    direction = (
        "Add a regression test for /api/v1/events/{id}/social to verify the "
        "social aggregation contract. test-only — do not modify production."
    )
    manifest = _manifest_with(
        "backend/api/v1/routes/detail_routes.py",
        "backend/api/v1/services/event_repository.py",
        # Note: no path containing 'social' as subject
    )

    result = evaluate_task_input_feasibility(
        task_direction=direction, file_manifest=manifest
    )

    assert result["status"] == "INFEASIBLE"
    assert "/api/v1/events/{id}/social" in (result.get("missing_surfaces") or [])
    assert result["detected_intent"] == "test_only"
    finding = result["finding"]
    assert finding["severity"] == "blocking"
    assert finding["category"] == "task_shape_infeasible"


def test_test_only_task_with_existing_route_surface_passes() -> None:
    """Same shape of task, but the route subject IS in the repo (e.g. there
    is a social_thread_service). Precheck must NOT trip — adding tests for
    an existing surface is legitimate."""
    direction = (
        "Add a regression test for /api/v1/events/{id}/social. test-only — "
        "do not modify production."
    )
    manifest = _manifest_with(
        "backend/api/v1/routes/detail_routes.py",
        "backend/api/v1/services/social_thread_service.py",
    )

    result = evaluate_task_input_feasibility(
        task_direction=direction, file_manifest=manifest
    )

    assert result["status"] == "OK"


def test_implementation_task_with_missing_route_passes() -> None:
    """Production-intent task naming a missing route → user is asking us to
    create it. Don't block."""
    direction = (
        "Implement /api/v1/events/{id}/social as a new route handler that "
        "wires up the social aggregation service. Add tests as appropriate."
    )
    manifest = _manifest_with("backend/api/v1/routes/detail_routes.py")

    result = evaluate_task_input_feasibility(
        task_direction=direction, file_manifest=manifest
    )

    assert result["status"] == "OK"
    assert result["detected_intent"] == "production"


def test_task_with_no_route_token_passes() -> None:
    """Tasks that don't name a concrete /api/v… route (docs, refactors, etc.)
    are not the precheck's target — pass through."""
    direction = "Refactor the article extractor to add trafilatura support."
    manifest = _manifest_with("backend/api/v1/services/article_extractor.py")

    result = evaluate_task_input_feasibility(
        task_direction=direction, file_manifest=manifest
    )

    assert result["status"] == "OK"
    assert result["reason"] == "no_route_token"


def test_ambiguous_intent_with_missing_route_passes() -> None:
    """When intent is unclear (no test markers AND no production verbs), do
    not trip — single-signal cases should fall through."""
    direction = "Look into /api/v1/events/{id}/social and improve it."
    manifest = _manifest_with("backend/api/v1/routes/detail_routes.py")

    result = evaluate_task_input_feasibility(
        task_direction=direction, file_manifest=manifest
    )

    assert result["status"] == "OK"
    assert result["detected_intent"] == "ambiguous"


def test_empty_task_direction_passes() -> None:
    result = evaluate_task_input_feasibility(task_direction="", file_manifest={})
    assert result["status"] == "OK"


def test_precheck_can_be_disabled_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Operators must have a kill switch. INFEASIBLE input + env=0 → OK."""
    monkeypatch.setenv("WORKFLOW_TASK_INPUT_FEASIBILITY_PRECHECK", "0")
    direction = (
        "Add a regression test for /api/v1/events/{id}/social. test-only."
    )
    manifest = _manifest_with("backend/api/v1/routes/detail_routes.py")

    result = evaluate_task_input_feasibility(
        task_direction=direction, file_manifest=manifest
    )

    assert result["status"] == "OK"
    assert result["reason"] == "precheck_disabled"


def test_chinese_test_only_marker_is_recognized() -> None:
    """User's project has Chinese-language task directions; ``补回归`` should
    count as a test-only marker."""
    direction = (
        "给 /api/v1/events/{id}/social 补回归测试，覆盖事件级聚合契约。"
    )
    manifest = _manifest_with("backend/api/v1/routes/detail_routes.py")

    result = evaluate_task_input_feasibility(
        task_direction=direction, file_manifest=manifest
    )

    assert result["status"] == "INFEASIBLE"
    assert result["detected_intent"] == "test_only"
