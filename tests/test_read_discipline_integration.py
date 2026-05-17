"""Real-world reproduction: 17 sliding-window reads of a 9KB file.

Replicates the failure mode observed in the prd11-mimo-social-aggregation
rerun: the model chops a 9KB file into 300-byte windows and burns the
budget without writing. With the read-discipline fix the executor must
hard-stop on EXECUTOR_STALLED_FRAGMENTED_READS before the legacy 17
iterations run their course.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kodawari.autopilot.execution import execution_openai_tool_use as runner
from kodawari.autopilot.execution.execution_backend import ExecutionBackendConfig


def _config(root: Path, planning_dir: Path, monkeypatch: pytest.MonkeyPatch) -> ExecutionBackendConfig:
    monkeypatch.setenv("WORKFLOW_FAKE_OPENAI_KEY", "tp-test")
    return ExecutionBackendConfig(
        backend="openai_tool_use",
        command="",
        project_root=root,
        planning_dir=planning_dir,
        feature="feature",
        model="mimo-v2.5-pro",
        base_url="http://localhost/v1",
        api_key_env="WORKFLOW_FAKE_OPENAI_KEY",
        api_format="openai_chat",
        transport_name="mimo_api",
        execution_protocol="exact_str_replace_v1",
        runtime_caps={
            "max_tool_iterations": 30,
            "max_token_budget": 200000,
            "max_same_tool_calls_per_path": 999,  # do not let the older guard fire first
            "max_tool_calls_per_response": 4,
            "max_wall_clock_seconds": 120,
            "max_no_progress_iterations": 50,
            "max_verify_retries": 2,
            "max_no_write_iterations": 50,  # let read discipline trigger before no-write
            "max_no_write_iterations_with_observation": 50,
            "max_redundant_read_count": 999,  # exact-match guard disabled so we test the new one
            "max_read_windows_per_path": 8,
        },
    )


def _request(root: Path, planning_dir: Path) -> dict[str, Any]:
    return {
        "schema_version": "execution.request.v1",
        "feature": "feature",
        "task": "T001",
        "backend": "openai_tool_use",
        "project_root": str(root),
        "planning_dir": str(planning_dir),
        "task_id": "T001",
        "requested_action": "Edit social_aggregation_service.py.",
        "review_round": 0,
        "attempt": 1,
        "files_to_change": ["social_aggregation_service.py"],
        "invariants": [],
        "task_card": {},
        "task_scope": "unit",
        "task_requirements": "Edit social_aggregation_service.py.",
        "verify_cmd": "",
        "execution_protocol": "exact_str_replace_v1",
    }


def _tool_response(name: str, args: dict[str, Any], idx: int) -> dict[str, Any]:
    return {
        "model": "mimo-v2.5-pro",
        "usage": {"total_tokens": 1},
        "choices": [
            {
                "message": {
                    "tool_calls": [
                        {
                            "id": f"call_{idx}",
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)},
                        }
                    ]
                }
            }
        ],
    }


def test_sliding_window_read_loop_is_caught_by_read_discipline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    planning_dir = root / ".claude" / "workflow" / "run"
    planning_dir.mkdir(parents=True)
    # 9KB target file — recreates the prd11-mimo-social-aggregation shape.
    target = root / "social_aggregation_service.py"
    target.write_text(("# line\n" * 1500), encoding="utf-8")  # ~9KB

    # Simulate 20 sequential 300-byte sliding-window reads. Without the
    # read-discipline fix this would proceed for many more iterations as each
    # new window earns observation_progress and resets the no-write timer.
    fragmented_calls = [
        ("read_file_partial", {"path": "social_aggregation_service.py", "offset": i * 300, "limit": 300})
        for i in range(20)
    ]
    index = {"value": 0}

    def _fake_post(**_kwargs: Any) -> dict[str, Any]:
        i = index["value"]
        index["value"] += 1
        if i >= len(fragmented_calls):
            raise AssertionError(
                "executor took more iterations than the legacy 17-round failure — "
                "read discipline did not hard-stop early"
            )
        name, args = fragmented_calls[i]
        return _tool_response(name, args, i)

    monkeypatch.setattr(runner, "_post_chat", _fake_post)

    result = runner.materialize_openai_tool_use_result(
        config=_config(root, planning_dir, monkeypatch),
        request_path=planning_dir / ".execution_request.json",
        request_payload=_request(root, planning_dir),
    )

    assert result["status"] == "BLOCKED"
    error_code = str(result.get("error_code") or "")
    # The read-discipline guard should fire well before iteration 20.
    assert error_code == "EXECUTOR_STALLED_FRAGMENTED_READS", result
    # Stall report records the per-path window count.
    stall_report_path = planning_dir / runner.STALL_REPORT_FILENAME
    assert stall_report_path.exists()
    stall_report = json.loads(stall_report_path.read_text(encoding="utf-8"))
    assert stall_report["error_code"] == "EXECUTOR_STALLED_FRAGMENTED_READS"
    assert stall_report["fragmented_read_paths"]["social_aggregation_service.py"] > 8
    # We must have stopped strictly before the legacy 17-round failure point.
    assert index["value"] <= 12, (
        f"executor took {index['value']} iterations to halt; "
        "read discipline should fire by iteration 9-10"
    )
