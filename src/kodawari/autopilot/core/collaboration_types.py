"""Collaboration models and shared helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import re
from typing import Any

from .collaboration_flow import resolve_next_action


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class CollaborationRole(str, Enum):
    # Role identifiers used for turn ownership and state-file owner/reviewer
    # fields. These are *role names*, distinct from the vendor-neutral
    # gateway source identifiers (e.g. ``kodawari.real_peer_review_*``).
    # The "opus" value is kept because the role semantically represents the
    # peer-reviewer turn across a large body of persisted state files and
    # round-records; renaming it would invalidate historical data without
    # adding expressive power.
    OPUS = "opus"
    CODEX = "codex"
    SYSTEM = "system"


class CollaborationAction(str, Enum):
    # Canonical values (new code should use these)
    DESIGN = "design"
    IMPLEMENT = "implement"
    PEER_REVIEW = "peer_review"
    SELF_REVIEW = "self_review"
    FIX_ROUND = "fix_round"
    VERIFY = "verify"
    RULES_GATE = "rules_gate"
    PROCEED_TO_GATE = "proceed_to_gate"
    FINISH = "finish"
    # Legacy aliases kept for backward compat with existing state files
    OPUS_DESIGN = "opus_design"
    CODEX_IMPLEMENT = "codex_implement"
    OPUS_REVIEW = "opus_review"
    CODEX_SELF_REVIEW = "codex_self_review"
    CODEX_FIX = "codex_fix"

    @classmethod
    def _missing_(cls, value: object) -> "CollaborationAction | None":
        from kodawari.autopilot.core.action_semantics import normalize_action

        canonical = normalize_action(str(value))
        for member in cls:
            if member.value == canonical:
                return member
        return None


def _clean_text(value: Any, *, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def _string_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    return [str(item) for item in values]


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _string_int_map(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    parsed: dict[str, int] = {}
    for key, raw in value.items():
        number = _optional_int(raw)
        if number is None:
            continue
        parsed[str(key)] = number
    return parsed


def _normalize_verdict(value: Any, *, allowed: set[str]) -> str | None:
    normalized = _clean_text(value).upper()
    if not normalized:
        return None
    if normalized in allowed:
        return normalized
    return None


def _normalize_attribution(value: Any, *, allowed: set[str]) -> str | None:
    """Lowercase enum normalizer for global_failure_attribution."""
    normalized = _clean_text(value).lower()
    if not normalized:
        return None
    if normalized in allowed:
        return normalized
    return None


def _evidence_refs(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    refs: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        artifact = _clean_text(item.get("artifact"))
        field_path = _clean_text(item.get("field_path"))
        reason = _clean_text(item.get("reason"))
        if not any([artifact, field_path, reason]):
            continue
        refs.append(
            {
                "artifact": artifact,
                "field_path": field_path,
                "reason": reason,
            }
        )
    return refs


def _deterministic_finding_responses(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    rows: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        finding_type = _clean_text(item.get("finding_type"))
        if not finding_type:
            continue
        rows.append(
            {
                "finding_type": finding_type,
                "acknowledged": bool(item.get("acknowledged", False)),
                "assessment": _clean_text(item.get("assessment")),
            }
        )
    return rows


def _context_constraints(data: dict[str, Any]) -> dict[str, list[str]]:
    constraints = dict(data.get("implementation_constraints", {}))
    return {
        "must_keep": _string_list(constraints.get("must_keep")),
        "forbidden": _string_list(constraints.get("forbidden")),
    }


def _build_context_constraints(
    implementation_constraints: dict[str, list[str]] | None,
) -> dict[str, list[str]]:
    source = dict(implementation_constraints or {})
    return {
        "must_keep": _string_list(source.get("must_keep")),
        "forbidden": _string_list(source.get("forbidden")),
    }


def _parse_architecture_decisions(payload: Any) -> list["ArchitectureDecision"]:
    if not isinstance(payload, list):
        return []
    return [ArchitectureDecision.from_dict(item) for item in payload if isinstance(item, dict)]


def _feedback_snapshot(feedback: "ReviewFeedback") -> "ReviewFeedback":
    return ReviewFeedback.from_dict(feedback.to_dict())


def _serialize_pattern_text(task_label: str, task_scope: str | None) -> str:
    return "\n".join(
        part.strip()
        for part in [str(task_label or ""), str(task_scope or "")]
        if part and str(part).strip()
    )


def _matches_any(patterns: list[re.Pattern[str]], text: str) -> bool:
    return any(pattern.search(text) for pattern in patterns)


def _is_design_pending(context: "CollaborationContext") -> bool:
    return (
        context.assigned_role == CollaborationRole.OPUS
        and not context.architecture_decisions
        and not context.implementation_started
    )


def _merge_unique_items(target: list[str], additions: list[str]) -> list[str]:
    existing = {str(item).strip().lower() for item in target if str(item).strip()}
    for item in additions:
        normalized = str(item).strip()
        if not normalized:
            continue
        lowered = normalized.lower()
        if lowered in existing:
            continue
        existing.add(lowered)
        target.append(normalized)
    return target


@dataclass
class ArchitectureDecision:
    """Architecture guidance persisted between roles."""

    decision_id: str
    decision: str
    rationale: str
    constraints: list[str] = field(default_factory=list)
    api_contracts: list[str] = field(default_factory=list)
    test_strategy: list[str] = field(default_factory=list)
    owner: str = CollaborationRole.OPUS.value
    created_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.decision_id,
            "decision": self.decision,
            "rationale": self.rationale,
            "constraints": list(self.constraints),
            "api_contracts": list(self.api_contracts),
            "test_strategy": list(self.test_strategy),
            "owner": self.owner,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ArchitectureDecision":
        return cls(
            decision_id=_clean_text(data.get("id"), default=_clean_text(data.get("decision_id"))),
            decision=_clean_text(data.get("decision")),
            rationale=_clean_text(data.get("rationale")),
            constraints=_string_list(data.get("constraints")),
            api_contracts=_string_list(data.get("api_contracts")),
            test_strategy=_string_list(data.get("test_strategy")),
            owner=_clean_text(data.get("owner"), default=CollaborationRole.OPUS.value),
            created_at=data.get("created_at"),
        )


@dataclass
class ReviewFeedback:
    approved: bool = False
    reviewer: CollaborationRole = CollaborationRole.OPUS
    summary: str | None = None
    must_fix: list[str] = field(default_factory=list)
    should_fix: list[str] = field(default_factory=list)
    blocking_items: list[str] = field(default_factory=list)
    severity: str = "info"
    score: int | None = None
    target_score: int | None = None
    min_dimension_score: int | None = None
    dimension_scores: dict[str, int] = field(default_factory=dict)
    gate_recommendation: str | None = None
    global_consistency_verdict: str | None = None
    local_implementation_verdict: str | None = None
    # When global_consistency_verdict=FAIL, where the failure originates:
    # 'this_task' (defect in this diff — overrides approved), 'sibling_tasks'
    # (other tasks not yet implemented — does NOT override), 'unknown'.
    # None means the reviewer did not emit the field. The structured field
    # replaces the older blocking_items substring-keyword heuristic.
    global_failure_attribution: str | None = None
    deterministic_finding_responses: list[dict[str, Any]] = field(default_factory=list)
    evidence_refs: list[dict[str, str]] = field(default_factory=list)
    source: str = "kodawari"
    reviewed_at: str | None = None
    review_iteration: int = 0
    # Set when `global_consistency_verdict=FAIL` overrides an `approved=True`
    # passed in by the caller. Empty string means no override occurred.
    # Callers can inspect this field programmatically to distinguish genuine
    # rejections from system-forced overrides.
    review_override_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "approved": self.approved,
            "reviewer": self.reviewer.value,
            "summary": self.summary,
            "must_fix": list(self.must_fix),
            "should_fix": list(self.should_fix),
            "blocking_items": list(self.blocking_items),
            "severity": self.severity,
            "score": self.score,
            "target_score": self.target_score,
            "min_dimension_score": self.min_dimension_score,
            "dimension_scores": dict(self.dimension_scores),
            "gate_recommendation": self.gate_recommendation,
            "global_consistency_verdict": self.global_consistency_verdict,
            "local_implementation_verdict": self.local_implementation_verdict,
            "global_failure_attribution": self.global_failure_attribution,
            "deterministic_finding_responses": [dict(item) for item in self.deterministic_finding_responses],
            "evidence_refs": [dict(item) for item in self.evidence_refs],
            "source": self.source,
            "reviewed_at": self.reviewed_at,
            "review_iteration": self.review_iteration,
            "review_override_reason": self.review_override_reason,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReviewFeedback":
        return cls(
            approved=bool(data.get("approved", False)),
            reviewer=CollaborationRole(_clean_text(data.get("reviewer"), default=CollaborationRole.OPUS.value)),
            summary=data.get("summary"),
            must_fix=_string_list(data.get("must_fix")),
            should_fix=_string_list(data.get("should_fix")),
            blocking_items=_string_list(data.get("blocking_items")),
            severity=_clean_text(data.get("severity"), default="info"),
            score=_optional_int(data.get("score")),
            target_score=_optional_int(data.get("target_score")),
            min_dimension_score=_optional_int(data.get("min_dimension_score")),
            dimension_scores=_string_int_map(data.get("dimension_scores")),
            gate_recommendation=data.get("gate_recommendation"),
            global_consistency_verdict=_normalize_verdict(
                data.get("global_consistency_verdict"),
                allowed={"PASS", "FAIL", "INSUFFICIENT_CONTEXT"},
            ),
            local_implementation_verdict=_normalize_verdict(
                data.get("local_implementation_verdict"),
                allowed={"PASS", "FAIL"},
            ),
            global_failure_attribution=_normalize_attribution(
                data.get("global_failure_attribution"),
                allowed={"this_task", "sibling_tasks", "unknown"},
            ),
            deterministic_finding_responses=_deterministic_finding_responses(data.get("deterministic_finding_responses")),
            evidence_refs=_evidence_refs(data.get("evidence_refs")),
            source=_clean_text(data.get("source"), default="kodawari"),
            reviewed_at=data.get("reviewed_at"),
            review_iteration=int(_optional_int(data.get("review_iteration")) or 0),
            review_override_reason=_clean_text(data.get("review_override_reason")),
        )


@dataclass
class CollaborationContext:
    task_id: str
    task_label: str
    assigned_role: CollaborationRole
    architecture_decisions: list[ArchitectureDecision] = field(default_factory=list)
    implementation_constraints: dict[str, list[str]] = field(
        default_factory=lambda: {"must_keep": [], "forbidden": []}
    )
    review_feedback: ReviewFeedback = field(default_factory=ReviewFeedback)
    review_history: list[ReviewFeedback] = field(default_factory=list)
    fix_history: list[dict[str, Any]] = field(default_factory=list)
    escalation_count: int = 0
    implementation_started: bool = False
    peer_review_enabled: bool = True
    verify_passed: bool = False
    rules_gate_passed: bool = False
    self_review_required: bool = True
    self_review_completed: bool = False
    self_review_approved: bool = False
    context_version: int = 1
    contract_source: str = "phase1"
    # "single_task": reviewer only fails on invariant/dependency violations in
    #   THIS task; sibling tasks not yet implemented → INSUFFICIENT_CONTEXT.
    # "full_feature": reviewer may fail on global consistency across all tasks.
    review_scope: str = "single_task"
    updated_at: str | None = None

    def next_action(self) -> CollaborationAction:
        return CollaborationAction(
            resolve_next_action(
                verify_passed=self.verify_passed,
                rules_gate_passed=self.rules_gate_passed,
                has_must_fix=bool(self.review_feedback.must_fix),
                design_pending=_is_design_pending(self),
                implementation_started=self.implementation_started,
                assigned_role=self.assigned_role.value,
                peer_review_enabled=self.peer_review_enabled,
                review_approved=self.review_feedback.approved,
                self_review_required=self.self_review_required,
                self_review_completed=self.self_review_completed,
                self_review_approved=self.self_review_approved,
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "task_label": self.task_label,
            "assigned_role": self.assigned_role.value,
            "architecture_decisions": [item.to_dict() for item in self.architecture_decisions],
            "implementation_constraints": {
                "must_keep": list(self.implementation_constraints.get("must_keep", [])),
                "forbidden": list(self.implementation_constraints.get("forbidden", [])),
            },
            "review_feedback": self.review_feedback.to_dict(),
            "review_history": [item.to_dict() for item in self.review_history],
            "fix_history": [dict(item) for item in self.fix_history],
            "escalation_count": self.escalation_count,
            "implementation_started": self.implementation_started,
            "peer_review_enabled": self.peer_review_enabled,
            "verify_passed": self.verify_passed,
            "rules_gate_passed": self.rules_gate_passed,
            "self_review_required": self.self_review_required,
            "self_review_completed": self.self_review_completed,
            "self_review_approved": self.self_review_approved,
            "context_version": self.context_version,
            "contract_source": self.contract_source,
            "review_scope": self.review_scope,
            "updated_at": self.updated_at,
            "next_action": self.next_action().value,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CollaborationContext":
        return cls(
            task_id=_clean_text(data.get("task_id")),
            task_label=_clean_text(data.get("task_label")),
            assigned_role=CollaborationRole(
                _clean_text(data.get("assigned_role"), default=CollaborationRole.CODEX.value)
            ),
            architecture_decisions=_parse_architecture_decisions(data.get("architecture_decisions")),
            implementation_constraints=_context_constraints(data),
            review_feedback=ReviewFeedback.from_dict(dict(data.get("review_feedback", {}))),
            review_history=[
                ReviewFeedback.from_dict(item)
                for item in data.get("review_history", [])
                if isinstance(item, dict)
            ],
            fix_history=[dict(item) for item in data.get("fix_history", []) if isinstance(item, dict)],
            escalation_count=int(_optional_int(data.get("escalation_count")) or 0),
            implementation_started=bool(data.get("implementation_started", False)),
            peer_review_enabled=bool(data.get("peer_review_enabled", True)),
            verify_passed=bool(data.get("verify_passed", False)),
            rules_gate_passed=bool(data.get("rules_gate_passed", False)),
            self_review_required=bool(data.get("self_review_required", True)),
            self_review_completed=bool(data.get("self_review_completed", False)),
            self_review_approved=bool(data.get("self_review_approved", False)),
            context_version=int(_optional_int(data.get("context_version")) or 1),
            contract_source=_clean_text(data.get("contract_source"), default="phase1"),
            review_scope=_clean_text(data.get("review_scope"), default="single_task"),
            updated_at=data.get("updated_at"),
        )


class TaskRouter:
    """Minimal routing rules derived from the collaboration protocol."""

    _OPUS_PATTERNS = [
        re.compile(r"\barchitecture\b", re.IGNORECASE),
        re.compile(r"\balgorithm\b", re.IGNORECASE),
        re.compile(r"\btrade[- ]?off\b", re.IGNORECASE),
        re.compile(r"\bdesign\b", re.IGNORECASE),
        re.compile(r"\bdiagnos", re.IGNORECASE),
        re.compile(r"\brefactor\b", re.IGNORECASE),
        re.compile(r"\breview\b", re.IGNORECASE),
    ]
    _CODEX_PATTERNS = [
        re.compile(r"\bcrud\b", re.IGNORECASE),
        re.compile(r"\bapi\b", re.IGNORECASE),
        re.compile(r"\bendpoint\b", re.IGNORECASE),
        re.compile(r"\bmodel\b", re.IGNORECASE),
        re.compile(r"\bbootstrap\b", re.IGNORECASE),
        re.compile(r"\bmigration\b", re.IGNORECASE),
        re.compile(r"\bschema\b", re.IGNORECASE),
        re.compile(r"\branking\b", re.IGNORECASE),
        re.compile(r"\bscore\b", re.IGNORECASE),
        re.compile(r"\btest(?:s|ing)?\b", re.IGNORECASE),
        re.compile(r"\bimplement\b", re.IGNORECASE),
        re.compile(r"\bfix\b", re.IGNORECASE),
    ]

    @classmethod
    def route(cls, task_label: str, task_scope: str | None = None) -> CollaborationRole:
        text = _serialize_pattern_text(task_label, task_scope)
        if _matches_any(cls._OPUS_PATTERNS, text):
            return CollaborationRole.OPUS
        if _matches_any(cls._CODEX_PATTERNS, text):
            return CollaborationRole.CODEX
        return CollaborationRole.CODEX


__all__ = [
    "ArchitectureDecision",
    "CollaborationAction",
    "CollaborationContext",
    "CollaborationRole",
    "ReviewFeedback",
    "TaskRouter",
]

