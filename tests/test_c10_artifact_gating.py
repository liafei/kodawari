"""Tests for C10 — extended artifact gating via should_emit_artifact.

Covers:
  - _ensure_placeholder_markdown honors policy.suppressed_artifacts
  - build_qa_report respects policy for QA_REPORT.md + .qa_report.json
  - build_ship_readiness_report respects policy for .ship_readiness.json + RELEASE.md
  - HEAVY lane generates all artifacts
  - LITE lane suppresses RELEASE.md / QA_REPORT.md / .ship_readiness.json
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kodawari.autopilot.lane_config import HEAVY_LANE, LITE_LANE, STANDARD_LANE
from kodawari.autopilot.workflow_policy import (
    ComplexityDecision,
    resolve_workflow_policy,
)
from kodawari.cli.autopilot_cmd import _cleanup_suppressed_artifacts
from kodawari.cli.delivery_common import _ensure_placeholder_markdown


def _policy(tier: str):
    lanes = {"lite": LITE_LANE, "standard": STANDARD_LANE, "heavy": HEAVY_LANE}
    decision = ComplexityDecision(
        tier=tier, confidence=1.0, source="explicit", static_score=0,
        hard_rule="", reasons=(), risk_flags=(), llm_used=False,
        learned_adjustments=(),
    )
    return resolve_workflow_policy(decision=decision, lane=lanes[tier])


# ---------------------------------------------------------------------------
# _ensure_placeholder_markdown gating
# ---------------------------------------------------------------------------


def test_placeholder_writes_when_no_policy(tmp_path):
    """Back-compat: legacy callers (no policy) always write."""
    path = tmp_path / "QA_REPORT.md"
    _ensure_placeholder_markdown(path, title="QA")
    assert path.exists()


def test_placeholder_writes_for_heavy_policy(tmp_path):
    path = tmp_path / "QA_REPORT.md"
    _ensure_placeholder_markdown(path, title="QA", policy=_policy("heavy"))
    assert path.exists()


def test_placeholder_skipped_for_lite_policy_qa_report(tmp_path):
    """LITE suppresses QA_REPORT.md."""
    path = tmp_path / "QA_REPORT.md"
    _ensure_placeholder_markdown(path, title="QA", policy=_policy("lite"))
    assert not path.exists()


def test_placeholder_skipped_for_standard_policy_qa_report(tmp_path):
    """STANDARD also suppresses QA_REPORT.md (per C4 spec: STANDARD stays light)."""
    path = tmp_path / "QA_REPORT.md"
    _ensure_placeholder_markdown(path, title="QA", policy=_policy("standard"))
    assert not path.exists()


def test_placeholder_writes_review_md_for_standard(tmp_path):
    """STANDARD does NOT suppress REVIEW.md."""
    path = tmp_path / "REVIEW.md"
    _ensure_placeholder_markdown(path, title="REVIEW", policy=_policy("standard"))
    assert path.exists()


def test_placeholder_skipped_for_lite_release_md(tmp_path):
    path = tmp_path / "RELEASE.md"
    _ensure_placeholder_markdown(path, title="RELEASE", policy=_policy("lite"))
    assert not path.exists()


def test_placeholder_idempotent_on_repeat(tmp_path):
    path = tmp_path / "QA_REPORT.md"
    _ensure_placeholder_markdown(path, title="QA", policy=_policy("heavy"))
    first = path.read_text(encoding="utf-8")
    _ensure_placeholder_markdown(path, title="QA_DIFFERENT_TITLE", policy=_policy("heavy"))
    # Existing file not overwritten
    assert path.read_text(encoding="utf-8") == first


# ---------------------------------------------------------------------------
# should_emit_artifact matrix — simple contract re-verification
# ---------------------------------------------------------------------------


def test_artifact_matrix_lite_suppresses_ship_and_release():
    from kodawari.autopilot.workflow_policy import should_emit_artifact

    p = _policy("lite")
    assert should_emit_artifact(".ship_readiness.json", p) is False
    assert should_emit_artifact("RELEASE.md", p) is False
    assert should_emit_artifact("QA_REPORT.md", p) is False


def test_artifact_matrix_heavy_emits_all():
    from kodawari.autopilot.workflow_policy import should_emit_artifact

    p = _policy("heavy")
    for name in (".ship_readiness.json", "RELEASE.md", "QA_REPORT.md", ".qa_report.json", "DESIGN.md", "REVIEW.md"):
        assert should_emit_artifact(name, p) is True


def test_artifact_matrix_standard_suppresses_release_keeps_design():
    from kodawari.autopilot.workflow_policy import should_emit_artifact

    p = _policy("standard")
    assert should_emit_artifact("DESIGN.md", p) is True
    assert should_emit_artifact("REVIEW.md", p) is True
    assert should_emit_artifact("RELEASE.md", p) is False
    assert should_emit_artifact("QA_REPORT.md", p) is False
    assert should_emit_artifact(".ship_readiness.json", p) is False


def test_cleanup_suppressed_artifacts_removes_policy_blocked_files(tmp_path):
    planning_dir = tmp_path / "planning" / "f1"
    planning_dir.mkdir(parents=True, exist_ok=True)
    (planning_dir / "RELEASE.md").write_text("# release\n", encoding="utf-8")
    (planning_dir / "QA_REPORT.md").write_text("# qa\n", encoding="utf-8")
    (planning_dir / "DESIGN.md").write_text("# design\n", encoding="utf-8")

    _cleanup_suppressed_artifacts(
        planning_dir=planning_dir,
        policy=_policy("lite"),
        policy_active=True,
    )
    assert not (planning_dir / "RELEASE.md").exists()
    assert not (planning_dir / "QA_REPORT.md").exists()
    assert not (planning_dir / "DESIGN.md").exists()


def test_cleanup_suppressed_artifacts_noop_when_policy_inactive(tmp_path):
    planning_dir = tmp_path / "planning" / "f1"
    planning_dir.mkdir(parents=True, exist_ok=True)
    release_path = planning_dir / "RELEASE.md"
    release_path.write_text("# release\n", encoding="utf-8")

    _cleanup_suppressed_artifacts(
        planning_dir=planning_dir,
        policy=_policy("lite"),
        policy_active=False,
    )
    assert release_path.exists()
