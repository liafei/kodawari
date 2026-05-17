"""Shared review-evidence contract helpers."""

from __future__ import annotations

from typing import Any


_PASS_STATUSES = {"PASS", "DONE", "SUCCESS"}
_PASS_LOOP_REASONS = {"PROCEED_TO_GATE", "PIPELINE_FINISH"}


def _safe_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def build_review_evidence_requirements(
    *,
    self_review_count: int,
    peer_review_count: int,
    execution_status: str = "",
    loop_reason: str = "",
    peer_review_enabled: bool | None = None,
    peer_review_summary: dict[str, Any] | None = None,
    require_real_peer_review: bool = False,
) -> dict[str, Any]:
    peer_summary = dict(peer_review_summary or {})
    execution_state = str(execution_status or "").strip().upper()
    loop_state = str(loop_reason or "").strip().upper()
    enabled = (
        bool(peer_review_enabled)
        if peer_review_enabled is not None
        else bool(peer_summary.get("enabled", True))
    )
    skipped = bool(peer_summary.get("skipped"))
    stage_issue = ""
    if self_review_count <= 0 and peer_review_count <= 0:
        if execution_state and execution_state not in _PASS_STATUSES:
            stage_issue = (
                f"Review stage did not execute because execution stage returned {execution_state}."
            )
        elif loop_state and loop_state not in _PASS_LOOP_REASONS:
            stage_issue = f"Review stage did not execute because loop ended with {loop_state}."
        elif skipped and not require_real_peer_review:
            stage_issue = "Peer review was skipped for this run."
    required_self_review_count = 0 if stage_issue else 1
    required_peer_review_count = 0 if stage_issue else 1 if require_real_peer_review else 0
    if not stage_issue and not require_real_peer_review and enabled and not skipped:
        required_peer_review_count = 1
    return {
        "required_self_review_count": required_self_review_count,
        "required_peer_review_count": required_peer_review_count,
        "self_review_label": "self-review",
        "peer_review_label": "real peer-review" if require_real_peer_review else "peer-review",
        "peer_review_enabled": enabled,
        "peer_review_skipped": skipped,
        "execution_status": execution_state,
        "loop_reason": loop_state,
        "stage_issue": stage_issue,
    }


def evaluate_review_evidence_contract(
    *,
    self_review_count: int,
    peer_review_count: int,
    must_fix_remaining: int,
    requirements: dict[str, Any],
    issues: list[str] | None = None,
) -> dict[str, Any]:
    problems = [str(item).strip() for item in list(issues or []) if str(item).strip()]
    stage_issue = str(requirements.get("stage_issue") or "").strip()
    if stage_issue and stage_issue not in problems:
        problems.append(stage_issue)
    required_self_review_count = _safe_int(requirements.get("required_self_review_count"))
    required_peer_review_count = _safe_int(requirements.get("required_peer_review_count"))
    self_review_label = str(requirements.get("self_review_label") or "self-review").strip() or "self-review"
    peer_review_label = str(requirements.get("peer_review_label") or "peer-review").strip() or "peer-review"
    if self_review_count < required_self_review_count:
        problems.append(f"Missing required {self_review_label} evidence.")
    if peer_review_count < required_peer_review_count:
        problems.append(f"Missing required {peer_review_label} evidence.")
    if must_fix_remaining > 0:
        problems.append("Must-fix items are still open.")
    status = "PASS" if not problems else "FAIL"
    return {
        "status": status,
        "blocking_reason": "" if status == "PASS" else problems[0],
        "issues": problems,
        "details": "Required review evidence present." if status == "PASS" else problems[0],
    }


__all__ = [
    "build_review_evidence_requirements",
    "evaluate_review_evidence_contract",
]
