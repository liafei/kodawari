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


def _peer_review_enabled(
    *,
    peer_review_enabled: bool | None,
    peer_summary: dict[str, Any],
) -> bool:
    if peer_review_enabled is not None:
        return bool(peer_review_enabled)
    return bool(peer_summary.get("enabled", True))


def _review_stage_issue(
    *,
    self_review_count: int,
    peer_review_count: int,
    execution_state: str,
    loop_state: str,
    skipped: bool,
    require_real_peer_review: bool,
) -> str:
    if self_review_count > 0 or peer_review_count > 0:
        return ""
    if execution_state and execution_state not in _PASS_STATUSES:
        return f"Review stage did not execute because execution stage returned {execution_state}."
    if loop_state and loop_state not in _PASS_LOOP_REASONS:
        return f"Review stage did not execute because loop ended with {loop_state}."
    if skipped and not require_real_peer_review:
        return "Peer review was skipped for this run."
    return ""


def _required_review_counts(
    *,
    stage_issue: str,
    require_real_peer_review: bool,
    peer_review_enabled: bool,
    peer_review_skipped: bool,
) -> tuple[int, int]:
    if stage_issue:
        return 0, 0
    if require_real_peer_review:
        return 1, 1
    peer_count = 1 if peer_review_enabled and not peer_review_skipped else 0
    return 1, peer_count


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
    enabled = _peer_review_enabled(
        peer_review_enabled=peer_review_enabled,
        peer_summary=peer_summary,
    )
    skipped = bool(peer_summary.get("skipped"))
    stage_issue = _review_stage_issue(
        self_review_count=self_review_count,
        peer_review_count=peer_review_count,
        execution_state=execution_state,
        loop_state=loop_state,
        skipped=skipped,
        require_real_peer_review=require_real_peer_review,
    )
    required_self_review_count, required_peer_review_count = _required_review_counts(
        stage_issue=stage_issue,
        require_real_peer_review=require_real_peer_review,
        peer_review_enabled=enabled,
        peer_review_skipped=skipped,
    )
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


def _initial_contract_problems(
    *,
    requirements: dict[str, Any],
    issues: list[str] | None,
) -> list[str]:
    problems = [str(item).strip() for item in list(issues or []) if str(item).strip()]
    stage_issue = str(requirements.get("stage_issue") or "").strip()
    if stage_issue and stage_issue not in problems:
        problems.append(stage_issue)
    return problems


def _append_missing_review_evidence(
    problems: list[str],
    *,
    self_review_count: int,
    peer_review_count: int,
    requirements: dict[str, Any],
) -> None:
    required_self_review_count = _safe_int(requirements.get("required_self_review_count"))
    required_peer_review_count = _safe_int(requirements.get("required_peer_review_count"))
    self_review_label = str(requirements.get("self_review_label") or "self-review").strip() or "self-review"
    peer_review_label = str(requirements.get("peer_review_label") or "peer-review").strip() or "peer-review"
    if self_review_count < required_self_review_count:
        problems.append(f"Missing required {self_review_label} evidence.")
    if peer_review_count < required_peer_review_count:
        problems.append(f"Missing required {peer_review_label} evidence.")


def evaluate_review_evidence_contract(
    *,
    self_review_count: int,
    peer_review_count: int,
    must_fix_remaining: int,
    requirements: dict[str, Any],
    issues: list[str] | None = None,
) -> dict[str, Any]:
    problems = _initial_contract_problems(requirements=requirements, issues=issues)
    _append_missing_review_evidence(
        problems,
        self_review_count=self_review_count,
        peer_review_count=peer_review_count,
        requirements=requirements,
    )
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
