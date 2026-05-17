"""Backend-aware review evidence contract helpers."""

from __future__ import annotations

from typing import Any

from kodawari.autopilot.execution.execution_backend import execution_backend_descriptor


MISSING_SELF_REVIEW_ISSUE = "Missing Codex self-review evidence."
MISSING_PEER_REVIEW_ISSUE = "Missing Opus peer-review evidence."


def resolve_review_evidence_requirements(
    *,
    execution_backend: str,
    self_review_count: int = 0,
    peer_review_summary: dict[str, Any] | None = None,
    peer_review_enabled: bool | None = None,
    require_real_peer_review: bool = False,
    default_require_self_review: bool = False,
) -> dict[str, Any]:
    peer_summary = dict(peer_review_summary or {})
    backend = str(execution_backend or "").strip().lower()
    contract_known = bool(backend)

    peer_review_count = int(peer_summary.get("review_count", 0) or 0)
    peer_flags_present = any(
        key in peer_summary
        for key in ("enabled", "skipped", "real_review_requested", "real_review_required")
    )
    real_requested = bool(peer_summary.get("real_review_requested", False))
    real_required = bool(peer_summary.get("real_review_required", False))
    effective_peer_enabled = (
        bool(peer_review_enabled)
        if peer_review_enabled is not None
        else bool(peer_summary.get("enabled", False))
    )
    review_loop_disabled = (
        peer_review_enabled is False
        or (peer_review_enabled is None and peer_flags_present and not effective_peer_enabled)
    )
    require_self_review = bool(default_require_self_review)
    if backend:
        require_self_review = bool(execution_backend_descriptor(backend).self_review_selectable)
    if review_loop_disabled:
        # In the collaboration flow, self-review is a follow-up inside the
        # peer-review loop.  If the loop is explicitly disabled, proceed
        # evidence must not require a self-review round that was never queued.
        require_self_review = False
    if int(self_review_count or 0) > 0:
        require_self_review = True
        contract_known = True

    require_peer_review = bool(
        require_real_peer_review or real_requested or real_required or effective_peer_enabled or peer_review_count > 0
    )
    if peer_flags_present or peer_review_count > 0 or require_real_peer_review:
        contract_known = True

    return {
        "execution_backend": backend,
        "contract_known": contract_known,
        "require_self_review": require_self_review,
        "require_peer_review": require_peer_review,
        "peer_review_enabled": effective_peer_enabled,
        "real_requested": real_requested,
        "real_required": bool(require_real_peer_review or real_required),
    }


def derive_runtime_review_evidence(
    *,
    run_result: dict[str, Any],
    execution_backend: str,
) -> dict[str, Any] | None:
    if not isinstance(run_result, dict):
        return None
    self_reviews = [dict(item) for item in list(run_result.get("codex_self_reviews") or []) if isinstance(item, dict)]
    peer_summary = dict(run_result.get("peer_review_summary") or {})
    self_review_count = len(self_reviews)
    peer_review_count = int(peer_summary.get("review_count", 0) or 0)
    must_fix_remaining = int(
        peer_summary.get("must_fix_remaining", 0) or len(list(run_result.get("must_fix_open_items") or []))
    )
    requirements = resolve_review_evidence_requirements(
        execution_backend=execution_backend,
        self_review_count=self_review_count,
        peer_review_summary=peer_summary,
    )
    if (
        self_review_count <= 0
        and peer_review_count <= 0
        and must_fix_remaining <= 0
        and not bool(requirements.get("contract_known"))
    ):
        return None

    issues: list[str] = []
    if bool(requirements.get("require_self_review")) and self_review_count <= 0:
        issues.append(MISSING_SELF_REVIEW_ISSUE)
    if bool(requirements.get("require_peer_review")) and peer_review_count <= 0:
        issues.append(MISSING_PEER_REVIEW_ISSUE)
    if peer_review_count > 0 and not bool(peer_summary.get("approved", False)):
        issues.append("Peer review is not approved.")
    if must_fix_remaining > 0:
        issues.append("Must-fix items are still open.")

    return {
        "status": "PASS" if not issues else "FAIL",
        "blocking_reason": "" if not issues else issues[0],
        "details": _review_contract_details(requirements=requirements, issues=issues),
        "issues": issues,
        "checks": {
            "self_review_count": self_review_count,
            "peer_review_count": peer_review_count,
            "must_fix_remaining": must_fix_remaining,
            "required_self_review": bool(requirements.get("require_self_review")),
            "required_peer_review": bool(requirements.get("require_peer_review")),
            "contract_known": bool(requirements.get("contract_known")),
        },
        "review_contract": {
            "execution_backend": str(requirements.get("execution_backend") or execution_backend or ""),
            "require_self_review": bool(requirements.get("require_self_review")),
            "require_peer_review": bool(requirements.get("require_peer_review")),
            "contract_known": bool(requirements.get("contract_known")),
        },
        "evidence": _runtime_review_evidence_items(
            self_reviews=self_reviews,
            peer_summary=peer_summary,
            execution_backend=str(requirements.get("execution_backend") or execution_backend or ""),
            requirements=requirements,
        ),
    }


def _runtime_review_evidence_items(
    *,
    self_reviews: list[dict[str, Any]],
    peer_summary: dict[str, Any],
    execution_backend: str,
    requirements: dict[str, Any],
) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = [
        {
            "file": ".task_run_result.json",
            "rule": "review_evidence.contract",
            "hit": (
                f"backend={execution_backend or 'unknown'}; "
                f"require_self_review={bool(requirements.get('require_self_review'))}; "
                f"require_peer_review={bool(requirements.get('require_peer_review'))}"
            ),
            "confidence": 1.0 if bool(requirements.get("contract_known")) else 0.5,
        }
    ]
    if self_reviews:
        latest_self = dict(self_reviews[-1])
        evidence.append(
            {
                "file": ".task_run_result.json",
                "rule": "review_evidence.self_review",
                "hit": (
                    f"self review count={len(self_reviews)}; "
                    f"source={str(latest_self.get('source') or 'unknown')}"
                ),
                "confidence": 1.0 if bool(latest_self.get("approved", False)) else 0.5,
            }
        )
    peer_review_count = int(peer_summary.get("review_count", 0) or 0)
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
    return evidence


def _review_contract_details(*, requirements: dict[str, Any], issues: list[str]) -> str:
    if issues:
        return issues[0]
    if not bool(requirements.get("require_self_review")) and not bool(requirements.get("require_peer_review")):
        return "Review evidence satisfies backend-aligned contract without additional self/peer review requirements."
    return "Review evidence satisfies backend-aligned contract."


__all__ = [
    "MISSING_PEER_REVIEW_ISSUE",
    "MISSING_SELF_REVIEW_ISSUE",
    "derive_runtime_review_evidence",
    "resolve_review_evidence_requirements",
]

