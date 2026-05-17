from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from kodawari.autopilot.core.collaboration import CollaborationAction
from kodawari.autopilot.engine.engine import AutopilotConfig, AutopilotEngine
from kodawari.autopilot.recovery.executor_recovery import RECOVERY_CARD_FILENAME, RECOVERY_DECISION_FILENAME
from kodawari.autopilot.recovery.failure_event import build_failure_event
from kodawari.autopilot.recovery.pytest_recovery import (
    PYTEST_COLLECTION_NAMEERROR_RECOVERY_ACTION,
    PYTEST_VERIFY_FAILURE_RECOVERY_ACTION,
    build_pytest_collection_nameerror_recovery,
    build_pytest_verify_failure_recovery,
)
from kodawari.autopilot.recovery.registry import RecoveryContext, route_deterministic_recovery


def test_pytest_collection_nameerror_recovery_targets_in_scope_file(tmp_path: Path) -> None:
    _write_project_file(tmp_path / "tests" / "test_external_trends_service.py", "def test_collects():\n    pass\n")

    result = build_pytest_collection_nameerror_recovery(
        project_root=tmp_path,
        original_card={
            "files_to_change": ["backend/api/v1/services/external_trends_service.py", "tests/test_external_trends_service.py"],
            "related_existing_tests": ["tests/test_external_trends_service.py"],
            "read_only_files": ["backend/api/v1/routes/external_trends.py"],
            "review_focus": ["Preserve provider limit pass-through."],
            "verify_cmd": "python -m pytest tests/test_external_trends_service.py -q",
        },
        task_id="T_EXT_01",
        must_fix=[_pytest_nameerror_output()],
    )

    assert result is not None
    decision, card = result
    assert decision["action"] == PYTEST_COLLECTION_NAMEERROR_RECOVERY_ACTION
    assert decision["pytest_nameerrors"] == [
        {"name": "_make_sample_items", "paths": ["tests/test_external_trends_service.py"]}
    ]
    assert card["files_to_change"] == ["tests/test_external_trends_service.py"]
    assert card["read_only_files"] == ["backend/api/v1/routes/external_trends.py"]
    assert "related_existing_tests" not in card
    assert card["review_focus"] == ["Preserve provider limit pass-through."]
    assert card["recovery"]["missing_names"] == ["_make_sample_items"]
    assert card["recovery"]["source_action"] == PYTEST_COLLECTION_NAMEERROR_RECOVERY_ACTION


def test_pytest_collection_nameerror_recovery_uses_execution_result_stdout(tmp_path: Path) -> None:
    _write_project_file(tmp_path / "tests" / "test_external_trends_service.py", "def test_collects():\n    pass\n")

    result = build_pytest_collection_nameerror_recovery(
        project_root=tmp_path,
        original_card={"files_to_change": ["tests/test_external_trends_service.py"]},
        task_id="T_EXT_01",
        must_fix=["verify failed"],
        execution_result={"verify_summary": {"stdout_excerpt": _pytest_nameerror_output()}},
    )

    assert result is not None
    assert result[1]["files_to_change"] == ["tests/test_external_trends_service.py"]


def test_pytest_collection_nameerror_recovery_uses_structured_collection_errors(tmp_path: Path) -> None:
    _write_project_file(tmp_path / "tests" / "test_external_trends_service.py", "def test_collects():\n    pass\n")

    result = build_pytest_collection_nameerror_recovery(
        project_root=tmp_path,
        original_card={"files_to_change": ["tests/test_external_trends_service.py"]},
        task_id="T_EXT_01",
        must_fix=["collection failed with wording that no longer matches stdout parser"],
        collection_errors=[
            {
                "file": "tests/test_external_trends_service.py",
                "exc_type": "NameError",
                "name": "_make_sample_items",
            }
        ],
    )

    assert result is not None
    assert result[0]["pytest_nameerrors"] == [
        {"name": "_make_sample_items", "paths": ["tests/test_external_trends_service.py"]}
    ]


def test_pytest_collection_nameerror_recovery_rejects_out_of_scope_file(tmp_path: Path) -> None:
    _write_project_file(tmp_path / "tests" / "test_external_trends_service.py", "def test_collects():\n    pass\n")

    result = build_pytest_collection_nameerror_recovery(
        project_root=tmp_path,
        original_card={"files_to_change": ["backend/api/v1/services/external_trends_service.py"]},
        task_id="T_EXT_01",
        must_fix=[_pytest_nameerror_output()],
    )

    assert result is None


def test_pytest_collection_nameerror_recovery_rejects_non_collection_failures(tmp_path: Path) -> None:
    _write_project_file(tmp_path / "tests" / "test_external_trends_service.py", "def test_collects():\n    pass\n")

    result = build_pytest_collection_nameerror_recovery(
        project_root=tmp_path,
        original_card={"files_to_change": ["tests/test_external_trends_service.py"]},
        task_id="T_EXT_01",
        must_fix=[
            """
FAILED tests/test_external_trends_service.py::test_limit
E   NameError: name '_make_sample_items' is not defined
"""
        ],
    )

    assert result is None


def test_pytest_verify_failure_recovery_builds_targeted_retry_card(tmp_path: Path) -> None:
    _write_project_file(tmp_path / "src" / "app.py", "def value():\n    return 1\n")
    _write_project_file(tmp_path / "tests" / "test_app.py", "def test_value():\n    assert True\n")

    result = build_pytest_verify_failure_recovery(
        project_root=tmp_path,
        original_card={
            "files_to_change": ["src/app.py", "tests/test_app.py"],
            "related_existing_tests": ["tests/test_app.py"],
            "verify_cmd": "python -m pytest tests/test_app.py -q",
        },
        task_id="T_APP_01",
        must_fix=[_pytest_assertion_failure_output()],
        verify_check={"passed": False, "stdout_excerpt": _pytest_assertion_failure_output()},
    )

    assert result is not None
    decision, card = result
    assert decision["action"] == PYTEST_VERIFY_FAILURE_RECOVERY_ACTION
    assert decision["failed_tests"] == ["tests/test_app.py::test_value"]
    assert card["recovery"]["source_action"] == PYTEST_VERIFY_FAILURE_RECOVERY_ACTION
    assert card["recovery"]["failed_tests"] == ["tests/test_app.py::test_value"]
    assert card["files_to_change"] == ["src/app.py", "tests/test_app.py"]


def test_pytest_verify_failure_routes_before_stale_no_write_stall(tmp_path: Path) -> None:
    _write_project_file(tmp_path / "src" / "app.py", "def value():\n    return 1\n")
    _write_project_file(tmp_path / "tests" / "test_app.py", "def test_value():\n    assert True\n")
    card = {
        "files_to_change": ["src/app.py", "tests/test_app.py"],
        "verify_cmd": "python -m pytest tests/test_app.py -q",
    }
    event = build_failure_event(
        stall_report={
            "error_code": "EXECUTOR_STALLED_NO_WRITE_PROGRESS",
            "counters": {"no_write_iterations": 13},
            "patch_plan": {},
        },
        execution_result={
            "error_code": "VERIFY_FAILED_RETRYABLE",
            "verify_summary": {"stdout_excerpt": _pytest_assertion_failure_output()},
        },
        verify_check={"passed": False, "stdout_excerpt": _pytest_assertion_failure_output()},
        must_fix=["scoped pytest failed"],
    )

    match = route_deterministic_recovery(
        RecoveryContext(
            project_root=tmp_path,
            original_card=card,
            task_id="T_APP_01",
            must_fix=["scoped pytest failed"],
            event=event,
        )
    )

    assert event.error_code == "VERIFY_FAILED_RETRYABLE"
    assert event.detector_hint == "pytest_verify_failure"
    assert match is not None
    assert match.name == "pytest_verify_failure"
    assert match.decision["action"] == PYTEST_VERIFY_FAILURE_RECOVERY_ACTION


def test_engine_routes_pytest_collection_nameerror_without_synthesizer(tmp_path: Path) -> None:
    planning_dir = tmp_path / "planning" / "newsapp"
    _write_project_file(tmp_path / "tests" / "test_external_trends_service.py", "def test_collects():\n    pass\n")
    _write_project_file(
        planning_dir / "TASK_CARD_ACTIVE.json",
        json.dumps(
            {
                "schema_version": "contract_first.task_card.v1",
                "task_id": "T_EXT_01",
                "task_name": "Implement external trends",
                "files_to_change": ["tests/test_external_trends_service.py"],
                "verify_cmd": "python -m pytest tests/test_external_trends_service.py -q",
            }
        ),
    )
    adapter = _NoSynthesizerAdapter()
    engine = AutopilotEngine(AutopilotConfig(project_root=tmp_path, feature="newsapp"), adapter=adapter)
    runtime = engine._create_loop_runtime(
        task_label="T_EXT_01: Implement external trends",
        task_scope="recover collection NameError",
        max_rounds=2,
        enable_peer_review=True,
    )
    runtime.context.review_feedback.must_fix = ["verify failed"]
    runtime.execution_result = {
        "error_code": "VERIFY_FAILED_RETRYABLE",
        "verify_summary": {"stdout_excerpt": _pytest_nameerror_output()},
    }

    result = engine._maybe_prepare_executor_recovery(
        runtime=runtime,
        action=CollaborationAction.FIX_ROUND,
        round_record={},
    )

    assert result is None
    assert adapter.recovery_calls == 0
    assert runtime.pending_recovery_card is not None
    assert runtime.pending_recovery_card["recovery"]["source_action"] == PYTEST_COLLECTION_NAMEERROR_RECOVERY_ACTION
    assert runtime.pending_recovery_card["files_to_change"] == ["tests/test_external_trends_service.py"]
    assert json.loads((planning_dir / RECOVERY_CARD_FILENAME).read_text(encoding="utf-8"))["recovery"][
        "source_action"
    ] == PYTEST_COLLECTION_NAMEERROR_RECOVERY_ACTION
    assert json.loads((planning_dir / RECOVERY_DECISION_FILENAME).read_text(encoding="utf-8"))[
        "action"
    ] == PYTEST_COLLECTION_NAMEERROR_RECOVERY_ACTION


class _NoSynthesizerAdapter:
    def __init__(self) -> None:
        self.recovery_calls = 0

    def synthesize_executor_recovery(self, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        self.recovery_calls += 1
        raise AssertionError("pytest collection NameError must not call the recovery synthesizer")


def _pytest_nameerror_output() -> str:
    return """
==================================== ERRORS ====================================
___________ ERROR collecting tests/test_external_trends_service.py ____________
tests\\test_external_trends_service.py:155: in TestYahooSuccess
    return_value=_make_sample_items("yahoo"),
E   NameError: name '_make_sample_items' is not defined
=========================== short test summary info ===========================
ERROR tests/test_external_trends_service.py - NameError: name '_make_sample_items' is not defined
"""


def _pytest_assertion_failure_output() -> str:
    return """
=================================== FAILURES ===================================
__________________________________ test_value __________________________________
tests/test_app.py:3: in test_value
    assert value() == 2
E   AssertionError: assert 1 == 2
=========================== short test summary info ===========================
FAILED tests/test_app.py::test_value - AssertionError: assert 1 == 2
"""


def _write_project_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
