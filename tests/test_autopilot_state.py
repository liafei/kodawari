"""Targeted tests for WS-101 autopilot state recovery."""

from pathlib import Path

from kodawari.autopilot.state import (
    ArchitectureDecision,
    AutopilotResult,
    AutopilotState,
    Stage,
    StopReason,
    SubtaskCheckpoint,
    SubtaskStatus,
)


def test_state_serialization_round_trip_with_subtasks_and_decisions(tmp_path: Path) -> None:
    state = AutopilotState(
        feature="newsapp",
        project_root=tmp_path,
        current_stage=Stage.IMPLEMENT,
        cycle=2,
        tokens_used=120,
        active_task="T010: Implement ranking",
    )
    state.verify_setup_recovery_attempted = 1
    state.verify_setup_recovery_succeeded = 1
    state.verify_setup_recovery_last_error = "fixture missing"
    state.add_subtask(
        SubtaskCheckpoint(
            subtask_id="T010.1",
            title="Implement ranking service",
            parent_task_id="T010",
            status=SubtaskStatus.DONE,
            changed_files=["backend/ranking.py"],
        )
    )
    state.add_architecture_decision(
        ArchitectureDecision(
            decision_id="ADR-001",
            decision="Keep API schema stable",
            rationale="Mobile clients depend on v1 schema",
        )
    )
    state.parallel_runtime = {
        "merge_status": "IN_PROGRESS",
        "worker_statuses": [{"worker_id": "worker-1", "role": "implementer", "running": 1}],
    }

    state_path = tmp_path / ".autopilot_state.json"
    state.save(state_path)
    loaded = AutopilotState.load(state_path)

    assert loaded.current_stage == Stage.IMPLEMENT
    assert loaded.verify_setup_recovery_last_error == "fixture missing"
    assert "T010.1" in loaded.subtasks
    assert loaded.architecture_decisions[0].decision_id == "ADR-001"
    assert loaded.parallel_runtime["merge_status"] == "IN_PROGRESS"


def test_mark_completed_sets_terminal_stage_and_clears_runtime(tmp_path: Path) -> None:
    state = AutopilotState(
        feature="demo",
        project_root=tmp_path,
        current_stage=Stage.VERIFY,
        active_task="T001: Demo",
        active_pid=123,
        active_attempt=2,
        active_subtask="T001.1",
    )

    state.mark_completed(StopReason.PASS, "PASS")

    assert state.current_stage == Stage.COMPLETED
    assert state.stop_reason == StopReason.PASS
    assert state.final_status == "PASS"
    assert state.active_task is None
    assert state.active_pid is None
    assert state.active_subtask is None


def test_subtask_queries_respect_dependency_completion(tmp_path: Path) -> None:
    state = AutopilotState(feature="demo", project_root=tmp_path, active_task="T100: Task")
    state.add_subtask(
        SubtaskCheckpoint(
            subtask_id="T100.1",
            title="Prepare fixtures",
            parent_task_id="T100",
            status=SubtaskStatus.DONE,
        )
    )
    state.add_subtask(
        SubtaskCheckpoint(
            subtask_id="T100.2",
            title="Implement feature",
            parent_task_id="T100",
            depends_on=["T100.1"],
            status=SubtaskStatus.PENDING,
        )
    )
    state.add_subtask(
        SubtaskCheckpoint(
            subtask_id="T100.3",
            title="Blocked dependency",
            parent_task_id="T100",
            depends_on=["T100.9"],
            status=SubtaskStatus.PENDING,
        )
    )

    pending = state.get_pending_subtasks("T100")

    assert [item.subtask_id for item in pending] == ["T100.2"]
    assert [item.subtask_id for item in state.get_completed_subtasks("T100")] == ["T100.1"]


def test_get_unified_status_reports_blocking_reason_and_next_action(tmp_path: Path) -> None:
    state = AutopilotState(
        feature="newsapp",
        project_root=tmp_path,
        current_stage=Stage.VERIFY,
        active_task="T200: Verify API",
    )
    state.add_subtask(
        SubtaskCheckpoint(
            subtask_id="T200.1",
            title="Run scoped verify",
            parent_task_id="T200",
            status=SubtaskStatus.FAILED,
            error="fixture not found",
        )
    )

    unified = state.get_unified_status()

    assert unified["current_phase"] == Stage.VERIFY.value
    assert unified["is_blocked"] is True
    assert "T200.1" in unified["failed_subtasks"]
    assert "fixture not found" in (unified["blocking_reason"] or "")
    assert "Repair the failed subtask" in unified["next_action"]


def test_get_unified_status_surfaces_parallel_worker_runtime(tmp_path: Path) -> None:
    state = AutopilotState(
        feature="newsapp",
        project_root=tmp_path,
        current_stage=Stage.IMPLEMENT,
        active_task="T210: Parallel runtime",
    )
    state.parallel_runtime = {
        "merge_status": "IN_PROGRESS",
        "worker_statuses": [
            {"worker_id": "worker-1", "role": "implementer", "running": 1},
            {"worker_id": "worker-2", "role": "reviewer", "pending": 1},
        ],
    }

    unified = state.get_unified_status()

    assert unified["parallel_merge_status"] == "IN_PROGRESS"
    assert len(unified["worker_statuses"]) == 2
    assert unified["parallel_runtime"]["merge_status"] == "IN_PROGRESS"


def test_autopilot_result_to_dict_contains_stop_reason_value() -> None:
    result = AutopilotResult(
        feature="newsapp",
        cycles_completed=3,
        tokens_used=900,
        final_status="PASS",
        stop_reason=StopReason.PASS,
        completed_tasks=["T001", "T002"],
    )

    payload = result.to_dict()

    assert payload["stop_reason"] == "PASS"
    assert payload["completed_tasks"] == ["T001", "T002"]


def test_state_add_error_records_structured_error_event_and_roundtrips(tmp_path: Path) -> None:
    state = AutopilotState(
        feature="newsapp",
        project_root=tmp_path,
        current_stage=Stage.VERIFY,
        active_task="T300: Verify endpoint",
    )
    event = state.add_error(
        "fixture db_session missing",
        phase=Stage.VERIFY.value,
        action="VERIFY",
        category="setup",
        recovery_attempted=True,
        recovery_succeeded=False,
    )

    assert state.last_error == "fixture db_session missing"
    assert state.error_history[-1] == "fixture db_session missing"
    assert event.phase == Stage.VERIFY.value
    assert event.action == "VERIFY"
    assert event.category == "setup"
    assert event.recovery_attempted is True
    assert event.recovery_succeeded is False
    assert state.error_events and state.error_events[-1].message == "fixture db_session missing"

    state_path = tmp_path / ".autopilot_state.json"
    state.save(state_path)
    loaded = AutopilotState.load(state_path)
    assert loaded.error_events
    assert loaded.error_events[-1].category == "setup"
    assert loaded.error_events[-1].phase == Stage.VERIFY.value


def test_state_add_error_inherits_run_id_and_persists_structured_fields(tmp_path: Path) -> None:
    """run_id, error_code, metadata propagate from add_error() into the
    persisted event so the learning layer sees them on the next session."""
    state = AutopilotState(
        feature="newsapp",
        project_root=tmp_path,
        current_stage=Stage.IMPLEMENT,
        active_task="T1: Codex impl",
        run_id="run_abc123",
    )
    event = state.add_error(
        "codex_cli execution timed out",
        phase=Stage.IMPLEMENT.value,
        action="IMPLEMENT",
        category="implement",
        error_code="CODEX_CLI_TIMEOUT",
        metadata={"backend": "codex_cli", "returncode": 124},
    )

    # run_id is inherited from the state (not passed explicitly).
    assert event.run_id == "run_abc123"
    assert event.error_code == "CODEX_CLI_TIMEOUT"
    assert event.metadata == {"backend": "codex_cli", "returncode": 124}

    state_path = tmp_path / ".autopilot_state.json"
    state.save(state_path)
    loaded = AutopilotState.load(state_path)
    persisted = loaded.error_events[-1]
    assert persisted.run_id == "run_abc123"
    assert persisted.error_code == "CODEX_CLI_TIMEOUT"
    assert persisted.metadata == {"backend": "codex_cli", "returncode": 124}


def test_state_add_error_explicit_run_id_overrides_state_run_id(tmp_path: Path) -> None:
    state = AutopilotState(
        feature="newsapp",
        project_root=tmp_path,
        current_stage=Stage.IMPLEMENT,
        active_task="T1",
        run_id="run_state",
    )
    event = state.add_error("boom", run_id="run_override")
    assert event.run_id == "run_override"


def test_state_add_error_omitted_metadata_does_not_serialize(tmp_path: Path) -> None:
    """Empty metadata/error_code/run_id stay out of the on-disk JSON to keep
    legacy state files byte-stable when the new fields are unused."""
    state = AutopilotState(
        feature="newsapp",
        project_root=tmp_path,
        current_stage=Stage.IMPLEMENT,
        active_task="T1",
    )
    event = state.add_error("legacy-style failure")
    payload = event.to_dict()
    assert "run_id" not in payload
    assert "error_code" not in payload
    assert "metadata" not in payload
