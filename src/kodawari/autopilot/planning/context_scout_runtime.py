"""Runtime policy helpers for Context Scout."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any


_CONTEXT_SCOUT_DEFAULTS_ENV = "WORKFLOW_CONTEXT_SCOUT_DEFAULTS"
_AUTO_VALUES = {"1", "auto", "on", "true", "yes"}
_PROGRESS_DECISION_FRACTION = 0.60
_MIN_SAFE_FILE_COMPLETION = 0.50


@dataclass(frozen=True)
class ScoutBudget:
    tier: str
    timeout_seconds: int
    file_limit: int
    token_limit: int
    unlimited: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "tier": self.tier,
            "timeout_seconds": self.timeout_seconds,
            "file_limit": self.file_limit,
            "token_limit": self.token_limit,
            "unlimited": self.unlimited,
        }


@dataclass(frozen=True)
class ScoutDecision:
    status: str  # READY | AUTO_ACCEPTED | AWAITING_USER_DECISION
    selected_tier: str
    requires_user_decision: bool
    auto_default_used: bool
    prompt: str
    rationale: tuple[str, ...]
    options: tuple[dict[str, object], ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "selected_tier": self.selected_tier,
            "requires_user_decision": self.requires_user_decision,
            "auto_default_used": self.auto_default_used,
            "prompt": self.prompt,
            "rationale": list(self.rationale),
            "options": [dict(item) for item in self.options],
        }


@dataclass(frozen=True)
class ScoutProgress:
    status: str  # READY | AWAITING_USER_DECISION
    tier: str
    elapsed_seconds: float
    budget_seconds: int
    files_read: int
    files_total: int
    completion_ratio: float
    context_quality: str  # empty | partial | full
    preflight_allow: bool
    degradation_reason: str
    prompt: str

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "tier": self.tier,
            "elapsed_seconds": self.elapsed_seconds,
            "budget_seconds": self.budget_seconds,
            "files_read": self.files_read,
            "files_total": self.files_total,
            "completion_ratio": self.completion_ratio,
            "context_quality": self.context_quality,
            "preflight_allow": self.preflight_allow,
            "degradation_reason": self.degradation_reason,
            "prompt": self.prompt,
        }


_BUDGETS: dict[str, ScoutBudget] = {
    "quick": ScoutBudget(tier="quick", timeout_seconds=30, file_limit=10, token_limit=20_000),
    "standard": ScoutBudget(tier="standard", timeout_seconds=90, file_limit=30, token_limit=80_000),
    "deep": ScoutBudget(tier="deep", timeout_seconds=300, file_limit=80, token_limit=200_000),
    "exhaustive": ScoutBudget(tier="exhaustive", timeout_seconds=0, file_limit=0, token_limit=0, unlimited=True),
}


def context_scout_defaults_auto() -> bool:
    raw = os.environ.get(_CONTEXT_SCOUT_DEFAULTS_ENV)
    return str(raw or "").strip().lower() in _AUTO_VALUES


def budget_for_tier(tier: str) -> ScoutBudget:
    key = str(tier or "").strip().lower()
    return _BUDGETS.get(key, _BUDGETS["standard"])


def _decision_options() -> tuple[dict[str, object], ...]:
    return tuple(budget.to_dict() for budget in _BUDGETS.values())


def build_scout_decision(recommendation: Any) -> ScoutDecision:
    tier = str(getattr(recommendation, "tier", "") or "standard")
    rationale = tuple(getattr(recommendation, "rationale", ()) or ())
    if not bool(getattr(recommendation, "requires_user_decision", False)):
        return ScoutDecision(
            status="READY",
            selected_tier=tier,
            requires_user_decision=False,
            auto_default_used=False,
            prompt="",
            rationale=rationale,
            options=(),
        )
    if context_scout_defaults_auto():
        return ScoutDecision(
            status="AUTO_ACCEPTED",
            selected_tier=tier,
            requires_user_decision=False,
            auto_default_used=True,
            prompt="",
            rationale=rationale,
            options=(),
        )
    return ScoutDecision(
        status="AWAITING_USER_DECISION",
        selected_tier=tier,
        requires_user_decision=True,
        auto_default_used=False,
        prompt=f"Context Scout recommends a {tier} scan. Confirm this tier or name the exact files to inspect.",
        rationale=rationale,
        options=_decision_options(),
    )


def _file_completion(files_read: int, files_total: int) -> float:
    total = max(0, int(files_total))
    if total <= 0:
        return 1.0
    read = max(0, min(int(files_read), total))
    return round(read / total, 4)


def _context_quality(*, files_read: int, files_total: int) -> str:
    if files_read <= 0 and files_total > 0:
        return "empty"
    if _file_completion(files_read, files_total) >= 1.0:
        return "full"
    return "partial"


def _progress_needs_decision(
    *,
    budget: ScoutBudget,
    elapsed_seconds: float,
    files_read: int,
    files_total: int,
) -> bool:
    if budget.unlimited or files_total <= 0:
        return False
    elapsed_ratio = float(elapsed_seconds) / max(1, budget.timeout_seconds)
    return (
        elapsed_ratio >= _PROGRESS_DECISION_FRACTION
        and _file_completion(files_read, files_total) < _MIN_SAFE_FILE_COMPLETION
    )


def evaluate_scout_progress(
    *,
    tier: str,
    elapsed_seconds: float,
    files_read: int,
    files_total: int,
) -> ScoutProgress:
    budget = budget_for_tier(tier)
    safe_read = max(0, int(files_read))
    safe_total = max(0, int(files_total))
    needs_decision = _progress_needs_decision(
        budget=budget,
        elapsed_seconds=float(elapsed_seconds),
        files_read=safe_read,
        files_total=safe_total,
    )
    return ScoutProgress(
        status="AWAITING_USER_DECISION" if needs_decision else "READY",
        tier=budget.tier,
        elapsed_seconds=float(elapsed_seconds),
        budget_seconds=int(budget.timeout_seconds),
        files_read=safe_read,
        files_total=safe_total,
        completion_ratio=_file_completion(safe_read, safe_total),
        context_quality=_context_quality(files_read=safe_read, files_total=safe_total),
        preflight_allow=not needs_decision,
        degradation_reason="budget_progress_insufficient" if needs_decision else "",
        prompt=_progress_prompt(needs_decision),
    )


def _progress_prompt(needs_decision: bool) -> str:
    if not needs_decision:
        return ""
    return (
        "Scout has partial context near the budget limit. "
        "Name the key files to inspect next, or approve continuing with partial context."
    )


def extract_selected_files_from_text(text: str, candidate_files: list[str]) -> list[str]:
    normalized = str(text or "").strip().lower().replace("\\", "/")
    selected: list[str] = []
    seen: set[str] = set()
    for raw in candidate_files:
        rel = str(raw or "").strip().replace("\\", "/")
        basename = rel.rsplit("/", 1)[-1].lower()
        rel_key = rel.lower()
        if not rel or (rel_key not in normalized and basename not in normalized):
            continue
        if rel_key not in seen:
            seen.add(rel_key)
            selected.append(rel)
    return selected


def build_scout_feedback_event(
    *,
    reason: str,
    task_id: str = "",
    selected_files: list[str] | None = None,
    scout_payload: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "reason": str(reason or "").strip(),
        "task_id": str(task_id or "").strip(),
        "selected_files": list(selected_files or []),
        "scout_payload": dict(scout_payload or {}),
    }


def append_scout_feedback(planning_dir: Path, event: dict[str, object]) -> Path:
    target = Path(planning_dir) / "scout_feedback.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(dict(event), ensure_ascii=False, sort_keys=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    return target


__all__ = [
    "ScoutBudget",
    "ScoutDecision",
    "ScoutProgress",
    "append_scout_feedback",
    "budget_for_tier",
    "build_scout_decision",
    "build_scout_feedback_event",
    "context_scout_defaults_auto",
    "evaluate_scout_progress",
    "extract_selected_files_from_text",
]
