"""Tests for planning_orchestrator.py — approval gate, task splitting, escalation, artifact output."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any

import pytest
import kodawari.autopilot.planning.planning_orchestrator as planning_orchestrator

from kodawari.autopilot.core.model_config import WorkflowTransportConfig
from kodawari.autopilot.planning.planning_orchestrator import (
    PlanningConfig,
    PlanningResult,
    _evaluate_approval,
    _split_tasks_if_needed,
    _build_escalation,
    PlanningRound,
    plan_to_task_graph,
    plan_to_task_cards,
    result_to_artifact,
)
from kodawari.autopilot.planning.task_card import validate_task_card
from kodawari.autopilot.planning.task_graph import validate_task_graph
from kodawari.instincts import PromptLessonStore


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content), encoding="utf-8")


def _task(
    task_id: str = "T1",
    files: list[str] | None = None,
    new_files: list[str] | None = None,
    invariants: list[str] | None = None,
    depends_on: list[str] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "task_name": f"Task {task_id}",
        "layer_owner": overrides.pop("layer_owner", "service"),
        "surface": overrides.pop("surface", "rest_api"),
        "files_to_change": files or ["backend/main.py"],
        "new_files": new_files or [],
        "coverage_hints": overrides.pop("coverage_hints", ["layer:service"]),
        "approach": "...",
        "invariants": invariants or ["no regression"],
        "test_plan": overrides.pop("test_plan", "pytest tests/ -q"),
        "verify_cmd": "pytest tests/ -q",
        "depends_on": depends_on or [],
        "forbidden_changes": [],
        "provides": overrides.pop("provides", []),
        "requires": overrides.pop("requires", []),
        "api_contracts": overrides.pop("api_contracts", []),
        **overrides,
    }


def _plan(tasks: list[dict] | None = None, **overrides: Any) -> dict[str, Any]:
    return {
        "summary": "test plan",
        "business_outcome": "test outcome",
        "out_of_scope": ["nothing"],
        "source_of_truth": ["backend/main.py"],
        "path_type": "write",
        "layers": ["service"],
        "coverage_hints": ["layer:service"],
        "module_boundaries": [
            {"name": "core", "surface": "rest_api", "roots": ["backend/"], "layers": ["service"]}
        ],
        "verify_recipes": [
            {"surface": "rest_api", "command": "pytest tests/ -q", "required": True, "roots": ["tests/"]}
        ],
        "tasks": tasks or [_task()],
        "risks": [],
        "change_log": [],
        "self_assessment": {"score": 9.0, "notes": []},
        **overrides,
    }


def _review(score: float = 9.0, approved: bool = True, **overrides: Any) -> dict[str, Any]:
    return {
        "score": score,
        "approved": approved,
        "findings": overrides.pop("findings", []),
        "contradictions": overrides.pop("contradictions", []),
        "assessment": "ok",
        **overrides,
    }


def _patch_planning_context(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        planning_orchestrator,
        "collect_planning_context",
        lambda **kwargs: {"repo_manifest": {"files": []}, "input_fingerprint": "sha256:test"},
    )
    monkeypatch.setattr(planning_orchestrator, "render_context_for_prompt", lambda context, **_kwargs: "context")
    monkeypatch.setattr(planning_orchestrator, "build_file_manifest", lambda files: {})
    monkeypatch.setattr(
        planning_orchestrator,
        "_planning_context_scout_payload",
        lambda **kwargs: {"enabled": False},
    )


# ---------------------------------------------------------------------------
# Approval Gate
# ---------------------------------------------------------------------------

def test_path_probe_helpers_treat_os_invalid_paths_as_not_ready(tmp_path: Path) -> None:
    plan = _plan(tasks=[_task(files=["backend/bad\0path.py"])])

    assert planning_orchestrator._all_existing_files_found(plan, tmp_path) is False
    assert planning_orchestrator._context_scout_line_counts(tmp_path, ["backend/bad\0path.py"]) == {}


def test_run_planning_conversation_dispatches_noop_drivers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_planning_context(monkeypatch)
    _write(tmp_path / "README.md", "noop")

    result = planning_orchestrator.run_planning_conversation(
        config=PlanningConfig(
            planner_executable="noop",
            reviewer_executable="noop",
            planner_driver="noop",
            reviewer_driver="noop",
            max_rounds=1,
        ),
        project_root=tmp_path,
        planning_dir=tmp_path / "planning" / "feature",
        task_direction="Noop contract dispatch",
        repo_inventory={"archetype": "test"},
        feature="feature",
    )

    assert result.status == "approved"
    assert result.final_plan["tasks"][0]["task_id"] == "TNOOP"
    assert result.final_review is not None
    assert result.final_review["approved"] is True


def test_http_planner_context_pressure_falls_back_next_round(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write(tmp_path / "backend" / "main.py", "app")
    monkeypatch.setattr(
        planning_orchestrator,
        "collect_planning_context",
        lambda **kwargs: {"repo_manifest": {"files": ["backend/main.py"]}, "input_fingerprint": "sha256:test"},
    )
    rendered_budgets: list[int] = []

    def fake_render(_context: dict[str, Any], *, max_chars: int = 0) -> str:
        rendered_budgets.append(max_chars)
        return f"context-budget:{max_chars}"

    monkeypatch.setattr(planning_orchestrator, "render_context_for_prompt", fake_render)
    monkeypatch.setattr(planning_orchestrator, "build_file_manifest", lambda files: {"main.py": ["backend/main.py"]})
    monkeypatch.setattr(
        planning_orchestrator,
        "_planning_context_scout_payload",
        lambda **kwargs: {"enabled": False},
    )
    calls: list[str] = []

    def fake_generate_plan(**kwargs: Any) -> tuple[dict[str, Any] | None, str]:
        calls.append(str(kwargs.get("context_text") or ""))
        diagnostics = kwargs.get("diagnostics_out")
        if len(calls) == 1:
            diagnostics.update({"chat_kind": "http_timeout", "request_bytes": 161_000})
            return None, "planner HTTP request timed out"
        diagnostics.update({"chat_kind": "ok", "request_bytes": 70_000})
        return _plan(), ""

    monkeypatch.setattr(planning_orchestrator, "generate_plan", fake_generate_plan)
    monkeypatch.setattr(planning_orchestrator, "review_plan", lambda **_kwargs: (_review(), ""))

    result = planning_orchestrator.run_planning_conversation(
        config=PlanningConfig(
            planner_transport=WorkflowTransportConfig(
                name="mimo_chat",
                kind="http",
                driver="openai_compatible",
                interface="chat",
                api_format="openai_chat",
                base_url="https://example.test/v1",
                api_key_env="WORKFLOW_TEST_OPENAI_KEY",
            ),
            max_rounds=2,
        ),
        project_root=tmp_path,
        planning_dir=tmp_path / "planning" / "feature",
        task_direction="Plan with fallback",
        repo_inventory={"archetype": "test"},
        feature="feature",
    )

    assert result.status == "approved"
    assert calls == ["context-budget:0", "context-budget:96000"]
    assert rendered_budgets[:2] == [0, 96000]
    assert result.rounds[0].planner_diagnostics["context_budget"] == 0
    assert result.rounds[1].planner_diagnostics["context_budget"] == 96000


def test_planning_conversation_escalates_tool_use_no_progress_without_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_planning_context(monkeypatch)
    _write(tmp_path / "backend" / "main.py", "app")
    calls = {"count": 0}

    def fake_generate_plan(**kwargs: Any) -> tuple[dict[str, Any] | None, str]:
        calls["count"] += 1
        diagnostics = kwargs.get("diagnostics_out")
        diagnostics.update({"planner_error_kind": "tool_use_no_progress", "transport_kind": "http_tool_use"})
        return None, "planner HTTP tool-use no progress after decision checkpoint: invalid_json"

    monkeypatch.setattr(planning_orchestrator, "generate_plan", fake_generate_plan)
    monkeypatch.setattr(planning_orchestrator, "review_plan", lambda **_kwargs: (_review(), ""))

    result = planning_orchestrator.run_planning_conversation(
        config=PlanningConfig(max_rounds=3),
        project_root=tmp_path,
        planning_dir=tmp_path / "planning" / "feature",
        task_direction="Plan until tool no progress",
        repo_inventory={"archetype": "test"},
        feature="feature",
    )

    assert result.status == "escalation_required"
    assert calls["count"] == 1
    assert result.escalation is not None
    assert result.escalation["environment_error_kind"] == "tool_use_no_progress"


def test_planning_conversation_escalates_checkpoint_invalid_json_without_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_planning_context(monkeypatch)
    _write(tmp_path / "backend" / "main.py", "app")
    calls = {"count": 0}

    def fake_generate_plan(**kwargs: Any) -> tuple[dict[str, Any] | None, str]:
        calls["count"] += 1
        diagnostics = kwargs.get("diagnostics_out")
        diagnostics.update(
            {
                "planner_error_kind": "planner_tool_use_checkpoint_invalid_json",
                "transport_kind": "http_tool_use",
                "tool_decision_checkpoint": True,
            }
        )
        return None, "planner HTTP tool-use checkpoint response still invalid JSON after repair: invalid_json"

    monkeypatch.setattr(planning_orchestrator, "generate_plan", fake_generate_plan)
    monkeypatch.setattr(planning_orchestrator, "review_plan", lambda **_kwargs: (_review(), ""))

    result = planning_orchestrator.run_planning_conversation(
        config=PlanningConfig(max_rounds=3),
        project_root=tmp_path,
        planning_dir=tmp_path / "planning" / "feature",
        task_direction="Plan until checkpoint invalid JSON",
        repo_inventory={"archetype": "test"},
        feature="feature",
    )

    assert result.status == "escalation_required"
    assert calls["count"] == 1
    assert result.escalation is not None
    assert result.escalation["environment_error_kind"] == "planner_tool_use_checkpoint_invalid_json"


def test_planning_conversation_falls_back_from_tool_use_empty_output_to_chat(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_planning_context(monkeypatch)
    _write(tmp_path / "backend" / "main.py", "app")
    calls: list[str] = []

    def fake_generate_plan(**kwargs: Any) -> tuple[dict[str, Any] | None, str]:
        transport = kwargs.get("transport")
        interface = str(getattr(transport, "interface", "") or "")
        calls.append(interface)
        diagnostics = kwargs.get("diagnostics_out")
        if interface == "tool_use":
            diagnostics.update(
                {
                    "planner_error_kind": "planner_output_truncated_empty",
                    "transport_kind": "http_tool_use",
                    "finish_reason": "length",
                }
            )
            return None, "planner HTTP tool-use returned empty output"
        diagnostics.update({"chat_kind": "ok", "transport_kind": "http_chat"})
        return _plan(), ""

    monkeypatch.setattr(planning_orchestrator, "generate_plan", fake_generate_plan)
    monkeypatch.setattr(planning_orchestrator, "review_plan", lambda **_kwargs: (_review(), ""))

    result = planning_orchestrator.run_planning_conversation(
        config=PlanningConfig(
            planner_transport=WorkflowTransportConfig(
                name="mimo_tool_use",
                kind="http",
                driver="openai_compatible",
                interface="tool_use",
                api_format="openai_chat",
                base_url="https://example.test/v1",
                api_key_env="WORKFLOW_TEST_OPENAI_KEY",
            ),
            max_rounds=1,
        ),
        project_root=tmp_path,
        planning_dir=tmp_path / "planning" / "feature",
        task_direction="Plan until empty output fallback",
        repo_inventory={"archetype": "test"},
        feature="feature",
    )

    assert result.status == "approved"
    assert calls == ["tool_use", "chat"]
    assert result.rounds[0].planner_diagnostics["planner_fallback_used"] is True
    assert result.rounds[0].planner_diagnostics["planner_fallback_reason"] == "planner_output_truncated_empty"


def test_planning_conversation_does_not_fallback_for_tool_use_auth(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_planning_context(monkeypatch)
    _write(tmp_path / "backend" / "main.py", "app")
    calls = {"count": 0}

    def fake_generate_plan(**kwargs: Any) -> tuple[dict[str, Any] | None, str]:
        calls["count"] += 1
        diagnostics = kwargs.get("diagnostics_out")
        diagnostics.update({"planner_error_kind": "auth_forbidden", "transport_kind": "http_tool_use"})
        return None, "planner auth forbidden"

    monkeypatch.setattr(planning_orchestrator, "generate_plan", fake_generate_plan)

    result = planning_orchestrator.run_planning_conversation(
        config=PlanningConfig(
            planner_transport=WorkflowTransportConfig(
                name="mimo_tool_use",
                kind="http",
                driver="openai_compatible",
                interface="tool_use",
                api_format="openai_chat",
                base_url="https://example.test/v1",
                api_key_env="WORKFLOW_TEST_OPENAI_KEY",
            ),
            max_rounds=3,
        ),
        project_root=tmp_path,
        planning_dir=tmp_path / "planning" / "feature",
        task_direction="Plan until auth failure",
        repo_inventory={"archetype": "test"},
        feature="feature",
    )

    assert result.status == "escalation_required"
    assert calls["count"] == 1
    assert result.escalation is not None
    assert result.escalation["environment_error_kind"] == "auth_forbidden"


def test_planning_conversation_stops_on_approved_nonblocking_findings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_planning_context(monkeypatch)
    _write(tmp_path / "backend" / "main.py", "app")
    calls: list[int] = []

    def fake_generate_plan(**_kwargs: Any) -> tuple[dict[str, Any], str]:
        calls.append(len(calls) + 1)
        return _plan(self_assessment={"score": 8.0, "notes": []}), ""

    monkeypatch.setattr(planning_orchestrator, "generate_plan", fake_generate_plan)
    monkeypatch.setattr(
        planning_orchestrator,
        "review_plan",
        lambda **_kwargs: (
            _review(
                82.0,
                approved=True,
                findings=[
                    {
                        "severity": "medium",
                        "category": "followup",
                        "description": "Add a later polish card.",
                        "recommendation": "Track as non-blocking follow-up.",
                    }
                ],
            ),
            "",
        ),
    )

    result = planning_orchestrator.run_planning_conversation(
        config=PlanningConfig(max_rounds=3),
        project_root=tmp_path,
        planning_dir=tmp_path / "planning" / "feature",
        task_direction="Plan until approved",
        repo_inventory={"archetype": "test"},
        feature="feature",
    )

    assert result.status == "approved"
    assert result.approval["decision"] == "auto_approve"
    assert len(result.rounds) == 1
    assert calls == [1]


def test_planning_conversation_soft_gate_stops_after_clean_round(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_planning_context(monkeypatch)
    _write(tmp_path / "backend" / "main.py", "app")
    calls: list[int] = []

    def fake_generate_plan(**_kwargs: Any) -> tuple[dict[str, Any], str]:
        calls.append(len(calls) + 1)
        return _plan(), ""

    monkeypatch.setattr(planning_orchestrator, "generate_plan", fake_generate_plan)
    monkeypatch.setattr(planning_orchestrator, "review_plan", lambda **_kwargs: (_review(), ""))

    result = planning_orchestrator.run_planning_conversation(
        config=PlanningConfig(max_rounds=5, decision_policy="soft-gate"),
        project_root=tmp_path,
        planning_dir=tmp_path / "planning" / "feature",
        task_direction="Plan with soft gate",
        repo_inventory={"archetype": "test"},
        feature="feature",
    )

    assert result.status == "auto_skipped"
    assert len(result.rounds) == 1
    assert calls == [1]


def test_planning_conversation_approval_required_stops_after_clean_round(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_planning_context(monkeypatch)
    _write(tmp_path / "backend" / "main.py", "app")
    calls: list[int] = []

    def fake_generate_plan(**_kwargs: Any) -> tuple[dict[str, Any], str]:
        calls.append(len(calls) + 1)
        return _plan(), ""

    monkeypatch.setattr(planning_orchestrator, "generate_plan", fake_generate_plan)
    monkeypatch.setattr(planning_orchestrator, "review_plan", lambda **_kwargs: (_review(), ""))

    result = planning_orchestrator.run_planning_conversation(
        config=PlanningConfig(max_rounds=5, decision_policy="approval-required"),
        project_root=tmp_path,
        planning_dir=tmp_path / "planning" / "feature",
        task_direction="Plan with approval gate",
        repo_inventory={"archetype": "test"},
        feature="feature",
    )

    assert result.status == "escalation_required"
    assert result.escalation is not None
    assert result.escalation["gate_reason"] == "approval_required"
    assert len(result.rounds) == 1
    assert calls == [1]


def test_planner_context_pressure_treats_mid_sized_timeout_as_fallback_signal() -> None:
    round_item = PlanningRound(
        round_number=1,
        plan_payload={},
        review_payload=None,
        review_error="timeout",
        structural_issues=["timeout"],
        blocking_findings_count=1,
        timestamp="2026-04-30T00:00:00Z",
        planner_error="timeout",
        planner_diagnostics={"chat_kind": "http_timeout", "request_bytes": 47_980},
    )

    assert planning_orchestrator._planner_context_pressure(round_item) is True


def test_planning_conversation_escalates_planner_environment_error_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_planning_context(monkeypatch)
    _write(tmp_path / "backend" / "main.py", "app")
    calls: list[int] = []

    def fake_generate_plan(**kwargs: Any) -> tuple[None, str]:
        calls.append(len(calls) + 1)
        diagnostics = kwargs.get("diagnostics_out")
        diagnostics.update({"planner_error_kind": "max_turns"})
        return None, "claude CLI hit --max-turns before producing a plan"

    monkeypatch.setattr(planning_orchestrator, "generate_plan", fake_generate_plan)
    monkeypatch.setattr(
        planning_orchestrator,
        "review_plan",
        lambda **_kwargs: (_review(), ""),
    )

    result = planning_orchestrator.run_planning_conversation(
        config=PlanningConfig(max_rounds=3),
        project_root=tmp_path,
        planning_dir=tmp_path / "planning" / "feature",
        task_direction="Plan with max turns failure",
        repo_inventory={"archetype": "test"},
        feature="feature",
    )

    assert result.status == "escalation_required"
    assert len(result.rounds) == 1
    assert calls == [1]
    assert result.escalation["gate_reason"] == "planner_environment_error"
    assert result.escalation["environment_error_kind"] == "max_turns"


def test_planning_conversation_escalates_reviewer_error_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_planning_context(monkeypatch)
    _write(tmp_path / "backend" / "main.py", "app")
    calls: list[int] = []

    def fake_generate_plan(**_kwargs: Any) -> tuple[dict[str, Any], str]:
        calls.append(len(calls) + 1)
        return _plan(), ""

    monkeypatch.setattr(planning_orchestrator, "generate_plan", fake_generate_plan)
    monkeypatch.setattr(
        planning_orchestrator,
        "review_plan",
        lambda **_kwargs: (None, "plan reviewer output is not valid json"),
    )

    result = planning_orchestrator.run_planning_conversation(
        config=PlanningConfig(max_rounds=3),
        project_root=tmp_path,
        planning_dir=tmp_path / "planning" / "feature",
        task_direction="Plan with reviewer parse failure",
        repo_inventory={"archetype": "test"},
        feature="feature",
    )

    assert result.status == "escalation_required"
    assert len(result.rounds) == 1
    assert calls == [1]
    assert result.escalation["gate_reason"] == "plan_reviewer_error"
    assert result.escalation["reviewer_error_kind"] == "reviewer_invalid_json"


class TestEvaluateApproval:

    def test_auto_approve_when_all_pass(self, tmp_path: Path) -> None:
        _write(tmp_path / "backend" / "main.py", "app")
        plan = _plan()
        review = _review(9.0)
        result = _evaluate_approval(
            final_plan=plan, final_review=review,
            project_root=tmp_path, config=PlanningConfig(),
        )
        assert result["decision"] == "auto_approve"

    def test_auto_approve_normalizes_reviewer_percent_score(self, tmp_path: Path) -> None:
        _write(tmp_path / "backend" / "main.py", "app")
        plan = _plan(self_assessment={"score": 8.0, "notes": []})
        review = _review(82.0)
        result = _evaluate_approval(
            final_plan=plan, final_review=review,
            project_root=tmp_path, config=PlanningConfig(),
        )
        assert result["decision"] == "auto_approve"
        assert result["checks"]["reviewer_score"] == 8.2
        assert result["checks"]["score_gap_ok"] is True

    def test_auto_approve_uses_reviewer_score_when_planner_score_missing(self, tmp_path: Path) -> None:
        _write(tmp_path / "backend" / "main.py", "app")
        plan = _plan()
        plan.pop("self_assessment", None)
        result = _evaluate_approval(
            final_plan=plan, final_review=_review(91.0),
            project_root=tmp_path, config=PlanningConfig(),
        )

        assert result["decision"] == "auto_approve"
        assert result["checks"]["planner_score"] == 9.1
        assert result["checks"]["score_gap_ok"] is True

    def test_auto_approve_normalizes_fractional_planner_score(self, tmp_path: Path) -> None:
        _write(tmp_path / "backend" / "main.py", "app")
        plan = _plan(self_assessment={"score": 0.91, "notes": []})
        result = _evaluate_approval(
            final_plan=plan, final_review=_review(91.0),
            project_root=tmp_path, config=PlanningConfig(),
        )

        assert result["decision"] == "auto_approve"
        assert result["checks"]["planner_score"] == 9.1

    def test_human_required_when_blocking_findings(self, tmp_path: Path) -> None:
        _write(tmp_path / "backend" / "main.py", "app")
        review = _review(findings=[{"severity": "blocking", "description": "bad scope"}])
        result = _evaluate_approval(
            final_plan=_plan(), final_review=review,
            project_root=tmp_path, config=PlanningConfig(),
        )
        assert result["decision"] == "human_required"

    def test_human_required_when_contradictions(self, tmp_path: Path) -> None:
        _write(tmp_path / "backend" / "main.py", "app")
        review = _review(approved=False, contradictions=["scope conflict"])
        result = _evaluate_approval(
            final_plan=_plan(), final_review=review,
            project_root=tmp_path, config=PlanningConfig(),
        )
        assert result["decision"] == "human_required"

    def test_auto_approve_when_reviewer_approved_with_nonblocking_contradictions(self, tmp_path: Path) -> None:
        _write(tmp_path / "backend" / "main.py", "app")
        review = _review(approved=True, contradictions=["non-blocking evidence hierarchy note"])
        result = _evaluate_approval(
            final_plan=_plan(), final_review=review,
            project_root=tmp_path, config=PlanningConfig(),
        )
        assert result["decision"] == "auto_approve"
        assert result["checks"]["no_contradictions"] is True

    def test_auto_approve_when_all_findings_demoted_by_repair(self, tmp_path: Path) -> None:
        _write(tmp_path / "backend" / "main.py", "app")
        review = _review(
            score=5.8,
            approved=False,
            findings=[
                {
                    "severity": "info",
                    "description": "stale reviewer issue already handled",
                    "severity_demoted": True,
                    "demoted_reason": "deterministic_repair_already_applied:remove_extra_gate",
                }
            ],
            contradictions=["stale contradiction already covered by deterministic repair"],
        )
        result = _evaluate_approval(
            final_plan=_plan(),
            final_review=review,
            project_root=tmp_path,
            config=PlanningConfig(),
        )

        assert result["decision"] == "auto_approve"
        assert result["checks"]["no_contradictions"] is True
        assert result["checks"]["reviewer_approved_effective"] is True
        assert result["checks"]["reviewer_findings_demoted_by_repair"] is True
        assert result["checks"]["reviewer_score_adjusted_by_repair"] is True
        assert result["checks"]["reviewer_score"] >= 8.0

    def test_human_required_when_reviewer_unavailable(self, tmp_path: Path) -> None:
        _write(tmp_path / "backend" / "main.py", "app")
        result = _evaluate_approval(
            final_plan=_plan(), final_review=None,
            project_root=tmp_path, config=PlanningConfig(),
        )
        assert result["decision"] == "human_required"
        assert result["checks"]["reviewer_available"] is False

    def test_human_required_when_low_planner_score(self, tmp_path: Path) -> None:
        _write(tmp_path / "backend" / "main.py", "app")
        plan = _plan(self_assessment={"score": 5.0, "notes": []})
        result = _evaluate_approval(
            final_plan=plan, final_review=_review(9.0),
            project_root=tmp_path, config=PlanningConfig(),
        )
        assert result["decision"] == "human_required"

    def test_relaxed_score_auto_approves_when_75_planner_75_reviewer(self, tmp_path: Path) -> None:
        """A3: 7.5/7.5 with all structural checks + no blocking → relaxed auto-approve."""
        _write(tmp_path / "backend" / "main.py", "app")
        plan = _plan(self_assessment={"score": 7.5, "notes": []})
        review = _review(7.5)
        result = _evaluate_approval(
            final_plan=plan, final_review=review,
            project_root=tmp_path, config=PlanningConfig(),
        )
        assert result["decision"] == "auto_approve"
        assert result["reason"] == "all_structural_checks_passed_relaxed_score"

    def test_strict_score_still_takes_strict_reason(self, tmp_path: Path) -> None:
        """When both scores >= 8.0, the strict reason is preferred over relaxed."""
        _write(tmp_path / "backend" / "main.py", "app")
        plan = _plan(self_assessment={"score": 9.0, "notes": []})
        review = _review(9.0)
        result = _evaluate_approval(
            final_plan=plan, final_review=review,
            project_root=tmp_path, config=PlanningConfig(),
        )
        assert result["decision"] == "auto_approve"
        assert result["reason"] == "all_structural_checks_passed"

    def test_relaxed_path_rejects_below_75(self, tmp_path: Path) -> None:
        _write(tmp_path / "backend" / "main.py", "app")
        plan = _plan(self_assessment={"score": 7.4, "notes": []})
        review = _review(7.5)
        result = _evaluate_approval(
            final_plan=plan, final_review=review,
            project_root=tmp_path, config=PlanningConfig(),
        )
        assert result["decision"] == "human_required"

    def test_human_required_when_score_gap_too_large(self, tmp_path: Path) -> None:
        _write(tmp_path / "backend" / "main.py", "app")
        plan = _plan(self_assessment={"score": 9.5, "notes": []})
        review = _review(6.0)
        result = _evaluate_approval(
            final_plan=plan, final_review=review,
            project_root=tmp_path, config=PlanningConfig(),
        )
        assert result["decision"] == "human_required"
        assert result["checks"]["score_gap_ok"] is False

    def test_human_required_when_auto_approve_disabled(self, tmp_path: Path) -> None:
        _write(tmp_path / "backend" / "main.py", "app")
        config = PlanningConfig(auto_approve_enabled=False)
        result = _evaluate_approval(
            final_plan=_plan(), final_review=_review(9.0),
            project_root=tmp_path, config=config,
        )
        assert result["decision"] == "human_required"

    def test_human_required_when_file_missing(self, tmp_path: Path) -> None:
        """File in files_to_change doesn't exist and isn't in new_files."""
        result = _evaluate_approval(
            final_plan=_plan(), final_review=_review(9.0),
            project_root=tmp_path, config=PlanningConfig(),
        )
        assert result["decision"] == "human_required"
        assert result["checks"]["all_existing_files_found"] is False

    def test_human_required_when_path_type_missing(self, tmp_path: Path) -> None:
        _write(tmp_path / "backend" / "main.py", "app")
        plan = _plan(path_type="")
        result = _evaluate_approval(
            final_plan=plan, final_review=_review(9.0),
            project_root=tmp_path, config=PlanningConfig(),
        )
        assert result["decision"] == "human_required"
        assert result["checks"]["path_type_valid"] is False

    def test_human_required_when_layers_empty(self, tmp_path: Path) -> None:
        _write(tmp_path / "backend" / "main.py", "app")
        plan = _plan(layers=[])
        result = _evaluate_approval(
            final_plan=plan, final_review=_review(9.0),
            project_root=tmp_path, config=PlanningConfig(),
        )
        assert result["decision"] == "human_required"
        assert result["checks"]["layers_present"] is False

    def test_human_required_when_module_boundaries_no_roots(self, tmp_path: Path) -> None:
        _write(tmp_path / "backend" / "main.py", "app")
        plan = _plan(module_boundaries=[{"name": "x", "surface": "y", "roots": [], "layers": ["service"]}])
        result = _evaluate_approval(
            final_plan=plan, final_review=_review(9.0),
            project_root=tmp_path, config=PlanningConfig(),
        )
        assert result["decision"] == "human_required"
        assert result["checks"]["module_boundaries_roots_present"] is False

    def test_human_required_when_verify_recipes_incomplete(self, tmp_path: Path) -> None:
        _write(tmp_path / "backend" / "main.py", "app")
        plan = _plan(verify_recipes=[{"surface": "rest_api"}])  # missing command and required
        result = _evaluate_approval(
            final_plan=plan, final_review=_review(9.0),
            project_root=tmp_path, config=PlanningConfig(),
        )
        assert result["decision"] == "human_required"
        assert result["checks"]["verify_recipes_complete"] is False

    def test_human_required_when_tasks_empty(self, tmp_path: Path) -> None:
        """Empty task list must not auto-approve — all([]) is True in Python."""
        plan = _plan()
        plan["tasks"] = []  # bypass the `tasks or [_task()]` default in _plan()
        result = _evaluate_approval(
            final_plan=plan, final_review=_review(9.0),
            project_root=tmp_path, config=PlanningConfig(),
        )
        assert result["decision"] == "human_required"
        assert result["checks"]["tasks_non_empty"] is False

    def test_human_required_when_plan_consistency_fails(self, tmp_path: Path) -> None:
        _write(tmp_path / "backend" / "main.py", "app")
        plan = _plan(
            tasks=[
                _task(
                    requires=[{"kind": "field", "name": "social_thread_snapshots.cluster_id"}]
                )
            ]
        )
        result = _evaluate_approval(
            final_plan=plan,
            final_review=_review(9.0),
            project_root=tmp_path,
            config=PlanningConfig(),
        )

        assert result["decision"] == "human_required"
        assert result["checks"]["plan_consistency_ok"] is False
        assert "plan_consistency_issues" in result


# ---------------------------------------------------------------------------
# Task Splitting
# ---------------------------------------------------------------------------

class TestSplitTasks:

    def test_no_split_when_within_limit(self) -> None:
        tasks = [_task(files=["a.py", "b.py", "c.py"])]
        result = _split_tasks_if_needed(tasks)
        assert len(result) == 1
        assert result[0]["task_id"] == "T1"

    def test_splits_when_over_3_files(self) -> None:
        tasks = [_task(files=["a.py", "b.py", "c.py", "d.py"])]
        result = _split_tasks_if_needed(tasks)
        assert len(result) == 2
        assert result[0]["task_id"] == "T1a"
        assert result[1]["task_id"] == "T1b"
        assert len(result[0]["files_to_change"]) == 3
        assert len(result[1]["files_to_change"]) == 1

    def test_split_preserves_dependency_chain(self) -> None:
        tasks = [_task("T1", files=["a.py", "b.py", "c.py", "d.py"])]
        result = _split_tasks_if_needed(tasks)
        assert result[1]["depends_on"] == ["T1a"]

    def test_split_new_files_stay_in_correct_chunk(self) -> None:
        tasks = [_task(files=["a.py", "b.py", "c.py", "d.py"], new_files=["d.py"])]
        result = _split_tasks_if_needed(tasks)
        assert "d.py" in result[1].get("new_files", [])
        assert "d.py" not in result[0].get("new_files", [])

    def test_downstream_deps_remap_to_last_chunk(self) -> None:
        tasks = [
            _task("T1", files=["a.py", "b.py", "c.py", "d.py"]),
            _task("T2", files=["e.py"], depends_on=["T1"]),
        ]
        result = _split_tasks_if_needed(tasks)
        t2 = next(t for t in result if t["task_id"] == "T2")
        assert "T1b" in t2["depends_on"]


# ---------------------------------------------------------------------------
# Escalation
# ---------------------------------------------------------------------------

class TestBuildEscalation:

    def test_collects_unresolved_findings(self) -> None:
        rounds = [
            PlanningRound(
                round_number=1,
                plan_payload={},
                review_payload={"findings": [{"severity": "blocking", "category": "scope", "description": "wrong file"}]},
                review_error="",
                structural_issues=[],
                blocking_findings_count=1,
                timestamp="",
            ),
            PlanningRound(
                round_number=2,
                plan_payload={},
                review_payload={"findings": [{"severity": "blocking", "category": "scope", "description": "wrong file"}]},
                review_error="",
                structural_issues=[],
                blocking_findings_count=1,
                timestamp="",
            ),
        ]
        esc = _build_escalation(rounds)
        assert len(esc["unresolved_findings"]) >= 1
        assert "suggested_human_questions" in esc

    def test_uses_latest_round_for_unresolved_findings(self) -> None:
        rounds = [
            PlanningRound(
                round_number=1,
                plan_payload={},
                review_payload={"findings": [{"severity": "blocking", "category": "scope", "description": "wrong file"}]},
                review_error="",
                structural_issues=[],
                blocking_findings_count=1,
                timestamp="",
            ),
            PlanningRound(
                round_number=2,
                plan_payload={},
                review_payload={"findings": [{"severity": "high", "category": "tests", "description": "missing route test"}]},
                review_error="",
                structural_issues=[],
                blocking_findings_count=0,
                timestamp="",
            ),
        ]
        esc = _build_escalation(rounds, threshold=None)
        descriptions = [item["description"] for item in esc["unresolved_findings"]]
        assert descriptions == ["missing route test"]

    def test_default_scope_is_unscoped(self) -> None:
        """Fix C: every escalation has a ``scope`` field. Default is
        ``unscoped`` for callers that haven't classified the blocker."""
        rounds = [
            PlanningRound(
                round_number=1,
                plan_payload={},
                review_payload={"findings": [{"severity": "blocking", "category": "x"}]},
                review_error="",
                structural_issues=[],
                blocking_findings_count=1,
                timestamp="",
            )
        ]

        esc = _build_escalation(rounds)

        assert esc["scope"] == "unscoped"

    def test_explicit_scope_is_preserved(self) -> None:
        rounds = [
            PlanningRound(
                round_number=1,
                plan_payload={},
                review_payload={"findings": [{"severity": "blocking", "category": "x"}]},
                review_error="",
                structural_issues=[],
                blocking_findings_count=1,
                timestamp="",
            )
        ]

        esc = _build_escalation(rounds, scope="active")

        assert esc["scope"] == "active"

    def test_scope_does_not_leak_into_error_code(self) -> None:
        """Fix C contract: ``scope`` is metadata; ``error_code`` on
        ``.planning_failure.json`` is still computed from
        ``termination_reason``/``gate_reason``/``reason``. self_repair
        classifier expects this field to remain stable across the fix."""
        rounds = [
            PlanningRound(
                round_number=1,
                plan_payload={},
                review_payload={"findings": [{"severity": "blocking", "category": "x"}]},
                review_error="",
                structural_issues=[],
                blocking_findings_count=1,
                timestamp="",
            )
        ]

        esc = _build_escalation(rounds, scope="active", termination_reason="planner_reviewer_deadlock")

        assert esc["scope"] == "active"
        assert esc["termination_reason"] == "planner_reviewer_deadlock"
        # ``scope`` must not be referenced as an error_code source.
        assert "error_code" not in esc

    def test_classifies_architecture_conflict_and_exposes_positions(self) -> None:
        rounds = [
            PlanningRound(
                round_number=1,
                plan_payload={"summary": "Move logic into a dedicated service module."},
                review_payload={
                    "assessment": "Current module boundary conflicts with layering.",
                    "findings": [
                        {
                            "severity": "blocking",
                            "category": "architecture",
                            "description": "module boundary conflict in service layer",
                            "recommendation": "align boundary roots with the service module",
                        }
                    ],
                },
                review_error="",
                structural_issues=[],
                blocking_findings_count=1,
                timestamp="",
            )
        ]
        esc = _build_escalation(rounds)
        assert esc["conflict_category"] == "architecture"
        assert esc["planner_position"] == "Move logic into a dedicated service module."
        assert "module boundary" in esc["reviewer_position"].lower()


# ---------------------------------------------------------------------------
# Task Graph Generation
# ---------------------------------------------------------------------------

class TestPlanToTaskGraph:

    def test_generates_valid_graph(self, tmp_path: Path) -> None:
        _write(tmp_path / "backend" / "main.py", "app")
        plan = _plan()
        graph = plan_to_task_graph(
            plan, feature="test-feature",
            repo_inventory={"archetype": "fastapi_api"},
            project_root=tmp_path,
        )
        assert graph["schema_version"] == "contract_first.task_graph.v1"
        assert len(graph["tasks"]) == 1
        assert graph["tasks"][0]["task_id"] == "T1"

    def test_graph_preserves_rich_task_contract_fields(self, tmp_path: Path) -> None:
        _write(tmp_path / "backend" / "main.py", "app")
        _write(tmp_path / "tests" / "test_api.py", "def test_ok():\n    assert True\n")
        plan = _plan(
            tasks=[
                _task(
                    files=["backend/main.py", "tests/test_api.py"],
                    new_files=["tests/test_api.py"],
                    target_symbols=[{"file": "backend/main.py", "kind": "function", "name": "handler"}],
                    read_only_symbols=[{"file": "backend/main.py", "kind": "function", "name": "_helper"}],
                    do_not_change=["db query order"],
                    read_only_files=["backend/db_schema.py"],
                    behavior_changes=[{"id": "display_count", "from": "5", "to": "4", "scope": "display only"}],
                    allowed_test_mutations=[
                        {
                            "file": "tests/test_api.py",
                            "match_kind": "literal_assert",
                            "old_pattern": "assert len(items) == 5",
                            "new_pattern": "assert len(items) == 4",
                            "behavior_change_id": "display_count",
                        }
                    ],
                    related_existing_tests=["tests/test_existing.py"],
                    review_focus=["confirm ranking stays unchanged"],
                    provides=[{"kind": "field", "name": "events.display_count"}],
                    requires=[{"kind": "field", "name": "events.id", "source": "existing"}],
                    api_contracts=[
                        {
                            "method": "GET",
                            "endpoint": "/events/{id}",
                            "response_shape": {"display_count": "number"},
                        }
                    ],
                    freshness={"scouted_at_commit": "abc123"},
                )
            ]
        )
        graph = plan_to_task_graph(
            plan,
            feature="test-feature",
            repo_inventory={"archetype": "fastapi_api"},
            project_root=tmp_path,
        )
        task = graph["tasks"][0]
        assert task["new_files"] == ["tests/test_api.py"]
        assert task["target_symbols"][0]["name"] == "handler"
        assert task["read_only_symbols"][0]["name"] == "_helper"
        assert task["read_only_files"] == ["backend/db_schema.py"]
        assert task["behavior_changes"][0]["id"] == "display_count"
        assert task["allowed_test_mutations"][0]["match_kind"] == "literal_assert"
        assert task["provides"][0]["name"] == "events.display_count"
        assert task["requires"][0]["source"] == "existing"
        assert task["api_contracts"][0]["endpoint"] == "/events/{id}"
        assert task["freshness"]["scouted_at_commit"] == "abc123"

    def test_graph_and_card_allow_verification_only_empty_scope(self, tmp_path: Path) -> None:
        _write(tmp_path / "tests" / "test_existing.py", "def test_ok():\n    assert True\n")
        task = _task(
            test_plan="python -m pytest tests/test_existing.py -q",
            verify_cmd="python -m pytest tests/test_existing.py -q",
            execution_constraints={
                "verification_only_noop": True,
                "executor_must_not_edit": True,
            },
            related_existing_tests=["tests/test_existing.py"],
            read_only_files=["tests/test_existing.py"],
            do_not_change=["tests/test_existing.py"],
        )
        task["files_to_change"] = []
        task["new_files"] = []
        plan = _plan(tasks=[task])

        graph = plan_to_task_graph(
            plan,
            feature="test-feature",
            repo_inventory={"archetype": "fastapi_api"},
            project_root=tmp_path,
        )
        cards = plan_to_task_cards(plan, graph)

        assert graph["tasks"][0]["core_files"] == []
        assert graph["tasks"][0]["execution_constraints"]["verification_only_noop"] is True
        assert validate_task_graph(graph) == []
        assert cards[0]["files_to_change"] == []
        assert cards[0]["execution_constraints"]["executor_must_not_edit"] is True
        assert validate_task_card(cards[0]) == []

    def test_graph_allows_downstream_reference_to_upstream_new_file(self, tmp_path: Path) -> None:
        plan = _plan(
            tasks=[
                _task("T1", files=["generated.py"], new_files=["generated.py"]),
                _task("T2", files=["generated.py"], depends_on=["T1"]),
            ]
        )
        graph = plan_to_task_graph(
            plan,
            feature="test-feature",
            repo_inventory={"archetype": "fastapi_api"},
            project_root=tmp_path,
        )
        assert graph["executability"]["status"] == "PASS"
        assert graph["tasks"][1]["executability"]["status"] == "PASS"

    def test_graph_rejects_reference_to_new_file_from_later_task(self, tmp_path: Path) -> None:
        plan = _plan(
            tasks=[
                _task("T1", files=["generated.py"]),
                _task("T2", files=["generated.py"], new_files=["generated.py"], depends_on=["T1"]),
            ]
        )
        graph = plan_to_task_graph(
            plan,
            feature="test-feature",
            repo_inventory={"archetype": "fastapi_api"},
            project_root=tmp_path,
        )
        assert graph["executability"]["status"] == "FAIL"
        assert any("generated.py" in issue for issue in graph["executability"]["issues"])

    def test_graph_serializes_parallel_write_conflicts(self, tmp_path: Path) -> None:
        _write(tmp_path / "backend" / "shared.py", "VALUE = 1\n")
        plan = _plan(
            tasks=[
                _task("T1", files=["backend/shared.py"]),
                _task("T2", files=["backend/shared.py"]),
            ]
        )

        graph = plan_to_task_graph(
            plan,
            feature="test-feature",
            repo_inventory={"archetype": "fastapi_api"},
            project_root=tmp_path,
        )

        assert graph["tasks"][1]["depends_on"] == ["T1"]
        assert graph["taskgraph_resolution_log"][0]["depends_on_added"] == "T1"

    def test_planning_conversation_serializes_parallel_write_conflicts(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        _patch_planning_context(monkeypatch)
        _write(tmp_path / "backend" / "shared.py", "VALUE = 1\n")
        plan = _plan(
            tasks=[
                _task("T1", files=["backend/shared.py"]),
                _task("T2", files=["backend/shared.py"]),
            ]
        )

        monkeypatch.setattr(planning_orchestrator, "generate_plan", lambda **_kwargs: (plan, ""))
        monkeypatch.setattr(planning_orchestrator, "review_plan", lambda **_kwargs: (_review(), ""))

        result = planning_orchestrator.run_planning_conversation(
            config=PlanningConfig(max_rounds=1),
            project_root=tmp_path,
            planning_dir=tmp_path / "planning" / "feature",
            task_direction="Plan shared file changes",
            repo_inventory={"archetype": "test"},
            feature="feature",
        )

        assert result.status == "approved"
        assert result.final_plan["tasks"][1]["depends_on"] == ["T1"]
        assert result.final_plan["taskgraph_resolution_log"][0]["resolution"] == "serialized_parallel_write_conflict"


class TestPlanToTaskCards:

    def test_generates_valid_cards(self, tmp_path: Path) -> None:
        _write(tmp_path / "backend" / "main.py", "app")
        plan = _plan()
        graph = plan_to_task_graph(
            plan, feature="test",
            repo_inventory={"archetype": "fastapi_api"},
            project_root=tmp_path,
        )
        cards = plan_to_task_cards(plan, graph)
        assert len(cards) == 1
        assert cards[0]["task_id"] == "T1"
        assert len(cards[0]["files_to_change"]) <= 3
        assert len(cards[0]["invariants"]) <= 5


# ---------------------------------------------------------------------------
# Planning Loop
# ---------------------------------------------------------------------------

class TestRunPlanningConversation:

    def test_run_round_passes_previous_plan_and_blocks_silent_rewrite(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        _write(tmp_path / "backend" / "main.py", "app")
        previous_plan = _plan(tasks=[_task(approach="old approach")])
        current_plan = _plan(tasks=[_task(approach="new approach")])
        seen: dict[str, Any] = {}

        def fake_generate_plan(**kwargs: Any) -> tuple[dict[str, Any], str]:
            seen["previous_plan"] = kwargs.get("previous_plan")
            return current_plan, ""

        def fake_review_plan(**kwargs: Any) -> tuple[dict[str, Any], str]:
            seen["structural_issues"] = list(kwargs.get("structural_issues") or [])
            seen["project_root"] = kwargs.get("project_root")
            return _review(), ""

        monkeypatch.setattr(planning_orchestrator, "generate_plan", fake_generate_plan)
        monkeypatch.setattr(planning_orchestrator, "review_plan", fake_review_plan)

        round_item = planning_orchestrator._run_round(
            config=PlanningConfig(),
            task_direction="Fix plan",
            context_text="context",
            context_budget=0,
            project_root=tmp_path,
            file_manifest={},
            previous_findings=[],
            previous_plan=previous_plan,
            round_number=2,
        )

        assert seen["previous_plan"] == previous_plan
        assert any("change_log missing modified task T1" in item for item in round_item.structural_issues)
        assert seen["structural_issues"] == round_item.structural_issues
        assert seen["project_root"] == tmp_path

    def test_run_round_applies_deterministic_repairs_before_review(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        _write(tmp_path / "backend" / "main.py", "app")
        _write(tmp_path / "tests" / "test_api.py", "def test_ok():\n    assert True\n")
        previous_plan = _plan(
            tasks=[
                _task("T1", files=["backend/main.py"]),
                _task("T2", files=["backend/main.py"], invariants=["old"]),
            ]
        )
        current_plan = _plan(
            tasks=[
                _task("T1", files=["backend/main.py"]),
                _task("T2", files=["backend/main.py"], invariants=["a", "b", "c", "d", "e", "f"]),
            ],
            change_log=[
                {
                    "task_id": "T2",
                    "fields": ["depends_on", "invariants"],
                    "reason": "Fix the second task deterministically.",
                }
            ],
        )
        seen: dict[str, Any] = {}

        def fake_generate_plan(**_kwargs: Any) -> tuple[dict[str, Any], str]:
            return current_plan, ""

        def fake_review_plan(**kwargs: Any) -> tuple[dict[str, Any], str]:
            seen["plan_payload"] = dict(kwargs.get("plan_payload") or {})
            seen["structural_issues"] = list(kwargs.get("structural_issues") or [])
            return _review(), ""

        monkeypatch.setattr(planning_orchestrator, "generate_plan", fake_generate_plan)
        monkeypatch.setattr(planning_orchestrator, "review_plan", fake_review_plan)

        round_item = planning_orchestrator._run_round(
            config=PlanningConfig(),
            task_direction="Fix plan",
            context_text="context",
            context_budget=0,
            project_root=tmp_path,
            file_manifest={},
            previous_findings=[{"severity": "blocking", "description": "T1 needs a narrower plan."}],
            previous_plan=previous_plan,
            round_number=2,
        )

        repaired_task = seen["plan_payload"]["tasks"][1]
        assert repaired_task["depends_on"] == ["T1"]
        assert repaired_task["invariants"] == ["a", "b", "c", "d", "e"]
        assert seen["structural_issues"] == []
        assert [item["rule"] for item in round_item.deterministic_repairs] == [
            "truncate_invariants",
            "change_log_known_task_ref",
            "serialize_parallel_file_conflicts",
        ]
        assert round_item.plan_payload["plan_revision_log"] == round_item.deterministic_repairs

    def test_successful_planning_ingests_deterministic_repair_prompt_lessons(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        _patch_planning_context(monkeypatch)
        _write(tmp_path / "backend" / "main.py", "app")
        _write(tmp_path / "tests" / "test_api.py", "def test_ok():\n    assert True\n")
        plan = _plan(tasks=[_task(invariants=["a", "b", "c", "d", "e", "f"])])

        monkeypatch.setattr(planning_orchestrator, "generate_plan", lambda **_kwargs: (plan, ""))
        monkeypatch.setattr(planning_orchestrator, "review_plan", lambda **_kwargs: (_review(), ""))

        first = planning_orchestrator.run_planning_conversation(
            config=PlanningConfig(max_rounds=1, planner_model="mimo-v2.5-pro"),
            project_root=tmp_path,
            planning_dir=tmp_path / "planning" / "feature",
            task_direction="Fix plan",
            repo_inventory={},
            feature="feature",
        )
        second = planning_orchestrator.run_planning_conversation(
            config=PlanningConfig(max_rounds=1, planner_model="mimo-v2.5-pro"),
            project_root=tmp_path,
            planning_dir=tmp_path / "planning" / "feature",
            task_direction="Fix plan",
            repo_inventory={},
            feature="feature",
        )

        assert first.prompt_lesson_learning["processed"] == 1
        assert second.prompt_lesson_learning["promoted"] == 1
        lessons = PromptLessonStore(tmp_path).load().learned_prompt_lessons
        assert [lesson.template_id for lesson in lessons] == ["planner.limit_invariants"]

    def test_round_two_receives_all_findings_not_just_blocking(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        _patch_planning_context(monkeypatch)
        seen_previous_findings: list[list[dict[str, Any]]] = []

        high_finding = {
            "severity": "high",
            "category": "scope",
            "description": "missing route coverage",
            "recommendation": "add route coverage to the plan",
        }

        def fake_run_round(
            *,
            previous_findings: list[dict[str, Any]] | None,
            round_number: int,
            **kwargs: Any,
        ) -> PlanningRound:
            seen_previous_findings.append([dict(item) for item in list(previous_findings or [])])
            if round_number == 1:
                return PlanningRound(
                    round_number=1,
                    plan_payload=_plan(),
                    review_payload=_review(findings=[high_finding]),
                    review_error="",
                    structural_issues=["missing task split"],
                    blocking_findings_count=1,
                    timestamp="2026-04-24T00:00:00+00:00",
                )
            return PlanningRound(
                round_number=2,
                plan_payload=_plan(),
                review_payload=_review(),
                review_error="",
                structural_issues=[],
                blocking_findings_count=0,
                timestamp="2026-04-24T00:00:01+00:00",
            )

        monkeypatch.setattr(planning_orchestrator, "_run_round", fake_run_round)

        result = planning_orchestrator.run_planning_conversation(
            config=PlanningConfig(
                max_rounds=2,
                blocking_severities=frozenset({"blocking"}),
                decision_policy="strict-gate",
            ),
            project_root=tmp_path,
            planning_dir=tmp_path / "planning" / "feature",
            task_direction="Fix planning feedback loop",
            repo_inventory={"archetype": "fastapi_api"},
            feature="feature",
        )

        assert result.status == "approved"
        assert seen_previous_findings[0] == []
        assert any(item.get("severity") == "high" for item in seen_previous_findings[1])
        assert any(
            item.get("category") == "structure" and item.get("description") == "missing task split"
            for item in seen_previous_findings[1]
        )

    def test_stubborn_rounds_trigger_early_escalation(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        _patch_planning_context(monkeypatch)
        call_rounds: list[int] = []

        def fake_run_round(
            *,
            round_number: int,
            **kwargs: Any,
        ) -> PlanningRound:
            call_rounds.append(round_number)
            return PlanningRound(
                round_number=round_number,
                plan_payload=_plan(),
                review_payload=_review(
                    findings=[
                        {
                            "severity": "blocking",
                            "category": "scope",
                            "description": "wrong file set",
                            "recommendation": "narrow files_to_change",
                        }
                    ]
                ),
                review_error="",
                structural_issues=[],
                blocking_findings_count=1,
                timestamp=f"2026-04-24T00:00:0{round_number}+00:00",
            )

        monkeypatch.setattr(planning_orchestrator, "_run_round", fake_run_round)

        result = planning_orchestrator.run_planning_conversation(
            config=PlanningConfig(max_rounds=5, decision_policy="strict-gate"),
            project_root=tmp_path,
            planning_dir=tmp_path / "planning" / "feature",
            task_direction="Fix repeated planning stubbornness",
            repo_inventory={"archetype": "fastapi_api"},
            feature="feature",
        )

        assert call_rounds == [1, 2, 3]
        assert len(result.rounds) == 3
        assert result.status == "escalation_required"
        assert result.escalation is not None
        assert result.escalation["stubborn_rounds"] == 2
        assert result.escalation["termination_reason"] == "stubborn_round_limit"

    def test_precondition_blocked_stops_before_reviewer_and_writes_artifacts(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        _patch_planning_context(monkeypatch)
        _write(
            tmp_path / "backend" / "db" / "schema.sql",
            """
            CREATE TABLE events (
              event_id TEXT PRIMARY KEY
            );
            """,
        )
        plan = _plan(
            tasks=[
                _task(
                    task_id="T108",
                    files=["backend/api/v1/services/channel_upgrade_scorer.py"],
                    new_files=["backend/api/v1/services/channel_upgrade_scorer.py"],
                    requires=[
                        {"kind": "field", "name": "events.event_id", "source": "existing"},
                        {"kind": "field", "name": "events.user_interest_align", "source": "existing"},
                    ],
                )
            ]
        )

        monkeypatch.setattr(planning_orchestrator, "generate_plan", lambda **_kwargs: (plan, ""))

        def fail_review(**_kwargs: Any) -> tuple[dict[str, Any] | None, str]:
            raise AssertionError("reviewer should not run when readiness blocks deterministically")

        monkeypatch.setattr(planning_orchestrator, "review_plan", fail_review)

        planning_dir = tmp_path / "planning" / "feature"
        result = planning_orchestrator.run_planning_conversation(
            config=PlanningConfig(max_rounds=7, decision_policy="strict-gate"),
            project_root=tmp_path,
            planning_dir=planning_dir,
            task_direction="Implement scorer that requires an existing events field",
            repo_inventory={"archetype": "fastapi_api"},
            feature="feature",
        )

        # New contract: planner gets one more round to insert prereq tasks
        # after a readiness BLOCK. Only after the planner stays stubborn
        # (same missing preconditions twice in a row) does the loop break.
        assert result.status == "precondition_blocked"
        assert len(result.rounds) == 2
        assert result.rounds[0].planning_readiness["status"] == "BLOCKED"
        assert result.rounds[1].planning_readiness["status"] == "BLOCKED"
        assert result.planning_readiness["missing_preconditions"] == ["events.user_interest_align"]
        assert result.escalation is not None
        assert result.escalation["gate_reason"] == "blocked_by_precondition"

        readiness = json.loads((planning_dir / ".execution_readiness.json").read_text(encoding="utf-8"))
        assert readiness["status"] == "BLOCKED"
        progress = json.loads((planning_dir / ".planning_in_progress.json").read_text(encoding="utf-8"))
        assert progress["status"] == "precondition_blocked"
        assert progress["round_count"] == 2
        failure = json.loads((planning_dir / ".planning_failure.json").read_text(encoding="utf-8"))
        assert failure["reason"] == "blocked_by_precondition"

    def test_stubborn_detection_robust_to_reviewer_rewording(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Reviewer 改措辞但讲同一件事，仍应触发顽固检测。"""
        _patch_planning_context(monkeypatch)

        # 三轮里 description/recommendation 措辞不同但核心 token (Redux/state)
        # 一致；早期实现做字面比较时这里会误判为"有变化"绕过顽固检测
        wordings = [
            ("Use Redux for global state", "Replace local state with Redux store"),
            ("Adopt Redux to manage global state", "Switch local state to Redux store"),
            ("Apply Redux for handling global state", "Migrate local state into Redux store"),
        ]

        def fake_run_round(*, round_number: int, **_kwargs: Any) -> PlanningRound:
            description, recommendation = wordings[round_number - 1]
            return PlanningRound(
                round_number=round_number,
                plan_payload=_plan(),
                review_payload=_review(
                    findings=[
                        {
                            "severity": "blocking",
                            "category": "architecture",
                            "description": description,
                            "recommendation": recommendation,
                        }
                    ]
                ),
                review_error="",
                structural_issues=[],
                blocking_findings_count=1,
                timestamp=f"2026-04-25T00:00:0{round_number}+00:00",
            )

        monkeypatch.setattr(planning_orchestrator, "_run_round", fake_run_round)

        result = planning_orchestrator.run_planning_conversation(
            config=PlanningConfig(max_rounds=5, decision_policy="strict-gate"),
            project_root=tmp_path,
            planning_dir=tmp_path / "planning" / "feature",
            task_direction="Detect stubborn rewording",
            repo_inventory={"archetype": "fastapi_api"},
            feature="feature",
        )

        assert result.status == "escalation_required"
        assert result.escalation is not None
        assert result.escalation["stubborn_rounds"] == 2
        assert result.escalation["termination_reason"] == "stubborn_round_limit"

    def test_repeated_blocker_theme_deadlocks_even_when_plan_changes(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        _patch_planning_context(monkeypatch)
        planning_dir = tmp_path / "planning" / "feature"

        def fake_run_round(*, round_number: int, **_kwargs: Any) -> PlanningRound:
            plan = _plan(
                summary=f"test plan revision {round_number}",
                tasks=[
                    _task(
                        invariants=[
                            "no regression",
                            f"revision marker {round_number}",
                        ]
                    )
                ],
            )
            return PlanningRound(
                round_number=round_number,
                plan_payload=plan,
                review_payload=_review(
                    findings=[
                        {
                            "severity": "blocking",
                            "category": "scope",
                            "description": "The social reply ranking plan still omits route-level regression tests",
                            "recommendation": "Add route-level regression tests before execution",
                        }
                    ]
                ),
                review_error="",
                structural_issues=[],
                blocking_findings_count=1,
                timestamp=f"2026-05-05T00:00:0{round_number}+00:00",
            )

        monkeypatch.setattr(planning_orchestrator, "_run_round", fake_run_round)

        result = planning_orchestrator.run_planning_conversation(
            config=PlanningConfig(max_rounds=7, deadlock_streak_limit=2, decision_policy="strict-gate"),
            project_root=tmp_path,
            planning_dir=planning_dir,
            task_direction="Detect repeated reviewer blocker themes",
            repo_inventory={"archetype": "fastapi_api"},
            feature="feature",
        )

        assert result.status == "escalation_required"
        assert result.escalation is not None
        assert result.escalation["termination_reason"] == "planner_reviewer_deadlock"
        assert result.escalation["gate_reason"] == "planner_reviewer_deadlock"
        assert result.escalation["repeated_blocker_rounds"] == 3
        assert len(result.rounds) == 3

        progress = json.loads((planning_dir / ".planning_in_progress.json").read_text(encoding="utf-8"))
        assert progress["terminal_reason"] == "planner_reviewer_deadlock"
        assert progress["rounds"][-1]["blocking_findings"][0]["category"] == "scope"
        failure = json.loads((planning_dir / ".planning_failure.json").read_text(encoding="utf-8"))
        assert failure["error_code"] == "planner_reviewer_deadlock"
        assert failure["rounds"][-1]["blocking_findings"][0]["description"].startswith("The social reply ranking")

    def test_review_evidence_pack_is_injected_into_next_planning_round(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        from kodawari.autopilot.planning.planning_context import render_context_for_prompt as real_render

        _write(tmp_path / "backend" / "main.py", "def route():\n    return {'ok': True}\n")
        captured_contexts: list[dict[str, Any]] = []

        def fake_collect(**_kwargs: Any) -> dict[str, Any]:
            return {
                "task_direction": "Add social reply route coverage",
                "prd_excerpt": "The PRD requires route-level regression coverage for social replies.",
                "repo_manifest": {"files": ["backend/main.py", "tests/test_main.py"]},
                "input_fingerprint": "sha256:test",
            }

        def fake_render(context: dict[str, Any], **kwargs: Any) -> str:
            captured_contexts.append(dict(context))
            return real_render(context, max_chars=0)

        monkeypatch.setattr(planning_orchestrator, "collect_planning_context", fake_collect)
        monkeypatch.setattr(planning_orchestrator, "render_context_for_prompt", fake_render)
        monkeypatch.setattr(
            planning_orchestrator,
            "_planning_context_scout_payload",
            lambda **kwargs: {"enabled": False},
        )

        def fake_generate_plan(*, round_number: int, **_kwargs: Any) -> tuple[dict[str, Any], str]:
            if round_number == 1:
                return _plan(tasks=[_task(files=["backend/main.py"])]), ""
            return (
                _plan(
                    tasks=[
                        _task(
                            files=["backend/main.py", "tests/test_main.py"],
                            new_files=["tests/test_main.py"],
                            related_existing_tests=["tests/test_main.py"],
                        )
                    ],
                    evidence_resolutions=[
                        {
                            "finding_id": "R1F1",
                            "status": "finding_supported",
                            "evidence_refs": ["plan:summary"],
                            "rationale": "Round 1 omitted tests; this revision adds route coverage.",
                        }
                    ],
                    change_log=[
                        {
                            "task_id": "T1",
                            "fields": ["files_to_change", "new_files", "related_existing_tests"],
                            "reason": "address R1F1 missing test coverage evidence",
                        }
                    ],
                ),
                "",
            )

        def fake_review_plan(*, round_number: int, **_kwargs: Any) -> tuple[dict[str, Any], str]:
            if round_number == 1:
                return (
                    _review(
                        approved=False,
                        score=3.0,
                        findings=[
                            {
                                "severity": "blocking",
                                "category": "coverage",
                                "description": "Missing route regression test coverage for social replies",
                                "recommendation": "Add the route test before execution",
                            }
                        ],
                    ),
                    "",
                )
            return _review(approved=True), ""

        monkeypatch.setattr(planning_orchestrator, "generate_plan", fake_generate_plan)
        monkeypatch.setattr(planning_orchestrator, "review_plan", fake_review_plan)

        planning_dir = tmp_path / "planning" / "feature"
        result = planning_orchestrator.run_planning_conversation(
            config=PlanningConfig(max_rounds=3, decision_policy="strict-gate"),
            project_root=tmp_path,
            planning_dir=planning_dir,
            task_direction="Add social reply route coverage",
            repo_inventory={"archetype": "fastapi_api"},
            feature="feature",
        )

        assert result.status == "approved"
        assert result.rounds[0].review_evidence_pack["requests"][0]["finding_id"] == "R1F1"
        assert any(context.get("review_triggered_evidence") for context in captured_contexts)
        artifact = result_to_artifact(result)
        # v2: scout no longer pre-stamps finding_supported; every factual
        # finding records as ``ambiguous`` and is settled by the planner's
        # next-round resolution.
        assert artifact["rounds"][0]["review_evidence_pack"]["requests"][0]["status"] == "ambiguous"

    def test_finding_signature_distinguishes_different_subjects(self) -> None:
        """不同主题的 findings 即使 severity/category 相同也应签名不同。"""
        from kodawari.autopilot.planning.planning_orchestrator import _finding_signature

        sig_a = _finding_signature([
            {
                "severity": "blocking",
                "category": "architecture",
                "description": "Redux store missing",
                "recommendation": "Add Redux provider",
            }
        ])
        sig_b = _finding_signature([
            {
                "severity": "blocking",
                "category": "architecture",
                "description": "GraphQL schema missing",
                "recommendation": "Add GraphQL endpoint",
            }
        ])
        assert sig_a != sig_b

    def test_finding_signature_collides_when_reviewer_pivots_category(self) -> None:
        """Layer B regression: 同一语义抱怨被 reviewer 在不同轮换 category 包装
        （scope correctness → structural_validity → consistency），canonical
        归一应让三轮签名 collide，触发 deadlock streak。
        """
        from kodawari.autopilot.planning.planning_orchestrator import _finding_signature

        owner_surface_finding_round1 = [
            {
                "severity": "blocking",
                "category": "scope correctness",
                "description": (
                    "files_to_change misses the route handler and service "
                    "owner_surface for events social aggregation"
                ),
                "recommendation": "Add the route handler to files_to_change",
            }
        ]
        owner_surface_finding_round2 = [
            {
                "severity": "blocking",
                "category": "structural_validity",
                "description": (
                    "files_to_change misses the route handler and service "
                    "owner_surface for events social aggregation"
                ),
                "recommendation": "Add the route handler to files_to_change",
            }
        ]
        owner_surface_finding_round3 = [
            {
                "severity": "blocking",
                "category": "consistency",
                "description": (
                    "files_to_change misses the route handler and service "
                    "owner_surface for events social aggregation"
                ),
                "recommendation": "Add the route handler to files_to_change",
            }
        ]

        sig_r1 = _finding_signature(owner_surface_finding_round1)
        sig_r2 = _finding_signature(owner_surface_finding_round2)
        sig_r3 = _finding_signature(owner_surface_finding_round3)

        assert sig_r1 == sig_r2 == sig_r3

    def test_finding_signature_payload_keeps_severity_category_tokens_shape(self) -> None:
        """Artifact 契约保护：_signature_payload 写到 escalation
        ['repeated_blocker_signature'] 的形状必须保持
        {severity, category, tokens} 三键 — meta-repair 已经落地的回归测试
        以及下游 self_repair 都依赖这个形状。1.B 只动 category 取值来源，
        形状不能动。
        """
        from kodawari.autopilot.planning.planning_orchestrator import (
            _finding_signature,
            _signature_payload,
        )

        signature = _finding_signature([
            {
                "severity": "blocking",
                "category": "scope correctness",
                "description": "owner_surface missing handler wiring",
                "recommendation": "Wire the handler",
            }
        ])
        payload = _signature_payload(signature)

        assert isinstance(payload, list) and len(payload) == 1
        entry = payload[0]
        assert set(entry.keys()) == {"severity", "category", "tokens"}
        assert entry["severity"] == "blocking"
        # canonical 化后 category 是 owner_surface（scout 4-bucket），
        # 不再是 reviewer 原文 "scope correctness"
        assert entry["category"] == "owner_surface"
        assert isinstance(entry["tokens"], list)

    def test_auto_skip_persists_non_blocking_findings_as_warnings(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        _patch_planning_context(monkeypatch)

        monkeypatch.setattr(
            planning_orchestrator,
            "_run_round",
            lambda **kwargs: PlanningRound(
                round_number=1,
                plan_payload=_plan(),
                review_payload=_review(
                    findings=[
                        {
                            "severity": "high",
                            "category": "scope",
                            "description": "missing route coverage",
                            "recommendation": "add route tests to verify_recipes",
                        }
                    ]
                ),
                review_error="",
                structural_issues=[],
                blocking_findings_count=0,
                timestamp="2026-04-24T00:00:00+00:00",
            ),
        )

        result = planning_orchestrator.run_planning_conversation(
            config=PlanningConfig(
                max_rounds=1,
                blocking_severities=frozenset({"blocking"}),
                decision_policy="auto-skip",
            ),
            project_root=tmp_path,
            planning_dir=tmp_path / "planning" / "feature",
            task_direction="Keep moving but surface reviewer warnings",
            repo_inventory={"archetype": "fastapi_api"},
            feature="feature",
        )

        assert result.status == "auto_skipped"
        assert result.escalation is not None
        assert result.escalation["unresolved_findings"][0]["description"] == "missing route coverage"

    def test_soft_gate_accepts_multiple_high_findings_as_warnings(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        _patch_planning_context(monkeypatch)

        monkeypatch.setattr(
            planning_orchestrator,
            "_run_round",
            lambda **kwargs: PlanningRound(
                round_number=1,
                plan_payload=_plan(),
                review_payload=_review(
                    findings=[
                        {
                            "severity": "high",
                            "category": "scope",
                            "description": "missing route coverage",
                            "recommendation": "add route tests",
                        },
                        {
                            "severity": "high",
                            "category": "boundary",
                            "description": "layer ownership is unclear",
                            "recommendation": "clarify service/route split",
                        },
                    ]
                ),
                review_error="",
                structural_issues=[],
                blocking_findings_count=0,
                timestamp="2026-04-24T00:00:00+00:00",
            ),
        )

        result = planning_orchestrator.run_planning_conversation(
            config=PlanningConfig(
                max_rounds=1,
                blocking_severities=frozenset({"blocking", "critical"}),
                decision_policy="soft-gate",
            ),
            project_root=tmp_path,
            planning_dir=tmp_path / "planning" / "feature",
            task_direction="Standard lane should escalate repeat risks",
            repo_inventory={"archetype": "fastapi_api"},
            feature="feature",
        )

        assert result.status == "auto_skipped"
        assert result.escalation is not None
        assert [item["severity"] for item in result.escalation["unresolved_findings"]] == ["high", "high"]
        task = result.final_plan["tasks"][0]
        assert any("missing route coverage" in item for item in task["review_focus"])
        assert any("layer ownership is unclear" in item for item in task["review_focus"])

    def test_soft_gate_targets_soft_findings_to_mentioned_task(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        _patch_planning_context(monkeypatch)
        plan = _plan(
            tasks=[
                _task(task_id="T_EXT_01", files=["backend/service.py"]),
                _task(task_id="T_EXT_02", files=["backend/route.py"]),
            ]
        )

        monkeypatch.setattr(
            planning_orchestrator,
            "_run_round",
            lambda **kwargs: PlanningRound(
                round_number=1,
                plan_payload=plan,
                review_payload=_review(
                    findings=[
                        {
                            "severity": "high",
                            "category": "api_contract",
                            "description": "T_EXT_02 does not pin the exact 404 and 503 error contract.",
                            "recommendation": "Add route-level assertions before finishing T_EXT_02.",
                        }
                    ]
                ),
                review_error="",
                structural_issues=[],
                blocking_findings_count=0,
                timestamp="2026-04-24T00:00:00+00:00",
            ),
        )

        result = planning_orchestrator.run_planning_conversation(
            config=PlanningConfig(
                max_rounds=1,
                blocking_severities=frozenset({"blocking", "critical"}),
                decision_policy="soft-gate",
            ),
            project_root=tmp_path,
            planning_dir=tmp_path / "planning" / "feature",
            task_direction="Carry soft reviewer findings into execution guidance",
            repo_inventory={"archetype": "fastapi_api"},
            feature="feature",
        )

        first, second = result.final_plan["tasks"]
        assert "review_focus" not in first
        assert any("exact 404 and 503 error contract" in item for item in second["review_focus"])

    def test_best_clean_round_ignores_later_degraded_rounds(
        self,
    ) -> None:
        good = PlanningRound(
            round_number=2,
            plan_payload=_plan(summary="good plan"),
            review_payload=_review(
                findings=[
                    {
                        "severity": "high",
                        "category": "scope",
                        "description": "add route coverage",
                        "recommendation": "keep warning visible",
                    }
                ]
            ),
            review_error="",
            structural_issues=[],
            blocking_findings_count=0,
            timestamp="2026-04-24T00:00:02+00:00",
        )
        degraded = PlanningRound(
            round_number=3,
            plan_payload=_plan(summary="degraded plan"),
            review_payload=_review(),
            review_error="",
            structural_issues=["files_to_change overlaps read_only_files"],
            blocking_findings_count=1,
            timestamp="2026-04-24T00:00:03+00:00",
        )

        selected = planning_orchestrator._best_clean_round(
            [
                PlanningRound(
                    round_number=1,
                    plan_payload=_plan(summary="blocked plan"),
                    review_payload=_review(
                        findings=[
                            {
                                "severity": "blocking",
                                "category": "boundary",
                                "description": "wrong layer",
                                "recommendation": "move task",
                            }
                        ]
                    ),
                    review_error="",
                    structural_issues=[],
                    blocking_findings_count=1,
                    timestamp="2026-04-24T00:00:01+00:00",
                ),
                good,
                degraded,
            ],
            threshold=frozenset({"blocking", "critical"}),
        )

        assert selected is good

    def test_soft_gate_keeps_critical_findings_hard(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        _patch_planning_context(monkeypatch)

        monkeypatch.setattr(
            planning_orchestrator,
            "_run_round",
            lambda **kwargs: PlanningRound(
                round_number=1,
                plan_payload=_plan(),
                review_payload=_review(
                    findings=[
                        {
                            "severity": "critical",
                            "category": "contract",
                            "description": "route contract is impossible to execute",
                            "recommendation": "fix the task graph before execution",
                        }
                    ]
                ),
                review_error="",
                structural_issues=[],
                blocking_findings_count=1,
                timestamp="2026-04-24T00:00:00+00:00",
            ),
        )

        result = planning_orchestrator.run_planning_conversation(
            config=PlanningConfig(
                max_rounds=1,
                blocking_severities=frozenset({"blocking"}),
                decision_policy="soft-gate",
            ),
            project_root=tmp_path,
            planning_dir=tmp_path / "planning" / "feature",
            task_direction="Critical findings must remain hard stops",
            repo_inventory={"archetype": "fastapi_api"},
            feature="feature",
        )

        assert result.status == "escalation_required"
        assert result.escalation is not None
        assert result.escalation["gate_reason"] == "critical_or_blocking_present"

    def test_soft_gate_keeps_security_high_findings_hard(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        _patch_planning_context(monkeypatch)

        monkeypatch.setattr(
            planning_orchestrator,
            "_run_round",
            lambda **kwargs: PlanningRound(
                round_number=1,
                plan_payload=_plan(),
                review_payload=_review(
                    findings=[
                        {
                            "severity": "high",
                            "category": "security",
                            "description": "token leak risk in request logging",
                            "recommendation": "redact credentials before execution",
                        }
                    ]
                ),
                review_error="",
                structural_issues=[],
                blocking_findings_count=1,
                timestamp="2026-04-24T00:00:00+00:00",
            ),
        )

        result = planning_orchestrator.run_planning_conversation(
            config=PlanningConfig(
                max_rounds=1,
                blocking_severities=frozenset({"blocking", "critical"}),
                decision_policy="soft-gate",
            ),
            project_root=tmp_path,
            planning_dir=tmp_path / "planning" / "feature",
            task_direction="Security high findings must remain hard stops",
            repo_inventory={"archetype": "fastapi_api"},
            feature="feature",
        )

        assert result.status == "escalation_required"
        assert result.escalation is not None
        assert result.escalation["gate_reason"] == "high_hard_stop_present"

    def test_soft_gate_keeps_scope_drift_high_findings_hard(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        _patch_planning_context(monkeypatch)

        monkeypatch.setattr(
            planning_orchestrator,
            "_run_round",
            lambda **kwargs: PlanningRound(
                round_number=1,
                plan_payload=_plan(),
                review_payload=_review(
                    findings=[
                        {
                            "severity": "high",
                            "category": "scope",
                            "description": "PATH_OUT_OF_SCOPE would write outside the task scope",
                            "recommendation": "narrow files_to_change before execution",
                        }
                    ]
                ),
                review_error="",
                structural_issues=[],
                blocking_findings_count=1,
                timestamp="2026-04-24T00:00:00+00:00",
            ),
        )

        result = planning_orchestrator.run_planning_conversation(
            config=PlanningConfig(
                max_rounds=1,
                blocking_severities=frozenset({"blocking", "critical"}),
                decision_policy="soft-gate",
            ),
            project_root=tmp_path,
            planning_dir=tmp_path / "planning" / "feature",
            task_direction="Scope drift high findings must remain hard stops",
            repo_inventory={"archetype": "fastapi_api"},
            feature="feature",
        )

        assert result.status == "escalation_required"
        assert result.escalation is not None
        assert result.escalation["gate_reason"] == "high_hard_stop_present"

    def test_soft_gate_warns_and_continues_on_medium_findings(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        _patch_planning_context(monkeypatch)

        monkeypatch.setattr(
            planning_orchestrator,
            "_run_round",
            lambda **kwargs: PlanningRound(
                round_number=1,
                plan_payload=_plan(),
                review_payload=_review(
                    findings=[
                        {
                            "severity": "medium",
                            "category": "tests",
                            "description": "add a narrower regression test",
                            "recommendation": "cover the new edge case",
                        }
                    ]
                ),
                review_error="",
                structural_issues=[],
                blocking_findings_count=0,
                timestamp="2026-04-24T00:00:00+00:00",
            ),
        )

        result = planning_orchestrator.run_planning_conversation(
            config=PlanningConfig(
                max_rounds=1,
                blocking_severities=frozenset({"blocking", "critical"}),
                decision_policy="soft-gate",
            ),
            project_root=tmp_path,
            planning_dir=tmp_path / "planning" / "feature",
            task_direction="Standard lane should keep medium warnings visible",
            repo_inventory={"archetype": "fastapi_api"},
            feature="feature",
        )

        assert result.status == "auto_skipped"
        assert result.escalation is not None
        assert result.escalation["unresolved_findings"][0]["severity"] == "medium"


# ---------------------------------------------------------------------------
# Artifact Output
# ---------------------------------------------------------------------------

class TestResultToArtifact:

    def test_all_required_fields_present(self) -> None:
        result = PlanningResult(
            status="approved",
            task_direction="fix bug",
            rounds=[],
            final_plan=_plan(),
            final_review=_review(),
            approval={"decision": "auto_approve", "reason": "ok", "checks": {}},
            escalation=None,
            business_outcome="fix translation",
            out_of_scope=["crawler"],
            source_of_truth=["backend/main.py"],
            source_of_truth_canonical=["backend/main.py"],
            path_type="write",
            layers=["service"],
            coverage_hints=["layer:service"],
            module_boundaries=[{"name": "core", "roots": ["backend/"]}],
            verify_recipes=[{"surface": "rest_api", "command": "pytest", "required": True, "roots": ["tests/"]}],
            approval_points=[],
            execution_constraints={},
            confidence="high",
            confidence_issues=[],
            archetype="fastapi_api",
            capabilities=["worker_scheduler"],
            input_fingerprint="sha256:abc",
            context_scout={"enabled": True, "decision": {"status": "READY"}},
        )
        artifact = result_to_artifact(result)
        assert artifact["schema_version"] == "planning.conversation.v1"
        # All v4 required fields
        for key in [
            "business_outcome", "out_of_scope", "source_of_truth",
            "source_of_truth_canonical", "path_type", "layers",
            "coverage_hints", "module_boundaries", "verify_recipes",
            "archetype", "capabilities", "confidence", "confidence_issues",
            "status", "rounds", "final_plan", "final_review",
            "approval", "escalation", "input_fingerprint", "task_direction",
            "context_scout",
        ]:
            assert key in artifact, f"Missing required field: {key}"
        assert artifact["context_scout"]["decision"]["status"] == "READY"


class TestAmbiguousEvidenceStreak:
    """v2: ``planning_evidence_blocked`` is no longer triggered by a single
    ``needs_human_decision`` resolution — that path was unreachable for
    legitimate plan revisions. It now fires only when the planner reports
    the same set of ``ambiguous`` evidence_resolutions for at least
    ``AMBIGUOUS_STREAK_LIMIT`` consecutive rounds."""

    def test_signature_extracts_ambiguous_finding_ids(self) -> None:
        plan = {
            "evidence_resolutions": [
                {"finding_id": "R1F1", "status": "ambiguous"},
                {"finding_id": "R1F2", "status": "finding_refuted"},
                {"finding_id": "R1F3", "status": "ambiguous"},
                {"finding_id": "R1F4", "status": "finding_supported"},
            ]
        }

        signature = planning_orchestrator._ambiguous_resolution_signature(plan)

        assert signature == ("R1F1", "R1F3")

    def test_signature_empty_when_no_ambiguous(self) -> None:
        plan = {
            "evidence_resolutions": [
                {"finding_id": "R1F1", "status": "finding_refuted"},
                {"finding_id": "R1F2", "status": "finding_supported"},
            ]
        }

        assert planning_orchestrator._ambiguous_resolution_signature(plan) == ()

    def test_signature_ignores_unknown_status(self) -> None:
        """Anything other than ``ambiguous`` is excluded from the
        signature — including the legacy ``needs_human_decision`` value
        that used to short-circuit the run."""
        plan = {
            "evidence_resolutions": [
                {"finding_id": "R1F1", "status": "needs_human_decision"},
                {"finding_id": "R1F2", "status": "ambiguous"},
            ]
        }

        assert planning_orchestrator._ambiguous_resolution_signature(plan) == ("R1F2",)

    def test_streak_limit_is_two(self) -> None:
        """Two consecutive identical ambiguous signatures is the minimum
        threshold that distinguishes ``planner needs another pass`` from
        ``planner is stuck``."""
        assert planning_orchestrator.AMBIGUOUS_STREAK_LIMIT == 2

    def test_any_ambiguous_streak_limit_is_three(self) -> None:
        assert planning_orchestrator.AMBIGUOUS_ANY_STREAK_LIMIT == 3

    def test_signature_handles_missing_evidence_resolutions(self) -> None:
        assert planning_orchestrator._ambiguous_resolution_signature({}) == ()
        assert planning_orchestrator._ambiguous_resolution_signature({"evidence_resolutions": None}) == ()


class TestEvidenceLoopRegressionScenarios:
    """End-to-end ``run_planning_conversation`` scenarios that reproduce
    the historical evidence-loop death scenarios. These hit the full
    orchestrator + scout + validator + context pipeline with mocked
    planner/reviewer so we can prove the v2 changes break the loop."""

    @staticmethod
    def _wire_mocks(
        monkeypatch: pytest.MonkeyPatch,
        *,
        generate_plan,
        review_plan,
    ) -> None:
        from kodawari.autopilot.planning.planning_context import render_context_for_prompt as real_render

        def fake_collect(**_kwargs: Any) -> dict[str, Any]:
            return {
                "task_direction": "Add social reply route coverage",
                "prd_excerpt": "The PRD defines Event objects as the canonical ranking atom for social replies.",
                "repo_manifest": {
                    "files": [
                        "backend/api/v1/services/social_event_service.py",
                        "backend/api/v1/services/social_post_service.py",
                        "tests/test_social_event.py",
                    ]
                },
                "input_fingerprint": "sha256:test",
            }

        def fake_render(context: dict[str, Any], **kwargs: Any) -> str:
            return real_render(context, max_chars=0)

        monkeypatch.setattr(planning_orchestrator, "collect_planning_context", fake_collect)
        monkeypatch.setattr(planning_orchestrator, "render_context_for_prompt", fake_render)
        monkeypatch.setattr(
            planning_orchestrator,
            "_planning_context_scout_payload",
            lambda **kwargs: {"enabled": False},
        )
        monkeypatch.setattr(planning_orchestrator, "generate_plan", generate_plan)
        monkeypatch.setattr(planning_orchestrator, "review_plan", review_plan)

    def test_v1_death_loop_no_longer_terminates_immediately(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """The exact v1 death scenario: a ``product_semantics`` finding
        the scout used to stamp ``needs_human_decision``, which the
        orchestrator turned into an immediate ``planning_evidence_blocked``
        no matter what the planner said. In v2 the scout marks it
        ``ambiguous`` and the planner can close it via plan revision."""
        _write(tmp_path / "backend" / "main.py", "def route():\n    return {'ok': True}\n")

        def fake_generate_plan(*, round_number: int, **_kwargs: Any) -> tuple[dict[str, Any], str]:
            if round_number == 1:
                return _plan(tasks=[_task(files=["backend/main.py"])]), ""
            # Round 2: planner addresses the semantics finding by revising
            # the plan AND citing a ref from the pack to refute the loose
            # claim. v1 forbade refute on a finding_supported request and
            # routed any needs_human_decision into immediate termination.
            return (
                _plan(
                    tasks=[
                        _task(
                            files=["backend/main.py"],
                            related_existing_tests=["tests/test_main.py"],
                            invariants=["preserve canonical Event semantics"],
                        )
                    ],
                    evidence_resolutions=[
                        {
                            "finding_id": "R1F1",
                            "status": "finding_refuted",
                            "evidence_refs": ["plan:summary"],
                            "rationale": "Plan summary anchors the route to the canonical Event service path.",
                        }
                    ],
                    change_log=[
                        {
                            "task_id": "T1",
                            "fields": ["related_existing_tests", "invariants"],
                            "reason": "anchor to canonical Event semantics per R1F1",
                        }
                    ],
                ),
                "",
            )

        # Test fixture path: backend/main.py exists; tests/test_main.py is
        # an existing related test file the planner cites. Write it so the
        # plan validator does not raise a missing-files structural issue
        # (which would mask the evidence-loop regression we are testing).
        _write(tmp_path / "tests" / "test_main.py", "def test_route():\n    assert True\n")

        def fake_review_plan(*, round_number: int, **_kwargs: Any) -> tuple[dict[str, Any], str]:
            if round_number == 1:
                return (
                    _review(
                        approved=False,
                        findings=[
                            {
                                "severity": "blocking",
                                "category": "product",
                                "description": "Event vs post ranking semantics are ambiguous in this plan",
                                "recommendation": "Anchor the plan to the canonical Event service",
                            }
                        ],
                    ),
                    "",
                )
            return _review(approved=True), ""

        self._wire_mocks(
            monkeypatch,
            generate_plan=fake_generate_plan,
            review_plan=fake_review_plan,
        )

        planning_dir = tmp_path / "planning" / "feature"
        result = planning_orchestrator.run_planning_conversation(
            config=PlanningConfig(max_rounds=3, decision_policy="strict-gate"),
            project_root=tmp_path,
            planning_dir=planning_dir,
            task_direction="Add social reply ranking with canonical Event semantics",
            repo_inventory={"archetype": "fastapi_api"},
            feature="feature",
        )

        # v1 would terminate as planning_evidence_blocked at round 1's end
        # (scout stamps needs_human_decision -> orchestrator immediate
        # break). v2: planner closes the request and the run reaches a
        # second round whose review approves.
        assert result.status == "approved", f"expected approved, got {result.status} (escalation={result.escalation})"
        assert (result.escalation or {}).get("gate_reason") != "planning_evidence_blocked"
        round1_pack = result.rounds[0].review_evidence_pack or {}
        assert round1_pack.get("requests"), "round 1 should have produced an evidence pack request"
        assert round1_pack["requests"][0]["status"] == "ambiguous"

    def test_persistent_ambiguous_escalates_only_after_streak_limit(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """When the planner keeps marking the same finding ``ambiguous``
        round after round, v2 escalates as ``planning_evidence_blocked``
        — but only after ``AMBIGUOUS_STREAK_LIMIT`` consecutive rounds
        with the same signature, never on round 1."""
        _write(tmp_path / "backend" / "main.py", "def route():\n    return {'ok': True}\n")

        def fake_generate_plan(*, round_number: int, **_kwargs: Any) -> tuple[dict[str, Any], str]:
            if round_number == 1:
                return _plan(tasks=[_task(files=["backend/main.py"])]), ""
            return (
                _plan(
                    tasks=[_task(files=["backend/main.py"])],
                    evidence_resolutions=[
                        {
                            "finding_id": "R1F1",
                            "status": "ambiguous",
                            "evidence_refs": ["plan:summary"],
                            "rationale": "Cannot determine semantics from current evidence.",
                        }
                    ],
                ),
                "",
            )

        def fake_review_plan(*, round_number: int, **_kwargs: Any) -> tuple[dict[str, Any], str]:
            return (
                _review(
                    approved=False,
                    findings=[
                        {
                            "severity": "blocking",
                            "category": "product",
                            "description": "Event vs post ranking semantics are ambiguous in this plan",
                            "recommendation": "Anchor the plan to the canonical Event service",
                        }
                    ],
                ),
                "",
            )

        self._wire_mocks(
            monkeypatch,
            generate_plan=fake_generate_plan,
            review_plan=fake_review_plan,
        )

        planning_dir = tmp_path / "planning" / "feature-streak"
        result = planning_orchestrator.run_planning_conversation(
            config=PlanningConfig(max_rounds=5, decision_policy="strict-gate"),
            project_root=tmp_path,
            planning_dir=planning_dir,
            task_direction="Repeatedly ambiguous evidence",
            repo_inventory={"archetype": "fastapi_api"},
            feature="feature-streak",
        )

        assert result.status == "escalation_required"
        escalation = result.escalation or {}
        assert escalation.get("gate_reason") == "planning_evidence_blocked"
        assert escalation.get("ambiguous_streak_rounds") == planning_orchestrator.AMBIGUOUS_STREAK_LIMIT
        assert "R1F1" in (escalation.get("ambiguous_evidence_signature") or [])
        # Critical: at least 2 rounds happened — v1 used to terminate
        # after round 1 even though the streak hadn't been established.
        assert len(result.rounds) >= 2

    def test_changing_ambiguous_finding_ids_escalate_after_any_streak_limit(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        _write(tmp_path / "backend" / "main.py", "def route():\n    return {'ok': True}\n")
        _write(tmp_path / "tests" / "test_main.py", "def test_route():\n    assert True\n")

        def fake_generate_plan(*, round_number: int, **_kwargs: Any) -> tuple[dict[str, Any], str]:
            evidence_resolutions: list[dict[str, str]] = []
            if round_number >= 2:
                evidence_resolutions = [
                    {
                        "finding_id": f"R{round_number - 1}F1",
                        "status": "ambiguous",
                        "evidence_refs": ["plan:summary"],
                        "rationale": "Still cannot close this finding from available evidence.",
                    }
                ]
            return (
                _plan(
                    tasks=[_task(files=["backend/main.py"])],
                    evidence_resolutions=evidence_resolutions,
                    change_log=(
                        [
                            {
                                "task_id": "plan",
                                "fields": ["evidence_resolutions"],
                                "reason": f"respond to reviewer evidence in round {round_number}",
                            }
                        ]
                        if round_number > 1
                        else []
                    ),
                ),
                "",
            )

        def fake_review_plan(*, round_number: int, **_kwargs: Any) -> tuple[dict[str, Any], str]:
            return (
                _review(
                    approved=False,
                    findings=[
                        {
                            "severity": "blocking",
                            "category": f"product_{round_number}",
                            "description": f"Ambiguous product evidence in round {round_number}",
                            "recommendation": f"Close reviewer evidence request {round_number}",
                        }
                    ],
                ),
                "",
            )

        self._wire_mocks(monkeypatch, generate_plan=fake_generate_plan, review_plan=fake_review_plan)

        result = planning_orchestrator.run_planning_conversation(
            config=PlanningConfig(max_rounds=5, decision_policy="strict-gate"),
            project_root=tmp_path,
            planning_dir=tmp_path / "planning" / "feature-any-streak",
            task_direction="Repeated ambiguous evidence with changing finding IDs",
            repo_inventory={"archetype": "fastapi_api"},
            feature="feature-any-streak",
        )

        assert result.status == "escalation_required"
        escalation = result.escalation or {}
        assert escalation.get("gate_reason") == "planning_evidence_blocked"
        assert escalation.get("any_ambiguous_streak") == planning_orchestrator.AMBIGUOUS_ANY_STREAK_LIMIT
        assert escalation.get("ambiguous_streak_rounds") is None

    def test_any_ambiguous_streak_resets_after_clean_resolution_round(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setattr(planning_orchestrator, "AMBIGUOUS_ANY_STREAK_LIMIT", 2)
        _write(tmp_path / "backend" / "main.py", "def route():\n    return {'ok': True}\n")
        _write(tmp_path / "tests" / "test_main.py", "def test_route():\n    assert True\n")

        def fake_generate_plan(*, round_number: int, **_kwargs: Any) -> tuple[dict[str, Any], str]:
            evidence_resolutions: list[dict[str, str]] = []
            if round_number in {2, 4}:
                evidence_resolutions = [
                    {
                        "finding_id": f"R{round_number - 1}F1",
                        "status": "ambiguous",
                        "evidence_refs": ["plan:summary"],
                        "rationale": "Evidence is still ambiguous for this round.",
                    }
                ]
            return (
                _plan(
                    tasks=[_task(files=["backend/main.py"])],
                    evidence_resolutions=evidence_resolutions,
                    change_log=(
                        [
                            {
                                "task_id": "plan",
                                "fields": ["evidence_resolutions"],
                                "reason": f"respond to reviewer evidence in round {round_number}",
                            }
                        ]
                        if round_number > 1
                        else []
                    ),
                ),
                "",
            )

        def fake_review_plan(*, round_number: int, **_kwargs: Any) -> tuple[dict[str, Any], str]:
            if round_number == 4:
                return _review(approved=True), ""
            return (
                _review(
                    approved=False,
                    findings=[
                        {
                            "severity": "blocking",
                            "category": f"product_{round_number}",
                            "description": f"Ambiguous product evidence in round {round_number}",
                            "recommendation": f"Close reviewer evidence request {round_number}",
                        }
                    ],
                ),
                "",
            )

        self._wire_mocks(monkeypatch, generate_plan=fake_generate_plan, review_plan=fake_review_plan)

        result = planning_orchestrator.run_planning_conversation(
            config=PlanningConfig(max_rounds=4, decision_policy="strict-gate"),
            project_root=tmp_path,
            planning_dir=tmp_path / "planning" / "feature-any-reset",
            task_direction="Reset ambiguous evidence streak after a clean round",
            repo_inventory={"archetype": "fastapi_api"},
            feature="feature-any-reset",
        )

        assert result.status == "approved"
        assert (result.escalation or {}).get("gate_reason") != "planning_evidence_blocked"

    def test_finding_refuted_with_valid_ref_closes_request_in_one_round(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """``finding_refuted`` with a pack-cited ref closes the request
        — the v1 validator forbade refute when the request was
        pre-stamped ``finding_supported``, leaving no legal answer."""
        _write(tmp_path / "backend" / "main.py", "def route():\n    return {'ok': True}\n")

        def fake_generate_plan(*, round_number: int, **_kwargs: Any) -> tuple[dict[str, Any], str]:
            if round_number == 1:
                return _plan(tasks=[_task(files=["backend/main.py"])]), ""
            return (
                _plan(
                    tasks=[
                        _task(
                            files=["backend/main.py"],
                            related_existing_tests=["tests/test_social_event.py"],
                        )
                    ],
                    evidence_resolutions=[
                        {
                            "finding_id": "R1F1",
                            "status": "finding_refuted",
                            "evidence_refs": ["plan:summary"],
                            "rationale": "Plan summary already references the right surface.",
                        }
                    ],
                    change_log=[
                        {
                            "task_id": "T1",
                            "fields": ["related_existing_tests"],
                            "reason": "Add owner-surface coverage for R1F1",
                        }
                    ],
                ),
                "",
            )

        def fake_review_plan(*, round_number: int, **_kwargs: Any) -> tuple[dict[str, Any], str]:
            if round_number == 1:
                return (
                    _review(
                        approved=False,
                        findings=[
                            {
                                "severity": "blocking",
                                "category": "structure",
                                "description": "Owner surface and files_to_change do not match the handler",
                                "recommendation": "Trace the handler chain to the actual files",
                            }
                        ],
                    ),
                    "",
                )
            return _review(approved=True), ""

        self._wire_mocks(
            monkeypatch,
            generate_plan=fake_generate_plan,
            review_plan=fake_review_plan,
        )

        planning_dir = tmp_path / "planning" / "feature-refute"
        result = planning_orchestrator.run_planning_conversation(
            config=PlanningConfig(max_rounds=3, decision_policy="strict-gate"),
            project_root=tmp_path,
            planning_dir=planning_dir,
            task_direction="Owner surface refutation",
            repo_inventory={"archetype": "fastapi_api"},
            feature="feature-refute",
        )

        assert result.status == "approved"
        # Verify the planner's resolution actually flowed through:
        round2_plan = result.rounds[1].plan_payload
        resolutions = round2_plan.get("evidence_resolutions") or []
        assert resolutions[0]["status"] == "finding_refuted"
        assert resolutions[0]["evidence_refs"]


class TestActiveScopeAutoSkipGate:
    """Fix B: lite auto-skip gate is now scoped to the active task.

    Pre-fix lite runs would auto-skip a plan whose reviewer rejected it on
    blockers about *future* tasks, because the gate only counted blocking
    findings count > 0 → escalate, otherwise → auto-skip. After Fix B:
      * blocking findings about the active task escalate;
      * blocking findings only about future tasks are recorded as
        future-scope debt and the run still auto-skips.
    """

    def _make_plan(self, *, with_future_task: bool = True) -> dict[str, Any]:
        tasks = [_task(task_id="TTS-DEG-02", files=["backend/main.py"])]
        if with_future_task:
            future_task = _task(task_id="TTS-DEG-01", files=["backend/main.py"])
            future_task["depends_on"] = ["TTS-DEG-02"]
            tasks.append(future_task)
        return _plan(tasks=tasks)

    def test_active_scope_blocker_escalates_under_auto_skip(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        plan_payload = self._make_plan()
        monkeypatch.setattr(
            planning_orchestrator,
            "_run_round",
            lambda **kwargs: PlanningRound(
                round_number=1,
                plan_payload=plan_payload,
                review_payload=_review(
                    findings=[
                        {
                            "severity": "blocking",
                            "category": "structure",
                            "description": "TTS-DEG-02 service is missing input validation",
                            "recommendation": "add validation",
                        }
                    ]
                ),
                review_error="",
                structural_issues=[],
                blocking_findings_count=1,
                timestamp="2026-05-06T00:00:00+00:00",
                blocking_findings=[
                    {
                        "severity": "blocking",
                        "category": "structure",
                        "description": "TTS-DEG-02 service is missing input validation",
                    }
                ],
            ),
        )
        _patch_planning_context(monkeypatch)

        planning_dir = tmp_path / "planning" / "feature"
        result = planning_orchestrator.run_planning_conversation(
            config=PlanningConfig(
                max_rounds=1,
                blocking_severities=frozenset({"blocking", "critical"}),
                decision_policy="auto-skip",
            ),
            project_root=tmp_path,
            planning_dir=planning_dir,
            task_direction="active task has a blocker",
            repo_inventory={"archetype": "fastapi_api"},
            feature="feature",
        )

        assert result.status == "escalation_required"
        escalation = result.escalation or {}
        assert escalation["gate_reason"] == "active_scope_blocker_under_auto_skip"
        assert escalation["scope"] == "active"
        assert "TTS-DEG-02" in escalation["active_scope_task_ids"]

    def test_future_scope_blocker_still_auto_skips_and_logs_debt(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        _write(tmp_path / "backend" / "main.py", "def handler():\n    return None\n")
        plan_payload = self._make_plan()
        monkeypatch.setattr(
            planning_orchestrator,
            "_run_round",
            lambda **kwargs: PlanningRound(
                round_number=1,
                plan_payload=plan_payload,
                review_payload=_review(
                    findings=[
                        {
                            "severity": "blocking",
                            "category": "scope",
                            "description": "TTS-DEG-01 route handler should not catch service exceptions",
                            "recommendation": "let exceptions surface to middleware",
                        }
                    ]
                ),
                review_error="",
                structural_issues=[],
                blocking_findings_count=1,
                timestamp="2026-05-06T00:00:00+00:00",
                blocking_findings=[
                    {
                        "severity": "blocking",
                        "category": "scope",
                        "description": "TTS-DEG-01 route handler should not catch service exceptions",
                    }
                ],
            ),
        )
        _patch_planning_context(monkeypatch)

        planning_dir = tmp_path / "planning" / "feature"
        result = planning_orchestrator.run_planning_conversation(
            config=PlanningConfig(
                max_rounds=1,
                blocking_severities=frozenset({"blocking", "critical"}),
                decision_policy="auto-skip",
            ),
            project_root=tmp_path,
            planning_dir=planning_dir,
            task_direction="future task only blocker",
            repo_inventory={"archetype": "fastapi_api"},
            feature="feature",
        )

        assert result.status == "auto_skipped"
        audit = planning_dir / planning_orchestrator.PLANNING_AUDIT_LOG_FILENAME
        assert audit.exists(), "future-scope debt should be recorded"
        rows = json.loads(audit.read_text(encoding="utf-8"))["entries"]
        assert len(rows) == 1
        assert rows[0]["future_scope_blocker_count"] == 1
        assert rows[0]["active_task_ids"] == ["TTS-DEG-02"]
        assert rows[0]["active_scope_source"] == "topological_first_leaf"
        assert result.approval["decision"] == "human_required"
        assert result.approval["scope"] == "full_plan"
        active_view = result.approval["active_scope_view"]
        assert active_view["decision"] == "auto_approve_active_scope"
        assert active_view["active_task_ids"] == ["TTS-DEG-02"]
        assert active_view["future_scope_blocker_count"] == 1
        assert active_view["checks_in_scope"]["test_plan_present"] is True
        assert result.final_review_active_scope is not None
        assert result.final_review_active_scope["approved"] is True
        assert len(result.final_review_active_scope["findings_future_scope"]) == 1
        artifact = result_to_artifact(result)
        assert artifact["approval"]["active_scope_view"]["decision"] == "auto_approve_active_scope"
        assert artifact["final_review_active_scope"]["approved"] is True

    def test_active_scope_view_requires_active_review_projection_to_pass(
        self,
        tmp_path: Path,
    ) -> None:
        _write(tmp_path / "backend" / "main.py", "def handler():\n    return None\n")
        plan_payload = self._make_plan()
        approval, active_review = planning_orchestrator._annotate_active_scope_views(
            approval={"decision": "human_required", "reason": "structural_checks_failed"},
            final_plan=plan_payload,
            final_review=_review(
                findings=[
                    {
                        "severity": "blocking",
                        "category": "scope",
                        "description": "TTS-DEG-02 still lacks the fallback behavior",
                        "recommendation": "finish the active task first",
                    }
                ]
            ),
            status="auto_skipped",
            rounds=[],
            planning_dir=tmp_path / "planning" / "feature",
            project_root=tmp_path,
            threshold=frozenset({"blocking", "critical"}),
            active_scope_outcome={
                "active_task_ids": ["TTS-DEG-02"],
                "scope_task_ids": ["TTS-DEG-02"],
                "source": "topological_first_leaf",
                "active_scope_blockers": [],
                "future_scope_blockers": [],
                "unscoped_blockers": [],
            },
        )

        assert active_review["approved"] is False
        assert approval["scope"] == "full_plan"
        assert "active_scope_view" not in approval

    def test_no_blocker_path_unchanged(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        plan_payload = self._make_plan()
        monkeypatch.setattr(
            planning_orchestrator,
            "_run_round",
            lambda **kwargs: PlanningRound(
                round_number=1,
                plan_payload=plan_payload,
                review_payload=_review(
                    findings=[
                        {
                            "severity": "high",
                            "category": "scope",
                            "description": "missing route coverage",
                            "recommendation": "add route tests",
                        }
                    ]
                ),
                review_error="",
                structural_issues=[],
                blocking_findings_count=0,
                timestamp="2026-05-06T00:00:00+00:00",
            ),
        )
        _patch_planning_context(monkeypatch)

        planning_dir = tmp_path / "planning" / "feature"
        result = planning_orchestrator.run_planning_conversation(
            config=PlanningConfig(
                max_rounds=1,
                blocking_severities=frozenset({"blocking"}),
                decision_policy="auto-skip",
            ),
            project_root=tmp_path,
            planning_dir=planning_dir,
            task_direction="no blockers; only advisory",
            repo_inventory={"archetype": "fastapi_api"},
            feature="feature",
        )

        # No blockers anywhere → auto_skip with no audit log entry.
        assert result.status == "auto_skipped"
        assert not (planning_dir / planning_orchestrator.PLANNING_AUDIT_LOG_FILENAME).exists()

    def test_strict_gate_unchanged_by_active_scope_filter(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """strict-gate (standard / heavy lanes) keeps the existing behavior:
        any blocker — regardless of scope — escalates. Active-scope filter
        only fires on auto-skip path."""
        plan_payload = self._make_plan()
        monkeypatch.setattr(
            planning_orchestrator,
            "_run_round",
            lambda **kwargs: PlanningRound(
                round_number=1,
                plan_payload=plan_payload,
                review_payload=_review(
                    findings=[
                        {
                            "severity": "blocking",
                            "category": "scope",
                            "description": "TTS-DEG-01 future task missing coverage",
                            "recommendation": "add tests",
                        }
                    ]
                ),
                review_error="",
                structural_issues=[],
                blocking_findings_count=1,
                timestamp="2026-05-06T00:00:00+00:00",
                blocking_findings=[
                    {
                        "severity": "blocking",
                        "category": "scope",
                        "description": "TTS-DEG-01 future task missing coverage",
                    }
                ],
            ),
        )
        _patch_planning_context(monkeypatch)

        result = planning_orchestrator.run_planning_conversation(
            config=PlanningConfig(
                max_rounds=1,
                blocking_severities=frozenset({"blocking"}),
                decision_policy="strict-gate",
            ),
            project_root=tmp_path,
            planning_dir=tmp_path / "planning" / "feature",
            task_direction="strict gate",
            repo_inventory={"archetype": "fastapi_api"},
            feature="feature",
        )

        # strict-gate: blocker still escalates regardless of scope.
        assert result.status == "escalation_required"
        # And we did NOT use the active-scope-specific gate_reason
        assert (result.escalation or {}).get("gate_reason") != "active_scope_blocker_under_auto_skip"


def test_input_feasibility_precheck_short_circuits_planning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Layer D: a test-only task targeting a route absent from the repo
    must escalate before any planner round runs, with termination_reason
    ``task_input_infeasible_surface`` so self_repair routes correctly.
    """

    monkeypatch.setattr(
        planning_orchestrator,
        "collect_planning_context",
        lambda **kwargs: {
            "repo_manifest": {
                "files": [
                    "backend/api/v1/routes/detail_routes.py",
                    "backend/api/v1/services/event_repository.py",
                ]
            },
            "input_fingerprint": "sha256:test",
        },
    )
    monkeypatch.setattr(
        planning_orchestrator,
        "render_context_for_prompt",
        lambda context, **_kwargs: "context",
    )
    monkeypatch.setattr(
        planning_orchestrator,
        "build_file_manifest",
        lambda files: {
            "detail_routes.py": ["backend/api/v1/routes/detail_routes.py"],
            "event_repository.py": ["backend/api/v1/services/event_repository.py"],
        },
    )
    monkeypatch.setattr(
        planning_orchestrator,
        "_planning_context_scout_payload",
        lambda **kwargs: {"enabled": False},
    )

    # Sentinel: planner must NOT be called when precheck trips.
    planner_calls = {"count": 0}

    def _trap_planner(**_kwargs):
        planner_calls["count"] += 1
        raise AssertionError("planner should not run when precheck trips")

    monkeypatch.setattr(planning_orchestrator, "_run_round", _trap_planner)

    result = planning_orchestrator.run_planning_conversation(
        config=PlanningConfig(
            planner_executable="noop",
            reviewer_executable="noop",
            planner_driver="noop",
            reviewer_driver="noop",
            max_rounds=3,
        ),
        project_root=tmp_path,
        planning_dir=tmp_path / "planning" / "infeasible-task",
        task_direction=(
            "Add a regression test for /api/v1/events/{id}/social to verify "
            "the event-level social aggregation contract. test-only — do not "
            "modify production."
        ),
        repo_inventory={"archetype": "test"},
        feature="infeasible-task",
    )

    assert result.status == "escalation_required"
    assert planner_calls["count"] == 0
    escalation = result.escalation or {}
    assert escalation.get("termination_reason") == "task_input_infeasible_surface"
    assert escalation.get("gate_reason") == "task_input_infeasible_surface"
    assert "/api/v1/events/{id}/social" in (escalation.get("missing_surfaces") or [])


def test_input_feasibility_precheck_does_not_block_existing_route(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Guard against false-positive: when the repo DOES have a file referencing
    the route subject (e.g. social_thread_service.py), precheck must NOT trip
    and the planner must run normally."""

    monkeypatch.setattr(
        planning_orchestrator,
        "collect_planning_context",
        lambda **kwargs: {
            "repo_manifest": {
                "files": [
                    "backend/api/v1/routes/detail_routes.py",
                    "backend/api/v1/services/social_thread_service.py",
                ]
            },
            "input_fingerprint": "sha256:test",
        },
    )
    monkeypatch.setattr(
        planning_orchestrator, "render_context_for_prompt", lambda context, **_kwargs: "context"
    )
    monkeypatch.setattr(
        planning_orchestrator,
        "build_file_manifest",
        lambda files: {
            "detail_routes.py": ["backend/api/v1/routes/detail_routes.py"],
            "social_thread_service.py": [
                "backend/api/v1/services/social_thread_service.py"
            ],
        },
    )
    monkeypatch.setattr(
        planning_orchestrator,
        "_planning_context_scout_payload",
        lambda **kwargs: {"enabled": False},
    )

    result = planning_orchestrator.run_planning_conversation(
        config=PlanningConfig(
            planner_executable="noop",
            reviewer_executable="noop",
            planner_driver="noop",
            reviewer_driver="noop",
            max_rounds=1,
        ),
        project_root=tmp_path,
        planning_dir=tmp_path / "planning" / "feasible-task",
        task_direction=(
            "Add a regression test for /api/v1/events/{id}/social. test-only."
        ),
        repo_inventory={"archetype": "test"},
        feature="feasible-task",
    )

    # The precheck must NOT have short-circuited — even if the noop driver's
    # resulting plan still escalates for unrelated reasons (file-path
    # validation, etc.), the termination reason must not be the precheck's.
    escalation = result.escalation or {}
    assert escalation.get("termination_reason") != "task_input_infeasible_surface"
    assert escalation.get("gate_reason") != "task_input_infeasible_surface"


# ---------------------------------------------------------------------------
# Phase B: meta-blocker streak demotion (orchestrator integration)
# ---------------------------------------------------------------------------


def _meta_finding(round_number: int) -> dict[str, Any]:
    return {
        "severity": "blocking",
        "category": "plan_consistency",
        "description": (
            f"evidence_resolutions[R{round_number + 4}F1] must include an "
            "evidence_ref about the meta-structural claim itself; "
            "recursive evidence requirement"
        ),
        "recommendation": "Add a self-referential evidence_ref",
    }


def test_meta_blocker_streak_demotion_unsticks_planner_reviewer_loop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Phase B positive path: 3 consecutive rounds where every blocker
    classifies as meta_blocker AND score guardrails pass → orchestrator
    demotes the blockers to info, the strict approval gate accepts the
    plan, and the audit log captures the demotion."""
    _write(tmp_path / "backend" / "main.py", "app")
    _patch_planning_context(monkeypatch)
    call_rounds: list[int] = []

    def fake_run_round(*, round_number: int, **kwargs: Any) -> PlanningRound:
        call_rounds.append(round_number)
        return PlanningRound(
            round_number=round_number,
            plan_payload=_plan(self_assessment={"score": 9.1, "notes": []}),
            review_payload=_review(
                score=8.6,
                approved=False,
                findings=[_meta_finding(round_number)],
            ),
            review_error="",
            structural_issues=[],
            blocking_findings_count=1,
            timestamp=f"2026-05-16T00:00:0{round_number}+00:00",
        )

    monkeypatch.setattr(planning_orchestrator, "_run_round", fake_run_round)

    result = planning_orchestrator.run_planning_conversation(
        config=PlanningConfig(max_rounds=5, decision_policy="strict-gate"),
        project_root=tmp_path,
        planning_dir=tmp_path / "planning" / "meta_streak",
        task_direction="Phase B integration",
        repo_inventory={"archetype": "fastapi_api"},
        feature="meta_streak",
    )

    assert call_rounds == [1, 2, 3]
    assert result.status == "approved"
    assert len(result.meta_blocker_demotion_log) == 1
    log_entry = result.meta_blocker_demotion_log[0]
    assert log_entry["round"] == 3
    assert log_entry["streak"] >= 3
    assert log_entry["demoted_count"] == 1
    assert log_entry["planner_score"] == pytest.approx(9.1)
    assert log_entry["reviewer_score"] == pytest.approx(8.6)
    last_round = result.rounds[-1]
    assert last_round.review_payload is not None
    assert last_round.review_payload["approved"] is True
    assert last_round.review_payload["approved_by_meta_blocker_demotion"] is True
    assert last_round.blocking_findings_count == 0


def test_meta_blocker_streak_does_not_demote_when_real_blocker_coexists(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Phase B negative path: a single real-scope blocker in the same round
    resets the meta_blocker streak. Demotion never fires; the orchestrator
    routes through the normal escalation paths (stubborn / deadlock)."""
    _write(tmp_path / "backend" / "main.py", "app")
    _patch_planning_context(monkeypatch)

    def fake_run_round(*, round_number: int, **kwargs: Any) -> PlanningRound:
        return PlanningRound(
            round_number=round_number,
            plan_payload=_plan(self_assessment={"score": 9.1, "notes": []}),
            review_payload=_review(
                score=8.6,
                approved=False,
                findings=[
                    _meta_finding(round_number),
                    {
                        "severity": "blocking",
                        "category": "scope",
                        "description": (
                            "real owner_surface mismatch on the route handler "
                            "call chain — files_to_change excludes the canonical module"
                        ),
                        "recommendation": "Add the handler module",
                    },
                ],
            ),
            review_error="",
            structural_issues=[],
            blocking_findings_count=2,
            timestamp=f"2026-05-16T00:00:0{round_number}+00:00",
        )

    monkeypatch.setattr(planning_orchestrator, "_run_round", fake_run_round)

    result = planning_orchestrator.run_planning_conversation(
        config=PlanningConfig(max_rounds=5, decision_policy="strict-gate"),
        project_root=tmp_path,
        planning_dir=tmp_path / "planning" / "mixed_streak",
        task_direction="Phase B negative-case",
        repo_inventory={"archetype": "fastapi_api"},
        feature="mixed_streak",
    )

    assert result.meta_blocker_demotion_log == []
    assert result.status == "escalation_required"
    # Pin the escalation reason so a future change cannot regress this test
    # into passing for the wrong reason (e.g. if stubborn-rounds or deadlock
    # started firing before the real-blocker signal terminated the run).
    escalation_payload = result.escalation or {}
    assert escalation_payload.get("termination_reason") not in {
        "planner_reviewer_deadlock",
    }


def test_meta_blocker_streak_does_not_demote_below_score_floor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Score guardrail: planner < 8.5 OR reviewer < 8.0 must block demotion
    even when 3 consecutive rounds carry only meta_blocker findings."""
    _write(tmp_path / "backend" / "main.py", "app")
    _patch_planning_context(monkeypatch)

    def fake_run_round(*, round_number: int, **kwargs: Any) -> PlanningRound:
        return PlanningRound(
            round_number=round_number,
            # planner score below 8.5 floor — guardrail must reject demotion
            plan_payload=_plan(self_assessment={"score": 7.9, "notes": []}),
            review_payload=_review(
                score=8.6,
                approved=False,
                findings=[_meta_finding(round_number)],
            ),
            review_error="",
            structural_issues=[],
            blocking_findings_count=1,
            timestamp=f"2026-05-16T00:00:0{round_number}+00:00",
        )

    monkeypatch.setattr(planning_orchestrator, "_run_round", fake_run_round)

    result = planning_orchestrator.run_planning_conversation(
        config=PlanningConfig(max_rounds=4, decision_policy="strict-gate"),
        project_root=tmp_path,
        planning_dir=tmp_path / "planning" / "low_score_meta",
        task_direction="Phase B score-guardrail",
        repo_inventory={"archetype": "fastapi_api"},
        feature="low_score_meta",
    )

    assert result.meta_blocker_demotion_log == []
    assert result.status != "approved"


# ---------------------------------------------------------------------------
# Phase C: late-round meta-only recovery (orchestrator integration)
# ---------------------------------------------------------------------------


def _scope_finding(round_number: int) -> dict[str, Any]:
    return {
        "severity": "blocking",
        "category": "scope",
        "description": (
            f"owner_surface mismatch on T{round_number} call chain — handler "
            f"X{round_number} not in files_to_change"
        ),
        "recommendation": f"Add the X{round_number} handler module",
    }


def _meta_blocker_finding_for_round(round_number: int) -> dict[str, Any]:
    return {
        "severity": "blocking",
        "category": "structure",
        "description": (
            f"evidence_resolutions[R{round_number - 1}F1] must cite the "
            f"Round {round_number} finding via an evidence_ref that directly "
            "addresses the reviewer's claim"
        ),
        "recommendation": "Add a self-referential evidence_ref",
    }


def test_phase_c_late_round_meta_recovery_demotes_single_shot_meta_block(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Phase C positive path: at max_rounds, every current blocker
    classifies as meta_blocker AND score guardrails pass. Streak counter
    is below LIMIT (only this final round is meta-only). Single-shot
    demoter fires with reason=meta_blocker_late_round_recovery."""
    _write(tmp_path / "backend" / "main.py", "app")
    _patch_planning_context(monkeypatch)
    call_rounds: list[int] = []

    def fake_run_round(*, round_number: int, **kwargs: Any) -> PlanningRound:
        call_rounds.append(round_number)
        if round_number < 3:
            return PlanningRound(
                round_number=round_number,
                plan_payload=_plan(self_assessment={"score": 9.1, "notes": []}),
                review_payload=_review(
                    score=8.6,
                    approved=False,
                    findings=[_scope_finding(round_number)],
                ),
                review_error="",
                structural_issues=[],
                blocking_findings_count=1,
                timestamp=f"2026-05-16T00:00:0{round_number}+00:00",
            )
        return PlanningRound(
            round_number=round_number,
            plan_payload=_plan(self_assessment={"score": 9.1, "notes": []}),
            review_payload=_review(
                score=8.6,
                approved=False,
                findings=[_meta_blocker_finding_for_round(round_number)],
            ),
            review_error="",
            structural_issues=[],
            blocking_findings_count=1,
            timestamp=f"2026-05-16T00:00:0{round_number}+00:00",
        )

    monkeypatch.setattr(planning_orchestrator, "_run_round", fake_run_round)

    result = planning_orchestrator.run_planning_conversation(
        config=PlanningConfig(max_rounds=3, decision_policy="strict-gate"),
        project_root=tmp_path,
        planning_dir=tmp_path / "planning" / "phase_c_positive",
        task_direction="Phase C positive",
        repo_inventory={"archetype": "fastapi_api"},
        feature="phase_c_positive",
    )

    assert call_rounds == [1, 2, 3]
    assert result.status == "approved"
    assert len(result.meta_blocker_demotion_log) == 1
    log_entry = result.meta_blocker_demotion_log[0]
    assert log_entry["round"] == 3
    assert log_entry["demoted_count"] == 1
    assert log_entry["demotion_reason"] == "meta_blocker_late_round_recovery"
    assert log_entry["planner_score"] == pytest.approx(9.1)
    assert log_entry["reviewer_score"] == pytest.approx(8.6)


def test_phase_c_does_not_fire_below_score_floor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Phase C guardrail: planner < 8.5 must block demotion even when the
    only final-round blocker is a meta_blocker."""
    _write(tmp_path / "backend" / "main.py", "app")
    _patch_planning_context(monkeypatch)

    def fake_run_round(*, round_number: int, **kwargs: Any) -> PlanningRound:
        return PlanningRound(
            round_number=round_number,
            plan_payload=_plan(self_assessment={"score": 8.0, "notes": []}),
            review_payload=_review(
                score=8.6,
                approved=False,
                findings=[_meta_blocker_finding_for_round(round_number)],
            ),
            review_error="",
            structural_issues=[],
            blocking_findings_count=1,
            timestamp=f"2026-05-16T00:00:0{round_number}+00:00",
        )

    monkeypatch.setattr(planning_orchestrator, "_run_round", fake_run_round)

    result = planning_orchestrator.run_planning_conversation(
        config=PlanningConfig(max_rounds=2, decision_policy="strict-gate"),
        project_root=tmp_path,
        planning_dir=tmp_path / "planning" / "phase_c_low_score",
        task_direction="Phase C score guardrail",
        repo_inventory={"archetype": "fastapi_api"},
        feature="phase_c_low_score",
    )

    assert result.meta_blocker_demotion_log == []
    assert result.status != "approved"


def test_phase_c_does_not_fire_when_non_meta_blocker_coexists(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Phase C correctness: a non-meta blocker at max_rounds breaks the
    all-meta precondition — the real concern must keep its severity and
    Phase C must NOT demote the meta finding alongside it."""
    _write(tmp_path / "backend" / "main.py", "app")
    _patch_planning_context(monkeypatch)

    def fake_run_round(*, round_number: int, **kwargs: Any) -> PlanningRound:
        return PlanningRound(
            round_number=round_number,
            plan_payload=_plan(self_assessment={"score": 9.1, "notes": []}),
            review_payload=_review(
                score=8.6,
                approved=False,
                findings=[
                    _meta_blocker_finding_for_round(round_number),
                    _scope_finding(round_number),
                ],
            ),
            review_error="",
            structural_issues=[],
            blocking_findings_count=2,
            timestamp=f"2026-05-16T00:00:0{round_number}+00:00",
        )

    monkeypatch.setattr(planning_orchestrator, "_run_round", fake_run_round)

    result = planning_orchestrator.run_planning_conversation(
        config=PlanningConfig(max_rounds=2, decision_policy="strict-gate"),
        project_root=tmp_path,
        planning_dir=tmp_path / "planning" / "phase_c_mixed",
        task_direction="Phase C mixed-blocker",
        repo_inventory={"archetype": "fastapi_api"},
        feature="phase_c_mixed",
    )

    assert result.meta_blocker_demotion_log == []
    assert result.status != "approved"


def test_phase_b_fires_phase_c_skips_when_streak_limit_hits_at_max_rounds(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """When streak >= LIMIT AND round_number == max_rounds coincide, Phase B
    must fire first and Phase C's ``if blocked`` guard must observe the
    post-demote empty blocker list and skip. Otherwise the same round would
    produce duplicate demotion log entries."""
    _write(tmp_path / "backend" / "main.py", "app")
    _patch_planning_context(monkeypatch)

    def fake_run_round(*, round_number: int, **kwargs: Any) -> PlanningRound:
        return PlanningRound(
            round_number=round_number,
            plan_payload=_plan(self_assessment={"score": 9.1, "notes": []}),
            review_payload=_review(
                score=8.6,
                approved=False,
                findings=[_meta_blocker_finding_for_round(round_number)],
            ),
            review_error="",
            structural_issues=[],
            blocking_findings_count=1,
            timestamp=f"2026-05-16T00:00:0{round_number}+00:00",
        )

    monkeypatch.setattr(planning_orchestrator, "_run_round", fake_run_round)

    result = planning_orchestrator.run_planning_conversation(
        config=PlanningConfig(max_rounds=3, decision_policy="strict-gate"),
        project_root=tmp_path,
        planning_dir=tmp_path / "planning" / "phase_b_c_dedupe",
        task_direction="Phase B + Phase C coincidence",
        repo_inventory={"archetype": "fastapi_api"},
        feature="phase_b_c_dedupe",
    )

    assert result.status == "approved"
    assert len(result.meta_blocker_demotion_log) == 1
    assert (
        result.meta_blocker_demotion_log[0]["demotion_reason"]
        == "meta_blocker_streak_demotion"
    )


def test_phase_c_resets_ambiguous_streak_so_detector_does_not_preempt_approval(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Regression for the ambiguous-detector race against Phase C. When the
    planner left an evidence_resolutions entry with status=ambiguous across
    rounds, the downstream ambiguous_evidence_streak / consecutive_any_ambiguous
    detectors would otherwise escalate the run as planning_evidence_blocked
    before the post-demote approval check ran. After Phase C successfully
    demotes meta blockers, those streak counters are re-armed so the run
    can reach the strict-gate approval path."""
    _write(tmp_path / "backend" / "main.py", "app")
    _patch_planning_context(monkeypatch)

    def fake_run_round(*, round_number: int, **kwargs: Any) -> PlanningRound:
        # Both rounds carry the same ambiguous evidence_resolutions signature
        # — without the Phase C reset this trips AMBIGUOUS_STREAK_LIMIT=2
        # before the loop checks `not blocked` for the approval path.
        plan = _plan(
            self_assessment={"score": 9.1, "notes": []},
            evidence_resolutions=[
                {
                    "finding_id": "R1F1",
                    "status": "ambiguous",
                    "evidence_refs": ["plan:summary"],
                    "rationale": "reviewer recurses; planner cannot resolve",
                }
            ],
        )
        if round_number == 1:
            findings = [_scope_finding(round_number)]
        else:
            findings = [_meta_blocker_finding_for_round(round_number)]
        return PlanningRound(
            round_number=round_number,
            plan_payload=plan,
            review_payload=_review(
                score=8.6,
                approved=False,
                findings=findings,
            ),
            review_error="",
            structural_issues=[],
            blocking_findings_count=1,
            timestamp=f"2026-05-16T00:00:0{round_number}+00:00",
        )

    monkeypatch.setattr(planning_orchestrator, "_run_round", fake_run_round)

    result = planning_orchestrator.run_planning_conversation(
        config=PlanningConfig(max_rounds=2, decision_policy="strict-gate"),
        project_root=tmp_path,
        planning_dir=tmp_path / "planning" / "phase_c_ambiguous_race",
        task_direction="Phase C ambiguous-detector race",
        repo_inventory={"archetype": "fastapi_api"},
        feature="phase_c_ambiguous_race",
    )

    assert result.status == "approved"
    assert len(result.meta_blocker_demotion_log) == 1
    assert (
        result.meta_blocker_demotion_log[0]["demotion_reason"]
        == "meta_blocker_late_round_recovery"
    )
