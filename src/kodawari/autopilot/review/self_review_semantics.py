"""Runtime semantics builder for Codex self-review payloads."""

from __future__ import annotations

from typing import Any


def _count_approved(entries: list[dict[str, Any]]) -> int:
    return sum(1 for item in entries if bool(item.get("approved", False)))


def _summary_payload(payload: dict[str, Any]) -> dict[str, Any]:
    summary = payload.get("self_review_summary")
    if isinstance(summary, dict):
        return dict(summary)
    return {}


def _review_entries(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw = payload.get("codex_self_reviews", [])
    if not isinstance(raw, list):
        return []
    return [dict(item) for item in raw if isinstance(item, dict)]


def _self_reviewers(entries: list[dict[str, Any]], summary: dict[str, Any]) -> list[str]:
    from_summary = [str(item) for item in list(summary.get("reviewers", [])) if str(item).strip()]
    if from_summary:
        return from_summary
    return sorted(
        {
            str(item.get("reviewer") or "").strip().lower()
            for item in entries
            if str(item.get("reviewer") or "").strip()
        }
    )


def _boundary_enforced_count(entries: list[dict[str, Any]], summary: dict[str, Any]) -> int:
    if summary.get("actor_boundary_enforced_count") is not None:
        return int(summary.get("actor_boundary_enforced_count") or 0)
    return sum(1 for item in entries if bool(item.get("actor_boundary_enforced")))


def build_self_review_runtime_semantics(payload: dict[str, Any]) -> dict[str, Any]:
    summary = _summary_payload(payload)
    reviews = _review_entries(payload)
    approved = _count_approved(reviews)
    return {
        "count": int(summary.get("review_count", len(reviews)) or 0),
        "approved_count": int(summary.get("approved_count", approved) or 0),
        "rejected_count": int(summary.get("rejected_count", max(0, len(reviews) - approved)) or 0),
        "reviewers": _self_reviewers(reviews, summary),
        "actor_boundary_enforced_count": _boundary_enforced_count(reviews, summary),
        "latest": dict(reviews[-1]) if reviews else None,
    }
