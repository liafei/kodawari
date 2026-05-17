from __future__ import annotations

from pathlib import Path

from kodawari.cli.autopilot_decision_runtime import (
    DECISION_REQUEST_FILENAME,
    DECISION_RESPONSE_FILENAME,
    DecisionKind,
    build_decision_request,
    build_decision_response,
    decision_pending,
    decision_runtime_snapshot,
    load_decision_request,
    load_decision_response,
    response_matches_request,
    write_decision_request,
    write_decision_response,
)


def test_decision_request_round_trip(tmp_path: Path) -> None:
    payload = build_decision_request(
        decision_id="decision-1",
        decision_kind=DecisionKind.ARCHITECTURE_FREEZE,
        question="Freeze architecture?",
        context_summary="greenfield project",
        options=[
            {"option_id": "approve", "label": "Approve"},
            {"option_id": "revise", "label": "Revise", "details": "Need more separation"},
        ],
        recommended_option="approve",
        blocking_reason="needs confirmation",
    )

    path = write_decision_request(tmp_path, payload)

    assert path.name == DECISION_REQUEST_FILENAME
    loaded = load_decision_request(tmp_path)
    assert loaded == payload
    assert loaded["decision_kind"] == "architecture_freeze"
    assert loaded["options"][1]["details"] == "Need more separation"


def test_decision_pending_until_matching_response_exists(tmp_path: Path) -> None:
    request = build_decision_request(
        decision_id="decision-2",
        decision_kind=DecisionKind.TASK_PLAN_FREEZE,
        question="Freeze task graph?",
        context_summary="12 tasks",
        options=[{"option_id": "ok", "label": "Looks good"}],
    )
    write_decision_request(tmp_path, request)

    assert decision_pending(tmp_path) is True

    mismatched = build_decision_response(
        decision_id="another-decision",
        selected_option="ok",
        rationale="wrong response",
    )
    write_decision_response(tmp_path, mismatched)
    assert decision_pending(tmp_path) is True

    matched = build_decision_response(
        decision_id="decision-2",
        selected_option="ok",
        rationale="approved",
    )
    write_decision_response(tmp_path, matched)
    assert decision_pending(tmp_path) is False
    assert response_matches_request(request, load_decision_response(tmp_path)) is True


def test_decision_runtime_snapshot_reports_pending_metadata(tmp_path: Path) -> None:
    request = build_decision_request(
        decision_id="decision-3",
        decision_kind=DecisionKind.RELEASE_APPROVAL,
        question="Approve release?",
        context_summary="qa pass",
        options=[
            {"option_id": "ship", "label": "Ship"},
            {"option_id": "hold", "label": "Hold"},
        ],
    )
    write_decision_request(tmp_path, request)

    snapshot = decision_runtime_snapshot(tmp_path)

    assert snapshot["decision_id"] == "decision-3"
    assert snapshot["decision_kind"] == "release_approval"
    assert snapshot["decision_request_present"] is True
    assert snapshot["decision_response_present"] is False
    assert snapshot["decision_pending"] is True
    assert snapshot["request_options"] == ["ship", "hold"]


def test_decision_runtime_snapshot_clears_request_present_when_response_matches(tmp_path: Path) -> None:
    request = build_decision_request(
        decision_id="decision-4",
        decision_kind=DecisionKind.RELEASE_APPROVAL,
        question="Approve release?",
        context_summary="qa pass",
        options=[{"option_id": "ship", "label": "Ship"}],
    )
    response = build_decision_response(
        decision_id="decision-4",
        selected_option="ship",
        rationale="approved",
    )
    write_decision_request(tmp_path, request)
    write_decision_response(tmp_path, response)

    snapshot = decision_runtime_snapshot(tmp_path)

    assert snapshot["decision_id"] == "decision-4"
    assert snapshot["decision_kind"] == "release_approval"
    assert snapshot["decision_request_present"] is False
    assert snapshot["decision_response_present"] is True
    assert snapshot["decision_pending"] is False
    assert snapshot["request_options"] == ["ship"]
