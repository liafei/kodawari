"""Phase 6 context scout helpers.

This module implements the deterministic part of Phase 6:
- Stage-1 scope summary from shallow inputs
- natural-language signal extraction
- budget-tier recommendation (quick/standard/deep/exhaustive)
- user-decision gating for deep/exhaustive recommendations
- budget progress degradation and feedback recording

It intentionally does NOT execute repo-wide deep scans or UI decisions.
It only emits recommendations and whether user confirmation is required.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Iterable

from kodawari.autopilot.planning.context_scout_runtime import (
    ScoutBudget,
    ScoutDecision,
    ScoutProgress,
    append_scout_feedback,
    budget_for_tier,
    build_scout_decision,
    build_scout_feedback_event,
    context_scout_defaults_auto,
    evaluate_scout_progress,
    extract_selected_files_from_text,
)


_CONTEXT_SCOUT_ENV = "WORKFLOW_CONTEXT_SCOUT"
_OFF_VALUES = {"0", "off", "false", "no", ""}
_LARGE_FILE_THRESHOLD = 800

_REFACTOR_KEYWORDS = (
    "重构",
    "refactor",
    "拆",
    "拆分",
)
_BREADTH_HIGH_KEYWORDS = (
    "跨模块",
    "多文件",
    "整个",
    "全部",
    "all files",
    "whole section",
    "整个章节",
    "全部实现",
)
_DEPTH_HIGH_KEYWORDS = (
    "详细",
    "深入",
    "完整",
    "全面",
    "deep",
    "in-depth",
    "detailed",
    "完整分析",
)
_DEPTH_LOW_KEYWORDS = (
    "快速",
    "简单",
    "只改",
    "单个",
    "quick",
    "simple",
    "just",
)
_EXHAUSTIVE_KEYWORDS = (
    "exhaustive",
    "完整扫一遍",
    "不限时间",
    "全量",
)


def context_scout_enabled() -> bool:
    raw = os.environ.get(_CONTEXT_SCOUT_ENV)
    if raw is None:
        return False
    return str(raw).strip().lower() not in _OFF_VALUES


def _normalize_text(text: str) -> str:
    return str(text or "").strip().lower()


def _contains_any(text: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword in text for keyword in keywords)


def _to_level(*, high: bool, low: bool) -> str:
    if high:
        return "high"
    if low:
        return "low"
    return "normal"


@dataclass(frozen=True)
class ScoutScope:
    files_estimate: int
    large_file_count: int
    has_large_file: bool
    refactor_hint: bool
    breadth_hint: str  # low | normal | high
    depth_hint: str  # low | normal | high
    prd_scope_breadth: str  # narrow | medium | wide
    signals: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "files_estimate": self.files_estimate,
            "large_file_count": self.large_file_count,
            "has_large_file": self.has_large_file,
            "refactor_hint": self.refactor_hint,
            "breadth_hint": self.breadth_hint,
            "depth_hint": self.depth_hint,
            "prd_scope_breadth": self.prd_scope_breadth,
            "signals": list(self.signals),
        }


@dataclass(frozen=True)
class ScoutBudgetRecommendation:
    tier: str  # quick | standard | deep | exhaustive
    requires_user_decision: bool
    rationale: tuple[str, ...]
    scope: ScoutScope

    def to_dict(self) -> dict[str, object]:
        return {
            "tier": self.tier,
            "requires_user_decision": self.requires_user_decision,
            "rationale": list(self.rationale),
            "scope": self.scope.to_dict(),
        }


def _count_large_files(
    *,
    candidate_files: Iterable[str],
    file_line_counts: dict[str, int] | None,
) -> int:
    if not file_line_counts:
        return 0
    total = 0
    for path in candidate_files:
        lines = int(file_line_counts.get(path, 0) or 0)
        if lines > _LARGE_FILE_THRESHOLD:
            total += 1
    return total


def _scope_breadth(*, files_estimate: int, breadth_hint: str) -> str:
    if breadth_hint == "high" or files_estimate > 20:
        return "wide"
    if files_estimate <= 5:
        return "narrow"
    return "medium"


def _collect_signals(
    *,
    refactor_hint: bool,
    breadth_hint: str,
    depth_hint: str,
    large_file_count: int,
) -> tuple[str, ...]:
    flags = (
        ("refactor", refactor_hint),
        ("breadth_high", breadth_hint == "high"),
        ("depth_high", depth_hint == "high"),
        ("depth_low", depth_hint == "low"),
        ("has_large_file", large_file_count > 0),
    )
    return tuple(name for name, active in flags if active)


def _resolve_files_estimate(
    *,
    candidate_files: list[str],
    files_estimate_override: int | None,
) -> tuple[list[str], int]:
    unique_files = [item for item in dict.fromkeys(candidate_files) if str(item).strip()]
    raw = int(files_estimate_override) if files_estimate_override is not None else len(unique_files)
    return unique_files, max(0, raw)


def build_scout_scope(
    *,
    user_text: str,
    candidate_files: list[str],
    file_line_counts: dict[str, int] | None = None,
    files_estimate_override: int | None = None,
) -> ScoutScope:
    text = _normalize_text(user_text)
    unique_files, files_estimate = _resolve_files_estimate(
        candidate_files=candidate_files,
        files_estimate_override=files_estimate_override,
    )
    large_file_count = _count_large_files(candidate_files=unique_files, file_line_counts=file_line_counts)
    refactor_hint = _contains_any(text, _REFACTOR_KEYWORDS)
    breadth_hint = _to_level(high=_contains_any(text, _BREADTH_HIGH_KEYWORDS), low=False)
    depth_high = _contains_any(text, _DEPTH_HIGH_KEYWORDS)
    depth_low = _contains_any(text, _DEPTH_LOW_KEYWORDS)
    depth_hint = _to_level(high=depth_high, low=depth_low and not depth_high)
    signals = _collect_signals(
        refactor_hint=refactor_hint,
        breadth_hint=breadth_hint,
        depth_hint=depth_hint,
        large_file_count=large_file_count,
    )
    return ScoutScope(
        files_estimate=files_estimate,
        large_file_count=large_file_count,
        has_large_file=large_file_count > 0,
        refactor_hint=refactor_hint,
        breadth_hint=breadth_hint,
        depth_hint=depth_hint,
        prd_scope_breadth=_scope_breadth(files_estimate=files_estimate, breadth_hint=breadth_hint),
        signals=signals,
    )


def _base_tier(scope: ScoutScope) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if scope.files_estimate <= 5 and not scope.has_large_file:
        reasons.append("files<=5_and_no_large_file")
        return "quick", reasons
    if scope.files_estimate <= 20 and scope.large_file_count <= 1:
        reasons.append("files<=20_and_large_files<=1")
        return "standard", reasons
    reasons.append("files>20_or_large_files>=2")
    return "deep", reasons


def _has_deep_keyword_signal(scope: ScoutScope) -> bool:
    return scope.refactor_hint or scope.breadth_hint == "high" or scope.depth_hint == "high"


def _apply_keyword_escalation(tier: str, scope: ScoutScope, text: str, reasons: list[str]) -> str:
    """按自然语言信号把 tier 抬高到 exhaustive / deep。"""
    if _contains_any(text, _EXHAUSTIVE_KEYWORDS):
        reasons.append("exhaustive_keyword")
        return "exhaustive"
    if not _has_deep_keyword_signal(scope):
        return tier
    if tier in {"quick", "standard"}:
        reasons.append("keyword_escalation_to_deep")
        return "deep"
    reasons.append("deep_keyword_confirmed")
    return tier


def _can_downshift_to_quick(tier: str, scope: ScoutScope) -> bool:
    return (
        tier == "standard"
        and scope.depth_hint == "low"
        and scope.files_estimate <= 5
        and scope.large_file_count == 0
    )


def recommend_scout_budget(
    *,
    user_text: str,
    candidate_files: list[str],
    file_line_counts: dict[str, int] | None = None,
    files_estimate_override: int | None = None,
) -> ScoutBudgetRecommendation:
    """Return Phase-6a budget suggestion based on shallow scope.

    This function only recommends a tier. Caller/UI decides whether to accept.
    """
    text = _normalize_text(user_text)
    scope = build_scout_scope(
        user_text=user_text,
        candidate_files=candidate_files,
        file_line_counts=file_line_counts,
        files_estimate_override=files_estimate_override,
    )
    tier, reasons = _base_tier(scope)
    tier = _apply_keyword_escalation(tier, scope, text, reasons)
    if _can_downshift_to_quick(tier, scope):
        tier = "quick"
        reasons.append("depth_low_downshift")
    return ScoutBudgetRecommendation(
        tier=tier,
        requires_user_decision=tier in {"deep", "exhaustive"},
        rationale=tuple(reasons),
        scope=scope,
    )


def build_context_scout_payload(
    *,
    user_text: str,
    candidate_files: list[str],
    file_line_counts: dict[str, int] | None = None,
    files_estimate_override: int | None = None,
    elapsed_seconds: float = 0.0,
    files_read: int | None = None,
    files_total: int | None = None,
) -> dict[str, object]:
    recommendation = recommend_scout_budget(
        user_text=user_text,
        candidate_files=candidate_files,
        file_line_counts=file_line_counts,
        files_estimate_override=files_estimate_override,
    )
    decision = build_scout_decision(recommendation)
    total = recommendation.scope.files_estimate if files_total is None else int(files_total)
    read = len(candidate_files) if files_read is None else int(files_read)
    progress = evaluate_scout_progress(
        tier=recommendation.tier,
        elapsed_seconds=elapsed_seconds,
        files_read=read,
        files_total=total,
    )
    return {
        "enabled": True,
        "recommendation": recommendation.to_dict(),
        "budget": budget_for_tier(recommendation.tier).to_dict(),
        "decision": decision.to_dict(),
        "progress": progress.to_dict(),
    }


__all__ = [
    "ScoutBudget",
    "ScoutBudgetRecommendation",
    "ScoutDecision",
    "ScoutProgress",
    "ScoutScope",
    "append_scout_feedback",
    "budget_for_tier",
    "build_context_scout_payload",
    "build_scout_decision",
    "build_scout_feedback_event",
    "build_scout_scope",
    "context_scout_enabled",
    "context_scout_defaults_auto",
    "evaluate_scout_progress",
    "extract_selected_files_from_text",
    "recommend_scout_budget",
]
