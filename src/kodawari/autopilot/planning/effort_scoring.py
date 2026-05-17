"""Effort scoring helpers for reasoning tier selection."""

from __future__ import annotations

from typing import Any

EFFORT_SCHEMA_VERSION = "effort.scoring.v1"

_DEEP_KEYWORDS = (
    "security",
    "migration",
    "architecture",
    "concurrency",
    "parallel",
    "worktree",
    "distributed",
    "data model",
    "schema",
)


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _string_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    return [str(item).strip() for item in values if str(item).strip()]


def _task_files(task_card: dict[str, Any], changed_files: list[str] | None) -> list[str]:
    if changed_files:
        return [item for item in changed_files if item]
    for key in ("files_to_change", "affected_files"):
        values = _string_list(task_card.get(key))
        if values:
            return values
    return []


def _contains_any(text: str, tokens: tuple[str, ...]) -> list[str]:
    lowered = text.lower()
    return [token for token in tokens if token in lowered]


def _touches_core(files: list[str]) -> bool:
    for path in files:
        normalized = path.replace("\\", "/").lower()
        if normalized.startswith("core/"):
            return True
        if "/core/" in normalized:
            return True
        if normalized.startswith("src/kodawari/autopilot/"):
            return True
    return False


def score_effort_profile(
    *,
    task_label: str,
    task_scope: str,
    requirements: str,
    task_card: dict[str, Any] | None = None,
    changed_files: list[str] | None = None,
    prior_failures: int = 0,
    project_model: dict[str, Any] | None = None,
) -> dict[str, Any]:
    card = dict(task_card or {})
    model = dict(project_model or {})
    files = _task_files(card, changed_files)
    reasons: list[str] = []
    score = 0

    if len(files) >= 4:
        score += 1
        reasons.append("touches_4_or_more_files")

    text_chunks = [
        _clean_text(task_label),
        _clean_text(task_scope),
        _clean_text(requirements),
        " ".join(_string_list(card.get("invariants"))),
        " ".join(_string_list(card.get("acceptance"))),
        _clean_text(model.get("surface")),
        " ".join(_string_list(model.get("capabilities"))),
    ]
    keyword_hits = _contains_any("\n".join(text_chunks), _DEEP_KEYWORDS)
    if keyword_hits:
        score += 1
        reasons.append("high_risk_keywords")

    prior_failures = max(0, int(prior_failures))
    if prior_failures > 0:
        score += 2
        reasons.append("prior_failures_present")

    core_touch = _touches_core(files)
    if core_touch:
        score += 1
        reasons.append("core_runtime_touch")

    if score >= 3:
        tier = "deep_reasoning"
    elif score >= 2:
        tier = "standard"
    else:
        tier = "economy"

    return {
        "schema_version": EFFORT_SCHEMA_VERSION,
        "tier": tier,
        "score": score,
        "reasons": reasons,
        "signals": {
            "changed_files_count": len(files),
            "prior_failures": prior_failures,
            "keyword_hits": keyword_hits,
            "core_touch": core_touch,
        },
    }


__all__ = ["EFFORT_SCHEMA_VERSION", "score_effort_profile"]
