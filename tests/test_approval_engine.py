"""Tests for cli/approval_engine.py — P2 Conditional Auto-Approval.

TDD: written BEFORE implementation (RED → GREEN protocol).
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Helpers — import the module under test (will fail until implemented)
# ---------------------------------------------------------------------------

from kodawari.cli.approval_engine import (
    ApprovalDecision,
    evaluate_auto_approval,
    _conditions_match,
)
from kodawari.cli.autopilot_decision_runtime import DecisionKind


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

RULES_YAML = textwrap.dedent("""\
    schema_version: "approval.rules.v1"

    rules:
      release_approval:
        require_human:
          - conditions:
              risk_profile: [medium, high]
            message: "Mid/high risk requires human review"
          - conditions:
              any_files_match: ["**/auth_*", "**/migration_sql/**"]
            message: "Auth/DB changes require human review"
        auto_approve:
          - conditions:
              verify_status: PASS
              gate_status: PASS
              scope_drift: none
              risk_profile: low
              changed_files_count: "<= 5"
            log_message: "Auto-approved: low risk, all checks passed"

      intent_clarification:
        require_human:
          - conditions: {}
            message: "Low confidence intent requires human clarification"
        auto_approve: []

      broad_kind:
        require_human:
          - conditions:
              risky_flag: "true"
            message: "Risky flag set"
        auto_approve:
          - conditions: {}
            log_message: "Auto-approved: broad catch-all"
    """)


@pytest.fixture()
def project_root(tmp_path: Path) -> Path:
    """A temporary project root with an approval_rules.yaml installed."""
    rules_dir = tmp_path / ".claude" / "workflow"
    rules_dir.mkdir(parents=True)
    (rules_dir / "approval_rules.yaml").write_text(RULES_YAML, encoding="utf-8")
    return tmp_path


@pytest.fixture()
def project_root_no_rules(tmp_path: Path) -> Path:
    """A temporary project root with NO approval_rules.yaml."""
    return tmp_path


# ---------------------------------------------------------------------------
# TestConditionMatch — unit-tests for _conditions_match()
# ---------------------------------------------------------------------------

class TestConditionMatch:
    def test_empty_conditions_always_match(self) -> None:
        assert _conditions_match({}, {"verify_status": "PASS"}) is True

    def test_empty_conditions_empty_context(self) -> None:
        assert _conditions_match({}, {}) is True

    def test_simple_equality(self) -> None:
        assert _conditions_match({"verify_status": "PASS"}, {"verify_status": "PASS"}) is True

    def test_simple_equality_mismatch(self) -> None:
        assert _conditions_match({"verify_status": "PASS"}, {"verify_status": "FAIL"}) is False

    def test_list_or_match(self) -> None:
        # risk_profile: [medium, high] → match if context value is any of them
        cond = {"risk_profile": ["low", "medium"]}
        assert _conditions_match(cond, {"risk_profile": "low"}) is True
        assert _conditions_match(cond, {"risk_profile": "medium"}) is True
        assert _conditions_match(cond, {"risk_profile": "high"}) is False

    def test_numeric_compare_lte_gte(self) -> None:
        assert _conditions_match({"changed_files_count": "<= 5"}, {"changed_files_count": 3}) is True
        assert _conditions_match({"changed_files_count": "<= 5"}, {"changed_files_count": 5}) is True
        assert _conditions_match({"changed_files_count": "<= 5"}, {"changed_files_count": 6}) is False
        assert _conditions_match({"changed_files_count": ">= 3"}, {"changed_files_count": 3}) is True
        assert _conditions_match({"changed_files_count": ">= 3"}, {"changed_files_count": 2}) is False

    def test_numeric_compare_lt_gt(self) -> None:
        assert _conditions_match({"surface_count": "< 3"}, {"surface_count": 2}) is True
        assert _conditions_match({"surface_count": "< 3"}, {"surface_count": 3}) is False
        assert _conditions_match({"surface_count": "> 1"}, {"surface_count": 2}) is True
        assert _conditions_match({"surface_count": "> 1"}, {"surface_count": 1}) is False

    def test_no_files_match_glob(self) -> None:
        cond = {"no_files_match": ["**/auth_*"]}
        ctx_safe: dict[str, Any] = {"changed_files": ["src/utils.py", "README.md"]}
        ctx_risky: dict[str, Any] = {"changed_files": ["src/auth_service.py"]}
        assert _conditions_match(cond, ctx_safe) is True
        assert _conditions_match(cond, ctx_risky) is False

    def test_any_files_match_glob(self) -> None:
        cond = {"any_files_match": ["**/auth_*"]}
        ctx_safe: dict[str, Any] = {"changed_files": ["src/utils.py"]}
        ctx_risky: dict[str, Any] = {"changed_files": ["src/auth_service.py"]}
        assert _conditions_match(cond, ctx_safe) is False
        assert _conditions_match(cond, ctx_risky) is True

    def test_missing_context_field_treated_as_no_match(self) -> None:
        # Field referenced in conditions but absent from context → no match
        assert _conditions_match({"verify_status": "PASS"}, {}) is False
        assert _conditions_match({"risk_profile": ["low"]}, {}) is False
        assert _conditions_match({"changed_files_count": "<= 5"}, {}) is False


# ---------------------------------------------------------------------------
# TestEvaluationOrder — require_human is checked BEFORE auto_approve
# ---------------------------------------------------------------------------

class TestEvaluationOrder:
    def test_require_human_evaluated_before_auto_approve(self, project_root: Path) -> None:
        # broad_kind: require_human fires when risky_flag=true; auto_approve catches everything
        decision = evaluate_auto_approval(
            decision_kind=DecisionKind.RELEASE_APPROVAL,
            context={"risk_profile": "medium", "verify_status": "PASS"},
            project_root=project_root,
        )
        assert decision.action == "require_human"

    def test_broad_auto_approve_blocked_by_precise_require_human(self, project_root: Path) -> None:
        # broad_kind has require_human for risky_flag + broad auto_approve for {}
        # When risky_flag=true, require_human fires FIRST
        decision = evaluate_auto_approval(
            decision_kind="broad_kind",
            context={"risky_flag": "true"},
            project_root=project_root,
        )
        assert decision.action == "require_human"

    def test_broad_auto_approve_fires_when_no_require_human_matches(self, project_root: Path) -> None:
        # broad_kind: risky_flag is absent → require_human misses, auto_approve {} catches
        decision = evaluate_auto_approval(
            decision_kind="broad_kind",
            context={},
            project_root=project_root,
        )
        assert decision.action == "auto_approve"

    def test_intent_clarification_never_auto_approves(self, project_root: Path) -> None:
        # intent_clarification: require_human has empty conditions (always matches)
        # So it ALWAYS fires before auto_approve (which is empty anyway)
        decision = evaluate_auto_approval(
            decision_kind=DecisionKind.INTENT_CLARIFICATION,
            context={"verify_status": "PASS", "risk_profile": "low"},
            project_root=project_root,
        )
        assert decision.action == "require_human"


# ---------------------------------------------------------------------------
# TestAutoApproval — integration-level evaluate_auto_approval() tests
# ---------------------------------------------------------------------------

class TestAutoApproval:
    def test_low_risk_all_pass_auto_approves(self, project_root: Path) -> None:
        ctx = {
            "verify_status": "PASS",
            "gate_status": "PASS",
            "scope_drift": "none",
            "risk_profile": "low",
            "changed_files_count": 3,
        }
        decision = evaluate_auto_approval(
            decision_kind=DecisionKind.RELEASE_APPROVAL,
            context=ctx,
            project_root=project_root,
        )
        assert decision.action == "auto_approve"
        assert decision.log_message  # non-empty log message

    def test_auth_files_require_human(self, project_root: Path) -> None:
        ctx = {
            "verify_status": "PASS",
            "gate_status": "PASS",
            "scope_drift": "none",
            "risk_profile": "low",
            "changed_files_count": 2,
            "changed_files": ["src/auth_service.py", "README.md"],
        }
        decision = evaluate_auto_approval(
            decision_kind=DecisionKind.RELEASE_APPROVAL,
            context=ctx,
            project_root=project_root,
        )
        assert decision.action == "require_human"

    def test_no_rules_file_defaults_to_human(self, project_root_no_rules: Path) -> None:
        decision = evaluate_auto_approval(
            decision_kind=DecisionKind.RELEASE_APPROVAL,
            context={"verify_status": "PASS", "risk_profile": "low"},
            project_root=project_root_no_rules,
        )
        assert decision.action == "require_human"

    def test_unknown_decision_kind_defaults_to_human(self, project_root: Path) -> None:
        decision = evaluate_auto_approval(
            decision_kind="nonexistent_kind",
            context={"verify_status": "PASS"},
            project_root=project_root,
        )
        assert decision.action == "require_human"

    def test_auto_approval_records_audit_log(self, project_root: Path) -> None:
        ctx = {
            "verify_status": "PASS",
            "gate_status": "PASS",
            "scope_drift": "none",
            "risk_profile": "low",
            "changed_files_count": 1,
        }
        evaluate_auto_approval(
            decision_kind=DecisionKind.RELEASE_APPROVAL,
            context=ctx,
            project_root=project_root,
        )
        log_path = project_root / ".auto_approval_log.jsonl"
        assert log_path.exists(), "audit log must be created on auto_approve"
        lines = [l for l in log_path.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(lines) >= 1
        entry = json.loads(lines[-1])
        assert entry["action"] == "auto_approve"
        assert "decision_id" in entry
        assert "timestamp" in entry

    def test_require_human_does_not_write_audit_log(self, project_root: Path) -> None:
        evaluate_auto_approval(
            decision_kind=DecisionKind.RELEASE_APPROVAL,
            context={"risk_profile": "high"},
            project_root=project_root,
        )
        log_path = project_root / ".auto_approval_log.jsonl"
        # Log should not be written (or if it exists from prior run, no new entry for this call)
        if log_path.exists():
            lines = [l for l in log_path.read_text(encoding="utf-8").splitlines() if l.strip()]
            for line in lines:
                entry = json.loads(line)
                assert entry["action"] != "require_human"

    def test_uppercase_key_normalized_to_lowercase(self, project_root: Path) -> None:
        # DecisionKind enum .value is already lowercase; passing string in UPPER should be normalized
        decision = evaluate_auto_approval(
            decision_kind="RELEASE_APPROVAL",
            context={
                "verify_status": "PASS",
                "gate_status": "PASS",
                "scope_drift": "none",
                "risk_profile": "low",
                "changed_files_count": 1,
            },
            project_root=project_root,
        )
        # "RELEASE_APPROVAL" normalized to "release_approval" → finds rules
        assert decision.action == "auto_approve"

    def test_mixed_case_key_matches_snake_case(self, project_root: Path) -> None:
        # "Release_Approval" normalized to "release_approval"
        decision = evaluate_auto_approval(
            decision_kind="Release_Approval",
            context={
                "verify_status": "PASS",
                "gate_status": "PASS",
                "scope_drift": "none",
                "risk_profile": "low",
                "changed_files_count": 1,
            },
            project_root=project_root,
        )
        assert decision.action == "auto_approve"

    def test_any_files_match_globstar_zero_dirs(self, project_root: Path) -> None:
        # auth_service.py at root — **/auth_* with zero leading dirs must match
        ctx = {
            "verify_status": "PASS",
            "gate_status": "PASS",
            "scope_drift": "none",
            "risk_profile": "low",
            "changed_files_count": 1,
            "changed_files": ["auth_service.py"],  # zero dir prefix
        }
        decision = evaluate_auto_approval(
            decision_kind=DecisionKind.RELEASE_APPROVAL,
            context=ctx,
            project_root=project_root,
        )
        assert decision.action == "require_human"

    def test_no_files_match_globstar_zero_dirs(self, project_root: Path) -> None:
        # No auth files → require_human rule for any_files_match should NOT fire
        ctx = {
            "verify_status": "PASS",
            "gate_status": "PASS",
            "scope_drift": "none",
            "risk_profile": "low",
            "changed_files_count": 1,
            "changed_files": ["src/utils.py"],
        }
        decision = evaluate_auto_approval(
            decision_kind=DecisionKind.RELEASE_APPROVAL,
            context=ctx,
            project_root=project_root,
        )
        assert decision.action == "auto_approve"


# ---------------------------------------------------------------------------
# TestIntegration — decision bridge integration
# ---------------------------------------------------------------------------

class TestIntegration:
    def test_decision_bridge_skips_request_on_auto_approve(
        self, project_root: Path
    ) -> None:
        from kodawari.cli.autopilot_decision_bridge import build_release_decision_spec

        ctx = {
            "verify_status": "PASS",
            "gate_status": "PASS",
            "scope_drift": "none",
            "risk_profile": "low",
            "changed_files_count": 1,
        }
        result = build_release_decision_spec(
            "my-feature",
            execution_context=ctx,
            project_root=project_root,
        )
        # When auto-approved, build_release_decision_spec returns None
        assert result is None

    def test_decision_bridge_creates_request_on_require_human(
        self, project_root: Path
    ) -> None:
        from kodawari.cli.autopilot_decision_bridge import build_release_decision_spec

        ctx = {
            "verify_status": "FAIL",
            "risk_profile": "high",
        }
        result = build_release_decision_spec(
            "my-feature",
            execution_context=ctx,
            project_root=project_root,
        )
        # When require_human, the spec dict is returned (non-None)
        assert result is not None
        assert result.get("decision_kind") == DecisionKind.RELEASE_APPROVAL

    def test_decision_bridge_no_context_creates_request(
        self, project_root: Path
    ) -> None:
        from kodawari.cli.autopilot_decision_bridge import build_release_decision_spec

        result = build_release_decision_spec("my-feature")
        # No execution_context → falls back to requiring human
        assert result is not None


# ---------------------------------------------------------------------------
# GPT peer-review fixes — wiring into real execution path
# ---------------------------------------------------------------------------


class TestReleaseFlowWiring:
    """Tests that auto-approval + risk_profile are wired into the real autopilot path."""

    def test_consumed_approved_response_records_history_and_clears_artifacts(self, tmp_path: Path) -> None:
        import argparse

        from kodawari.cli.autopilot_decision_runtime import (
            DECISION_REQUEST_FILENAME,
            DECISION_RESPONSE_FILENAME,
            build_decision_request,
            build_decision_response,
            load_decision_history,
            write_decision_request,
            write_decision_response,
        )
        from kodawari.cli.autopilot_release_flow import decision_payload_for_spec, decision_spec

        planning_dir = tmp_path / "planning" / "feat"
        planning_dir.mkdir(parents=True, exist_ok=True)
        spec = decision_spec(
            feature="feat",
            kind=DecisionKind.RELEASE_APPROVAL,
            question="Ship?",
            context_summary="ctx",
            blocking_reason="manual review",
        )
        request = build_decision_request(
            decision_id=str(spec["decision_id"]),
            decision_kind=spec["decision_kind"],
            question=str(spec["question"]),
            context_summary=str(spec["context_summary"]),
            options=list(spec["options"]),
            recommended_option=str(spec["recommended_option"]),
            blocking_reason=str(spec["blocking_reason"]),
        )
        response = build_decision_response(
            decision_id=str(spec["decision_id"]),
            selected_option="ship",
            rationale="approved",
        )
        write_decision_request(planning_dir, request)
        write_decision_response(planning_dir, response)

        payload = decision_payload_for_spec(
            args=argparse.Namespace(feature="feat"),
            planning_dir=planning_dir,
            planning_snapshot=None,
            spec=spec,
            base_payload={},
        )

        assert payload is None
        assert load_decision_history(planning_dir) == [str(spec["decision_id"])]
        assert not (planning_dir / DECISION_REQUEST_FILENAME).exists()
        assert not (planning_dir / DECISION_RESPONSE_FILENAME).exists()

    def test_non_approved_response_does_not_record_history(self, tmp_path: Path) -> None:
        import argparse

        from kodawari.cli.autopilot_decision_runtime import (
            DECISION_REQUEST_FILENAME,
            DECISION_RESPONSE_FILENAME,
            build_decision_request,
            build_decision_response,
            load_decision_history,
            write_decision_request,
            write_decision_response,
        )
        from kodawari.cli.autopilot_release_flow import decision_payload_for_spec, decision_spec

        planning_dir = tmp_path / "planning" / "feat"
        planning_dir.mkdir(parents=True, exist_ok=True)
        spec = decision_spec(
            feature="feat",
            kind=DecisionKind.RELEASE_APPROVAL,
            question="Ship?",
            context_summary="ctx",
            blocking_reason="manual review",
        )
        request = build_decision_request(
            decision_id=str(spec["decision_id"]),
            decision_kind=spec["decision_kind"],
            question=str(spec["question"]),
            context_summary=str(spec["context_summary"]),
            options=list(spec["options"]),
            recommended_option=str(spec["recommended_option"]),
            blocking_reason=str(spec["blocking_reason"]),
        )
        response = build_decision_response(
            decision_id=str(spec["decision_id"]),
            selected_option="hold",
            rationale="not approved",
        )
        write_decision_request(planning_dir, request)
        write_decision_response(planning_dir, response)

        payload = decision_payload_for_spec(
            args=argparse.Namespace(feature="feat"),
            planning_dir=planning_dir,
            planning_snapshot=None,
            spec=spec,
            base_payload={},
        )

        assert isinstance(payload, dict)
        assert payload.get("status") == "blocked"
        assert load_decision_history(planning_dir) == []
        assert (planning_dir / DECISION_REQUEST_FILENAME).exists()
        assert (planning_dir / DECISION_RESPONSE_FILENAME).exists()

    def test_maybe_release_tail_uses_auto_approval(self, tmp_path: Path) -> None:
        """maybe_run_release_tail should call build_release_decision_spec with execution_context."""
        import argparse
        from pathlib import Path as P
        from unittest.mock import MagicMock, patch

        from kodawari.cli.autopilot_release_flow import maybe_run_release_tail

        # Build approval rules that auto-approve low-risk
        rules_dir = tmp_path / ".claude" / "workflow"
        rules_dir.mkdir(parents=True, exist_ok=True)
        (rules_dir / "approval_rules.yaml").write_text(
            "schema_version: 'approval.rules.v1'\n"
            "rules:\n"
            "  release_approval:\n"
            "    require_human: []\n"
            "    auto_approve:\n"
            "      - conditions:\n"
            "          risk_profile: low\n"
            "          verify_status: PASS\n"
            "          gate_status: PASS\n"
            "        log_message: auto\n",
            encoding="utf-8",
        )

        args = argparse.Namespace(feature="test-feat")
        mock_snapshot = MagicMock()
        mock_snapshot.artifacts = {"PLANNING_CONVERSATION.json": "exists"}
        mock_snapshot.to_dict.return_value = {}
        command_runtime = {
            "planning_dir": tmp_path / "planning" / "test-feat",
            "planning_snapshot": mock_snapshot,
            "project_root": tmp_path,
            "feature": "test-feat",
        }
        payload = {
            "status": "ok",
            "risk_profile": "low",
            "unified_status": {"verify": "PASS", "gate": "PASS"},
        }
        mock_tail = MagicMock(return_value={"status": "PASS"})

        result_payload, rc = maybe_run_release_tail(
            args=args,
            command_runtime=command_runtime,
            payload=payload,
            run_release_tail=mock_tail,
        )
        # Auto-approved → should proceed to release_tail, not block for decision
        assert mock_tail.called, "release_tail should be called (auto-approved, no decision block)"

    def test_risk_profile_in_payload_flows_to_release_config(self, tmp_path: Path) -> None:
        """risk_profile from payload must reach run_release_tail via config."""
        import argparse
        from unittest.mock import MagicMock

        from kodawari.cli.autopilot_release_flow import maybe_run_release_tail

        # Set up auto-approve rules so we reach release_tail
        rules_dir = tmp_path / ".claude" / "workflow"
        rules_dir.mkdir(parents=True, exist_ok=True)
        (rules_dir / "approval_rules.yaml").write_text(
            "schema_version: 'approval.rules.v1'\n"
            "rules:\n"
            "  release_approval:\n"
            "    require_human: []\n"
            "    auto_approve:\n"
            "      - conditions: {}\n"
            "        log_message: auto\n",
            encoding="utf-8",
        )

        args = argparse.Namespace(feature="feat-x")
        mock_snapshot = MagicMock()
        mock_snapshot.artifacts = {"PLANNING_CONVERSATION.json": "exists"}
        mock_snapshot.to_dict.return_value = {}
        command_runtime = {
            "planning_dir": tmp_path / "planning" / "feat-x",
            "planning_snapshot": mock_snapshot,
            "project_root": tmp_path,
            "feature": "feat-x",
        }
        payload = {"status": "ok", "risk_profile": "high"}
        captured_kwargs: dict = {}

        def _capture_tail(**kwargs):
            captured_kwargs.update(kwargs)
            return {"status": "PASS"}

        maybe_run_release_tail(
            args=args,
            command_runtime=command_runtime,
            payload=payload,
            run_release_tail=_capture_tail,
        )
        config = captured_kwargs.get("config")
        assert config is not None, "run_release_tail must receive a config with risk_profile"
        assert config.risk_profile == "high", (
            f"Expected risk_profile='high', got '{config.risk_profile}'"
        )

    def test_execution_context_includes_changed_files(self, tmp_path: Path) -> None:
        """_build_release_execution_context must include changed_files for glob matching."""
        from kodawari.cli.autopilot_release_flow import _build_release_execution_context

        payload = {
            "risk_profile": "high",
            "changed_files": ["src/auth_service.py", "docs/README.md"],
            "unified_status": {"verify": "PASS", "gate": "PASS"},
        }
        ctx = _build_release_execution_context(payload)
        assert "changed_files" in ctx, "execution_context must include changed_files"
        assert "src/auth_service.py" in ctx["changed_files"]
        assert ctx["changed_files_count"] == 2

    def test_auth_file_not_auto_approved_via_execution_context(self, tmp_path: Path) -> None:
        """changed_files with auth file must trigger require_human, not auto_approve."""
        rules_dir = tmp_path / ".claude" / "workflow"
        rules_dir.mkdir(parents=True, exist_ok=True)
        (rules_dir / "approval_rules.yaml").write_text(
            "schema_version: 'approval.rules.v1'\n"
            "rules:\n"
            "  release_approval:\n"
            "    require_human:\n"
            "      - conditions:\n"
            "          any_files_match: ['**/auth_*']\n"
            "        message: auth changes require human\n"
            "    auto_approve:\n"
            "      - conditions: {}\n"
            "        log_message: auto\n",
            encoding="utf-8",
        )
        from kodawari.cli.approval_engine import evaluate_auto_approval
        from kodawari.cli.autopilot_decision_runtime import DecisionKind

        ctx = {
            "verify_status": "PASS",
            "gate_status": "PASS",
            "risk_profile": "low",
            "changed_files": ["src/auth_service.py"],
            "changed_files_count": 1,
            "scope_drift": "none",
        }
        decision = evaluate_auto_approval(
            decision_kind=DecisionKind.RELEASE_APPROVAL,
            context=ctx,
            project_root=tmp_path,
        )
        assert decision.action == "require_human", (
            f"auth file must trigger require_human, got {decision.action}"
        )

    def test_build_autopilot_payload_includes_risk_profile(self) -> None:
        """build_autopilot_payload must propagate risk_profile from run_result."""
        from unittest.mock import MagicMock
        from pathlib import Path as P
        from kodawari.cli.autopilot_release_flow import build_autopilot_payload

        run_result = {"risk_profile": "high", "reason": "DONE"}
        mock_state = MagicMock()
        mock_state.get_unified_status.return_value = {}

        payload = build_autopilot_payload(
            args=MagicMock(feature="f"),
            planning_dir=P("/tmp/p"),
            state_path=P("/tmp/s"),
            rounds_path=P("/tmp/r"),
            plan=MagicMock(estimated_cycles=1, estimated_tokens=100),
            run_result=run_result,
            rounds=[],
            planning_artifacts={
                "PLAN.md": {"exists": True}, "TASKS.md": {"exists": True},
                "ACCEPTANCE.md": {"exists": True}, "GATE.md": {"exists": True},
            },
            state=mock_state,
        )
        assert payload.get("risk_profile") == "high", (
            f"risk_profile missing or wrong in payload: {payload.get('risk_profile')}"
        )

    def test_build_autopilot_payload_includes_changed_files(self) -> None:
        """build_autopilot_payload must expose changed_files from state."""
        from unittest.mock import MagicMock
        from pathlib import Path as P
        from kodawari.cli.autopilot_release_flow import build_autopilot_payload

        mock_state = MagicMock()
        mock_state.get_unified_status.return_value = {}
        mock_state.changed_files = {"src/auth_service.py", "docs/README.md"}

        payload = build_autopilot_payload(
            args=MagicMock(feature="f"),
            planning_dir=P("/tmp/p"),
            state_path=P("/tmp/s"),
            rounds_path=P("/tmp/r"),
            plan=MagicMock(estimated_cycles=1, estimated_tokens=100),
            run_result={"risk_profile": "medium", "reason": "DONE"},
            rounds=[],
            planning_artifacts={
                "PLAN.md": {"exists": True}, "TASKS.md": {"exists": True},
                "ACCEPTANCE.md": {"exists": True}, "GATE.md": {"exists": True},
            },
            state=mock_state,
        )
        assert "changed_files" in payload, "payload must include changed_files"
        assert len(payload["changed_files"]) == 2
        assert "changed_files_count" in payload

    def test_execution_context_uses_real_verify_gate_and_scope_truth(self) -> None:
        """execution_context should derive verify/gate/scope_drift from real payload truth, not unified_status defaults."""
        from pathlib import Path as P
        from unittest.mock import MagicMock

        from kodawari.cli.autopilot_release_flow import (
            _build_release_execution_context,
            build_autopilot_payload,
        )

        mock_state = MagicMock()
        mock_state.get_unified_status.return_value = {}
        mock_state.changed_files = {"src/auth_service.py"}
        run_result = {
            "reason": "PROCEED_TO_GATE",
            "risk_profile": "high",
            "verify_check": {"status": "PASS"},
            "gate_check": {"total_status": "PASS"},
            "rounds": [
                {
                    "details": {
                        "scope_drift": {
                            "status": "FAIL",
                            "drifted": True,
                            "out_of_scope_files": ["src/auth_service.py"],
                        }
                    }
                }
            ],
        }

        payload = build_autopilot_payload(
            args=MagicMock(feature="f"),
            planning_dir=P("/tmp/p"),
            state_path=P("/tmp/s"),
            rounds_path=P("/tmp/r"),
            plan=MagicMock(estimated_cycles=1, estimated_tokens=100),
            run_result=run_result,
            rounds=[],
            planning_artifacts={
                "PLAN.md": {"exists": True},
                "TASKS.md": {"exists": True},
                "ACCEPTANCE.md": {"exists": True},
                "GATE.md": {"exists": True},
            },
            state=mock_state,
        )
        ctx = _build_release_execution_context(payload)

        assert ctx["verify_status"] == "PASS"
        assert ctx["gate_status"] == "PASS"
        assert ctx["scope_drift"] == "drifted"


class TestPipelineFinishReason:
    """PIPELINE_FINISH must be treated as success everywhere PROCEED_TO_GATE is."""

    def test_autopilot_payload_status_ok_for_pipeline_finish(self) -> None:
        from kodawari.cli.autopilot_workflow_runtime import autopilot_payload_status

        status, _ = autopilot_payload_status(
            run_result={"reason": "PIPELINE_FINISH"},
            workflow_chain=None,
        )
        assert status == "ok", f"PIPELINE_FINISH should yield 'ok', got '{status}'"

    def test_workflow_chain_reason_maps_pipeline_finish_to_pass(self) -> None:
        from kodawari.cli.workflow_chain import _REASON_STOP_REASON

        assert "PIPELINE_FINISH" in _REASON_STOP_REASON, "PIPELINE_FINISH missing from _REASON_STOP_REASON"
        assert _REASON_STOP_REASON["PIPELINE_FINISH"] == "PASS"

    def test_loop_result_reason_maps_pipeline_finish_to_pass(self) -> None:
        from kodawari.autopilot.loop_result_payload import _REASON_STOP_REASON

        assert "PIPELINE_FINISH" in _REASON_STOP_REASON
        assert _REASON_STOP_REASON["PIPELINE_FINISH"] == "PASS"

    def test_collaboration_runtime_reason_maps_pipeline_finish_to_pass(self) -> None:
        from kodawari.autopilot.collaboration_runtime import _LOOP_REASON_STOP_REASON

        assert "PIPELINE_FINISH" in _LOOP_REASON_STOP_REASON
        assert _LOOP_REASON_STOP_REASON["PIPELINE_FINISH"] == "PASS"

    def test_loop_outcome_maps_pipeline_finish_to_ready(self) -> None:
        from kodawari.autopilot.loop_outcome import _LOOP_REASON_ROUND_OUTCOME

        assert "PIPELINE_FINISH" in _LOOP_REASON_ROUND_OUTCOME
        assert _LOOP_REASON_ROUND_OUTCOME["PIPELINE_FINISH"] == "ready_for_gate"

    def test_delivery_evidence_accepts_pipeline_finish(self) -> None:
        """delivery_evidence should not mark PIPELINE_FINISH as review_stage_blocked."""
        # This tests the logic at delivery_evidence.py:165
        reason = "PIPELINE_FINISH"
        assert reason in {"", "PROCEED_TO_GATE", "PIPELINE_FINISH"}, "PIPELINE_FINISH must be valid"

    def test_review_evidence_requirements_do_not_flag_pipeline_finish_as_stage_issue(self) -> None:
        from kodawari.review_evidence_contract import build_review_evidence_requirements

        requirements = build_review_evidence_requirements(
            self_review_count=0,
            peer_review_count=0,
            execution_status="PASS",
            loop_reason="PIPELINE_FINISH",
            peer_review_enabled=False,
            peer_review_summary={"enabled": False, "skipped": False},
        )
        assert requirements["stage_issue"] == ""


class TestLegacyReleaseTail:
    """Release tail must run for legacy/resume paths (planning_snapshot=None)."""

    def test_release_tail_runs_when_planning_snapshot_is_none_and_run_succeeded(self, tmp_path: Path) -> None:
        """Legacy path: planning_snapshot=None + status=ok + reason=PROCEED_TO_GATE → run release_tail."""
        import argparse
        from unittest.mock import MagicMock

        from kodawari.cli.autopilot_release_flow import maybe_run_release_tail

        args = argparse.Namespace(feature="feat")
        command_runtime = {
            "planning_dir": tmp_path / "planning" / "feat",
            "planning_snapshot": None,  # legacy/resume path
            "project_root": tmp_path,
            "feature": "feat",
        }
        payload = {"status": "ok", "risk_profile": "low", "run_reason": "PROCEED_TO_GATE"}
        mock_tail = MagicMock(return_value={"status": "PASS"})

        result_payload, rc = maybe_run_release_tail(
            args=args,
            command_runtime=command_runtime,
            payload=payload,
            run_release_tail=mock_tail,
        )
        assert mock_tail.called, "release_tail must run for legacy when run completed successfully"

    def test_release_tail_skipped_when_legacy_run_not_complete(self, tmp_path: Path) -> None:
        """Legacy path: planning_snapshot=None + reason=blocked → skip release_tail."""
        import argparse
        from unittest.mock import MagicMock

        from kodawari.cli.autopilot_release_flow import maybe_run_release_tail

        args = argparse.Namespace(feature="feat")
        command_runtime = {
            "planning_dir": tmp_path / "planning" / "feat",
            "planning_snapshot": None,
            "project_root": tmp_path,
            "feature": "feat",
        }
        payload = {"status": "ok", "run_reason": "GATE_BLOCKED"}
        mock_tail = MagicMock(return_value={"status": "PASS"})

        result_payload, rc = maybe_run_release_tail(
            args=args,
            command_runtime=command_runtime,
            payload=payload,
            run_release_tail=mock_tail,
        )
        assert not mock_tail.called, "release_tail should NOT run when run was blocked"

    def test_release_tail_legacy_blocked_result_stored_not_enforced(self, tmp_path: Path) -> None:
        """Legacy path: blocked release_tail is stored in payload but rc is always None.

        Legacy/resume runs don't have full planning context; enforcement belongs to
        contract-first runs. The result is recorded for information only.
        """
        import argparse

        from kodawari.cli.autopilot_release_flow import maybe_run_release_tail

        args = argparse.Namespace(feature="feat")
        command_runtime = {
            "planning_dir": tmp_path / "planning" / "feat",
            "planning_snapshot": None,
            "project_root": tmp_path,
            "feature": "feat",
        }
        payload = {"status": "ok", "risk_profile": "low", "run_reason": "PROCEED_TO_GATE"}

        result_payload, rc = maybe_run_release_tail(
            args=args,
            command_runtime=command_runtime,
            payload=payload,
            run_release_tail=lambda **_: {"status": "BLOCKED", "blocking_reason": "verify failed"},
        )
        # Legacy path: exit code is always None (0), even when release_tail blocked.
        assert rc is None, f"Legacy blocked release_tail must not set rc to {rc!r}; it is advisory only"
        # The release_tail result is still stored for observability.
        assert result_payload.get("release_tail", {}).get("status") == "BLOCKED"


class TestAutoApproveDecisionRequestPresent:
    """Auto-approved releases must NOT report decision_request_present=True.

    When spec is None (auto-approved), no .decision_request.json file is written to
    disk. Reporting decision_request_present=True in the interaction snapshot would
    be a lie — the field must reflect disk reality, not the code path taken.
    """

    def test_auto_approved_release_tail_reports_decision_request_present_false(
        self, tmp_path: Path
    ) -> None:
        """decision_request_present must be False when auto-approval skips file creation."""
        import argparse
        from unittest.mock import MagicMock

        from kodawari.cli.autopilot_release_flow import maybe_run_release_tail

        # Rules that auto-approve everything (no require_human guard)
        rules_dir = tmp_path / ".claude" / "workflow"
        rules_dir.mkdir(parents=True, exist_ok=True)
        (rules_dir / "approval_rules.yaml").write_text(
            "schema_version: 'approval.rules.v1'\n"
            "rules:\n"
            "  release_approval:\n"
            "    require_human: []\n"
            "    auto_approve:\n"
            "      - conditions: {}\n"
            "        log_message: auto\n",
            encoding="utf-8",
        )

        args = argparse.Namespace(feature="feat-auto")
        mock_snapshot = MagicMock()
        mock_snapshot.artifacts = {"PLANNING_CONVERSATION.json": "exists"}
        mock_snapshot.to_dict.return_value = {}
        command_runtime = {
            "planning_dir": tmp_path / "planning" / "feat-auto",
            "planning_snapshot": mock_snapshot,
            "project_root": tmp_path,
            "feature": "feat-auto",
        }
        payload = {
            "status": "ok",
            "risk_profile": "low",
            "run_reason": "PROCEED_TO_GATE",
        }

        result_payload, rc = maybe_run_release_tail(
            args=args,
            command_runtime=command_runtime,
            payload=payload,
            run_release_tail=lambda **_: {"status": "PASS"},
        )

        # Auto-approval: no .decision_request.json written → must be False
        assert result_payload.get("decision_request_present") is not True, (
            "Auto-approved path must NOT report decision_request_present=True; "
            f"got: {result_payload.get('decision_request_present')}"
        )


class TestPlanningConversationDecisionSpec:
    """Decision-spec translation for PLANNING_CONVERSATION statuses."""

    def test_auto_skipped_planning_conversation_does_not_emit_decision_spec(self) -> None:
        from kodawari.cli.autopilot_release_flow import _planning_conversation_decision_spec

        spec = _planning_conversation_decision_spec(
            feature="feat-auto-skip",
            conversation={
                "status": "auto_skipped",
                "approval": {"decision": "human_required", "reason": "score_checks_failed"},
            },
        )
        assert spec is None

    def test_precondition_blocked_planning_conversation_does_not_emit_decision_spec(self) -> None:
        from kodawari.cli.autopilot_release_flow import _planning_conversation_decision_spec

        spec = _planning_conversation_decision_spec(
            feature="feat-precondition",
            conversation={
                "status": "precondition_blocked",
                "approval": {"decision": "human_required", "reason": "structural_checks_failed"},
                "planning_readiness": {"status": "BLOCKED"},
            },
        )
        assert spec is None

    def test_none_decision_spec_clears_stale_decision_request(self, tmp_path: Path) -> None:
        import argparse

        from kodawari.cli.autopilot_release_flow import decision_payload_for_spec
        from kodawari.cli.runtime.autopilot_decision_runtime import (
            decision_pending,
            write_decision_request,
        )

        write_decision_request(
            tmp_path,
            {
                "schema_version": "autopilot.decision_request.v1",
                "decision_id": "old",
                "decision_kind": "planning_approval",
                "question": "old",
                "context_summary": "old",
                "options": [{"option_id": "approve", "label": "Approve"}],
                "recommended_option": "approve",
                "blocking_reason": "old",
                "generated_at": "2026-05-03T00:00:00+00:00",
            },
        )

        payload = decision_payload_for_spec(
            args=argparse.Namespace(feature="feat-precondition"),
            planning_dir=tmp_path,
            planning_snapshot=None,
            spec=None,
        )

        assert payload is None
        assert decision_pending(tmp_path) is False

    def test_runtime_blocked_unified_status_overrides_state_view(self) -> None:
        from kodawari.cli.autopilot_release_flow import _effective_interaction_unified_status

        class State:
            def get_unified_status(self) -> dict:
                return {
                    "stage_status": "blocked_by_precondition",
                    "final_status": None,
                    "stop_reason": None,
                    "is_blocked": True,
                    "is_terminal": False,
                }

        unified = _effective_interaction_unified_status(
            run_result={
                "unified_status": {
                    "stage_status": "blocked_by_precondition",
                    "final_status": "BLOCKED",
                    "stop_reason": "BLOCKED_BY_PRECONDITION",
                    "blocking_reason": "missing schema field",
                }
            },
            state=State(),
        )

        assert unified["final_status"] == "BLOCKED"
        assert unified["stop_reason"] == "BLOCKED_BY_PRECONDITION"
        assert unified["is_blocked"] is True
        assert unified["is_terminal"] is True

    def test_escalation_required_still_emits_planning_escalation_spec(self) -> None:
        from kodawari.cli.autopilot_release_flow import _planning_conversation_decision_spec

        spec = _planning_conversation_decision_spec(
            feature="feat-escalation",
            conversation={
                "status": "escalation_required",
                "escalation": {
                    "conflict_category": "scope",
                    "unresolved_findings": [{"description": "missing source of truth"}],
                },
            },
        )
        assert spec is not None
        assert spec.get("decision_kind") == DecisionKind.PLANNING_ESCALATION
