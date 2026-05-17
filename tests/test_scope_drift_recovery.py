"""Deterministic scope-drift recovery detector.

Pin the contract that when FailureEvent.affected_paths references real
files outside the original card's writable scope, the registry routes
the failure through the deterministic scope-drift detector instead of
falling through to the synthesizer fallback.
"""

from __future__ import annotations

from pathlib import Path

from kodawari.autopilot.recovery.failure_event import FailureEvent
from kodawari.autopilot.recovery.registry import RecoveryContext, route_deterministic_recovery
from kodawari.autopilot.recovery.stall_recovery import (
    SCOPE_DRIFT_RECOVERY_ACTION,
    build_scope_drift_recovery,
)


def _seed_workspace(tmp_path: Path, files: dict[str, str]) -> Path:
    for relpath, content in files.items():
        target = tmp_path / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return tmp_path


def test_scope_drift_detected_for_real_out_of_scope_file(tmp_path: Path) -> None:
    project_root = _seed_workspace(tmp_path, {"src/scope_a.py": "x = 1\n", "src/scope_b.py": "y = 2\n"})
    card = {"files_to_change": ["src/scope_a.py"], "task_id": "T1"}
    result = build_scope_drift_recovery(
        project_root=project_root,
        original_card=card,
        task_id="T1",
        must_fix=["update scope_b.py"],
        affected_paths=["src/scope_b.py"],
    )
    assert result is not None
    decision, recovery_card = result
    assert decision["action"] == SCOPE_DRIFT_RECOVERY_ACTION
    assert "src/scope_b.py" in decision["requested_files"]
    assert "src/scope_b.py" in recovery_card["files_to_change"]
    assert "src/scope_a.py" in recovery_card["files_to_change"]


def test_scope_drift_ignores_in_scope_paths(tmp_path: Path) -> None:
    project_root = _seed_workspace(tmp_path, {"src/scope_a.py": "x = 1\n"})
    card = {"files_to_change": ["src/scope_a.py"], "task_id": "T1"}
    result = build_scope_drift_recovery(
        project_root=project_root,
        original_card=card,
        task_id="T1",
        must_fix=["fix scope_a"],
        affected_paths=["src/scope_a.py"],
    )
    assert result is None


def test_scope_drift_ignores_verification_only_empty_scope(tmp_path: Path) -> None:
    project_root = _seed_workspace(tmp_path, {"tests/test_contract.py": "def test_ok():\n    assert True\n"})
    card = {
        "task_id": "V001",
        "files_to_change": [],
        "new_files": [],
        "related_existing_tests": ["tests/test_contract.py"],
        "verify_cmd": "python -m pytest tests/test_contract.py -q",
        "execution_constraints": {
            "verification_only_noop": True,
            "executor_must_not_edit": True,
        },
    }
    result = build_scope_drift_recovery(
        project_root=project_root,
        original_card=card,
        task_id="V001",
        must_fix=["verification-only task passed with no product edits"],
        affected_paths=["tests/test_contract.py"],
    )
    assert result is None


def test_scope_drift_ignores_nonexistent_paths(tmp_path: Path) -> None:
    project_root = _seed_workspace(tmp_path, {"src/scope_a.py": "x = 1\n"})
    card = {"files_to_change": ["src/scope_a.py"], "task_id": "T1"}
    result = build_scope_drift_recovery(
        project_root=project_root,
        original_card=card,
        task_id="T1",
        must_fix=["touch ghost"],
        affected_paths=["src/does_not_exist.py"],
    )
    assert result is None


def test_scope_drift_rejects_paths_outside_project_root(tmp_path: Path) -> None:
    project_root = _seed_workspace(tmp_path, {"src/scope_a.py": "x = 1\n"})
    card = {"files_to_change": ["src/scope_a.py"], "task_id": "T1"}
    result = build_scope_drift_recovery(
        project_root=project_root,
        original_card=card,
        task_id="T1",
        must_fix=["escape"],
        affected_paths=["../../etc/passwd", "/abs/path"],
    )
    assert result is None


def test_registry_routes_scope_drift_before_tool_limit(tmp_path: Path) -> None:
    project_root = _seed_workspace(
        tmp_path,
        {"src/scope_a.py": "x = 1\n", "src/scope_b.py": "y = 2\n"},
    )
    card = {"files_to_change": ["src/scope_a.py"], "task_id": "T1"}
    event = FailureEvent(
        phase="implement",
        error_code="EXECUTOR_STALLED_NO_WRITE_PROGRESS",
        affected_paths=["src/scope_b.py"],
    )
    match = route_deterministic_recovery(
        RecoveryContext(
            project_root=project_root,
            original_card=card,
            task_id="T1",
            must_fix=["update scope_b"],
            event=event,
        )
    )
    assert match is not None
    assert match.name == "scope_drift"
    assert match.priority == 15


def test_registry_falls_through_to_other_detectors_when_no_drift(tmp_path: Path) -> None:
    project_root = _seed_workspace(tmp_path, {"src/scope_a.py": "x = 1\n"})
    card = {"files_to_change": ["src/scope_a.py"], "task_id": "T1"}
    event = FailureEvent(
        phase="implement",
        error_code="GENERIC",
        affected_paths=[],
    )
    match = route_deterministic_recovery(
        RecoveryContext(
            project_root=project_root,
            original_card=card,
            task_id="T1",
            must_fix=["something"],
            event=event,
        )
    )
    # No drift, no other deterministic detector matches generic event -> None
    assert match is None or match.name != "scope_drift"
