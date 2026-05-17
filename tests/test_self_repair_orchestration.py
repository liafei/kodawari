"""Test the autopilot finalization hook that ties Phase 1+2+3 end-to-end.

When an autopilot run finalizes with ``run_truth`` indicating a workflow
runtime failure that has a known classifier, ``_maybe_write_self_repair_artifacts``
writes the proposal artifact + markdown. Phase 3 auto-execute is a
separate opt-in (``WORKFLOW_SELF_REPAIR_AUTO_EXECUTE=1``) so real failed
runs can collect diagnostics without spawning a kodawari repair task.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pytest

from kodawari.cli.contract.autopilot_contract_bridge import AutopilotPlanningBridgeError
from kodawari.cli.evidence.self_repair import SELF_REPAIR_FILENAME
from kodawari.cli.evidence.self_repair_execute import (
    ENV_AUTO_EXECUTE,
    ENV_DEPTH,
    ENV_ENABLED,
    ENV_SDK_ROOT,
    SELF_REPAIR_EXECUTION_FILENAME,
)
from kodawari.cli.runtime.autopilot_cmd import (
    _emit_planning_bridge_error,
    _maybe_auto_execute_self_repair,
    _maybe_write_self_repair_artifacts,
)


def _run_truth_blocked(planning_dir: Path) -> dict[str, Any]:
    return {
        "schema_version": "run.truth.v1",
        "feature": planning_dir.name,
        "final_status": "BLOCKED",
        "run_reason": "RECOVERY_SYNTHESIZER_TIMEOUT",
        "blocking_reason": "",
    }


def _write_failure_evidence(planning_dir: Path) -> None:
    planning_dir.mkdir(parents=True, exist_ok=True)
    (planning_dir / ".execution_failure_snapshot.json").write_text(
        json.dumps({"error_code": "RECOVERY_SYNTHESIZER_TIMEOUT"}), encoding="utf-8"
    )


def _write_recovery_timeout_targets(sdk_root: Path) -> None:
    for rel in (
        "src/kodawari/autopilot/engine/engine_recovery_mixin.py",
        "src/kodawari/autopilot/execution/local_adapter_recovery.py",
        "tests/test_stall_recovery.py",
    ):
        path = sdk_root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# existing target\n", encoding="utf-8")


def test_artifacts_hook_emits_proposal_when_classifier_matches(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv(ENV_ENABLED, raising=False)
    project_root = tmp_path
    planning_dir = tmp_path / "planning" / "feat-block"
    _write_failure_evidence(planning_dir)
    run_truth = _run_truth_blocked(planning_dir)

    summary = _maybe_write_self_repair_artifacts(
        project_root=project_root,
        planning_dir=planning_dir,
        run_truth=run_truth,
    )

    assert summary["status"] == "ready"
    assert summary["root_cause"] == "recovery_synthesizer_timeout"
    assert summary["artifact"] == SELF_REPAIR_FILENAME
    # Auto-execution opt-out (env not set) — no auto_execution key.
    assert "auto_execution" not in summary
    assert (planning_dir / SELF_REPAIR_FILENAME).exists()


def test_planning_bridge_error_finalizes_run_truth_and_self_repair(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.delenv(ENV_ENABLED, raising=False)
    planning_dir = tmp_path / "planning" / "feat-plan"
    planning_dir.mkdir(parents=True)
    (planning_dir / ".planning_failure.json").write_text(
        json.dumps(
            {
                "error_code": "critical_or_blocking_present",
                "reason": "escalation_required",
                "round_count": 1,
                "rounds": [
                    {
                        "round_number": 1,
                        "blocking_findings_count": 1,
                        "blocking_findings": [
                            {
                                "severity": "blocking",
                                "category": "structure",
                                "description": "planner returned an invalid evidence resolution",
                            }
                        ],
                    }
                ],
                "escalation": {"gate_reason": "critical_or_blocking_present"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (planning_dir / "PLANNING_CONVERSATION.json").write_text(
        json.dumps(
            {
                "schema_version": "planning.conversation.v1",
                "status": "escalation_required",
                "round_count": 1,
                "rounds": [],
                "final_plan": {
                    "tasks": [],
                    "change_log": [{"target": "T1", "reason": "change_log evidence resolution stayed ambiguous"}],
                },
                "escalation": {"gate_reason": "critical_or_blocking_present"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    rc = _emit_planning_bridge_error(
        args=argparse.Namespace(project_root=str(tmp_path), feature="feat-plan"),
        error=AutopilotPlanningBridgeError(
            error_code="planning_escalation_required",
            message="planning stopped before executable artifacts",
            remediation=["revise the plan"],
            details={"planning_status": "escalation_required"},
        ),
    )

    payload = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert payload["planning_finalization"]["status"] == "finalized"
    assert payload["planning_finalization"]["run_truth"] == ".run_truth.json"
    assert (planning_dir / ".run_truth.json").exists()
    assert (planning_dir / SELF_REPAIR_FILENAME).exists()


def test_fresh_planning_exception_bridge_error_finalizes_run_truth(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.delenv(ENV_ENABLED, raising=False)
    planning_dir = tmp_path / "planning" / "feat-fresh-exception"
    planning_dir.mkdir(parents=True)
    (planning_dir / ".planning_failure.json").write_text(
        json.dumps(
            {
                "schema_version": "planning.progress.v1",
                "status": "error",
                "reason": "fresh_planning_exception",
                "error_code": "fresh_planning_exception",
                "error_type": "OSError",
                "message": "[Errno 22] Invalid argument",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    rc = _emit_planning_bridge_error(
        args=argparse.Namespace(project_root=str(tmp_path), feature="feat-fresh-exception"),
        error=AutopilotPlanningBridgeError(
            error_code="fresh_planning_exception",
            message="fresh planning failed",
            remediation=["inspect planning failure"],
            details={"planning_status": "error", "reason": "fresh_planning_exception"},
        ),
    )

    payload = json.loads(capsys.readouterr().out)
    truth = json.loads((planning_dir / ".run_truth.json").read_text(encoding="utf-8"))

    assert rc == 2
    assert payload["planning_finalization"]["run_truth"] == ".run_truth.json"
    assert truth["final_status"] == "BLOCKED"
    assert truth["run_reason"] == "fresh_planning_exception"


def test_artifacts_hook_skips_proposal_when_run_succeeded(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv(ENV_ENABLED, raising=False)
    project_root = tmp_path
    planning_dir = tmp_path / "planning" / "feat-pass"
    planning_dir.mkdir(parents=True, exist_ok=True)
    run_truth = {
        "schema_version": "run.truth.v1",
        "feature": planning_dir.name,
        "final_status": "OK",
        "run_reason": "PROCEED_TO_GATE",
    }

    summary = _maybe_write_self_repair_artifacts(
        project_root=project_root,
        planning_dir=planning_dir,
        run_truth=run_truth,
    )

    assert summary == {}
    assert not (planning_dir / SELF_REPAIR_FILENAME).exists()


def test_auto_execute_hook_skipped_without_env(tmp_path: Path, monkeypatch) -> None:
    """Without ``WORKFLOW_SELF_REPAIR_AUTO_EXECUTE=1`` the auto-execute hook returns
    empty — hook is a no-op so existing pipelines see zero behavior change."""

    monkeypatch.delenv(ENV_ENABLED, raising=False)
    monkeypatch.delenv(ENV_AUTO_EXECUTE, raising=False)
    planning = tmp_path / "planning" / "f"
    planning.mkdir(parents=True)
    (planning / SELF_REPAIR_FILENAME).write_text("{}", encoding="utf-8")

    result = _maybe_auto_execute_self_repair(
        planning_dir=planning,
        proposal_path=planning / SELF_REPAIR_FILENAME,
    )
    assert result == {}


def test_auto_execute_hook_diagnostics_only_with_self_repair_env(tmp_path: Path, monkeypatch) -> None:
    """``WORKFLOW_SELF_REPAIR=1`` writes diagnostics only; automatic spawn
    stays off unless the narrower auto-execute flag is also present."""

    monkeypatch.setenv(ENV_ENABLED, "1")
    monkeypatch.delenv(ENV_AUTO_EXECUTE, raising=False)
    planning = tmp_path / "planning" / "fdiagnostic"
    planning.mkdir(parents=True)
    (planning / SELF_REPAIR_FILENAME).write_text("{}", encoding="utf-8")

    result = _maybe_auto_execute_self_repair(
        planning_dir=planning,
        proposal_path=planning / SELF_REPAIR_FILENAME,
    )
    assert result == {}


def test_auto_execute_hook_requires_diagnostic_env_too(tmp_path: Path, monkeypatch) -> None:
    """The auto-execute flag is not enough on its own; the underlying
    Phase-3 execution gate still requires diagnostics to be enabled."""

    monkeypatch.delenv(ENV_ENABLED, raising=False)
    monkeypatch.setenv(ENV_AUTO_EXECUTE, "1")
    planning = tmp_path / "planning" / "fmissingdiag"
    planning.mkdir(parents=True)
    (planning / SELF_REPAIR_FILENAME).write_text("{}", encoding="utf-8")

    result = _maybe_auto_execute_self_repair(
        planning_dir=planning,
        proposal_path=planning / SELF_REPAIR_FILENAME,
    )
    assert result["status"] == "skipped"
    assert result["reason"] == "self_repair_diagnostics_disabled"


def test_auto_execute_hook_skipped_when_already_inside_self_repair_run(tmp_path: Path, monkeypatch) -> None:
    """A spawn from a Phase-3 autopilot must not chain another self-repair.
    The depth env is the marker — when ≥1, the auto-execute hook returns
    a structured ``skipped`` record without touching the gate machinery."""

    monkeypatch.setenv(ENV_ENABLED, "1")
    monkeypatch.setenv(ENV_AUTO_EXECUTE, "1")
    monkeypatch.setenv(ENV_DEPTH, "1")
    planning = tmp_path / "planning" / "fdepth"
    planning.mkdir(parents=True)
    (planning / SELF_REPAIR_FILENAME).write_text("{}", encoding="utf-8")

    result = _maybe_auto_execute_self_repair(
        planning_dir=planning,
        proposal_path=planning / SELF_REPAIR_FILENAME,
    )
    assert result["status"] == "skipped"
    assert result["reason"] == "already_inside_self_repair_run"


def test_auto_execute_chains_into_phase3_when_env_set(tmp_path: Path, monkeypatch) -> None:
    """End-to-end plumbing: artifacts hook → Phase-3 auto-execute. The
    real subprocess spawn is mocked out to keep the test bounded; what
    we verify is that all seven gates run, the spawn DOES get called when
    they pass, and the execution record is persisted alongside the
    proposal so post-mortem operators can audit the chain."""

    monkeypatch.setenv(ENV_ENABLED, "1")
    monkeypatch.setenv(ENV_AUTO_EXECUTE, "1")
    fake_sdk_root = tmp_path / "fake-sdk"
    fake_sdk_root.mkdir()
    _write_recovery_timeout_targets(fake_sdk_root)
    monkeypatch.setenv(ENV_SDK_ROOT, str(fake_sdk_root))
    monkeypatch.delenv(ENV_DEPTH, raising=False)

    # Mock subprocess.run so the test doesn't spawn a real kodawari.
    import subprocess
    from kodawari.cli.evidence import self_repair_execute as exec_mod

    class _FakeCompleted:
        returncode = 0
        stdout = "spawned ok\n"
        stderr = ""

    spawn_calls: list[list[str]] = []

    def _fake_run(cmd, **kwargs):
        spawn_calls.append(list(cmd))
        return _FakeCompleted()

    monkeypatch.setattr(exec_mod.subprocess, "run", _fake_run)

    project_root = tmp_path
    planning_dir = tmp_path / "planning" / "feat-chain"
    _write_failure_evidence(planning_dir)
    run_truth = _run_truth_blocked(planning_dir)

    summary = _maybe_write_self_repair_artifacts(
        project_root=project_root,
        planning_dir=planning_dir,
        run_truth=run_truth,
    )

    assert summary["status"] == "ready"
    assert "auto_execution" in summary, "Phase-3 auto-execute hook must fire when auto opt-in"
    auto = summary["auto_execution"]
    # All seven gates pass (env override makes both the proposal's
    # kodawari_root and the gate's resolved root point at fake-sdk),
    # so the spawn was attempted via the mocked subprocess.run.
    assert auto["status"] == "executed"
    assert auto["spawn_status"] == "ok"
    assert spawn_calls, "subprocess.run must be invoked when all gates pass"
    assert "autopilot" in spawn_calls[0]
    # Execution record was persisted alongside the proposal.
    assert (planning_dir / SELF_REPAIR_EXECUTION_FILENAME).exists()


def test_auto_execute_records_full_gate_report(tmp_path: Path, monkeypatch) -> None:
    """The execution record on disk must carry the full seven-gate report
    so post-mortem operators can see exactly which gate refused. The
    subprocess spawn is mocked here too — we only care about the
    persisted record shape."""

    monkeypatch.setenv(ENV_ENABLED, "1")
    monkeypatch.setenv(ENV_AUTO_EXECUTE, "1")
    fake_sdk_root = tmp_path / "fake"
    fake_sdk_root.mkdir()
    _write_recovery_timeout_targets(fake_sdk_root)
    monkeypatch.setenv(ENV_SDK_ROOT, str(fake_sdk_root.resolve()))
    monkeypatch.delenv(ENV_DEPTH, raising=False)

    from kodawari.cli.evidence import self_repair_execute as exec_mod

    class _FakeCompleted:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(exec_mod.subprocess, "run", lambda *a, **kw: _FakeCompleted())

    project_root = tmp_path
    planning_dir = tmp_path / "planning" / "feat-record"
    _write_failure_evidence(planning_dir)
    run_truth = _run_truth_blocked(planning_dir)

    _maybe_write_self_repair_artifacts(
        project_root=project_root,
        planning_dir=planning_dir,
        run_truth=run_truth,
    )

    record = json.loads((planning_dir / SELF_REPAIR_EXECUTION_FILENAME).read_text(encoding="utf-8"))
    assert record["schema_version"] == "workflow.self_repair.execution.v1"
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
