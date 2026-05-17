import json
from pathlib import Path

from kodawari.cli.artifact_truth import resolve_review_evidence_truth
from kodawari.cli.delivery_evidence import _review_evidence
from kodawari.cli.review_evidence_runtime import derive_review_evidence_from_run_result


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_review_evidence_reconciles_legacy_payload_for_claude_blocked_run(tmp_path: Path) -> None:
    planning_dir = tmp_path / "planning" / "feature-a"
    _write_json(
        planning_dir / ".review_evidence.json",
        {
            "schema_version": "review.evidence.v1",
            "generated_at": "2026-04-02T00:00:00+00:00",
            "feature": "feature-a",
            "planning_dir": str(planning_dir),
            "entrypoint": "kodawari task-run",
            "status": "FAIL",
            "blocking_reason": "Missing Codex self-review evidence.",
            "details": "Missing Codex self-review evidence.",
            "issues": [],
            "checks": {
                "self_review_count": 0,
                "peer_review_count": 0,
                "must_fix_remaining": 0,
            },
            "evidence": [],
        },
    )
    _write_json(
        planning_dir / ".execution_result.json",
        {
            "schema_version": "execution.result.v1",
            "feature": "feature-a",
            "task": "T1",
            "backend": "claude_code",
            "status": "BLOCKED",
            "changed_files": [],
        },
    )

    payload = _review_evidence(
        planning_dir=planning_dir,
        workflow_chain={
            "peer_review_enabled": True,
            "upstream": {
                "status": "BLOCKED",
                "reason": "CLAUDE_CODE_CHANGED_FILES_MISSING",
                "peer_review_enabled": True,
                "peer_review_runtime": {
                    "real_requested": False,
                    "real_required": False,
                },
            },
            "final_quality_review": {"status": "BLOCKED"},
        },
        semantic_compact=None,
        gate_payload={"total_status": "PASS"},
    )

    assert payload["review_evidence_status"] == "PASS"
    assert payload["review_evidence_source"] == "reconciled:.review_evidence.json"
    checks = dict(payload["review_evidence_payload"]["checks"])
    assert checks["required_self_review"] is False
    assert checks["required_peer_review"] is False


def test_review_evidence_reconciles_stale_fail_after_later_peer_approval(tmp_path: Path) -> None:
    planning_dir = tmp_path / "planning" / "feature-peer-pass"
    _write_json(
        planning_dir / ".review_evidence.json",
        {
            "schema_version": "review.evidence.v1",
            "generated_at": "2026-04-02T00:00:00+00:00",
            "feature": "feature-peer-pass",
            "planning_dir": str(planning_dir),
            "entrypoint": "kodawari autopilot",
            "status": "FAIL",
            "blocking_reason": "Peer review is not approved.",
            "issues": [
                "Peer review is not approved.",
                "Must-fix items are still open.",
            ],
            "checks": {
                "self_review_count": 0,
                "peer_review_count": 1,
                "must_fix_remaining": 4,
                "required_self_review": False,
                "required_peer_review": True,
            },
            "evidence": [],
        },
    )
    _write_json(
        planning_dir / ".execution_result.json",
        {
            "schema_version": "execution.result.v1",
            "feature": "feature-peer-pass",
            "task": "T1",
            "backend": "claude_code",
            "status": "PASS",
            "changed_files": ["backend/api.py", "tests/test_api.py"],
        },
    )

    payload = _review_evidence(
        planning_dir=planning_dir,
        workflow_chain={
            "peer_review_enabled": True,
            "upstream": {
                "status": "PASS",
                "reason": "PROCEED_TO_GATE",
                "peer_review_enabled": True,
                "peer_review_summary": {
                    "review_count": 2,
                    "approved": True,
                    "must_fix_remaining": 0,
                },
                "peer_review_runtime": {
                    "real_requested": True,
                    "real_required": True,
                },
            },
            "final_quality_review": {"status": "PASS"},
        },
        semantic_compact={"must_fix": []},
        gate_payload={"total_status": "PASS"},
        review_payload={"status": "PASS"},
    )

    assert payload["review_evidence_status"] == "PASS"
    assert payload["review_evidence_source"] == "reconciled:.review_evidence.json"
    checks = dict(payload["review_evidence_payload"]["checks"])
    assert checks["required_self_review"] is False
    assert checks["required_peer_review"] is True
    assert checks["peer_review_count"] == 2
    assert checks["must_fix_remaining"] == 0
    assert payload["review_evidence_payload"]["issues"] == []


def test_review_evidence_truth_marks_legacy_contract_artifact_stale(tmp_path: Path) -> None:
    planning_dir = tmp_path / "planning" / "feature-b"
    _write_json(
        planning_dir / ".review_evidence.json",
        {
            "schema_version": "review.evidence.v1",
            "generated_at": "2026-04-02T00:00:00+00:00",
            "feature": "feature-b",
            "planning_dir": str(planning_dir),
            "entrypoint": "kodawari task-run",
            "status": "FAIL",
            "checks": {
                "self_review_count": 0,
                "peer_review_count": 0,
                "must_fix_remaining": 0,
            },
            "issues": [],
            "evidence": [],
        },
    )

    truth = resolve_review_evidence_truth(
        planning_dir=planning_dir,
        review_result_truth={"stale": False},
    )

    assert truth["stale"] is True
    assert "review_evidence_legacy_contract" in truth["stale_reasons"]


def test_review_evidence_runtime_respects_legacy_require_real_opus_review_flag() -> None:
    payload = derive_review_evidence_from_run_result(
        {
            "require_real_opus_review": True,
            "peer_review_summary": {
                "review_count": 0,
                "enabled": True,
                "skipped": True,
            },
            "codex_self_reviews": [
                {
                    "approved": True,
                    "source": "kodawari.codex_self_review",
                }
            ],
            "must_fix_open_items": [],
            "execution_result": {"status": "PASS"},
            "reason": "PASS",
        }
    )

    assert payload is not None
    assert payload["status"] == "FAIL"
    assert payload["blocking_reason"] == "Missing required real peer-review evidence."
    checks = dict(payload["checks"])
    assert checks["required_peer_review_count"] == 1
    assert checks["peer_review_skipped"] is True
