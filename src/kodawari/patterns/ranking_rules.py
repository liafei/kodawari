"""Ranking rules pattern specialized for recommendation logic."""

from __future__ import annotations

import re

from kodawari.patterns.registry import TaskPattern


class RankingRulesPattern(TaskPattern):
    pattern_id = "ranking-rules"
    title = "Ranking Rules Pattern"
    rationale = "Task looks like recommendation ranking logic that benefits from explicit scoring structure."
    confidence = 0.92
    checklist = [
        "Define ranking dimensions and weights.",
        "Implement score calculation functions.",
        "Normalize scores before sorting.",
        "Handle cold-start and boundary cases.",
        "Add ranking-focused tests.",
        "Document tuning assumptions.",
    ]
    verify_hints = ["test_*ranking*.py", "test_*score*.py"]
    triggers = [
        re.compile(r"(ranking|rank|sort|order|priorit|score).*(rule|logic|algorithm|strategy)", re.IGNORECASE),
        re.compile(r"\branking\b", re.IGNORECASE),
        re.compile(r"\bscore\b", re.IGNORECASE),
    ]
