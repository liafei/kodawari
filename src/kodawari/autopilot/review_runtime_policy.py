"""Shared runtime classification helpers for peer-review semantics.

This module intentionally classifies runtime facts only. It does not encode
engine policy decisions (for example: whether degraded review should block).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import os
from typing import Any

# Keep this set local to avoid package-init import cycles in early runtime
# modules (engine/context/status import this helper before review package init).
REAL_REVIEW_MODES = frozenset(
    {
        "real_peer_review_gateway",
        "real_opus_gateway",
        "real_cli_reviewer",
        "real_mcp_reviewer",
        "real_codex_reviewer",
    }
)


_UNAVAILABLE_REVIEW_MODES = frozenset(
    {
        "real_required_failed",
        "real_requested_failed",
    }
)


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _bool_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = _clean_text(value).lower()
    if not text:
        return False
    return text in {"1", "true", "yes", "on"}


def review_quality_grading_enabled() -> bool:
    mode = _clean_text(os.environ.get("WORKFLOW_REVIEW_QUALITY_GRADING", "1")).lower()
    return mode not in {"0", "false", "off", "no"}


@dataclass(frozen=True)
class ReviewRuntimeClassification:
    real_requested: bool
    real_required: bool
    fallback_used: bool
    mode: str
    is_real_review: bool
    semantic_review_performed: bool
    review_quality: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def classify_review_runtime(
    runtime: dict[str, Any] | None,
    *,
    require_real_peer_review: bool = False,
) -> ReviewRuntimeClassification:
    payload = dict(runtime or {})
    mode = _clean_text(payload.get("mode"))
    real_requested = _bool_value(payload.get("real_requested"))
    real_required = bool(require_real_peer_review or _bool_value(payload.get("real_required")))
    fallback_used = _bool_value(payload.get("fallback_used"))
    is_real_review = mode in REAL_REVIEW_MODES
    semantic_review_performed = bool(is_real_review and not fallback_used)

    if semantic_review_performed:
        review_quality = "real"
    elif real_requested and fallback_used:
        review_quality = "degraded"
    elif real_requested and (not mode or mode in _UNAVAILABLE_REVIEW_MODES):
        review_quality = "unavailable"
    elif not real_requested and (not mode or mode == "simulate_local"):
        review_quality = "simulated"
    elif is_real_review:
        review_quality = "real"
    elif mode == "simulate_local":
        review_quality = "degraded" if real_requested else "simulated"
    else:
        review_quality = "unavailable" if real_requested else "simulated"

    return ReviewRuntimeClassification(
        real_requested=real_requested,
        real_required=real_required,
        fallback_used=fallback_used,
        mode=mode,
        is_real_review=is_real_review,
        semantic_review_performed=semantic_review_performed,
        review_quality=review_quality,
    )


REVIEW_QUALITY_VALUES: frozenset[str] = frozenset({"real", "degraded", "simulated", "unavailable"})


def review_quality_acceptance(
    quality: str,
    *,
    release_phase: bool = False,
) -> dict[str, Any]:
    """Return the structured acceptance decision for a review_quality value.

    Centralizes the matrix of "which review_quality modes count as a pass for
    which phase" so callers (gate, release flow, delivery report, dashboards)
    no longer encode the rule inline. Phases:

      * non-release (default): a degraded / simulated review can still flow
        through dev iterations because the gate enforces structural checks
      * release_phase=True: only ``real`` is acceptable; everything else
        blocks the release until a real reviewer signs off

    Returns: ``{"accept": bool, "block_release": bool, "reason": str}``.
    """

    normalized = _clean_text(quality).lower()
    if normalized == "real":
        return {"accept": True, "block_release": False, "reason": "semantic_review_performed"}
    if normalized == "simulated":
        return {
            "accept": not release_phase,
            "block_release": True,
            "reason": "simulate_local_only" if release_phase else "simulate_local_dev_ok",
        }
    if normalized == "degraded":
        return {"accept": False, "block_release": True, "reason": "fallback_used"}
    if normalized == "unavailable":
        return {"accept": False, "block_release": True, "reason": "real_review_unavailable"}
    # Unknown / empty quality strings are treated as unavailable so a
    # serialization gap cannot silently wave a release through.
    return {"accept": False, "block_release": True, "reason": "unknown_quality"}


__all__ = [
    "REAL_REVIEW_MODES",
    "REVIEW_QUALITY_VALUES",
    "ReviewRuntimeClassification",
    "classify_review_runtime",
    "review_quality_acceptance",
    "review_quality_grading_enabled",
]
