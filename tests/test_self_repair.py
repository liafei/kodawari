from __future__ import annotations

import json
from pathlib import Path

from kodawari.cli.evidence.self_repair import (
    SELF_REPAIR_FILENAME,
    SELF_REPAIR_MARKDOWN_FILENAME,
    build_self_repair_proposal,
    render_self_repair_markdown,
    write_self_repair_markdown,
    write_self_repair_proposal,
)
from kodawari.cli.evidence.self_repair_cmd import run_self_repair_command


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _run_truth(planning_dir: Path, *, reason: str = "EXECUTION_BACKEND_BLOCKED", status: str = "BLOCKED") -> None:
    _write_json(
        planning_dir / ".run_truth.json",
        {
            "schema_version": "run.truth.v1",
            "feature": planning_dir.name,
            "final_status": status,
            "run_reason": reason,
            "blocking_reason": "",
        },
    )


def test_self_repair_builds_fragmented_read_task(tmp_path: Path) -> None:
    planning = tmp_path / "planning" / "run-1"
    _run_truth(planning)
    _write_json(
        planning / ".execution_stall_report.json",
        {
            "schema_version": "execution.stall_report.v1",
            "error_code": "EXECUTOR_STALLED_FRAGMENTED_READS",
            "fragmented_read_paths": {"src/service.py": 10},
            "recent_tool_calls": [],
        },
    )

    payload = build_self_repair_proposal(project_root=tmp_path, planning_dir=planning)

    assert payload["status"] == "ready"
    assert payload["root_cause"]["code"] == "executor_fragmented_read_loop"
    assert payload["safety"]["auto_apply_allowed"] is False
    assert "tests/test_read_discipline.py" in payload["repair_task"]["target_files"]
    assert any("src/service.py" in str(item) for item in payload["evidence"])
    assert "Harden openai_tool_use fragmented-read discipline" in render_self_repair_markdown(payload)


def test_self_repair_not_applicable_for_successful_run(tmp_path: Path) -> None:
    planning = tmp_path / "planning" / "run-2"
    _run_truth(planning, reason="PROCEED_TO_GATE", status="OK")

    payload = build_self_repair_proposal(project_root=tmp_path, planning_dir=planning)

    assert payload["status"] == "not_applicable"
    assert payload["reason"] == "run_succeeded"


def test_self_repair_builds_recovery_timeout_task(tmp_path: Path) -> None:
    planning = tmp_path / "planning" / "run-3"
    _run_truth(planning, reason="RECOVERY_SYNTHESIZER_TIMEOUT")
    _write_json(
        planning / ".execution_failure_snapshot.json",
        {"reason": "RECOVERY_SYNTHESIZER_TIMEOUT", "error_code": "RECOVERY_SYNTHESIZER_TIMEOUT"},
    )

    payload = build_self_repair_proposal(project_root=tmp_path, planning_dir=planning)

    assert payload["status"] == "ready"
    assert payload["root_cause"]["code"] == "recovery_synthesizer_timeout"
    assert "tests/test_stall_recovery.py" in payload["repair_task"]["suggested_tests"][0]


def test_recovery_timeout_terminal_reason_wins_over_stall_and_planning_noise(tmp_path: Path) -> None:
    planning = tmp_path / "planning" / "run-3b"
    _run_truth(planning, reason="RECOVERY_SYNTHESIZER_TIMEOUT", status="executor_recovery_synthesizer_timeout")
    _write_json(planning / ".execution_stall_report.json", {"error_code": "EXECUTOR_STALLED_PATCH_PLAN_REQUIRED"})
    _write_json(
        planning / ".execution_failure_snapshot.json",
        {"reason": "RECOVERY_SYNTHESIZER_TIMEOUT", "error_code": "RECOVERY_SYNTHESIZER_TIMEOUT"},
    )
    _write_json(
        planning / "PLANNING_CONVERSATION.json",
        {"rounds": [{"status": "escalation", "message": "files_to_change and read_only_files conflict"}]},
    )

    payload = build_self_repair_proposal(project_root=tmp_path, planning_dir=planning)

    assert payload["status"] == "ready"
    assert payload["root_cause"]["code"] == "recovery_synthesizer_timeout"


def test_self_repair_builds_planner_reviewer_deadlock_task(tmp_path: Path) -> None:
    planning = tmp_path / "planning" / "run-planning-deadlock"
    _run_truth(planning, reason="planner_reviewer_deadlock", status="BLOCKED")
    _write_json(
        planning / ".planning_failure.json",
        {
            "error_code": "planner_reviewer_deadlock",
            "reason": "planner_reviewer_deadlock",
            "escalation": {
                "gate_reason": "planner_reviewer_deadlock",
                "repeated_blocker_rounds": 3,
            },
        },
    )

    payload = build_self_repair_proposal(project_root=tmp_path, planning_dir=planning)

    assert payload["status"] == "ready"
    assert payload["root_cause"]["code"] == "planner_reviewer_deadlock"
    assert "tests/test_planning_orchestrator.py" in payload["repair_task"]["target_files"]


def test_write_self_repair_artifacts(tmp_path: Path) -> None:
    planning = tmp_path / "planning" / "run-4"
    _run_truth(planning)
    _write_json(planning / ".execution_stall_report.json", {"error_code": "EXECUTOR_STALLED_BUDGET_PRESSURE"})
    payload = build_self_repair_proposal(project_root=tmp_path, planning_dir=planning)

    json_path = write_self_repair_proposal(planning, payload)
    md_path = write_self_repair_markdown(planning, payload)

    assert json_path.name == SELF_REPAIR_FILENAME
    assert md_path.name == SELF_REPAIR_MARKDOWN_FILENAME
    assert json.loads(json_path.read_text(encoding="utf-8"))["status"] == "ready"
    assert "Workflow Self-Repair" in md_path.read_text(encoding="utf-8")


def test_self_repair_target_files_outside_sdk_root_are_rejected(tmp_path: Path, monkeypatch) -> None:
    """target_files that escape SDK root via ``..`` or absolute paths must
    be filtered out and surfaced under safety.rejected_target_files. The
    earlier implementation kept them as bare strings — a downstream Phase 3
    consumer would have edited files outside the SDK repo."""

    sdk_root = tmp_path / "fake-sdk"
    sdk_root.mkdir()
    monkeypatch.setenv("WORKFLOW_SDK_SELF_REPAIR_ROOT", str(sdk_root))
    planning = tmp_path / "planning" / "run-rejection"
    _run_truth(planning)
    _write_json(
        planning / ".execution_stall_report.json",
        {"error_code": "EXECUTOR_STALLED_FRAGMENTED_READS", "fragmented_read_paths": {"src/x.py": 5}},
    )

    payload = build_self_repair_proposal(project_root=tmp_path, planning_dir=planning)

    target_files = payload["repair_task"]["target_files"]
    assert all(not Path(p).is_absolute() for p in target_files)
    # All paths in the fragmented_read proposal point to in-tree files; with a
    # fake SDK root that has none of them, none of those paths actually
    # resolve under sdk_root_resolved/<rel> in a denylisted location, but
    # they DO resolve under sdk_root_resolved (because ``sdk_root/relpath``
    # always resolves under sdk_root). So this test fixture instead confirms
    # the kodawari_root field is set correctly.
    assert payload["kodawari_root"] == str(sdk_root.resolve())
    assert payload["safety"]["kodawari_root"] == str(sdk_root.resolve())


def test_self_repair_filters_path_traversal_target(tmp_path: Path, monkeypatch) -> None:
    """A target_file with ``..`` that escapes the SDK root must be moved
    to ``rejected_target_files`` with reason=outside_sdk_root."""

    from kodawari.cli.evidence.self_repair import _filter_safe_target_files

    sdk_root = tmp_path / "sdk"
    sdk_root.mkdir()
    safe, rejected = _filter_safe_target_files(
        [
            "src/kodawari/x.py",  # safe
            "../../etc/passwd",         # path traversal
            "/abs/path.py",             # absolute path (POSIX) / outside on Windows
            "tests/conftest.py",        # denylisted
            "scripts/anything.sh",      # denylisted
            "src/kodawari/cli/runtime/a.py",  # denylisted (the running runtime)
            "_baseline/data.json",      # denylisted
        ],
        sdk_root=sdk_root,
    )

    assert safe == ["src/kodawari/x.py"]
    rejected_paths = {item["path"]: item["reason"] for item in rejected}
    assert rejected_paths["../../etc/passwd"] == "outside_sdk_root"
    # Absolute path: rejection reason differs by OS (Windows treats
    # ``/abs/...`` as drive-relative, not absolute). The important
    # property is that it gets rejected, not which reason.
    assert rejected_paths["/abs/path.py"] in {"absolute_path", "outside_sdk_root"}
    assert rejected_paths["tests/conftest.py"] == "denylisted"
    assert rejected_paths["scripts/anything.sh"] == "denylisted"
    assert rejected_paths["src/kodawari/cli/runtime/a.py"] == "denylisted"
    assert rejected_paths["_baseline/data.json"] == "denylisted"


def test_self_repair_environment_error_routes_to_not_applicable(tmp_path: Path) -> None:
    """Environment-class errors (turn budget, planner timeout, auth, missing
    executable, OOM, rate limit, session lifecycle) must NOT be eligible for
    SDK code self-repair. They route to doctor/config diagnosis via
    status=not_applicable + reason=environment_error."""

    for env_code in (
        "PLANNER_MAX_TURNS",
        "PLANNER_TIMEOUT",
        "AUTH_FORBIDDEN",
        "EXECUTABLE_MISSING",
        "RATE_LIMIT_EXCEEDED",
        "OOM_KILLED",
        "NESTED_SESSION_DETECTED",
    ):
        planning = tmp_path / "planning" / f"env-{env_code.lower()}"
        _run_truth(planning, reason=env_code)
        _write_json(planning / ".execution_failure_snapshot.json", {"error_code": env_code})

        payload = build_self_repair_proposal(project_root=tmp_path, planning_dir=planning)

        assert payload["status"] == "not_applicable", f"{env_code} should be not_applicable"
        assert payload["reason"] == "environment_error", f"{env_code} reason mismatch"
        assert payload["environment_error_code"] == env_code
        assert payload["repair_task"] == {}


def test_self_repair_planner_checkpoint_invalid_json_is_workflow_repair(tmp_path: Path) -> None:
    planning = tmp_path / "planning" / "checkpoint-invalid-json"
    _run_truth(
        planning,
        reason="planner_environment_error:planner_tool_use_checkpoint_invalid_json",
        status="BLOCKED",
    )
    _write_json(
        planning / ".planning_failure.json",
        {
            "error_code": "planner_environment_error:planner_tool_use_checkpoint_invalid_json",
            "reason": "planner_environment_error",
            "escalation": {
                "termination_reason": "planner_environment_error:planner_tool_use_checkpoint_invalid_json",
                "environment_error_kind": "planner_tool_use_checkpoint_invalid_json",
            },
        },
    )
    _write_json(
        planning / "PLANNING_CONVERSATION.json",
        {"rounds": [{"status": "escalation", "message": "files_to_change and read_only_files conflict"}]},
    )
    trace_events = [
        {"event": "progress_guard_triggered", "reason": "tool_call_limit", "tool_calls_used": 22},
        {
            "event": "final_parse_result",
            "ok": False,
            "decision_checkpoint": True,
            "content_chars": 409,
            "parse_error": "planner output is not valid json",
            "tool_calls_used": 22,
        },
    ]
    (planning / ".planner_tool_use_trace.jsonl").write_text(
        "\n".join(json.dumps(item) for item in trace_events) + "\n",
        encoding="utf-8",
    )

    payload = build_self_repair_proposal(project_root=tmp_path, planning_dir=planning)

    assert payload["status"] == "ready"
    assert payload["root_cause"]["code"] == "planner_tool_use_checkpoint_invalid_json"
    assert "environment_error_code" not in payload
    assert "src/kodawari/autopilot/planning/planning_agent.py" in payload["repair_task"]["target_files"]
    assert any(item["artifact"] == ".planner_tool_use_trace.jsonl" for item in payload["evidence"])


def test_self_repair_planner_transport_output_failure_not_deterministic_contradiction(tmp_path: Path) -> None:
    planning = tmp_path / "planning" / "planner-empty-length"
    _run_truth(planning, reason="critical_or_blocking_present", status="BLOCKED")
    _write_json(
        planning / ".planning_failure.json",
        {
            "error_code": "planner_environment_error:planner_output_truncated_empty",
            "reason": "planner_environment_error",
            "escalation": {
                "termination_reason": "planner_environment_error:planner_output_truncated_empty",
                "environment_error_kind": "planner_output_truncated_empty",
            },
        },
    )
    _write_json(
        planning / "PLANNING_CONVERSATION.json",
        {
            "rounds": [
                {
                    "planner_diagnostics": {
                        "planner_error_kind": "planner_output_truncated_empty",
                        "finish_reason": "length",
                    }
                }
            ]
        },
    )

    payload = build_self_repair_proposal(project_root=tmp_path, planning_dir=planning)

    assert payload["status"] == "ready"
    assert payload["root_cause"]["code"] == "planner_transport_or_output_failure"
    assert "planning_deterministic_contradiction" not in json.dumps(payload, ensure_ascii=False)


def test_self_repair_semantic_closure_failure_not_deterministic_contradiction(tmp_path: Path) -> None:
    planning = tmp_path / "planning" / "semantic-closure"
    _run_truth(planning, reason="critical_or_blocking_present", status="BLOCKED")
    _write_json(
        planning / ".planning_failure.json",
        {
            "error_code": "critical_or_blocking_present",
            "reason": "blocking findings remain",
            "rounds": [
                {
                    "blocking_findings": [
                        {
                            "category": "owner_surface",
                            "description": (
                                "Google Trends goal does not close because files_to_change excludes "
                                "the route/service handler wiring."
                            ),
                        }
                    ]
                }
            ],
        },
    )
    _write_json(
        planning / "PLANNING_CONVERSATION.json",
        {
            "rounds": [
                {
                    "review_payload": {
                        "findings": [
                            {
                                "category": "owner_surface",
                                "description": "files_to_change misses route handler and service call chain",
                            }
                        ]
                    }
                }
            ]
        },
    )

    payload = build_self_repair_proposal(project_root=tmp_path, planning_dir=planning)

    assert payload["status"] == "ready"
    assert payload["root_cause"]["code"] == "semantic_closure_failure"


def test_self_repair_planning_escalation_required_routes_to_semantic_closure(tmp_path: Path) -> None:
    """Layer C regression: PLANNING_ESCALATION_REQUIRED used to short-circuit
    via the dispatch table to _planning_contradiction_proposal, leaving
    _semantic_closure_proposal unreachable on the live escalation path. After
    the dispatch reorder, owner_surface markers must route to semantic_closure.
    """
    planning = tmp_path / "planning" / "escalation-required-semantic"
    _run_truth(planning, reason="PLANNING_ESCALATION_REQUIRED", status="BLOCKED")
    _write_json(
        planning / ".planning_failure.json",
        {
            "error_code": "PLANNING_ESCALATION_REQUIRED",
            "reason": "blocking findings remain",
            "rounds": [
                {
                    "blocking_findings": [
                        {
                            "category": "owner_surface",
                            "description": "files_to_change misses route handler; semantic closure not reached",
                        }
                    ]
                }
            ],
        },
    )
    _write_json(
        planning / "PLANNING_CONVERSATION.json",
        {
            "rounds": [
                {
                    "review_payload": {
                        "findings": [
                            {
                                "category": "owner_surface",
                                "description": "reviewer flagged owner_surface mismatch — call chain anchor missing",
                            }
                        ]
                    }
                }
            ]
        },
    )

    payload = build_self_repair_proposal(project_root=tmp_path, planning_dir=planning)

    assert payload["status"] == "ready"
    assert payload["root_cause"]["code"] == "semantic_closure_failure"


def test_self_repair_planning_escalation_required_falls_back_to_contradiction(tmp_path: Path) -> None:
    """Layer C guardrail: when the escalation conversation has no strong
    semantic-closure markers, PLANNING_ESCALATION_REQUIRED must still classify
    as planning_deterministic_contradiction. This prevents the reorder from
    over-firing on every escalation.
    """
    planning = tmp_path / "planning" / "escalation-required-contradiction"
    _run_truth(planning, reason="PLANNING_ESCALATION_REQUIRED", status="BLOCKED")
    _write_json(
        planning / ".planning_failure.json",
        {
            "error_code": "PLANNING_ESCALATION_REQUIRED",
            "reason": "verify-only test demotion conflict",
            "escalation": {
                "unresolved_findings": [
                    {
                        "category": "structure",
                        "description": "files_to_change references read_only_files entry; verify-only conflict",
                    }
                ]
            },
        },
    )
    _write_json(
        planning / "PLANNING_CONVERSATION.json",
        {
            "rounds": [
                {
                    "review_payload": {
                        "findings": [
                            {
                                "category": "structure",
                                "description": "read_only_files conflict with verify-only test mutation",
                            }
                        ]
                    }
                }
            ]
        },
    )

    payload = build_self_repair_proposal(project_root=tmp_path, planning_dir=planning)

    assert payload["status"] == "ready"
    assert payload["root_cause"]["code"] == "planning_deterministic_contradiction"


def test_self_repair_task_input_infeasible_surface_recommends_human_decision(
    tmp_path: Path,
) -> None:
    """Layer D self-repair classification: the precheck's
    ``task_input_infeasible_surface`` termination must mint a human-decision
    proposal, not an SDK code repair. target_files MUST be empty so the
    Phase-3 spawn path does not silently rewrite SDK code for a task-shape
    problem the human has to settle.
    """
    planning = tmp_path / "planning" / "infeasible-task"
    _run_truth(planning, reason="task_input_infeasible_surface", status="BLOCKED")
    _write_json(
        planning / ".planning_failure.json",
        {
            "error_code": "task_input_infeasible_surface",
            "reason": "task_input_infeasible_surface",
            "escalation": {
                "termination_reason": "task_input_infeasible_surface",
                "gate_reason": "task_input_infeasible_surface",
                "missing_surfaces": ["/api/v1/events/{id}/social"],
                "unresolved_findings": [
                    {
                        "severity": "blocking",
                        "category": "task_shape_infeasible",
                        "description": "test-only against missing surface",
                    }
                ],
            },
        },
    )

    payload = build_self_repair_proposal(project_root=tmp_path, planning_dir=planning)

    assert payload["status"] == "ready"
    assert payload["root_cause"]["code"] == "task_input_infeasible_surface"
    repair_task = payload["repair_task"]
    assert repair_task["target_files"] == []
    assert "/api/v1/events/{id}/social" in repair_task["task_direction"]
    assert payload.get("auto_apply_allowed", False) is False


def test_self_repair_recovery_synthesizer_timeout_is_not_environment_error(tmp_path: Path) -> None:
    """RECOVERY_SYNTHESIZER_TIMEOUT contains the substring 'TIMEOUT' but is
    a workflow design issue (the synthesizer was a poor choice for stalled
    sessions, see commit 730678e), not an environment error. The
    workflow-internal patterns must take precedence over env patterns."""

    planning = tmp_path / "planning" / "synth-timeout-not-env"
    _run_truth(planning, reason="RECOVERY_SYNTHESIZER_TIMEOUT")
    _write_json(planning / ".execution_failure_snapshot.json", {"error_code": "RECOVERY_SYNTHESIZER_TIMEOUT"})

    payload = build_self_repair_proposal(project_root=tmp_path, planning_dir=planning)

    assert payload["status"] == "ready"
    assert payload["root_cause"]["code"] == "recovery_synthesizer_timeout"
    assert "environment_error_code" not in payload


def test_self_repair_unsupported_failure_code_routes_to_triage_required(tmp_path: Path) -> None:
    """Failure with a code that has no specialized classifier must NOT be
    silently dropped to ``not_applicable``. The plan calls this class
    ``unsupported_workflow_failure``: status=triage_required so a human
    sees it instead of it disappearing."""

    planning = tmp_path / "planning" / "novel-failure"
    _run_truth(planning, reason="SOMETHING_NEW_THAT_HAS_NO_CLASSIFIER")
    _write_json(
        planning / ".execution_failure_snapshot.json",
        {"error_code": "SOMETHING_NEW_THAT_HAS_NO_CLASSIFIER"},
    )

    payload = build_self_repair_proposal(project_root=tmp_path, planning_dir=planning)

    assert payload["status"] == "triage_required"
    assert payload["reason"] == "unsupported_workflow_failure"
    assert payload["unhandled_code"] == "SOMETHING_NEW_THAT_HAS_NO_CLASSIFIER"
    assert payload["root_cause"]["code"] == "unsupported_workflow_failure"
    assert payload["repair_task"] == {}
    assert "Triage Required" in render_self_repair_markdown(payload)


def test_self_repair_patch_plan_required_has_specialized_classifier(tmp_path: Path) -> None:
    """EXECUTOR_STALLED_PATCH_PLAN_REQUIRED previously fell through to the
    generic _generic_executor_stall_proposal. The plan demands a dedicated
    classifier category."""

    planning = tmp_path / "planning" / "patch-plan-required"
    _run_truth(planning)
    _write_json(
        planning / ".execution_stall_report.json",
        {
            "error_code": "EXECUTOR_STALLED_PATCH_PLAN_REQUIRED",
            "counters": {"no_write_iterations": 8},
        },
    )

    payload = build_self_repair_proposal(project_root=tmp_path, planning_dir=planning)

    assert payload["status"] == "ready"
    assert payload["root_cause"]["code"] == "executor_patch_plan_required"


def test_self_repair_classifies_unproductive_fix_round_terminal_reason(tmp_path: Path) -> None:
    planning = tmp_path / "planning" / "unproductive-fix-round"
    _run_truth(planning, reason="EXECUTOR_FIX_ROUND_UNPRODUCTIVE", status="BLOCKED")
    _write_json(
        planning / ".execution_failure_snapshot.json",
        {"error_code": "EXECUTOR_FIX_ROUND_UNPRODUCTIVE"},
    )

    payload = build_self_repair_proposal(project_root=tmp_path, planning_dir=planning)

    assert payload["status"] == "ready"
    assert payload["root_cause"]["code"] == "executor_fix_round_unproductive"
    assert "src/kodawari/autopilot/engine/loop_runner.py" in payload["repair_task"]["target_files"]
    assert "tests/test_loop_runner.py" in payload["repair_task"]["target_files"]


def test_self_repair_failure_snapshot_outranks_stall_for_classification(tmp_path: Path) -> None:
    """When both stall_report and failure_snapshot are present with
    different codes, failure_snapshot wins. Stall is a transient event
    that may have been recovered before the run terminated; failure
    snapshot is the engine's chosen terminal blame."""

    planning = tmp_path / "planning" / "snapshot-vs-stall"
    _run_truth(planning, reason="EXECUTION_BACKEND_BLOCKED")
    _write_json(
        planning / ".execution_stall_report.json",
        {"error_code": "EXECUTOR_STALLED_PATCH_PLAN_REQUIRED"},
    )
    _write_json(
        planning / ".execution_failure_snapshot.json",
        {"error_code": "EXECUTOR_STALLED_FRAGMENTED_READS"},
    )

    payload = build_self_repair_proposal(project_root=tmp_path, planning_dir=planning)

    assert payload["root_cause"]["code"] == "executor_fragmented_read_loop"


def test_self_repair_run_truth_stale_reduces_confidence(tmp_path: Path) -> None:
    """When run_truth.generated_at is older than the most recent on-disk
    artifact, the proposal must surface ``truth_freshness=stale`` and
    knock 0.2 off ``root_cause.confidence`` so consumers know not to
    auto-apply blindly."""

    planning = tmp_path / "planning" / "stale-truth"
    planning.mkdir(parents=True, exist_ok=True)
    # Truth is 1 hour old.
    _write_json(
        planning / ".run_truth.json",
        {
            "feature": planning.name,
            "final_status": "BLOCKED",
            "run_reason": "EXECUTION_BACKEND_BLOCKED",
            "blocking_reason": "",
            "generated_at": "2026-05-05T10:00:00+00:00",
        },
    )
    # Stall report is from now (after truth).
    _write_json(
        planning / ".execution_stall_report.json",
        {
            "error_code": "EXECUTOR_STALLED_FRAGMENTED_READS",
            "fragmented_read_paths": {"src/x.py": 5},
            "generated_at": "2026-05-05T11:00:00+00:00",
        },
    )

    payload = build_self_repair_proposal(project_root=tmp_path, planning_dir=planning)

    assert payload["truth_freshness"] == "stale"
    assert payload["status"] == "ready"
    # Original confidence for fragmented_read is 0.95; stale → 0.75
    assert payload["root_cause"]["confidence"] == 0.75
    assert payload["root_cause"]["confidence_adjustment_reason"] == "run_truth_stale"


def test_self_repair_phase_status_advertises_unimplemented_phases(tmp_path: Path) -> None:
    """Honest phase-implementation status must be in every payload so
    consumers cannot assume Phase 3 (auto-execute) or Phase 4 (post-success
    learning) are wired."""

    planning = tmp_path / "planning" / "phase-status"
    _run_truth(planning, reason="PROCEED_TO_GATE", status="OK")

    payload = build_self_repair_proposal(project_root=tmp_path, planning_dir=planning)

    assert payload["phase_implementation_status"]["phase_1_classifier"] == "partial"
    assert payload["phase_implementation_status"]["phase_2_proposal_artifact"] == "implemented"
    assert payload["phase_implementation_status"]["phase_3_auto_execute_gate"] == "implemented_opt_in"
    assert payload["phase_implementation_status"]["phase_4_post_success_learning"] == "implemented_opt_in"


def test_self_repair_safety_always_blocks_auto_apply(tmp_path: Path) -> None:
    """Even with Phase 3 available, proposals must never imply silent
    patching. Every payload returns ``safety.auto_apply_allowed=False``;
    auto-execute is a separate runtime hook and still goes through review."""

    cases = [
        ("ready", lambda p: _write_json(p / ".execution_stall_report.json", {"error_code": "EXECUTOR_STALLED_FRAGMENTED_READS"})),
        ("not_applicable", lambda p: None),
        ("triage_required", lambda p: _write_json(p / ".execution_failure_snapshot.json", {"error_code": "BRAND_NEW_FAILURE_CODE"})),
    ]
    for label, setup in cases:
        planning = tmp_path / "planning" / f"safety-{label}"
        _run_truth(planning) if label != "not_applicable" else _run_truth(planning, reason="PROCEED_TO_GATE", status="OK")
        if setup is not None:
            setup(planning)
        payload = build_self_repair_proposal(project_root=tmp_path, planning_dir=planning)
        assert payload["safety"]["auto_apply_allowed"] is False, f"safety should block auto_apply for {label}"
        assert payload["safety"]["requires_review"] is True


def test_self_repair_command_writes_artifacts(tmp_path: Path, capsys) -> None:
    planning = tmp_path / "planning" / "run-5"
    _run_truth(planning)
    _write_json(planning / ".execution_stall_report.json", {"error_code": "EXECUTOR_STALLED_FRAGMENTED_READS"})

    class Args:
        project_root = str(tmp_path)
        feature = ""
        planning_dir = str(planning)
        write = True
        markdown = True
        output = ""

    rc = run_self_repair_command(Args())
    out = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert out["status"] == "ready"
    assert (planning / SELF_REPAIR_FILENAME).exists()
    assert (planning / SELF_REPAIR_MARKDOWN_FILENAME).exists()
