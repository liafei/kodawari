from __future__ import annotations

import argparse
import json

import pytest

from kodawari.cli.contract.autopilot_contract_bridge import (
    AutopilotPlanningBridgeError,
    _raise_if_context_scout_awaiting_decision,
)
from kodawari.cli.runtime.autopilot_cmd import _emit_planning_bridge_error
from kodawari.autopilot.planning.context_scout import (
    append_scout_feedback,
    budget_for_tier,
    build_context_scout_payload,
    build_scout_decision,
    build_scout_feedback_event,
    build_scout_scope,
    context_scout_enabled,
    evaluate_scout_progress,
    extract_selected_files_from_text,
    recommend_scout_budget,
)


def test_context_scout_flag_default_off(monkeypatch) -> None:
    monkeypatch.delenv("WORKFLOW_CONTEXT_SCOUT", raising=False)
    assert context_scout_enabled() is False


def test_context_scout_flag_on(monkeypatch) -> None:
    monkeypatch.setenv("WORKFLOW_CONTEXT_SCOUT", "1")
    assert context_scout_enabled() is True


def test_recommend_quick_for_small_scope_without_large_files() -> None:
    decision = recommend_scout_budget(
        user_text="只改一个小地方，快速处理",
        candidate_files=["a.py", "tests/test_a.py"],
        file_line_counts={"a.py": 120, "tests/test_a.py": 60},
    )
    assert decision.tier == "quick"
    assert decision.requires_user_decision is False


def test_recommend_standard_for_mid_scope() -> None:
    files = [f"src/f{i}.py" for i in range(10)]
    decision = recommend_scout_budget(
        user_text="常规实现",
        candidate_files=files,
        file_line_counts={path: 120 for path in files},
    )
    assert decision.tier == "standard"
    assert decision.requires_user_decision is False


def test_recommend_deep_for_many_files() -> None:
    files = [f"src/f{i}.py" for i in range(25)]
    decision = recommend_scout_budget(
        user_text="实现这项需求",
        candidate_files=files,
        file_line_counts={path: 120 for path in files},
    )
    assert decision.tier == "deep"
    assert decision.requires_user_decision is True


def test_recommend_deep_for_two_large_files() -> None:
    decision = recommend_scout_budget(
        user_text="实现",
        candidate_files=["a.py", "b.py", "c.py"],
        file_line_counts={"a.py": 900, "b.py": 901, "c.py": 10},
    )
    assert decision.tier == "deep"
    assert "files>20_or_large_files>=2" in decision.rationale


def test_recommend_deep_from_refactor_keyword_even_small_scope() -> None:
    decision = recommend_scout_budget(
        user_text="重构这个模块，顺便拆分函数",
        candidate_files=["a.py", "tests/test_a.py"],
        file_line_counts={"a.py": 200, "tests/test_a.py": 50},
    )
    assert decision.tier == "deep"
    assert decision.scope.refactor_hint is True
    assert decision.requires_user_decision is True


def test_recommend_deep_from_depth_high_keyword() -> None:
    decision = recommend_scout_budget(
        user_text="请深入完整分析这次改动",
        candidate_files=["a.py", "tests/test_a.py"],
        file_line_counts={"a.py": 150, "tests/test_a.py": 40},
    )
    assert decision.tier == "deep"
    assert decision.scope.depth_hint == "high"


def test_recommend_exhaustive_from_explicit_keyword() -> None:
    decision = recommend_scout_budget(
        user_text="完整扫一遍，不限时间",
        candidate_files=["a.py", "b.py", "c.py"],
        file_line_counts={"a.py": 100, "b.py": 110, "c.py": 120},
    )
    assert decision.tier == "exhaustive"
    assert decision.requires_user_decision is True


def test_build_scope_reports_wide_breadth_from_keyword() -> None:
    scope = build_scout_scope(
        user_text="跨模块全部实现",
        candidate_files=["a.py", "b.py"],
        file_line_counts={"a.py": 10, "b.py": 20},
    )
    assert scope.breadth_hint == "high"
    assert scope.prd_scope_breadth == "wide"


def test_budget_tiers_have_expected_caps() -> None:
    assert budget_for_tier("quick").timeout_seconds == 30
    assert budget_for_tier("standard").file_limit == 30
    assert budget_for_tier("deep").token_limit == 200_000
    assert budget_for_tier("exhaustive").unlimited is True


def test_deep_recommendation_waits_for_user_by_default(monkeypatch) -> None:
    monkeypatch.delenv("WORKFLOW_CONTEXT_SCOUT_DEFAULTS", raising=False)
    recommendation = recommend_scout_budget(
        user_text="重构这个模块",
        candidate_files=["a.py"],
        file_line_counts={"a.py": 20},
    )
    decision = build_scout_decision(recommendation)
    assert decision.status == "AWAITING_USER_DECISION"
    assert decision.requires_user_decision is True
    assert decision.options


def test_deep_recommendation_auto_accepts_when_defaults_auto(monkeypatch) -> None:
    monkeypatch.setenv("WORKFLOW_CONTEXT_SCOUT_DEFAULTS", "auto")
    recommendation = recommend_scout_budget(
        user_text="重构这个模块",
        candidate_files=["a.py"],
        file_line_counts={"a.py": 20},
    )
    decision = build_scout_decision(recommendation)
    assert decision.status == "AUTO_ACCEPTED"
    assert decision.auto_default_used is True
    assert decision.requires_user_decision is False


def test_extract_selected_files_from_natural_language() -> None:
    selected = extract_selected_files_from_text(
        "先只看 services/top_list_service.py 和 test_top.py",
        [
            "backend/api/v1/services/top_list_service.py",
            "tests/test_top.py",
            "tests/test_other.py",
        ],
    )
    assert selected == [
        "backend/api/v1/services/top_list_service.py",
        "tests/test_top.py",
    ]


def test_progress_waits_for_user_when_budget_nearly_spent_with_little_context() -> None:
    progress = evaluate_scout_progress(
        tier="standard",
        elapsed_seconds=60,
        files_read=4,
        files_total=20,
    )
    assert progress.status == "AWAITING_USER_DECISION"
    assert progress.preflight_allow is False
    assert progress.degradation_reason == "budget_progress_insufficient"


def test_progress_ready_when_enough_context_read() -> None:
    progress = evaluate_scout_progress(
        tier="standard",
        elapsed_seconds=60,
        files_read=12,
        files_total=20,
    )
    assert progress.status == "READY"
    assert progress.preflight_allow is True


def test_progress_exhaustive_never_pauses_for_budget() -> None:
    progress = evaluate_scout_progress(
        tier="exhaustive",
        elapsed_seconds=999,
        files_read=1,
        files_total=20,
    )
    assert progress.status == "READY"
    assert progress.budget_seconds == 0


def test_context_scout_payload_contains_decision_and_budget(monkeypatch) -> None:
    monkeypatch.setenv("WORKFLOW_CONTEXT_SCOUT_DEFAULTS", "auto")
    payload = build_context_scout_payload(
        user_text="深入完整分析",
        candidate_files=["a.py"],
        file_line_counts={"a.py": 20},
    )
    assert payload["enabled"] is True
    assert payload["recommendation"]["tier"] == "deep"
    assert payload["decision"]["status"] == "AUTO_ACCEPTED"
    assert payload["budget"]["tier"] == "deep"


def test_feedback_event_appends_jsonl(tmp_path) -> None:
    event = build_scout_feedback_event(
        reason="card_stale",
        task_id="T1",
        selected_files=["a.py"],
        scout_payload={"decision": {"status": "READY"}},
    )
    path = append_scout_feedback(tmp_path, event)
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["reason"] == "card_stale"
    assert rows[0]["task_id"] == "T1"
    assert rows[0]["selected_files"] == ["a.py"]


def test_bridge_blocks_pending_context_scout_decision() -> None:
    with pytest.raises(AutopilotPlanningBridgeError) as exc_info:
        _raise_if_context_scout_awaiting_decision(
            {
                "context_scout": {
                    "decision": {
                        "status": "AWAITING_USER_DECISION",
                        "selected_tier": "deep",
                        "prompt": "Confirm deep scan",
                    }
                }
            }
        )
    assert exc_info.value.error_code == "context_scout_user_decision_required"
    assert exc_info.value.details["selected_tier"] == "deep"


def test_bridge_allows_ready_context_scout_decision() -> None:
    _raise_if_context_scout_awaiting_decision(
        {"context_scout": {"decision": {"status": "READY", "selected_tier": "quick"}}}
    )


def test_cli_payload_marks_context_scout_as_awaiting_decision(tmp_path, capsys) -> None:
    error = AutopilotPlanningBridgeError(
        error_code="context_scout_user_decision_required",
        message="Context Scout requires a user decision before executor startup.",
        remediation=["Confirm the scout tier."],
        details={"selected_tier": "deep"},
    )
    rc = _emit_planning_bridge_error(
        args=argparse.Namespace(project_root=str(tmp_path), feature="demo"),
        error=error,
    )
    payload = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert payload["interaction_state"] == "AWAITING_DECISION"
    assert payload["decision_kind"] == "context_scout"
    assert payload["next_action_type"] == "await_decision"
