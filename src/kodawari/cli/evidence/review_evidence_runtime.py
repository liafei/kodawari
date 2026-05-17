"""Runtime review-evidence derivation helpers."""

from __future__ import annotations

from typing import Any

from kodawari.cli.evidence.review_evidence_artifact import coerce_review_evidence_payload
from kodawari.review_evidence_contract import (
    build_review_evidence_requirements,
    evaluate_review_evidence_contract,
)


def derive_review_evidence_from_run_result(run_result: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(run_result, dict):
        return None
    self_reviews = [
        dict(item)
        for item in list(run_result.get("codex_self_reviews") or [])
        if isinstance(item, dict)
    ]
    peer_summary = dict(run_result.get("peer_review_summary") or {})
    self_review_count = len(self_reviews)
    peer_review_count = int(peer_summary.get("review_count", 0) or 0)
    must_fix_remaining = int(
        peer_summary.get("must_fix_remaining", 0)
        or len(list(run_result.get("must_fix_open_items") or []))
    )
    execution_payload = dict(run_result.get("execution_result") or {})
    execution_status = str(
        execution_payload.get("status")
        or run_result.get("execution_status")
        or ""
    ).strip().upper()
    require_real_peer_review = bool(
        run_result.get("require_real_peer_review")
        or run_result.get("real_peer_review")
        or run_result.get("require_real_opus_review")  # legacy key
        or run_result.get("real_opus_review")  # legacy key
        or peer_summary.get("real_review_required")
        or peer_summary.get("real_review_requested")
    )
    requirements = build_review_evidence_requirements(
        self_review_count=self_review_count,
        peer_review_count=peer_review_count,
        execution_status=execution_status,
        loop_reason=str(run_result.get("reason") or ""),
        peer_review_summary=peer_summary,
        require_real_peer_review=require_real_peer_review,
    )
    evaluation = evaluate_review_evidence_contract(
        self_review_count=self_review_count,
        peer_review_count=peer_review_count,
        must_fix_remaining=must_fix_remaining,
        requirements=requirements,
    )
    if (
        self_review_count <= 0
        and peer_review_count <= 0
        and not evaluation["issues"]
        and must_fix_remaining <= 0
    ):
        return None

    evidence: list[dict[str, Any]] = []
    if self_reviews:
        latest_self = dict(self_reviews[-1])
        evidence.append(
            {
                "file": ".task_run_result.json",
                "rule": "review_evidence.self_review",
                "hit": (
                    f"self review count={self_review_count}; "
                    f"source={str(latest_self.get('source') or 'unknown')}"
                ),
                "confidence": 1.0 if bool(latest_self.get("approved", False)) else 0.5,
            }
        )
    if peer_review_count > 0:
        evidence.append(
            {
                "file": ".task_run_result.json",
                "rule": "review_evidence.peer_review",
                "hit": (
                    f"peer review count={peer_review_count}; "
                    f"source={str(peer_summary.get('latest_source') or 'unknown')}; "
                    f"mode={str(peer_summary.get('latest_review_mode') or 'unknown')}"
                ),
                "confidence": 1.0 if bool(peer_summary.get("approved", False)) else 0.5,
            }
        )
    for issue in evaluation["issues"]:
        evidence.append(
            {
                "file": "<runtime>",
                "rule": "review_evidence.review_issue",
                "hit": issue,
                "confidence": 0.95,
            }
        )
    checks = {
        "self_review_count": self_review_count,
        "peer_review_count": peer_review_count,
        "must_fix_remaining": must_fix_remaining,
        "required_self_review_count": int(requirements["required_self_review_count"]),
        "required_peer_review_count": int(requirements["required_peer_review_count"]),
        "peer_review_enabled": bool(requirements["peer_review_enabled"]),
        "peer_review_skipped": bool(requirements["peer_review_skipped"]),
        "execution_status": str(requirements["execution_status"]),
    }
    return coerce_review_evidence_payload(
        {
            "status": evaluation["status"],
            "blocking_reason": evaluation["blocking_reason"],
            "details": evaluation["details"],
            "issues": list(evaluation["issues"]),
            "checks": checks,
            "evidence": evidence,
        },
        source=".task_run_result.json.runtime_reviews",
        explicit=True,
    )


__all__ = ["derive_review_evidence_from_run_result"]

