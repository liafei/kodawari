from __future__ import annotations

import json
from pathlib import Path
import time
from typing import Any

from kodawari.autopilot.core.collaboration import CollaborationAction
from kodawari.autopilot.engine.engine import AutopilotConfig, AutopilotEngine
from kodawari.autopilot.execution.tool_use_stall import StallDetector
from kodawari.autopilot.execution.tool_use_types import OpenAIToolUseExecutionError
from kodawari.autopilot.recovery.executor_recovery import RECOVERY_CARD_FILENAME, RECOVERY_DECISION_FILENAME
from kodawari.autopilot.recovery.stall_recovery import (
    NO_WRITE_STALL_RECOVERY_ACTION,
    TOOL_CALL_LIMIT_RECOVERY_ACTION,
    build_no_write_stall_recovery,
    build_tool_call_limit_recovery,
)


def test_no_write_stall_recovery_builds_write_first_retry_for_scoped_files(tmp_path: Path) -> None:
    result = build_no_write_stall_recovery(
        project_root=tmp_path,
        original_card={
            "files_to_change": [
                "backend/api/v1/routes/external_trends_routes.py",
                "backend/api/v1/router.py",
                "tests/test_external_trends_routes.py",
            ],
            "new_files": [
                "backend/api/v1/routes/external_trends_routes.py",
                "tests/test_external_trends_routes.py",
            ],
            "read_only_files": ["backend/api/v1/routes/feed_routes.py"],
            "review_focus": ["Preserve exact error contract."],
            "verify_cmd": "python -m pytest tests/test_external_trends_routes.py -q",
        },
        task_id="T002",
        must_fix=["executor made no write/patch/finish progress within the configured stall window"],
        stall_report=_stall_report(),
    )

    assert result is not None
    decision, card = result
    assert decision["action"] == NO_WRITE_STALL_RECOVERY_ACTION
    assert decision["stall_counters"]["no_write_iterations"] == 13
    assert card["files_to_change"] == [
        "backend/api/v1/routes/external_trends_routes.py",
        "backend/api/v1/router.py",
        "tests/test_external_trends_routes.py",
    ]
    assert card["new_files"] == [
        "backend/api/v1/routes/external_trends_routes.py",
        "tests/test_external_trends_routes.py",
    ]
    assert card["read_only_files"] == ["backend/api/v1/routes/feed_routes.py"]
    assert card["review_focus"] == ["Preserve exact error contract."]
    assert card["recovery"]["source_action"] == NO_WRITE_STALL_RECOVERY_ACTION
    assert any("first write step" in item for item in card["recovery"]["instructions"])


def test_no_write_stall_recovery_preserves_previous_verify_failure_context(tmp_path: Path) -> None:
    result = build_no_write_stall_recovery(
        project_root=tmp_path,
        original_card={
            "files_to_change": ["src/app.py", "tests/test_app.py"],
            "verify_cmd": "python -m pytest tests/test_app.py -q",
            "recovery": {
                "source_action": "pytest_verify_failure_fix",
                "must_fix": ["FAILED tests/test_app.py::test_value - AssertionError"],
                "failed_tests": ["tests/test_app.py::test_value"],
                "instructions": ["Fix the assertion target before broad reads."],
            },
        },
        task_id="T002",
        must_fix=["executor kept reading/searching after patch-plan-required mode"],
        stall_report=_stall_report(error_code="EXECUTOR_STALLED_PATCH_PLAN_REQUIRED"),
    )

    assert result is not None
    _decision, card = result
    assert card["recovery"]["must_fix"] == [
        "executor kept reading/searching after patch-plan-required mode",
        "FAILED tests/test_app.py::test_value - AssertionError",
    ]
    assert card["recovery"]["previous_recovery_context"]["source_action"] == "pytest_verify_failure_fix"
    assert card["recovery"]["previous_recovery_context"]["failed_tests"] == ["tests/test_app.py::test_value"]


def test_no_write_stall_recovery_rejects_scope_exhaustion_or_patch_failures(tmp_path: Path) -> None:
    card = {"files_to_change": ["src/router.py"]}

    exhausted = _stall_report(read_scope_exhausted=True)
    patch_failed = _stall_report(patch_apply_failures=1)

    assert build_no_write_stall_recovery(
        project_root=tmp_path,
        original_card=card,
        task_id="T002",
        must_fix=["stalled"],
        stall_report=exhausted,
    ) is None
    assert build_no_write_stall_recovery(
        project_root=tmp_path,
        original_card=card,
        task_id="T002",
        must_fix=["stalled"],
        stall_report=patch_failed,
    ) is None


def test_no_write_stall_recovery_rejects_existing_patch_plan(tmp_path: Path) -> None:
    report = _stall_report()
    report["patch_plan"] = {"total": 2, "applied": [], "remaining": ["p1", "p2"]}

    assert build_no_write_stall_recovery(
        project_root=tmp_path,
        original_card={"files_to_change": ["src/router.py"]},
        task_id="T002",
        must_fix=["stalled"],
        stall_report=report,
    ) is None


def test_tool_call_limit_recovery_builds_consolidated_retry(tmp_path: Path) -> None:
    result = build_tool_call_limit_recovery(
        project_root=tmp_path,
        original_card={
            "files_to_change": ["src/router.py", "tests/test_router.py"],
            "new_files": ["tests/test_router.py"],
            "review_focus": ["Wire route to service."],
            "verify_cmd": "python -m pytest tests/test_router.py -q",
        },
        task_id="T002",
        must_fix=["Wire route to service."],
        execution_result={
            "error_code": "MAX_SAME_TOOL_CALLS_PER_PATH",
            "blocking_reason": "str_replace called too many times for tests/test_router.py",
        },
    )

    assert result is not None
    decision, card = result
    assert decision["action"] == TOOL_CALL_LIMIT_RECOVERY_ACTION
    assert decision["tool_call_limit"] == {"tool": "str_replace", "path": "tests/test_router.py"}
    assert card["files_to_change"] == ["src/router.py", "tests/test_router.py"]
    assert card["new_files"] == ["tests/test_router.py"]
    assert card["review_focus"] == ["Wire route to service."]
    assert card["recovery"]["source_action"] == TOOL_CALL_LIMIT_RECOVERY_ACTION
    assert card["recovery"]["tool_call_limit"]["path"] == "tests/test_router.py"


def test_tool_call_limit_recovery_prefers_structured_stall_report(tmp_path: Path) -> None:
    result = build_tool_call_limit_recovery(
        project_root=tmp_path,
        original_card={"files_to_change": ["src/router.py", "tests/test_router.py"]},
        task_id="T002",
        must_fix=["Use one consolidated edit."],
        stall_report={
            "schema_version": "execution.stall_report.v1",
            "error_code": "MAX_SAME_TOOL_CALLS_PER_PATH",
            "tool_call_limit": {"tool": "str_replace", "path": "tests/test_router.py", "count": 6},
            "error_message": "same target exceeded guard",
        },
        execution_result={
            "error_code": "MAX_SAME_TOOL_CALLS_PER_PATH",
            "blocking_reason": "wording changed and no longer matches legacy regex",
        },
    )

    assert result is not None
    decision, card = result
    assert decision["tool_call_limit"] == {"tool": "str_replace", "path": "tests/test_router.py"}
    assert card["recovery"]["tool_call_limit"]["path"] == "tests/test_router.py"


def test_stall_report_snapshot_includes_structured_tool_call_limit(tmp_path: Path) -> None:
    detector = StallDetector(config=object())
    detector.record_tool_call_limit(tool="str_replace", path="tests/test_router.py", count=6)
    runtime = _SnapshotRuntime(tmp_path)

    payload = detector.snapshot(runtime=runtime, iteration=7, reason="MAX_SAME_TOOL_CALLS_PER_PATH")

    assert payload["tool_call_limit"] == {"tool": "str_replace", "path": "tests/test_router.py", "count": 6}


def test_record_prompt_cache_accumulates_across_iterations(tmp_path: Path) -> None:
    """record_prompt_cache sums (not max) because cache stats are per-call deltas."""
    detector = StallDetector(config=object())
    detector.record_prompt_cache(hit=100, miss=20)
    detector.record_prompt_cache(hit=50, miss=30)
    detector.record_prompt_cache(hit=0, miss=0)  # noisy zero call from provider w/o cache

    runtime = _SnapshotRuntime(tmp_path)
    payload = detector.snapshot(runtime=runtime, iteration=3, reason="EXECUTOR_STALLED_NO_WRITE_PROGRESS")

    assert payload["prompt_cache_hit_tokens"] == 150
    assert payload["prompt_cache_miss_tokens"] == 50


def test_record_prompt_cache_clamps_negative_inputs(tmp_path: Path) -> None:
    detector = StallDetector(config=object())
    detector.record_prompt_cache(hit=-10, miss=-5)
    detector.record_prompt_cache(hit=20, miss=10)

    runtime = _SnapshotRuntime(tmp_path)
    payload = detector.snapshot(runtime=runtime, iteration=2, reason="EXECUTOR_STALLED_NO_WRITE_PROGRESS")

    assert payload["prompt_cache_hit_tokens"] == 20
    assert payload["prompt_cache_miss_tokens"] == 10


def test_snapshot_defaults_cache_tokens_to_zero(tmp_path: Path) -> None:
    """Detector never seeing cache fields -> snapshot reports 0 (not missing key)."""
    detector = StallDetector(config=object())
    runtime = _SnapshotRuntime(tmp_path)
    payload = detector.snapshot(runtime=runtime, iteration=1, reason="EXECUTOR_STALLED_NO_WRITE_PROGRESS")

    assert payload["prompt_cache_hit_tokens"] == 0
    assert payload["prompt_cache_miss_tokens"] == 0


def test_no_write_guard_extends_window_while_observation_progress_continues() -> None:
    detector = StallDetector(
        config=_Caps(
            {
                "max_no_write_iterations": 3,
                "max_no_write_iterations_with_observation": 8,
                "max_no_write_observation_grace_iterations": 2,
            }
        )
    )

    detector.record_observation_progress(4)
    detector.enforce_no_write_progress(4)
    detector.enforce_no_write_progress(6)

    try:
        detector.enforce_no_write_progress(7)
    except OpenAIToolUseExecutionError as exc:
        assert exc.code == "EXECUTOR_STALLED_NO_WRITE_PROGRESS"
    else:  # pragma: no cover - guard assertion
        raise AssertionError("stale observation progress should not suppress no-write stall forever")


def test_tool_call_limit_recovery_rejects_out_of_scope_path(tmp_path: Path) -> None:
    assert build_tool_call_limit_recovery(
        project_root=tmp_path,
        original_card={"files_to_change": ["src/router.py"]},
        task_id="T002",
        must_fix=["fix"],
        execution_result={
            "error_code": "MAX_SAME_TOOL_CALLS_PER_PATH",
            "blocking_reason": "str_replace called too many times for tests/test_router.py",
        },
    ) is None


def test_engine_routes_no_write_stall_without_synthesizer(tmp_path: Path) -> None:
    planning_dir = tmp_path / "planning" / "newsapp"
    _write_project_file(
        planning_dir / "TASK_CARD_ACTIVE.json",
        json.dumps(
            {
                "schema_version": "contract_first.task_card.v1",
                "task_id": "T002",
                "task_name": "Create external trends route module",
                "files_to_change": [
                    "backend/api/v1/routes/external_trends_routes.py",
                    "backend/api/v1/router.py",
                    "tests/test_external_trends_routes.py",
                ],
                "new_files": [
                    "backend/api/v1/routes/external_trends_routes.py",
                    "tests/test_external_trends_routes.py",
                ],
                "verify_cmd": "python -m pytest tests/test_external_trends_routes.py -q",
            }
        ),
    )
    adapter = _NoSynthesizerAdapter()
    engine = AutopilotEngine(AutopilotConfig(project_root=tmp_path, feature="newsapp"), adapter=adapter)
    runtime = engine._create_loop_runtime(
        task_label="T002: Create external trends route module",
        task_scope="recover no-write stall",
        max_rounds=2,
        enable_peer_review=True,
    )
    runtime.context.review_feedback.must_fix = [
        "executor made no write/patch/finish progress within the configured stall window"
    ]
    runtime.execution_result = {
        "error_code": "EXECUTOR_STALLED_NO_WRITE_PROGRESS",
        "stall_report": _stall_report(),
    }

    result = engine._maybe_prepare_executor_recovery(
        runtime=runtime,
        action=CollaborationAction.FIX_ROUND,
        round_record={},
    )

    assert result is None
    assert adapter.recovery_calls == 0
    assert runtime.pending_recovery_card is not None
    assert runtime.pending_recovery_card["recovery"]["source_action"] == NO_WRITE_STALL_RECOVERY_ACTION
    assert runtime.recovery_decisions[-1]["detector_name"] == "no_write_stall"
    assert json.loads((planning_dir / RECOVERY_CARD_FILENAME).read_text(encoding="utf-8"))["recovery"][
        "source_action"
    ] == NO_WRITE_STALL_RECOVERY_ACTION
    assert json.loads((planning_dir / RECOVERY_DECISION_FILENAME).read_text(encoding="utf-8"))[
        "action"
    ] == NO_WRITE_STALL_RECOVERY_ACTION


def test_engine_yields_repeated_no_write_stall_to_recovery_synthesizer(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("WORKFLOW_AUTOPILOT_RECOVERY_SYNTHESIZER", "1")
    planning_dir = tmp_path / "planning" / "newsapp"
    _write_project_file(tmp_path / "src" / "router.py", "def route():\n    return {'ok': False}\n")
    _write_project_file(
        planning_dir / "TASK_CARD_ACTIVE.json",
        json.dumps(
            {
                "schema_version": "contract_first.task_card.v1",
                "task_id": "T002",
                "task_name": "Create route",
                "files_to_change": ["src/router.py"],
                "verify_cmd": "python -m pytest tests/test_router.py -q",
            }
        ),
    )
    adapter = _PatchPlanSynthesizerAdapter()
    engine = AutopilotEngine(AutopilotConfig(project_root=tmp_path, feature="newsapp"), adapter=adapter)
    runtime = engine._create_loop_runtime(
        task_label="T002: Create route",
        task_scope="recover repeated no-write stall",
        max_rounds=2,
        enable_peer_review=True,
    )
    must_fix = ["executor made no write/patch/finish progress within the configured stall window"]
    runtime.context.review_feedback.must_fix = must_fix
    runtime.execution_result = {
        "error_code": "EXECUTOR_STALLED_NO_WRITE_PROGRESS",
        "stall_report": _stall_report(),
    }

    first = engine._maybe_prepare_executor_recovery(
        runtime=runtime,
        action=CollaborationAction.FIX_ROUND,
        round_record={},
    )
    assert first is None
    assert adapter.recovery_calls == 0
    assert runtime.pending_recovery_card is not None
    runtime.recovery_attempt_signature = engine._executor_recovery_attempt_signature(
        runtime=runtime,
        must_fix=must_fix,
        error_code="EXECUTOR_STALLED_NO_WRITE_PROGRESS",
        failure_mode_tag="no_write_stall",
        affected_paths=[],
    )
    runtime.recovery_attempt_signature = ""
    runtime.recovery_attempts_for_signature = 0

    second = engine._maybe_prepare_executor_recovery(
        runtime=runtime,
        action=CollaborationAction.FIX_ROUND,
        round_record={},
    )

    assert second is None
    assert adapter.recovery_calls == 1
    assert adapter.last_context["yielded_deterministic_detector"] == "no_write_stall"
    assert runtime.pending_recovery_card is not None
    assert runtime.pending_recovery_card["recovery"]["source_action"] == "narrow_patch_plan"
    assert runtime.pending_recovery_card["patch_plan"][0]["path"] == "src/router.py"
    assert runtime.recovery_decisions[-1]["role"] == "recovery_synthesizer"


def test_engine_routes_tool_call_limit_without_synthesizer(tmp_path: Path) -> None:
    planning_dir = tmp_path / "planning" / "newsapp"
    _write_project_file(
        planning_dir / "TASK_CARD_ACTIVE.json",
        json.dumps(
            {
                "schema_version": "contract_first.task_card.v1",
                "task_id": "T002",
                "task_name": "Create route",
                "files_to_change": ["src/router.py", "tests/test_router.py"],
                "new_files": ["tests/test_router.py"],
                "verify_cmd": "python -m pytest tests/test_router.py -q",
            }
        ),
    )
    adapter = _NoSynthesizerAdapter()
    engine = AutopilotEngine(AutopilotConfig(project_root=tmp_path, feature="newsapp"), adapter=adapter)
    runtime = engine._create_loop_runtime(
        task_label="T002: Create route",
        task_scope="recover tool limit",
        max_rounds=2,
        enable_peer_review=True,
    )
    runtime.context.review_feedback.must_fix = ["Consolidate the test edits and wire the route."]
    runtime.execution_result = {
        "error_code": "MAX_SAME_TOOL_CALLS_PER_PATH",
        "blocking_reason": "str_replace called too many times for tests/test_router.py",
    }

    result = engine._maybe_prepare_executor_recovery(
        runtime=runtime,
        action=CollaborationAction.FIX_ROUND,
        round_record={},
    )

    assert result is None
    assert adapter.recovery_calls == 0
    assert runtime.pending_recovery_card is not None
    assert runtime.pending_recovery_card["recovery"]["source_action"] == TOOL_CALL_LIMIT_RECOVERY_ACTION
    assert runtime.recovery_decisions[-1]["detector_name"] == "same_path_tool_limit"
    assert json.loads((planning_dir / RECOVERY_DECISION_FILENAME).read_text(encoding="utf-8"))[
        "action"
    ] == TOOL_CALL_LIMIT_RECOVERY_ACTION


def test_engine_does_not_synthesize_recovery_for_peer_review_must_fix(tmp_path: Path) -> None:
    _write_project_file(
        tmp_path / "planning" / "newsapp" / "TASK_CARD_ACTIVE.json",
        json.dumps(
            {
                "schema_version": "contract_first.task_card.v1",
                "task_id": "T002",
                "task_name": "Create external trends route module",
                "files_to_change": ["src/router.py", "tests/test_router.py"],
                "verify_cmd": "python -m pytest tests/test_router.py -q",
            }
        ),
    )
    adapter = _NoSynthesizerAdapter()
    engine = AutopilotEngine(AutopilotConfig(project_root=tmp_path, feature="newsapp"), adapter=adapter)
    runtime = engine._create_loop_runtime(
        task_label="T002: Create external trends route module",
        task_scope="normal reviewer fix",
        max_rounds=2,
        enable_peer_review=True,
    )
    runtime.context.review_feedback.must_fix = ["Wire the route to the real service layer."]
    runtime.execution_result = {"schema_version": "execution.result.v1", "status": "PASS", "error_code": ""}
    runtime.verify_check = {"status": "PASS", "passed": True}
    runtime.gate_check = {"total_status": "PASS", "passed": True}

    result = engine._maybe_prepare_executor_recovery(
        runtime=runtime,
        action=CollaborationAction.FIX_ROUND,
        round_record={},
    )

    assert result is None
    assert adapter.recovery_calls == 0
    assert runtime.pending_recovery_card is None


def test_engine_does_not_accept_no_write_resume_with_open_must_fix(tmp_path: Path) -> None:
    planning_dir = tmp_path / "planning" / "newsapp"
    _write_project_file(tmp_path / "src" / "router.py", "def route():\n    return {'ok': False}\n")
    _write_project_file(tmp_path / "tests" / "test_router.py", "def test_router():\n    assert True\n")
    _write_project_file(
        planning_dir / "TASK_CARD_ACTIVE.json",
        json.dumps(
            {
                "schema_version": "contract_first.task_card.v1",
                "task_id": "T002",
                "task_name": "Create route",
                "files_to_change": ["src/router.py", "tests/test_router.py"],
                "new_files": ["src/router.py", "tests/test_router.py"],
                "verify_cmd": "python -m pytest tests/test_router.py -q",
            }
        ),
    )
    engine = AutopilotEngine(AutopilotConfig(project_root=tmp_path, feature="newsapp"))
    runtime = engine._create_loop_runtime(
        task_label="T002: Create route",
        task_scope="review fix should require a write",
        max_rounds=2,
        enable_peer_review=True,
    )
    runtime.context.review_feedback.must_fix = ["Replace the local stub with the real service import."]

    accepted = engine._maybe_accept_no_write_resume(
        runtime=runtime,
        action=CollaborationAction.FIX_ROUND,
        round_record={},
        result={"backend": "openai_tool_use"},
        backend_error_code="EXECUTOR_STALLED_NO_WRITE_PROGRESS",
        impl_context={"project_root": str(tmp_path)},
    )

    assert accepted is False
    assert not (planning_dir / ".execution_result.json").exists()


def test_recovery_signature_isolates_failure_mode_without_must_fix_drift(tmp_path: Path) -> None:
    engine = AutopilotEngine(AutopilotConfig(project_root=tmp_path, feature="newsapp"))
    runtime = engine._create_loop_runtime(
        task_label="T002: Create route",
        task_scope="signature check",
        max_rounds=2,
        enable_peer_review=True,
    )
    runtime.execution_result = {"error_code": "VERIFY_FAILED", "summary": "FAILED tests/test_router.py::test_route"}

    original = engine._executor_recovery_attempt_signature(
        runtime=runtime,
        must_fix=["Wire the route to the service."],
        error_code="VERIFY_FAILED",
        failure_mode_tag="pytest_collection_nameerror",
        affected_paths=["tests/test_router.py"],
    )
    punctuation_only = engine._executor_recovery_attempt_signature(
        runtime=runtime,
        must_fix=["  Wire the route to the service!  "],
        error_code="VERIFY_FAILED",
        failure_mode_tag="pytest_collection_nameerror",
        affected_paths=["tests/test_router.py"],
    )
    different_mode = engine._executor_recovery_attempt_signature(
        runtime=runtime,
        must_fix=["Wire the route to the service."],
        error_code="VERIFY_FAILED",
        failure_mode_tag="same_path_tool_limit",
        affected_paths=["tests/test_router.py"],
    )
    different_code = engine._executor_recovery_attempt_signature(
        runtime=runtime,
        must_fix=["Wire the route to the service."],
        error_code="MAX_SAME_TOOL_CALLS_PER_PATH",
        failure_mode_tag="same_path_tool_limit",
        affected_paths=["tests/test_router.py"],
    )

    assert punctuation_only == original
    assert different_mode != original
    assert different_code != original


def test_recovery_synthesizer_timeout_blocks_without_counting_attempt(monkeypatch, tmp_path: Path) -> None:
    planning_dir = tmp_path / "planning" / "newsapp"
    _write_project_file(
        planning_dir / "TASK_CARD_ACTIVE.json",
        json.dumps({"schema_version": "contract_first.task_card.v1", "task_id": "T002", "files_to_change": ["src/router.py"]}),
    )
    monkeypatch.setenv("WORKFLOW_AUTOPILOT_RECOVERY_SYNTHESIZER", "1")
    monkeypatch.setenv("WORKFLOW_RECOVERY_SYNTHESIZER_TIMEOUT_SECONDS", "1")
    adapter = _SlowSynthesizerAdapter()
    engine = AutopilotEngine(AutopilotConfig(project_root=tmp_path, feature="newsapp"), adapter=adapter)
    runtime = engine._create_loop_runtime(
        task_label="T002: Create route",
        task_scope="timeout recovery",
        max_rounds=2,
        enable_peer_review=True,
    )
    runtime.context.review_feedback.must_fix = ["Patch failures need recovery."]
    runtime.execution_result = {
        "error_code": "EXECUTOR_STALLED_PATCH_FAILURES",
        "blocking_reason": "executor repeatedly failed to apply patch operations",
    }

    result = engine._maybe_prepare_executor_recovery(
        runtime=runtime,
        action=CollaborationAction.FIX_ROUND,
        round_record={},
    )

    assert result is not None
    assert result["reason"] == "RECOVERY_SYNTHESIZER_TIMEOUT"
    assert runtime.recovery_attempts == 0
    assert runtime.recovery_attempts_for_signature == 0
    assert (planning_dir / ".execution_failure_snapshot.json").exists()
    assert json.loads((planning_dir / RECOVERY_DECISION_FILENAME).read_text(encoding="utf-8"))[
        "error_code"
    ] == "RECOVERY_SYNTHESIZER_TIMEOUT"


class _NoSynthesizerAdapter:
    def __init__(self) -> None:
        self.recovery_calls = 0

    def synthesize_executor_recovery(self, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        self.recovery_calls += 1
        raise AssertionError("no-write stalls must not call the recovery synthesizer")


class _SlowSynthesizerAdapter:
    def __init__(self) -> None:
        self.recovery_calls = 0

    def synthesize_executor_recovery(self, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        self.recovery_calls += 1
        time.sleep(5)
        return {"status": "ok", "decision": {"action": "narrow_patch_plan"}}


class _PatchPlanSynthesizerAdapter:
    def __init__(self) -> None:
        self.recovery_calls = 0
        self.last_context: dict[str, Any] = {}

    def synthesize_executor_recovery(self, **kwargs: Any) -> dict[str, Any]:
        self.recovery_calls += 1
        self.last_context = dict(kwargs.get("context") or {})
        return {
            "status": "ok",
            "role": "recovery_synthesizer",
            "source": "kodawari.recovery_synthesizer",
            "backend": "test",
            "model": "test",
            "decision": {
                "schema_version": "execution.recovery_decision.v1",
                "action": "narrow_patch_plan",
                "reason": "write concrete recovery patch after repeated no-write stall",
                "patch_plan": [
                    {
                        "id": "fix_route",
                        "operation": "str_replace",
                        "path": "src/router.py",
                        "old_text": "return {'ok': False}",
                        "new_text": "return {'ok': True}",
                    }
                ],
            },
        }


class _SnapshotRuntime:
    run_id = "run-1"
    request_payload = {"task_id": "T002"}
    read_scope_widenings: list[dict[str, Any]] = []
    read_scope_exhausted = False

    def __init__(self, root: Path) -> None:
        self.root = root

    def patch_plan_status(self) -> dict[str, Any]:
        return {"total": 0, "applied": [], "remaining": []}

    def tool_log_path(self) -> Path:
        return self.root / ".execution_tool_calls.jsonl"


class _Caps:
    def __init__(self, runtime_caps: dict[str, int]) -> None:
        self.runtime_caps = runtime_caps


def _stall_report(
    *,
    read_scope_exhausted: bool = False,
    patch_apply_failures: int = 0,
    error_code: str = "EXECUTOR_STALLED_NO_WRITE_PROGRESS",
) -> dict[str, Any]:
    return {
        "schema_version": "execution.stall_report.v1",
        "error_code": error_code,
        "reason": error_code,
        "read_scope_exhausted": read_scope_exhausted,
        "counters": {
            "no_write_iterations": 13,
            "patch_apply_failures": patch_apply_failures,
            "read_scope_widenings": 0,
        },
        "patch_plan": {"total": 0, "applied": [], "remaining": []},
    }


def _write_project_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
