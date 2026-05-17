"""Tests for kodawari approve command."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kodawari.cli.runtime.autopilot_decision_runtime import (
    build_decision_request,
    build_decision_response,
    load_decision_response,
    write_decision_request,
    write_decision_response,
    DECISION_HISTORY_FILENAME,
    DECISION_RESPONSE_FILENAME,
)
from kodawari.cli.main import build_parser


def _run(parser, capsys, argv: list[str]) -> tuple[dict, int]:
    args = parser.parse_args(argv)
    rc = args.handler(args)
    payload = json.loads(capsys.readouterr().out)
    return payload, rc


def _make_request(planning_dir: Path, *, recommended: str = "A", options: list[dict] | None = None) -> dict:
    if options is None:
        options = [
            {"option_id": "A", "label": "Option A"},
            {"option_id": "B", "label": "Option B"},
        ]
    request = build_decision_request(
        decision_id="test-decision-1",
        decision_kind="planning_approval",
        question="Which option?",
        context_summary="Context here",
        options=options,
        recommended_option=recommended,
    )
    write_decision_request(planning_dir, request)
    return request


class TestApproveDefaultsToRecommended:
    def test_uses_recommended_option_when_no_option_given(self, tmp_path: Path, capsys) -> None:
        parser = build_parser()
        planning_dir = tmp_path / "planning" / "feat"
        planning_dir.mkdir(parents=True)
        _make_request(planning_dir, recommended="A")

        payload, rc = _run(
            parser,
            capsys,
            ["approve", "--project-root", str(tmp_path), "--feature", "feat"],
        )
        assert rc == 0
        assert payload["selected_option"] == "A"
        assert payload["decision_id"] == "test-decision-1"

    def test_uses_explicit_option_when_given(self, tmp_path: Path, capsys) -> None:
        parser = build_parser()
        planning_dir = tmp_path / "planning" / "feat"
        planning_dir.mkdir(parents=True)
        _make_request(planning_dir, recommended="A")

        payload, rc = _run(
            parser,
            capsys,
            ["approve", "--project-root", str(tmp_path), "--feature", "feat", "--option", "B"],
        )
        assert rc == 0
        assert payload["selected_option"] == "B"


class TestApproveWritesArtifacts:
    def test_response_file_written(self, tmp_path: Path, capsys) -> None:
        parser = build_parser()
        planning_dir = tmp_path / "planning" / "feat"
        planning_dir.mkdir(parents=True)
        _make_request(planning_dir)

        _run(parser, capsys, ["approve", "--project-root", str(tmp_path), "--feature", "feat"])

        response = load_decision_response(planning_dir)
        assert response is not None
        assert response["selected_option"] == "A"
        assert response["decision_id"] == "test-decision-1"

    def test_approve_does_not_mark_decision_consumed(self, tmp_path: Path, capsys) -> None:
        parser = build_parser()
        planning_dir = tmp_path / "planning" / "feat"
        planning_dir.mkdir(parents=True)
        _make_request(planning_dir)

        _run(parser, capsys, ["approve", "--project-root", str(tmp_path), "--feature", "feat"])

        history_path = planning_dir / DECISION_HISTORY_FILENAME
        assert not history_path.exists()

    def test_rationale_stored_in_response(self, tmp_path: Path, capsys) -> None:
        parser = build_parser()
        planning_dir = tmp_path / "planning" / "feat"
        planning_dir.mkdir(parents=True)
        _make_request(planning_dir)

        payload, rc = _run(
            parser,
            capsys,
            ["approve", "--project-root", str(tmp_path), "--feature", "feat", "--rationale", "looks good"],
        )
        assert rc == 0
        assert payload["rationale"] == "looks good"
        response = load_decision_response(planning_dir)
        assert response["rationale"] == "looks good"


class TestApproveGuards:
    def test_error_when_no_request_file(self, tmp_path: Path, capsys) -> None:
        parser = build_parser()
        planning_dir = tmp_path / "planning" / "feat"
        planning_dir.mkdir(parents=True)

        payload, rc = _run(
            parser,
            capsys,
            ["approve", "--project-root", str(tmp_path), "--feature", "feat"],
        )
        assert rc == 1
        assert payload["error_code"] == "no_decision_request"

    def test_error_when_response_already_exists(self, tmp_path: Path, capsys) -> None:
        parser = build_parser()
        planning_dir = tmp_path / "planning" / "feat"
        planning_dir.mkdir(parents=True)
        request = _make_request(planning_dir)
        existing = build_decision_response(
            decision_id=request["decision_id"], selected_option="A"
        )
        write_decision_response(planning_dir, existing)

        payload, rc = _run(
            parser,
            capsys,
            ["approve", "--project-root", str(tmp_path), "--feature", "feat"],
        )
        assert rc == 1
        assert payload["error_code"] == "decision_already_responded"

    def test_force_overwrites_existing_response(self, tmp_path: Path, capsys) -> None:
        parser = build_parser()
        planning_dir = tmp_path / "planning" / "feat"
        planning_dir.mkdir(parents=True)
        request = _make_request(planning_dir)
        existing = build_decision_response(
            decision_id=request["decision_id"], selected_option="A"
        )
        write_decision_response(planning_dir, existing)

        payload, rc = _run(
            parser,
            capsys,
            ["approve", "--project-root", str(tmp_path), "--feature", "feat", "--option", "B", "--force"],
        )
        assert rc == 0
        assert payload["selected_option"] == "B"

    def test_error_when_invalid_option(self, tmp_path: Path, capsys) -> None:
        parser = build_parser()
        planning_dir = tmp_path / "planning" / "feat"
        planning_dir.mkdir(parents=True)
        _make_request(planning_dir)

        payload, rc = _run(
            parser,
            capsys,
            ["approve", "--project-root", str(tmp_path), "--feature", "feat", "--option", "INVALID"],
        )
        assert rc == 1
        assert payload["error_code"] == "invalid_option"

    def test_force_bypasses_invalid_option_check(self, tmp_path: Path, capsys) -> None:
        parser = build_parser()
        planning_dir = tmp_path / "planning" / "feat"
        planning_dir.mkdir(parents=True)
        _make_request(planning_dir)

        payload, rc = _run(
            parser,
            capsys,
            ["approve", "--project-root", str(tmp_path), "--feature", "feat", "--option", "CUSTOM", "--force"],
        )
        assert rc == 0
        assert payload["selected_option"] == "CUSTOM"

    def test_requires_feature_when_no_planning_dir(self, tmp_path: Path) -> None:
        parser = build_parser()
        args = parser.parse_args(["approve", "--project-root", str(tmp_path)])
        with pytest.raises(ValueError, match="approve requires --feature"):
            args.handler(args)


class TestApproveExplicitPlanningDir:
    def test_explicit_planning_dir_binding(self, tmp_path: Path, capsys) -> None:
        parser = build_parser()
        planning_dir = tmp_path / "custom-dir"
        planning_dir.mkdir(parents=True)
        _make_request(planning_dir)

        payload, rc = _run(
            parser,
            capsys,
            ["approve", "--project-root", str(tmp_path), "--planning-dir", str(planning_dir)],
        )
        assert rc == 0
        assert Path(payload["planning_dir"]) == planning_dir.resolve()


class TestApprovePayloadContract:
    def test_payload_has_contract_version(self, tmp_path: Path, capsys) -> None:
        parser = build_parser()
        planning_dir = tmp_path / "planning" / "feat"
        planning_dir.mkdir(parents=True)
        _make_request(planning_dir)

        payload, rc = _run(
            parser,
            capsys,
            ["approve", "--project-root", str(tmp_path), "--feature", "feat"],
        )
        assert rc == 0
        assert payload["contract_version"] == "ws115.v1"
        assert payload["command"] == "approve"
        assert "next_action" in payload
        assert "autopilot" in payload["next_action"]
        assert "response_file" in payload

    def test_release_decision_next_action_uses_feature_not_decision_id(self, tmp_path: Path, capsys) -> None:
        parser = build_parser()
        planning_dir = tmp_path / "planning" / "feat"
        planning_dir.mkdir(parents=True)
        request = build_decision_request(
            decision_id="feat:release_approval",
            decision_kind="release_approval",
            question="Ship?",
            context_summary="Ready",
            options=[{"option_id": "ship", "label": "Ship"}],
            recommended_option="ship",
        )
        write_decision_request(planning_dir, request)

        payload, rc = _run(
            parser,
            capsys,
            ["approve", "--project-root", str(tmp_path), "--feature", "feat", "--option", "ship"],
        )

        assert rc == 0
        assert "--feature feat" in payload["next_action"]
        assert "feat:release_approval" not in payload["next_action"]
