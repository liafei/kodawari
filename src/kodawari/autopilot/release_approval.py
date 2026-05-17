"""Semi-automatic release approval decision.

Reads a RunTruth payload (and optional task_card / readiness payload) and
returns either AUTO_APPROVE or MANUAL_REQUIRED with the list of risk triggers
that fired. Centralizes the rule so the release flow, the release audit
script, and the gate all branch on the same decision.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from kodawari.autopilot.review_runtime_policy import review_quality_acceptance


_HEAVY_TIER_VALUES: frozenset[str] = frozenset({"heavy"})
_SCHEMA_MUTATION_PATH_HINTS: tuple[str, ...] = (
    "/migration",
    ".sql",
    "/schema/",
    "models.py",
    "tables.py",
)


@dataclass(frozen=True)
class ReleaseApprovalDecision:
    mode: str
    triggers: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def evaluate_release_approval(
    *,
    run_truth: dict[str, Any] | None,
    task_card: dict[str, Any] | None = None,
    readiness_history: list[dict[str, Any]] | None = None,
    recovery_total_threshold: int = 5,
) -> ReleaseApprovalDecision:
    """Decide whether the release can auto-approve or must be human-confirmed.

    Triggers (any one fires manual_required):
      - heavy_lane: lane_observation actual_tier or predicted_tier is heavy
      - schema_mutation: changed_files include schema/migration paths
      - readiness_blocked_history: any readiness payload in the run was BLOCKED
      - recovery_pressure: total recovery attempts >= recovery_total_threshold
      - non_real_review_quality: review_quality not in the accepted release set
    """

    truth = dict(run_truth or {})
    triggers: list[str] = []
    detail: dict[str, Any] = {}

    tier = _heavy_tier(truth)
    if tier:
        triggers.append("heavy_lane")
        detail["lane_tier"] = tier

    schema_files = _schema_mutation_files(truth, task_card)
    if schema_files:
        triggers.append("schema_mutation")
        detail["schema_mutation_files"] = schema_files

    blocked_readiness = _readiness_blocked_history(truth, readiness_history)
    if blocked_readiness:
        triggers.append("readiness_blocked_history")
        detail["readiness_blocked_count"] = blocked_readiness

    total_recovery = _total_recovery(truth)
    if total_recovery >= recovery_total_threshold:
        triggers.append("recovery_pressure")
        detail["recovery_total"] = total_recovery
        detail["recovery_threshold"] = recovery_total_threshold

    quality = str(truth.get("review_quality") or "").strip().lower()
    if quality:
        verdict = review_quality_acceptance(quality, release_phase=True)
        if not verdict["accept"]:
            triggers.append("non_real_review_quality")
            detail["review_quality"] = quality
            detail["review_quality_reason"] = verdict["reason"]

    mode = "manual_required" if triggers else "auto"
    return ReleaseApprovalDecision(mode=mode, triggers=triggers, detail=detail)


def _heavy_tier(truth: dict[str, Any]) -> str:
    lane = truth.get("lane_observation") if isinstance(truth.get("lane_observation"), dict) else {}
    for key in ("actual_tier", "predicted_tier"):
        value = str(lane.get(key) or "").strip().lower()
        if value in _HEAVY_TIER_VALUES:
            return value
    return ""


def _schema_mutation_files(truth: dict[str, Any], task_card: dict[str, Any] | None) -> list[str]:
    candidates: list[str] = []
    for source in (truth.get("changed_files"), (task_card or {}).get("files_to_change"), (task_card or {}).get("new_files")):
        if isinstance(source, list):
            candidates.extend(str(item) for item in source if str(item).strip())
    seen: set[str] = set()
    matched: list[str] = []
    for path in candidates:
        normalized = path.replace("\\", "/").lower()
        if any(hint in normalized for hint in _SCHEMA_MUTATION_PATH_HINTS):
            if normalized not in seen:
                seen.add(normalized)
                matched.append(path)
    return matched


def _readiness_blocked_history(
    truth: dict[str, Any],
    readiness_history: list[dict[str, Any]] | None,
) -> int:
    history = list(readiness_history or [])
    truth_history = truth.get("readiness_history")
    if isinstance(truth_history, list):
        history.extend(truth_history)
    blocked_marker = str(truth.get("readiness_status") or "").strip().upper()
    blocked = sum(1 for item in history if isinstance(item, dict) and str(item.get("status") or "").upper() == "BLOCKED")
    if blocked_marker == "BLOCKED":
        blocked += 1
    return blocked


def _total_recovery(truth: dict[str, Any]) -> int:
    try:
        deterministic = int(truth.get("deterministic_recovery_hits") or 0)
        synthesizer = int(truth.get("synthesizer_calls") or 0)
        pressure = int(truth.get("recovery_pressure") or 0)
    except (TypeError, ValueError):
        return 0
    return max(pressure, deterministic + synthesizer)


__all__ = ["ReleaseApprovalDecision", "evaluate_release_approval"]
