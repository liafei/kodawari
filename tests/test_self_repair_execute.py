"""Tests for Phase 3 self-repair execution gate."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kodawari.cli.evidence.self_repair_execute import (
    DEFAULT_CONFIDENCE_MIN,
    ENV_CONFIDENCE_MIN,
    ENV_DEPTH,
    ENV_ENABLED,
    ENV_SDK_ROOT,
    SELF_REPAIR_EXECUTION_FILENAME,
    execute_self_repair_proposal,
    write_execution_record,
)


def _ready_proposal(sdk_root: Path, *, confidence: float = 0.95) -> dict[str, Any]:
    target = sdk_root / "src" / "kodawari" / "autopilot" / "execution" / "tool_use_stall.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# existing target\n", encoding="utf-8")
    return {
        "schema_version": "workflow.self_repair.v1",
        "status": "ready",
        "kodawari_root": str(sdk_root.resolve()),
        "root_cause": {
            "code": "executor_fragmented_read_loop",
            "confidence": confidence,
            "summary": "fragmented reads",
        },
        "repair_task": {
            "title": "fix",
            "task_direction": "Patch read discipline.",
            "target_files": ["src/kodawari/autopilot/execution/tool_use_stall.py"],
            "suggested_tests": ["pytest -q tests/test_read_discipline.py"],
            "acceptance": ["fragmented reads block before budget pressure"],
        },
        "safety": {"auto_apply_allowed": False, "rejected_target_files": []},
    }


def _write_proposal(planning: Path, payload: dict[str, Any]) -> Path:
    planning.mkdir(parents=True, exist_ok=True)
    path = planning / ".workflow_self_repair.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _all_gates_env(sdk_root: Path, monkeypatch) -> None:
    monkeypatch.setenv(ENV_ENABLED, "1")
    monkeypatch.setenv(ENV_SDK_ROOT, str(sdk_root.resolve()))
    monkeypatch.delenv(ENV_DEPTH, raising=False)


def test_execute_dry_run_passes_all_seven_gates(tmp_path: Path, monkeypatch) -> None:
    sdk_root = tmp_path / "sdk"
    sdk_root.mkdir()
    planning = tmp_path / "planning" / "f1"
    proposal_path = _write_proposal(planning, _ready_proposal(sdk_root))
    _all_gates_env(sdk_root, monkeypatch)

    record = execute_self_repair_proposal(
        proposal_path=proposal_path,
        sdk_root=sdk_root,
        dry_run=True,
    )

    assert record["status"] == "dry_run"
    assert record["reason"] == "all_gates_passed"
    gate_names = [g["name"] for g in record["gates"]]
    assert gate_names == [
        "env_gate",
        "depth_gate",
        "status_gate",
        "confidence_gate",
        "target_files_gate",
        "target_files_exist_gate",
        "sdk_root_gate",
    ]
    assert all(g["passed"] for g in record["gates"])


def test_execute_blocked_when_env_gate_off(tmp_path: Path, monkeypatch) -> None:
    sdk_root = tmp_path / "sdk"
    sdk_root.mkdir()
    planning = tmp_path / "planning" / "f-env"
    proposal_path = _write_proposal(planning, _ready_proposal(sdk_root))
    monkeypatch.delenv(ENV_ENABLED, raising=False)
    monkeypatch.setenv(ENV_SDK_ROOT, str(sdk_root.resolve()))

    record = execute_self_repair_proposal(proposal_path=proposal_path, sdk_root=sdk_root, dry_run=True)

    assert record["status"] == "skipped"
    assert "env_gate" in record["failed_gates"]


def test_execute_blocked_when_depth_already_one(tmp_path: Path, monkeypatch) -> None:
    """A self-repair run cannot itself spawn another self-repair. Depth ≥ 1 → refuse."""

    sdk_root = tmp_path / "sdk"
    sdk_root.mkdir()
    planning = tmp_path / "planning" / "f-depth"
    proposal_path = _write_proposal(planning, _ready_proposal(sdk_root))
    _all_gates_env(sdk_root, monkeypatch)
    monkeypatch.setenv(ENV_DEPTH, "1")

    record = execute_self_repair_proposal(proposal_path=proposal_path, sdk_root=sdk_root, dry_run=True)

    assert record["status"] == "skipped"
    assert "depth_gate" in record["failed_gates"]


def test_execute_blocked_when_status_not_ready(tmp_path: Path, monkeypatch) -> None:
    sdk_root = tmp_path / "sdk"
    sdk_root.mkdir()
    planning = tmp_path / "planning" / "f-triage"
    proposal = _ready_proposal(sdk_root)
    proposal["status"] = "triage_required"
    proposal_path = _write_proposal(planning, proposal)
    _all_gates_env(sdk_root, monkeypatch)

    record = execute_self_repair_proposal(proposal_path=proposal_path, sdk_root=sdk_root, dry_run=True)

    assert record["status"] == "skipped"
    assert "status_gate" in record["failed_gates"]


def test_execute_blocked_when_confidence_below_threshold(tmp_path: Path, monkeypatch) -> None:
    sdk_root = tmp_path / "sdk"
    sdk_root.mkdir()
    planning = tmp_path / "planning" / "f-conf"
    proposal_path = _write_proposal(planning, _ready_proposal(sdk_root, confidence=0.5))
    _all_gates_env(sdk_root, monkeypatch)

    record = execute_self_repair_proposal(proposal_path=proposal_path, sdk_root=sdk_root, dry_run=True)

    assert record["status"] == "skipped"
    assert "confidence_gate" in record["failed_gates"]
    confidence_gate = next(g for g in record["gates"] if g["name"] == "confidence_gate")
    assert confidence_gate["detail"]["threshold"] == DEFAULT_CONFIDENCE_MIN


def test_execute_confidence_threshold_overridable_via_env(tmp_path: Path, monkeypatch) -> None:
    sdk_root = tmp_path / "sdk"
    sdk_root.mkdir()
    planning = tmp_path / "planning" / "f-conf-env"
    proposal_path = _write_proposal(planning, _ready_proposal(sdk_root, confidence=0.5))
    _all_gates_env(sdk_root, monkeypatch)
    monkeypatch.setenv(ENV_CONFIDENCE_MIN, "0.4")

    record = execute_self_repair_proposal(proposal_path=proposal_path, sdk_root=sdk_root, dry_run=True)

    assert record["status"] == "dry_run"


def test_execute_blocked_when_target_files_have_path_traversal(tmp_path: Path, monkeypatch) -> None:
    sdk_root = tmp_path / "sdk"
    sdk_root.mkdir()
    planning = tmp_path / "planning" / "f-path"
    proposal = _ready_proposal(sdk_root)
    proposal["repair_task"]["target_files"] = [
        "src/kodawari/x.py",
        "../../etc/passwd",  # path traversal — must be rejected
    ]
    proposal_path = _write_proposal(planning, proposal)
    _all_gates_env(sdk_root, monkeypatch)

    record = execute_self_repair_proposal(proposal_path=proposal_path, sdk_root=sdk_root, dry_run=True)

    assert record["status"] == "skipped"
    assert "target_files_gate" in record["failed_gates"]
    gate = next(g for g in record["gates"] if g["name"] == "target_files_gate")
    assert any(item["path"] == "../../etc/passwd" for item in gate["detail"]["rejected"])


def test_execute_blocked_when_target_file_missing_from_sdk_worktree(tmp_path: Path, monkeypatch) -> None:
    sdk_root = tmp_path / "sdk"
    sdk_root.mkdir()
    planning = tmp_path / "planning" / "f-missing"
    proposal = _ready_proposal(sdk_root)
    proposal["repair_task"]["target_files"] = ["src/kodawari/autopilot/planning/deterministic_repair.py"]
    proposal_path = _write_proposal(planning, proposal)
    _all_gates_env(sdk_root, monkeypatch)

    record = execute_self_repair_proposal(proposal_path=proposal_path, sdk_root=sdk_root, dry_run=True)

    assert record["status"] == "skipped"
    assert "target_files_exist_gate" in record["failed_gates"]
    gate = next(g for g in record["gates"] if g["name"] == "target_files_exist_gate")
    assert gate["detail"]["missing"] == ["src/kodawari/autopilot/planning/deterministic_repair.py"]


def test_execute_allows_missing_target_when_declared_new_file(tmp_path: Path, monkeypatch) -> None:
    sdk_root = tmp_path / "sdk"
    sdk_root.mkdir()
    planning = tmp_path / "planning" / "f-new"
    proposal = _ready_proposal(sdk_root)
    proposal["repair_task"]["target_files"] = ["src/kodawari/new_module.py"]
    proposal["repair_task"]["new_files"] = ["src/kodawari/new_module.py"]
    proposal_path = _write_proposal(planning, proposal)
    _all_gates_env(sdk_root, monkeypatch)

    record = execute_self_repair_proposal(proposal_path=proposal_path, sdk_root=sdk_root, dry_run=True)

    assert record["status"] == "dry_run"
    gate = next(g for g in record["gates"] if g["name"] == "target_files_exist_gate")
    assert gate["passed"] is True
    assert gate["detail"]["allowed_new_files"] == ["src/kodawari/new_module.py"]


def test_spawn_prefers_repo_local_wrapper_and_strips_parent_pythonpath(tmp_path: Path, monkeypatch) -> None:
    from kodawari.cli.evidence import self_repair_execute as mod

    sdk_root = tmp_path / "sdk"
    wrapper = sdk_root / "scripts" / "kodawari.ps1"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text("# repo local wrapper\n", encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "other-sdk" / "src"))
    for key in mod.SPAWN_DEFAULT_ENV:
        monkeypatch.delenv(key, raising=False)

    command = mod._build_kodawari_command(
        kodawari=None,
        sdk_root=sdk_root,
        feature="meta-repair-test",
        task_direction="repair",
    )
    env = mod._build_spawn_env(env_overrides={})

    assert str(wrapper) in command
    assert "PYTHONPATH" not in env
    assert env["WORKFLOW_PLANNER_TIMEOUT"] == "600"
    assert env["WORKFLOW_PLAN_REVIEWER_TIMEOUT"] == "300"
    assert env["WORKFLOW_PLANNING_MAX_ROUNDS"] == "2"


def test_spawn_env_overrides_self_repair_defaults(monkeypatch) -> None:
    from kodawari.cli.evidence import self_repair_execute as mod

    for key in mod.SPAWN_DEFAULT_ENV:
        monkeypatch.delenv(key, raising=False)

    env = mod._build_spawn_env(env_overrides={"WORKFLOW_PLAN_REVIEWER_TIMEOUT": "45"})

    assert env["WORKFLOW_PLAN_REVIEWER_TIMEOUT"] == "45"
    assert env["WORKFLOW_PLANNER_TIMEOUT"] == "600"


def test_execute_blocked_when_sdk_root_env_missing(tmp_path: Path, monkeypatch) -> None:
    sdk_root = tmp_path / "sdk"
    sdk_root.mkdir()
    planning = tmp_path / "planning" / "f-sdkroot"
    proposal_path = _write_proposal(planning, _ready_proposal(sdk_root))
    monkeypatch.setenv(ENV_ENABLED, "1")
    monkeypatch.delenv(ENV_SDK_ROOT, raising=False)

    record = execute_self_repair_proposal(proposal_path=proposal_path, sdk_root=sdk_root, dry_run=True)

    assert record["status"] == "skipped"
    assert "sdk_root_gate" in record["failed_gates"]


def test_execute_blocked_when_sdk_root_env_mismatches_proposal(tmp_path: Path, monkeypatch) -> None:
    sdk_root = tmp_path / "sdk-real"
    sdk_root.mkdir()
    other_root = tmp_path / "sdk-other"
    other_root.mkdir()
    planning = tmp_path / "planning" / "f-mismatch"
    proposal_path = _write_proposal(planning, _ready_proposal(sdk_root))
    monkeypatch.setenv(ENV_ENABLED, "1")
    monkeypatch.setenv(ENV_SDK_ROOT, str(other_root.resolve()))

    record = execute_self_repair_proposal(proposal_path=proposal_path, sdk_root=sdk_root, dry_run=True)

    assert record["status"] == "skipped"
    assert "sdk_root_gate" in record["failed_gates"]


def test_execute_spawn_passes_through_a_fake_kodawari(tmp_path: Path, monkeypatch) -> None:
    """Real spawn end-to-end: use a fake ``kodawari`` that just echoes
    the args + writes a sentinel file, then verify the spawn record looks
    right and depth was bumped."""

    sdk_root = tmp_path / "sdk"
    sdk_root.mkdir()
    planning = tmp_path / "planning" / "f-spawn"
    proposal_path = _write_proposal(planning, _ready_proposal(sdk_root))
    _all_gates_env(sdk_root, monkeypatch)

    # Fake kodawari: a Python script that records the env and exits 0.
    fake_ctl = tmp_path / "fake_kodawari.py"
    spawn_log = tmp_path / "spawn.log"
    fake_ctl.write_text(
        f'import os, sys, json\n'
        f'with open(r"{spawn_log}", "w", encoding="utf-8") as f:\n'
        f'    json.dump({{"argv": sys.argv, "depth": os.environ.get("{ENV_DEPTH}", "0"), "enabled": os.environ.get("{ENV_ENABLED}", "")}}, f)\n'
        f'sys.exit(0)\n',
        encoding="utf-8",
    )

    import sys
    record = execute_self_repair_proposal(
        proposal_path=proposal_path,
        sdk_root=sdk_root,
        kodawari=sys.executable,
        spawn_env={},
    )

    # Inject the script into the command via a wrapper: we'll override
    # ``kodawari`` to be ``python <fake_ctl>``. Easiest: rerun with
    # an explicit list. The prior call already exercised the spawn path
    # but with kodawari=sys.executable, the args won't include our
    # script. Patch the spawn directly:
    from kodawari.cli.evidence import self_repair_execute as mod

    original = mod._build_kodawari_command

    def patched(*, kodawari, sdk_root, feature, task_direction):
        return [sys.executable, str(fake_ctl)] + original(
            kodawari=kodawari, sdk_root=sdk_root, feature=feature, task_direction=task_direction
        )[1:]

    monkeypatch.setattr(mod, "_build_kodawari_command", patched)

    record = execute_self_repair_proposal(proposal_path=proposal_path, sdk_root=sdk_root)

    assert record["status"] == "executed"
    assert record["reason"] == "spawn_ok"
    assert record["spawn"]["status"] == "ok"
    assert record["spawn"]["exit_code"] == 0
    assert record["spawn"]["sdk_root"] == str(sdk_root.resolve())

    # Verify the spawned process saw bumped depth and the env flag.
    log = json.loads(spawn_log.read_text(encoding="utf-8"))
    assert log["depth"] == "1", "spawn must bump WORKFLOW_SELF_REPAIR_DEPTH"
    assert log["enabled"] == "1"
    assert "autopilot" in log["argv"]
    assert "--project-root" in log["argv"]


def test_write_execution_record_round_trips(tmp_path: Path, monkeypatch) -> None:
    sdk_root = tmp_path / "sdk"
    sdk_root.mkdir()
    planning = tmp_path / "planning" / "f-write"
    proposal_path = _write_proposal(planning, _ready_proposal(sdk_root))
    _all_gates_env(sdk_root, monkeypatch)

    record = execute_self_repair_proposal(proposal_path=proposal_path, sdk_root=sdk_root, dry_run=True)
    artifact = write_execution_record(planning, record)

    assert artifact.name == SELF_REPAIR_EXECUTION_FILENAME
    loaded = json.loads(artifact.read_text(encoding="utf-8"))
    assert loaded["status"] == "dry_run"
    assert loaded["schema_version"] == "workflow.self_repair.execution.v1"
