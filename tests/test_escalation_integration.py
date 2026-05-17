"""Integration tests for executor recovery escalation to Planner redesign."""

import json
import tempfile
from pathlib import Path

import pytest

from kodawari.autopilot.recovery.escalation_handler import (
    escalation_count_from_context,
    write_redesign_request,
    read_redesign_response,
    is_gate_complexity_exhausted,
    REDESIGN_REQUEST,
    REDESIGN_RESPONSE,
    REDESIGN_CONTEXT,
)
from kodawari.autopilot.recovery.failure_event import FailureEvent
from kodawari.gui.redesign_chooser import DesignChoice


class TestEscalationHandler:
    """Test escalation_handler module."""

    def test_escalation_count_from_context_new_dir(self):
        """Escalation count is 0 for new directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            count = escalation_count_from_context(Path(tmpdir))
            assert count == 0

    def test_write_redesign_request_increments_count(self):
        """write_redesign_request increments the escalation count."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)

            # First write
            failure_event = FailureEvent(
                phase="gate",
                error_code="GATE_BLOCKED",
                detector_hint="gate_complexity",
                evidence="Function too complex",
            )
            write_redesign_request(tmpdir, failure_event, ["T1"], "T2")
            assert escalation_count_from_context(tmpdir) == 1

            # Second write should increment
            write_redesign_request(tmpdir, failure_event, ["T1"], "T2")
            assert escalation_count_from_context(tmpdir) == 2

    def test_redesign_request_format(self):
        """Redesign request has correct schema."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            failure_event = FailureEvent(
                phase="gate",
                error_code="GATE_BLOCKED",
                detector_hint="gate_complexity",
                evidence="test evidence",
            )
            write_redesign_request(tmpdir, failure_event, ["T1"], "T2")

            request_file = tmpdir / REDESIGN_REQUEST
            assert request_file.exists()

            request = json.loads(request_file.read_text())
            assert request["schema_version"] == "execution.redesign_request.v1"
            assert request["task_id"] == "T2"
            assert request["detector_hint"] == "gate_complexity"
            assert request["completed_task_ids"] == ["T1"]
            assert request["escalation_count"] == 1

    def test_read_redesign_response_not_found(self):
        """read_redesign_response returns None when file missing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            response = read_redesign_response(Path(tmpdir))
            assert response is None

    def test_read_redesign_response_skip(self):
        """read_redesign_response parses skip action."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            response_file = tmpdir / REDESIGN_RESPONSE
            response_file.write_text(json.dumps({"action": "skip", "task_id": "T2"}))

            choice = read_redesign_response(tmpdir)
            assert choice is not None
            assert choice.action == "skip"

    def test_read_redesign_response_accept(self):
        """read_redesign_response parses accept action."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            response_file = tmpdir / REDESIGN_RESPONSE
            response_file.write_text(
                json.dumps({
                    "action": "accept",
                    "task_id": "T2",
                    "option_index": 1,
                    "option": {"title": "Option 1"},
                })
            )

            choice = read_redesign_response(tmpdir)
            assert choice is not None
            assert choice.action == "accept"
            assert choice.option_index == 1

    def test_read_redesign_response_custom(self):
        """read_redesign_response parses custom action."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            response_file = tmpdir / REDESIGN_RESPONSE
            response_file.write_text(
                json.dumps({
                    "action": "custom",
                    "task_id": "T2",
                    "description": "Custom approach description",
                })
            )

            choice = read_redesign_response(tmpdir)
            assert choice is not None
            assert choice.action == "custom"
            assert choice.custom_text == "Custom approach description"

    def test_is_gate_complexity_exhausted_true(self):
        """is_gate_complexity_exhausted returns True for gate_complexity."""
        failure_event = FailureEvent(
            phase="gate",
            error_code="GATE_BLOCKED",
            detector_hint="gate_complexity",
        )
        assert is_gate_complexity_exhausted(failure_event) is True

    def test_is_gate_complexity_exhausted_false_wrong_detector(self):
        """is_gate_complexity_exhausted returns False for other detectors."""
        failure_event = FailureEvent(
            phase="gate",
            error_code="GATE_BLOCKED",
            detector_hint="pytest_verify_failure",
        )
        assert is_gate_complexity_exhausted(failure_event) is False

    def test_is_gate_complexity_exhausted_false_wrong_code(self):
        """is_gate_complexity_exhausted returns False for non-GATE_BLOCKED."""
        failure_event = FailureEvent(
            phase="executor",
            error_code="EXECUTOR_STALLED",
            detector_hint="gate_complexity",
        )
        assert is_gate_complexity_exhausted(failure_event) is False


class TestDesignChoice:
    """Test DesignChoice namedtuple."""

    def test_design_choice_skip(self):
        """DesignChoice can be created with skip action."""
        choice = DesignChoice(action="skip")
        assert choice.action == "skip"
        assert choice.option_index is None
        assert choice.custom_text == ""

    def test_design_choice_accept(self):
        """DesignChoice can be created with accept action."""
        choice = DesignChoice(action="accept", option_index=0)
        assert choice.action == "accept"
        assert choice.option_index == 0

    def test_design_choice_custom(self):
        """DesignChoice can be created with custom action."""
        choice = DesignChoice(action="custom", custom_text="My custom approach")
        assert choice.action == "custom"
        assert choice.custom_text == "My custom approach"


__all__ = [
    "TestEscalationHandler",
    "TestDesignChoice",
]
