from __future__ import annotations

from pathlib import Path

from kodawari.autopilot.execution import execution_artifacts
from kodawari.autopilot.review.review_bridge import run_post_execution_qa


def _write_execution_result(
    planning_dir: Path,
    *,
    status: str = "PASS",
    changed_files: list[str] | None = None,
    verification_only_noop: bool = False,
) -> None:
    payload = execution_artifacts.build_execution_result(
        feature="demo",
        task="TVERIFY: close verified task",
        backend="openai_tool_use",
        status=status,
        changed_files=changed_files or [],
        summary="scoped execution completed",
    )
    if verification_only_noop:
        payload["verification_only_noop"] = True
        payload["verify_summary"] = {
            "status": "PASS",
            "passed": True,
            "command_executed": True,
            "returncode": 0,
            "verify_cmd": "python -m pytest tests/test_existing.py -q",
            "summary": "1 passed",
        }
    execution_artifacts.write_execution_result(planning_dir / ".execution_result.json", payload)


def test_post_execution_qa_allows_verified_noop_without_changed_files(tmp_path: Path) -> None:
    planning_dir = tmp_path / "planning" / "demo"
    planning_dir.mkdir(parents=True)
    _write_execution_result(planning_dir, verification_only_noop=True)

    payload = run_post_execution_qa(
        "demo",
        artifacts=[],
        context={
            "planning_dir": str(planning_dir),
            "verify_cmd": "python -m pytest tests/test_existing.py -q",
            "task_card": {
                "files_to_change": [],
                "verify_cmd": "python -m pytest tests/test_existing.py -q",
                "execution_constraints": {
                    "verification_only_noop": True,
                    "executor_must_not_edit": True,
                },
            },
        },
    )

    assert payload["status"] == "PASS"
    assert payload["checks"]["changed_files_present"] is False
    assert payload["checks"]["changed_files_required"] is False
    assert payload["checks"]["verification_only_noop"] is True
    assert payload["checks"]["verify_evidence_available"] is True


def test_post_execution_qa_still_rejects_ordinary_no_change_task(tmp_path: Path) -> None:
    planning_dir = tmp_path / "planning" / "demo"
    planning_dir.mkdir(parents=True)
    _write_execution_result(planning_dir)

    payload = run_post_execution_qa(
        "demo",
        artifacts=[],
        context={
            "planning_dir": str(planning_dir),
            "verify_cmd": "python -m pytest tests/test_existing.py -q",
            "task_card": {"files_to_change": ["app/main.py"]},
        },
    )

    assert payload["status"] == "FAIL"
    assert payload["checks"]["changed_files_required"] is True
    assert payload["checks"]["verification_only_noop"] is False
