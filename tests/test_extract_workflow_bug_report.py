"""Unit tests for ``scripts/extract_workflow_bug_report.py``.

Two fixtures correspond to real failure modes observed during the
generalization runs on /e/wf-test/newsapp:

1. **synthesizer_timeout**: deterministic recovery yielded to the LLM
   synthesizer which timed out at 60s. Final state: STUCK / BLOCKED with
   ``last_stage_status=executor_recovery_synthesizer_timeout``.

2. **review_round_limit**: executor stalled then was recovered by a
   deterministic ``no_write_stall`` retry. Code was written and verify
   passed, but peer review requested changes and the loop hit
   ``max_review_rounds=1`` cap. Final state: STUCK / BLOCKED with
   ``last_stage_status=round_limit``.

The extractor must distinguish the two even though both terminate with
``stop_reason=STUCK`` and ``final_status=BLOCKED``.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "extract_workflow_bug_report.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("extract_workflow_bug_report", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["extract_workflow_bug_report"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def extractor():
    return _load_module()


def _write_planning(planning_dir: Path, *, state: dict, rounds: list[dict], extras: dict[str, dict]) -> None:
    planning_dir.mkdir(parents=True, exist_ok=True)
    (planning_dir / ".autopilot_state.json").write_text(json.dumps(state), encoding="utf-8")
    (planning_dir / ".autopilot_rounds.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rounds), encoding="utf-8"
    )
    for name, payload in extras.items():
        (planning_dir / name).write_text(json.dumps(payload), encoding="utf-8")


def test_synthesizer_timeout_is_classified_under_recovery_engine(extractor, tmp_path: Path) -> None:
    planning = tmp_path / "feat-001"
    state = {
        "feature": "prd11-x-001",
        "project_root": str(tmp_path),
        "current_stage": "COMPLETED",
        "final_status": "BLOCKED",
        "stop_reason": "STUCK",
        "last_error": "recovery synthesizer timed out after 60s",
        "last_stage_status": "executor_recovery_synthesizer_timeout",
        "changed_files": [],
        "error_events": [
            {
                "phase": "IMPLEMENT",
                "category": "implement",
                "error_code": "EXECUTOR_STALLED_PATCH_PLAN_REQUIRED",
                "message": "executor kept reading without writing",
            },
            {
                "phase": "IMPLEMENT",
                "category": "recovery",
                "error_code": "RECOVERY_SYNTHESIZER_TIMEOUT",
                "message": "recovery synthesizer timed out after 60s",
            },
        ],
    }
    rounds = [
        {"action": "implement", "task_id": "T1", "stage_status": "needs_recovery"},
        {
            "action": "fix_round",
            "task_id": "T1",
            "stage_status": "blocked",
            "details": {"recovery": {"role": "recovery_synthesizer", "status": "timeout", "error_code": "RECOVERY_SYNTHESIZER_TIMEOUT"}},
        },
        {"action": "fix_round", "task_id": "T1", "stage_status": "blocked"},
    ]
    extras = {
        ".execution_recovery_decision.json": {
            "action": "escalate_to_human",
            "error_code": "RECOVERY_SYNTHESIZER_TIMEOUT",
            "diagnosis": "recovery synthesizer timed out after 60s",
        },
        ".execution_stall_report.json": {"error_code": "EXECUTOR_STALLED_PATCH_PLAN_REQUIRED"},
    }
    _write_planning(planning, state=state, rounds=rounds, extras=extras)

    report = extractor.build_bug_report(planning)

    sig = report["failure_signature"]
    assert sig["error_code"] == "RECOVERY_SYNTHESIZER_TIMEOUT"
    assert sig["stall_kind"] == "EXECUTOR_STALLED_PATCH_PLAN_REQUIRED"
    assert sig["synthesizer_invoked"] is True
    assert sig["synthesizer_outcome"] == "timeout"
    assert sig["terminal_signal"] == "STAGE_STATUS:EXECUTOR_RECOVERY_SYNTHESIZER_TIMEOUT"
    assert "src/kodawari/autopilot/engine/engine_recovery_mixin.py" in report["suspected_components"]


def test_review_round_limit_is_classified_under_review_engine(extractor, tmp_path: Path) -> None:
    planning = tmp_path / "feat-002"
    state = {
        "feature": "prd11-x-002",
        "project_root": str(tmp_path),
        "current_stage": "COMPLETED",
        "final_status": "BLOCKED",
        "stop_reason": "STUCK",
        "last_error": "Reached review round limit (1)",
        "last_stage_status": "round_limit",
        "changed_files": ["backend/api/v1/services/aggregation.py"],
        "error_events": [
            {
                "phase": "IMPLEMENT",
                "category": "implement",
                "error_code": "EXECUTOR_STALLED_PATCH_PLAN_REQUIRED",
                "message": "executor kept reading without writing",
            },
            {
                "phase": "PLAN_REVIEW",
                "category": "review",
                "error_code": None,
                "message": "Reached review round limit (1)",
            },
        ],
    }
    rounds = [
        {"action": "implement", "task_id": "T1", "stage_status": "needs_recovery"},
        {
            "action": "fix_round",
            "task_id": "T1",
            "stage_status": "ok",
            "details": {"recovery": {"role": "deterministic_recovery", "detector_name": "no_write_stall"}},
        },
        {"action": "verify", "task_id": "T1", "stage_status": "pass"},
        {"action": "rules_gate", "task_id": "T1", "stage_status": "pass"},
        {"action": "peer_review", "task_id": "T1", "stage_status": "changes_requested"},
    ]
    _write_planning(planning, state=state, rounds=rounds, extras={})

    report = extractor.build_bug_report(planning)

    sig = report["failure_signature"]
    # Most-recent real error_code: the IMPLEMENT stall (the review event has error_code=null).
    assert sig["error_code"] == "EXECUTOR_STALLED_PATCH_PLAN_REQUIRED"
    assert sig["terminal_signal"] == "STAGE_STATUS:ROUND_LIMIT"
    assert sig["deterministic_recovery_hits"] == 1
    assert sig["synthesizer_invoked"] is False
    # Suspected components must include the review-side files (the stop is at review round limit).
    assert "src/kodawari/autopilot/engine/engine_review_mixin.py" in report["suspected_components"]


def test_distinct_failure_modes_get_distinct_bug_signature_hashes(extractor, tmp_path: Path) -> None:
    """Different failure modes must produce different bug_signature_hash values
    so the meta-autopilot does not de-duplicate genuinely-different bugs."""

    a = tmp_path / "a"
    b = tmp_path / "b"
    base_state = {
        "feature": "f",
        "project_root": str(tmp_path),
        "final_status": "BLOCKED",
        "stop_reason": "STUCK",
    }
    _write_planning(
        a,
        state={**base_state, "last_stage_status": "executor_recovery_synthesizer_timeout",
                "error_events": [{"phase": "IMPLEMENT", "error_code": "RECOVERY_SYNTHESIZER_TIMEOUT"}]},
        rounds=[],
        extras={},
    )
    _write_planning(
        b,
        state={**base_state, "last_stage_status": "round_limit",
                "error_events": [{"phase": "IMPLEMENT", "error_code": "EXECUTOR_STALLED_PATCH_PLAN_REQUIRED"}]},
        rounds=[],
        extras={},
    )
    assert extractor.build_bug_report(a)["bug_signature_hash"] != extractor.build_bug_report(b)["bug_signature_hash"]


def test_missing_state_file_raises(extractor, tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        extractor.build_bug_report(tmp_path)
