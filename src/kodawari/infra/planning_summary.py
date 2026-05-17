"""Small planning-summary helper retained outside the retired spec generator."""

from __future__ import annotations

from typing import Any


def summarize_plan(
    feature: str,
    sections: list[str] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    selected_sections = list(sections or [])
    return {
        "feature": str(feature),
        "sections": selected_sections,
        "section_count": len(selected_sections),
        "options": dict(kwargs),
    }


__all__ = ["summarize_plan"]
