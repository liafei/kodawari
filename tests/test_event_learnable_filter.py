"""PR3: ``_event_is_learnable`` filter pins which events reach the instinct
learning queue.

Old behavior: only events in {setup, implement, review, verify, gate} were
ever ingested. Runtime/external_gateway errors (codex auth, token budget,
review gateway) had no path into instincts at all — that's why the wf-test
session showed zero LearnedInstincts despite real failures.

New behavior:
- {setup, implement, review, verify, gate} → always learnable
- {runtime, external_gateway} → learnable only when error_code is non-empty
- everything else → not learnable

We do not change error category routing — those categories still mean what
they did to root-cause buckets, dashboards, and other consumers.
"""

from __future__ import annotations

from pathlib import Path

from kodawari.autopilot.core.state import (
    AutopilotState,
    ErrorEvent,
    Stage,
    _event_is_learnable,
)
from kodawari.instincts.storage import InstinctStore


def _evt(category: str, *, error_code: str = "", message: str = "boom") -> ErrorEvent:
    return ErrorEvent(
        timestamp="2026-04-28T00:00:00Z",
        phase="IMPLEMENT",
        action="IMPLEMENT",
        category=category,
        message=message,
        error_code=error_code,
    )


def test_setup_implement_review_verify_gate_always_learnable() -> None:
    for category in ("setup", "implement", "review", "verify", "gate"):
        # No error_code needed for these.
        assert _event_is_learnable(_evt(category)) is True


def test_runtime_without_error_code_not_learnable() -> None:
    assert _event_is_learnable(_evt("runtime")) is False


def test_runtime_with_error_code_is_learnable() -> None:
    assert _event_is_learnable(_evt("runtime", error_code="TOKEN_BUDGET_EXCEEDED")) is True


def test_external_gateway_without_error_code_not_learnable() -> None:
    assert _event_is_learnable(_evt("external_gateway")) is False


def test_external_gateway_with_error_code_is_learnable() -> None:
    assert _event_is_learnable(_evt("external_gateway", error_code="REVIEW_GATEWAY_BLOCKED")) is True


def test_unknown_category_never_learnable() -> None:
    assert _event_is_learnable(_evt("lane")) is False
    assert _event_is_learnable(_evt("noise", error_code="X")) is False


def test_runtime_event_with_error_code_reaches_instinct_store(tmp_path: Path) -> None:
    """End-to-end: runtime + error_code now produces a LearningCandidate."""
    state = AutopilotState(
        feature="newsapp",
        project_root=tmp_path,
        current_stage=Stage.IMPLEMENT,
        active_task="T1",
        run_id="run_alpha",
    )
    state.add_error(
        "codex_cli execution timed out",
        phase=Stage.IMPLEMENT.value,
        action="IMPLEMENT",
        category="runtime",
        error_code="CODEX_CLI_TIMEOUT",
        metadata={"backend": "codex_cli"},
    )

    payload = InstinctStore(tmp_path).load()
    assert len(payload.learning_candidates) == 1
    candidate = payload.learning_candidates[0]
    assert candidate.category == "runtime"
    assert candidate.distinct_run_count == 1
    assert candidate.seen_run_ids == ["run_alpha"]
    # Backend metadata flowed through into the candidate too.
    assert candidate.metadata.get("backend") == "codex_cli"


def test_runtime_event_without_error_code_does_not_create_candidate(tmp_path: Path) -> None:
    """The wf-test footgun pre-PR3: runtime errors without structure
    were silently dropped from learning. Now they're EXPLICITLY dropped
    by the same filter, so the store stays empty until someone attaches
    an error_code at the producer site."""
    state = AutopilotState(
        feature="newsapp",
        project_root=tmp_path,
        current_stage=Stage.IMPLEMENT,
        active_task="T1",
    )
    state.add_error(
        "transient runtime hiccup",
        phase=Stage.IMPLEMENT.value,
        action="IMPLEMENT",
        category="runtime",
        # NOTE: no error_code on purpose.
    )
    payload = InstinctStore(tmp_path).load()
    assert payload.learning_candidates == []
