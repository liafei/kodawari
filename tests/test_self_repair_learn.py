"""Tests for Phase 4 self-repair post-success learning."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kodawari.cli.evidence.self_repair_execute import SELF_REPAIR_EXECUTION_SCHEMA_VERSION
from kodawari.cli.evidence.self_repair_learn import (
    SELF_REPAIR_JOURNAL_RELATIVE_PATH,
    learn_from_self_repair,
)


def _execution_record(
    *,
    status: str = "executed",
    spawn_status: str = "ok",
    spawn_exit_code: int = 0,
    root_cause_code: str = "executor_fragmented_read_loop",
    root_cause_summary: str = "fragmented reads",
) -> dict[str, Any]:
    return {
        "schema_version": SELF_REPAIR_EXECUTION_SCHEMA_VERSION,
        "status": status,
        "proposal_status": "ready",
        "proposal_root_cause": {
            "code": root_cause_code,
            "summary": root_cause_summary,
            "confidence": 0.95,
        },
        "spawn": {
            "status": spawn_status,
            "exit_code": spawn_exit_code,
            "feature": "meta-repair-test-12345",
            "sdk_root": "",
        },
        "gates": [],
        "reason": "spawn_ok" if status == "executed" else status,
    }


def _write_record(planning: Path, payload: dict[str, Any]) -> Path:
    planning.mkdir(parents=True, exist_ok=True)
    path = planning / ".workflow_self_repair_execution.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _write_target_state(planning: Path, *, run_reason: str, final_status: str = "OK") -> None:
    planning.mkdir(parents=True, exist_ok=True)
    (planning / ".run_truth.json").write_text(
        json.dumps(
            {
                "feature": planning.name,
                "final_status": final_status,
                "run_reason": run_reason,
                "blocking_reason": "",
            }
        ),
        encoding="utf-8",
    )


def test_learn_emits_lesson_when_target_run_advances_to_proceed_to_gate(tmp_path: Path) -> None:
    sdk_root = tmp_path / "sdk"
    sdk_root.mkdir()
    record_path = _write_record(tmp_path / "planning" / "exec-1", _execution_record())
    target_after = tmp_path / "target-after"
    _write_target_state(target_after, run_reason="PROCEED_TO_GATE")

    result = learn_from_self_repair(
        execution_record_path=record_path,
        target_after_planning_dir=target_after,
        sdk_root=sdk_root,
        project_root_for_lesson=sdk_root,
    )

    assert result["outcome"]["level"] == 2
    assert result["outcome"]["action"] == "lesson_emitted"
    assert result["outcome"]["template_id"] == "self_repair.executor_fix_validated"
    # Journal must be written.
    journal = sdk_root / SELF_REPAIR_JOURNAL_RELATIVE_PATH
    assert journal.exists()
    entries = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(entries) == 1
    assert entries[0]["outcome"]["action"] == "lesson_emitted"
    # Prompt lesson store updated.
    lessons = json.loads((sdk_root / ".workflow" / "prompt_lessons.json").read_text(encoding="utf-8"))
    assert any(
        candidate["template_id"] == "self_repair.executor_fix_validated"
        for candidate in lessons.get("prompt_lesson_candidates", [])
    )


def test_learn_skips_lesson_when_only_sdk_succeeded_no_target_rerun(tmp_path: Path) -> None:
    """Level 1 (SDK passed but no target rerun) → telemetry only, no lesson."""

    sdk_root = tmp_path / "sdk"
    sdk_root.mkdir()
    record_path = _write_record(tmp_path / "planning" / "exec-2", _execution_record())

    result = learn_from_self_repair(
        execution_record_path=record_path,
        target_after_planning_dir=None,
        sdk_root=sdk_root,
        project_root_for_lesson=sdk_root,
    )

    assert result["outcome"]["level"] == 1
    assert result["outcome"]["action"] == "telemetry_only"
    assert result["outcome"]["reason"] == "target_after_run_not_provided"
    assert not (sdk_root / ".workflow" / "prompt_lessons.json").exists()


def test_learn_skips_lesson_when_sdk_run_failed(tmp_path: Path) -> None:
    """Level 0 (SDK spawn non-zero exit) → telemetry only."""

    sdk_root = tmp_path / "sdk"
    sdk_root.mkdir()
    record_path = _write_record(
        tmp_path / "planning" / "exec-3",
        _execution_record(spawn_status="non_zero_exit", spawn_exit_code=1),
    )
    target_after = tmp_path / "target-after"
    _write_target_state(target_after, run_reason="PROCEED_TO_GATE")

    result = learn_from_self_repair(
        execution_record_path=record_path,
        target_after_planning_dir=target_after,
        sdk_root=sdk_root,
        project_root_for_lesson=sdk_root,
    )

    assert result["outcome"]["level"] == 0
    assert result["outcome"]["action"] == "telemetry_only"
    # Either the spawn-status check or the exit-code check can fire first;
    # both indicate "SDK run failed → no learning".
    assert result["outcome"]["reason"] in {"sdk_spawn_failed", "sdk_run_non_zero_exit"}
    assert not (sdk_root / ".workflow" / "prompt_lessons.json").exists()


def test_learn_skips_lesson_when_target_did_not_advance(tmp_path: Path) -> None:
    """Level 1: SDK passed, but target re-run still BLOCKED with the same
    stop reason → fix did not actually unblock anything."""

    sdk_root = tmp_path / "sdk"
    sdk_root.mkdir()
    record_path = _write_record(
        tmp_path / "planning" / "exec-4",
        _execution_record(root_cause_summary="EXECUTION_BACKEND_BLOCKED"),
    )
    target_after = tmp_path / "target-after"
    _write_target_state(target_after, run_reason="EXECUTION_BACKEND_BLOCKED", final_status="BLOCKED")

    result = learn_from_self_repair(
        execution_record_path=record_path,
        target_after_planning_dir=target_after,
        sdk_root=sdk_root,
        project_root_for_lesson=sdk_root,
    )

    assert result["outcome"]["level"] == 1
    assert result["outcome"]["action"] == "telemetry_only"
    assert "target_did_not_advance" in result["outcome"]["reason"]


def test_learn_emits_lesson_when_target_advances_past_original_stop_even_if_not_full_success(tmp_path: Path) -> None:
    """If the target re-run stops at a DIFFERENT failure code than the
    original (e.g. now stalls in implement instead of recovery), the
    specific failure mode the self-repair targeted IS gone — count this
    as Level 2."""

    sdk_root = tmp_path / "sdk"
    sdk_root.mkdir()
    record_path = _write_record(
        tmp_path / "planning" / "exec-5",
        _execution_record(
            root_cause_code="recovery_synthesizer_timeout",
            root_cause_summary="RECOVERY_SYNTHESIZER_TIMEOUT",
        ),
    )
    target_after = tmp_path / "target-after"
    _write_target_state(target_after, run_reason="EXECUTOR_STALLED_FRAGMENTED_READS", final_status="BLOCKED")

    result = learn_from_self_repair(
        execution_record_path=record_path,
        target_after_planning_dir=target_after,
        sdk_root=sdk_root,
        project_root_for_lesson=sdk_root,
    )

    assert result["outcome"]["level"] == 2
    assert result["outcome"]["action"] == "lesson_emitted"
    assert result["outcome"]["template_id"] == "self_repair.recovery_fix_validated"


def test_learn_emits_specific_lesson_for_unproductive_fix_round(tmp_path: Path) -> None:
    sdk_root = tmp_path / "sdk"
    sdk_root.mkdir()
    record_path = _write_record(
        tmp_path / "planning" / "exec-unproductive-fix-round",
        _execution_record(
            root_cause_code="executor_fix_round_unproductive",
            root_cause_summary="EXECUTOR_FIX_ROUND_UNPRODUCTIVE",
        ),
    )
    target_after = tmp_path / "target-after"
    _write_target_state(target_after, run_reason="PROCEED_TO_GATE")

    result = learn_from_self_repair(
        execution_record_path=record_path,
        target_after_planning_dir=target_after,
        sdk_root=sdk_root,
        project_root_for_lesson=sdk_root,
    )

    assert result["outcome"]["level"] == 2
    assert result["outcome"]["action"] == "lesson_emitted"
    assert result["outcome"]["template_id"] == "executor.fix_round_unproductive"
    lessons = json.loads((sdk_root / ".workflow" / "prompt_lessons.json").read_text(encoding="utf-8"))
    assert any(
        candidate["template_id"] == "executor.fix_round_unproductive"
        for candidate in lessons.get("prompt_lesson_candidates", [])
    )


def test_learn_handles_unknown_root_cause_with_telemetry_only(tmp_path: Path) -> None:
    """A root cause without a registered template falls back to telemetry."""

    sdk_root = tmp_path / "sdk"
    sdk_root.mkdir()
    record_path = _write_record(
        tmp_path / "planning" / "exec-6",
        _execution_record(root_cause_code="brand_new_classifier_category"),
    )
    target_after = tmp_path / "target-after"
    _write_target_state(target_after, run_reason="PROCEED_TO_GATE")

    result = learn_from_self_repair(
        execution_record_path=record_path,
        target_after_planning_dir=target_after,
        sdk_root=sdk_root,
        project_root_for_lesson=sdk_root,
    )

    assert result["outcome"]["action"] == "telemetry_only"
    assert "no_template_for_root_cause" in result["outcome"]["reason"]
    assert not (sdk_root / ".workflow" / "prompt_lessons.json").exists()


def test_learn_journal_appends_each_attempt(tmp_path: Path) -> None:
    """Multiple invocations append to the journal (jsonl format)."""

    sdk_root = tmp_path / "sdk"
    sdk_root.mkdir()
    for n in range(3):
        record_path = _write_record(
            tmp_path / "planning" / f"exec-multi-{n}",
            _execution_record(),
        )
        learn_from_self_repair(
            execution_record_path=record_path,
            target_after_planning_dir=None,
            sdk_root=sdk_root,
            project_root_for_lesson=sdk_root,
        )
    journal = sdk_root / SELF_REPAIR_JOURNAL_RELATIVE_PATH
    entries = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(entries) == 3


def test_learn_returns_failure_when_execution_record_missing(tmp_path: Path) -> None:
    sdk_root = tmp_path / "sdk"
    sdk_root.mkdir()

    result = learn_from_self_repair(
        execution_record_path=tmp_path / "nonexistent.json",
        sdk_root=sdk_root,
        project_root_for_lesson=sdk_root,
    )

    assert result["outcome"]["level"] == 0
    assert result["outcome"]["reason"] == "execution_record_unreadable"
