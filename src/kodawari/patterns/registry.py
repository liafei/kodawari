"""Pattern suggestion registry used by autopilot implementation context."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any


def _normalize_text(*parts: str | None) -> str:
    return "\n".join(str(part or "").strip() for part in parts if str(part or "").strip())


@dataclass
class PatternSuggestion:
    pattern_id: str
    title: str
    rationale: str
    confidence: float
    checklist: list[str] = field(default_factory=list)
    verify_hints: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "pattern_id": self.pattern_id,
            "title": self.title,
            "rationale": self.rationale,
            "confidence": self.confidence,
            "checklist": list(self.checklist),
            "verify_hints": list(self.verify_hints),
        }

    def to_hint(self) -> dict[str, Any]:
        """Compatibility output consumed by implementation context builders."""
        return {
            "pattern_id": self.pattern_id,
            "title": self.title,
            "why": self.rationale,
            "confidence": self.confidence,
            "checklist": list(self.checklist),
            "verify_hints": list(self.verify_hints),
        }


class TaskPattern:
    pattern_id = "base"
    title = "Base Pattern"
    rationale = ""
    confidence = 0.5
    checklist: list[str] = []
    verify_hints: list[str] = []
    triggers: list[re.Pattern[str]] = []

    def matches(self, *, task_label: str, task_scope: str | None, requirements: str | None) -> bool:
        text = _normalize_text(task_label, task_scope, requirements)
        return any(pattern.search(text) for pattern in self.triggers)

    def analyze(
        self,
        *,
        task_id: str,
        task_label: str,
        task_scope: str | None,
        requirements: str | None,
    ) -> PatternSuggestion | None:
        del task_id
        if not self.matches(task_label=task_label, task_scope=task_scope, requirements=requirements):
            return None
        return PatternSuggestion(
            pattern_id=self.pattern_id,
            title=self.title,
            rationale=self.rationale,
            confidence=self.confidence,
            checklist=list(self.checklist),
            verify_hints=list(self.verify_hints),
        )


class PatternRegistry:
    def __init__(self, patterns: list[TaskPattern] | None = None) -> None:
        self._patterns = list(patterns or [])

    def register(self, pattern: TaskPattern) -> None:
        self._patterns.append(pattern)

    def analyze(
        self,
        *,
        task_id: str,
        task_label: str,
        task_scope: str | None = None,
        requirements: str | None = None,
    ) -> list[PatternSuggestion]:
        matches: list[PatternSuggestion] = []
        for pattern in self._patterns:
            suggestion = pattern.analyze(
                task_id=task_id,
                task_label=task_label,
                task_scope=task_scope,
                requirements=requirements,
            )
            if suggestion is not None:
                matches.append(suggestion)
        return sorted(matches, key=lambda item: item.confidence, reverse=True)

    def analyze_hints(
        self,
        *,
        task_id: str,
        task_label: str,
        task_scope: str | None = None,
        requirements: str | None = None,
    ) -> list[dict[str, Any]]:
        return [
            suggestion.to_hint()
            for suggestion in self.analyze(
                task_id=task_id,
                task_label=task_label,
                task_scope=task_scope,
                requirements=requirements,
            )
        ]
