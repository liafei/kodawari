"""Verify review_approved falls back to review_evidence.status when
``peer_review_summary.approved`` is missing/None.

Regression test for the case where HTTP reviewer adapters (mimo gateway)
do not populate ``run_result.peer_review_summary``, causing run_truth to
report ``review_approved=False`` even when ``.review_evidence.json`` shows
``status=PASS`` with no blocking findings.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from kodawari.cli.evidence.artifact_truth import build_run_truth


def _planning_dir_with_review_evidence(tmp: Path, status: str, must_fix: list, blocking: int = 0) -> Path:
    pdir = tmp / "planning_dir"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / ".review_evidence.json").write_text(
        json.dumps({
            "schema_version": "review.evidence.v1",
            "status": status,
            "must_fix": must_fix,
            "blocking_findings": blocking,
        }),
        encoding="utf-8",
    )
    return pdir


def _build(planning_dir: Path, payload: dict, run_result: dict) -> dict:
    return build_run_truth(
        feature="x",
        planning_dir=planning_dir,
        project_root=planning_dir.parent,
        payload=payload,
        run_result=run_result,
        rounds=[],
    )


def test_review_approved_true_when_evidence_pass_and_summary_missing():
    """peer_summary not in run_result + evidence PASS → approved=True."""
    with tempfile.TemporaryDirectory() as tmp:
        pdir = _planning_dir_with_review_evidence(Path(tmp), status="PASS", must_fix=[])
        truth = _build(
            planning_dir=pdir,
            payload={},
            run_result={"reason": "PROCEED_TO_GATE"},
        )
        assert truth["review_approved"] is True


def test_review_approved_false_when_evidence_fail():
    """peer_summary missing + evidence FAIL → approved=False (no incorrect upgrade)."""
    with tempfile.TemporaryDirectory() as tmp:
        pdir = _planning_dir_with_review_evidence(
            Path(tmp), status="FAIL", must_fix=["fix something"], blocking=1
        )
        truth = _build(
            planning_dir=pdir,
            payload={},
            run_result={"reason": "OPUS_REVIEW_BLOCKED"},
        )
        assert truth["review_approved"] is False


def test_review_approved_false_when_evidence_pass_but_must_fix_present():
    """Even if status=PASS, presence of must_fix items → not auto-approved."""
    with tempfile.TemporaryDirectory() as tmp:
        pdir = _planning_dir_with_review_evidence(
            Path(tmp), status="PASS", must_fix=["some lingering finding"]
        )
        truth = _build(
            planning_dir=pdir,
            payload={},
            run_result={"reason": "any"},
        )
        assert truth["review_approved"] is False


def test_review_approved_uses_summary_when_present():
    """If peer_review_summary.approved is explicitly set, do not override."""
    with tempfile.TemporaryDirectory() as tmp:
        pdir = _planning_dir_with_review_evidence(Path(tmp), status="PASS", must_fix=[])
        truth = _build(
            planning_dir=pdir,
            payload={},
            run_result={
                "reason": "OPUS_REVIEW_BLOCKED",
                "peer_review_summary": {"approved": False, "review_count": 1},
            },
        )
        # Explicit summary wins — even though evidence PASS, user-set approved=False
        assert truth["review_approved"] is False


def test_review_approved_no_evidence_file_keeps_legacy_behavior():
    """Without .review_evidence.json, default behavior unchanged: approved=False."""
    with tempfile.TemporaryDirectory() as tmp:
        pdir = Path(tmp) / "planning_dir"
        pdir.mkdir(parents=True, exist_ok=True)
        truth = _build(
            planning_dir=pdir,
            payload={},
            run_result={"reason": "any"},
        )
        assert truth["review_approved"] is False
