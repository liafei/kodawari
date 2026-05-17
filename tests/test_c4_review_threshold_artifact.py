"""Tests for C4 — review threshold parameterization + artifact profile primitive.

Covers:
  - _is_blocking_finding accepts threshold and returns correct membership
  - _blocking_findings respects threshold parameter
  - PlanningConfig has blocking_severities field with default
  - DEFAULT_BLOCKING_SEVERITIES preserves pre-C4 behavior
  - should_emit_artifact() correctly gates by suppressed_artifacts
  - _ensure_design_artifact respects policy.suppressed_artifacts
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kodawari.autopilot.lane_config import HEAVY_LANE, LITE_LANE, STANDARD_LANE
from kodawari.autopilot.planning_orchestrator import (
    DEFAULT_BLOCKING_SEVERITIES,
    PlanningConfig,
    _blocking_findings,
    _is_blocking_finding,
)
from kodawari.autopilot.workflow_policy import (
    ComplexityDecision,
    UserPolicyOverrides,
    resolve_workflow_policy,
    should_emit_artifact,
)
from kodawari.cli.delivery_common import _ensure_design_artifact


def _decision(tier: str = "lite"):
    return ComplexityDecision(
        tier=tier, confidence=1.0, source="explicit", static_score=0,
        hard_rule="", reasons=(), risk_flags=(), llm_used=False,
        learned_adjustments=(),
    )


# ---------------------------------------------------------------------------
# _is_blocking_finding parameterization
# ---------------------------------------------------------------------------


def test_is_blocking_finding_default_includes_high():
    """Pre-C4 default behavior: high counts as blocking."""
    item = {"severity": "high"}
    assert _is_blocking_finding(item) is True


def test_is_blocking_finding_default_includes_critical():
    item = {"severity": "critical"}
    assert _is_blocking_finding(item) is True


def test_is_blocking_finding_default_excludes_medium():
    assert _is_blocking_finding({"severity": "medium"}) is False
    assert _is_blocking_finding({"severity": "low"}) is False
    assert _is_blocking_finding({"severity": "info"}) is False


def test_is_blocking_finding_lite_threshold_excludes_high():
    """LITE tier threshold = blocking only — high becomes warning."""
    threshold = LITE_LANE.review_blocking_threshold  # frozenset({"blocking"})
    assert _is_blocking_finding({"severity": "high"}, threshold) is False
    assert _is_blocking_finding({"severity": "critical"}, threshold) is False
    assert _is_blocking_finding({"severity": "blocking"}, threshold) is True


def test_is_blocking_finding_standard_threshold_excludes_high():
    """STANDARD tier threshold = blocking + critical — high is not blocker."""
    threshold = STANDARD_LANE.review_blocking_threshold
    assert _is_blocking_finding({"severity": "high"}, threshold) is False
    assert _is_blocking_finding({"severity": "critical"}, threshold) is True


def test_is_blocking_finding_heavy_threshold_keeps_high():
    threshold = HEAVY_LANE.review_blocking_threshold
    assert _is_blocking_finding({"severity": "high"}, threshold) is True
    assert _is_blocking_finding({"severity": "critical"}, threshold) is True
    assert _is_blocking_finding({"severity": "blocking"}, threshold) is True


# ---------------------------------------------------------------------------
# _blocking_findings respects threshold
# ---------------------------------------------------------------------------


def test_blocking_findings_default_threshold_includes_high():
    review = {"findings": [
        {"severity": "high", "description": "missing flag"},
        {"severity": "medium", "description": "doc nitpick"},
    ]}
    blocked = _blocking_findings(review_payload=review, structural_issues=[])
    assert len(blocked) == 1
    assert blocked[0]["severity"] == "high"


def test_blocking_findings_lite_threshold_drops_high():
    review = {"findings": [
        {"severity": "high", "description": "missing flag"},
        {"severity": "blocking", "description": "real blocker"},
    ]}
    blocked = _blocking_findings(
        review_payload=review,
        structural_issues=[],
        threshold=LITE_LANE.review_blocking_threshold,
    )
    assert len(blocked) == 1
    assert blocked[0]["severity"] == "blocking"


def test_blocking_findings_includes_structural_issues_regardless_of_threshold():
    """Structural issues always count as blocking (they're structural, not severity-driven)."""
    blocked = _blocking_findings(
        review_payload={"findings": []},
        structural_issues=["bad_plan_shape"],
        threshold=LITE_LANE.review_blocking_threshold,
    )
    assert len(blocked) == 1
    assert blocked[0]["severity"] == "blocking"
    assert "bad_plan_shape" in blocked[0]["description"]


# ---------------------------------------------------------------------------
# PlanningConfig new field
# ---------------------------------------------------------------------------


def test_planning_config_blocking_severities_default_matches_pre_c4():
    config = PlanningConfig()
    assert config.blocking_severities == DEFAULT_BLOCKING_SEVERITIES


def test_planning_config_blocking_severities_can_be_overridden():
    config = PlanningConfig(blocking_severities=frozenset({"blocking"}))
    assert config.blocking_severities == frozenset({"blocking"})
    assert "high" not in config.blocking_severities


def test_default_blocking_severities_is_frozenset():
    assert isinstance(DEFAULT_BLOCKING_SEVERITIES, frozenset)
    assert DEFAULT_BLOCKING_SEVERITIES == frozenset({"blocking", "critical", "high"})


# ---------------------------------------------------------------------------
# should_emit_artifact primitive
# ---------------------------------------------------------------------------


def test_should_emit_artifact_returns_true_when_policy_none():
    """Back-compat: legacy callers without policy keep emitting everything."""
    assert should_emit_artifact("DESIGN.md", None) is True
    assert should_emit_artifact("RELEASE.md", None) is True


def test_should_emit_artifact_lite_policy_suppresses_design():
    policy = resolve_workflow_policy(decision=_decision("lite"), lane=LITE_LANE)
    assert should_emit_artifact("DESIGN.md", policy) is False
    assert should_emit_artifact("QA_REPORT.md", policy) is False
    assert should_emit_artifact("RELEASE.md", policy) is False


def test_should_emit_artifact_standard_policy_keeps_design():
    """STANDARD allows DESIGN.md (just suppresses QA/RELEASE)."""
    policy = resolve_workflow_policy(decision=_decision("standard"), lane=STANDARD_LANE)
    assert should_emit_artifact("DESIGN.md", policy) is True
    assert should_emit_artifact("QA_REPORT.md", policy) is False


def test_should_emit_artifact_heavy_policy_emits_everything():
    policy = resolve_workflow_policy(decision=_decision("heavy"), lane=HEAVY_LANE)
    assert should_emit_artifact("DESIGN.md", policy) is True
    assert should_emit_artifact("RELEASE.md", policy) is True
    assert should_emit_artifact("anything.json", policy) is True


# ---------------------------------------------------------------------------
# _ensure_design_artifact respects policy
# ---------------------------------------------------------------------------


def test_ensure_design_artifact_writes_when_no_policy(tmp_path):
    """Back-compat: no policy => always writes."""
    _ensure_design_artifact(planning_dir=tmp_path, feature="f1", state_payload=None)
    assert (tmp_path / "DESIGN.md").exists()


def test_ensure_design_artifact_writes_for_heavy_policy(tmp_path):
    policy = resolve_workflow_policy(decision=_decision("heavy"), lane=HEAVY_LANE)
    _ensure_design_artifact(
        planning_dir=tmp_path, feature="f1", state_payload=None, policy=policy,
    )
    assert (tmp_path / "DESIGN.md").exists()


def test_ensure_design_artifact_skipped_for_lite_policy(tmp_path):
    """LITE suppresses DESIGN.md — file must NOT be created."""
    policy = resolve_workflow_policy(decision=_decision("lite"), lane=LITE_LANE)
    _ensure_design_artifact(
        planning_dir=tmp_path, feature="f1", state_payload=None, policy=policy,
    )
    assert not (tmp_path / "DESIGN.md").exists()


def test_ensure_design_artifact_writes_for_standard_policy(tmp_path):
    """STANDARD does NOT suppress DESIGN.md."""
    policy = resolve_workflow_policy(decision=_decision("standard"), lane=STANDARD_LANE)
    _ensure_design_artifact(
        planning_dir=tmp_path, feature="f1", state_payload=None, policy=policy,
    )
    assert (tmp_path / "DESIGN.md").exists()


def test_ensure_design_artifact_idempotent_no_overwrite_on_repeat(tmp_path):
    policy = resolve_workflow_policy(decision=_decision("heavy"), lane=HEAVY_LANE)
    _ensure_design_artifact(planning_dir=tmp_path, feature="f1", state_payload=None, policy=policy)
    first_content = (tmp_path / "DESIGN.md").read_text(encoding="utf-8")
    # Second call must not overwrite
    _ensure_design_artifact(planning_dir=tmp_path, feature="f1", state_payload=None, policy=policy)
    assert (tmp_path / "DESIGN.md").read_text(encoding="utf-8") == first_content
