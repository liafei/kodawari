"""Minimal instinct data models for WS-116 absorption and P0 error-learning."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_text(value: Any, *, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def _to_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


@dataclass
class Instinct:
    id: str
    pattern: str
    category: str = "recovery"
    confidence: float = 0.5
    archived: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Instinct":
        return cls(
            id=_clean_text(payload.get("id")),
            pattern=_clean_text(payload.get("pattern")),
            category=_clean_text(payload.get("category"), default="recovery"),
            confidence=_to_float(payload.get("confidence"), 0.5),
            archived=_to_bool(payload.get("archived"), False),
        )


def _to_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


@dataclass
class LearningCandidate:
    id: str
    signature: str
    category: str
    phase: str
    action: str = ""
    example_message: str = ""
    suggested_pattern: str = ""
    # ``count`` is the raw event count: every ingested error event bumps it.
    # Kept under the legacy name for backward-compatible reads of older
    # ``instincts.json`` files. Decisions (confidence, promotion) MUST use
    # ``distinct_run_count`` instead — see PR2.5: the same noisy run firing
    # ten retries should not promote a candidate.
    count: int = 0
    # Number of distinct ``run_id`` values that have produced this signature.
    # An event with empty run_id only bumps ``count``; it does not bump
    # ``distinct_run_count`` because we cannot tell whether it is a fresh
    # session.
    distinct_run_count: int = 0
    # The set of run_ids already accounted for in ``distinct_run_count``,
    # stored as a sorted list (set is not JSON-serialisable). Bounded at
    # ``_SEEN_RUN_IDS_LIMIT`` to keep the store small for chronic patterns.
    seen_run_ids: list[str] = field(default_factory=list)
    first_seen: str = field(default_factory=_utc_now_iso)
    last_seen: str = field(default_factory=_utc_now_iso)
    promoted: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "LearningCandidate":
        now = _utc_now_iso()
        raw_metadata = payload.get("metadata")
        metadata = dict(raw_metadata) if isinstance(raw_metadata, dict) else {}
        return cls(
            id=_clean_text(payload.get("id")),
            signature=_clean_text(payload.get("signature")),
            category=_clean_text(payload.get("category"), default="runtime"),
            phase=_clean_text(payload.get("phase"), default="RUNTIME"),
            action=_clean_text(payload.get("action")),
            example_message=_clean_text(payload.get("example_message")),
            suggested_pattern=_clean_text(payload.get("suggested_pattern")),
            count=max(0, _to_int(payload.get("count"), 0)),
            distinct_run_count=max(0, _to_int(payload.get("distinct_run_count"), 0)),
            seen_run_ids=_to_str_list(payload.get("seen_run_ids")),
            first_seen=_clean_text(payload.get("first_seen"), default=now),
            last_seen=_clean_text(payload.get("last_seen"), default=now),
            promoted=_to_bool(payload.get("promoted"), False),
            metadata=metadata,
        )


@dataclass
class LearnedInstinct:
    id: str
    signature: str
    pattern: str
    category: str = "recovery"
    confidence: float = 0.75
    count: int = 0
    source: str = "error_learning"
    explanation: str = ""
    first_seen: str = field(default_factory=_utc_now_iso)
    last_seen: str = field(default_factory=_utc_now_iso)
    archived: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "LearnedInstinct":
        now = _utc_now_iso()
        raw_metadata = payload.get("metadata")
        metadata = dict(raw_metadata) if isinstance(raw_metadata, dict) else {}
        return cls(
            id=_clean_text(payload.get("id")),
            signature=_clean_text(payload.get("signature")),
            pattern=_clean_text(payload.get("pattern")),
            category=_clean_text(payload.get("category"), default="recovery"),
            confidence=_to_float(payload.get("confidence"), 0.75),
            count=max(0, _to_int(payload.get("count"), 0)),
            source=_clean_text(payload.get("source"), default="error_learning"),
            explanation=_clean_text(payload.get("explanation")),
            first_seen=_clean_text(payload.get("first_seen"), default=now),
            last_seen=_clean_text(payload.get("last_seen"), default=now),
            archived=_to_bool(payload.get("archived"), False),
            metadata=metadata,
        )


@dataclass
class InstinctStoreData:
    schema_version: str = "v1"
    instincts: list[Instinct] = field(default_factory=list)
    learning_candidates: list[LearningCandidate] = field(default_factory=list)
    learned_instincts: list[LearnedInstinct] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "instincts": [item.to_dict() for item in self.instincts],
            "learning_candidates": [item.to_dict() for item in self.learning_candidates],
            "learned_instincts": [item.to_dict() for item in self.learned_instincts],
        }


def schema_document() -> dict[str, Any]:
    return {
        "schema_version": "v1",
        "required_fields": ["id", "pattern", "category", "confidence"],
        "error_learning_fields": ["learning_candidates", "learned_instincts"],
    }

