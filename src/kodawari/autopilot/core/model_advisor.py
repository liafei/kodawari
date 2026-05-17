"""Optional model-driven advisory — instinct pattern suggestion and compact compression.

All public functions return None when the advisor is not configured or on any failure.
The main autopilot loop MUST treat a None return as "use heuristic fallback"; it MUST NOT
raise or propagate any exception from this module.

## Activation

Enabled when ALL of the following are true:
  1. ``WORKFLOW_MODEL_ADVISOR`` is not set to a falsy value (0/false/no/off).
  2. The ``anthropic`` Python package is importable.
  3. ``WORKFLOW_ADVISOR_API_KEY`` or ``ANTHROPIC_API_KEY`` env var is non-empty.

## Model

Uses ``claude-sonnet-4-6`` with short timeouts. These are utility calls — prompts are
kept narrow, max_tokens is capped low, and timeouts default to 15 s.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_ADVISOR_MODEL = "claude-sonnet-4-6"
_DEFAULT_TIMEOUT = 15.0

# Compact fields larger than this char count are eligible for model compression.
COMPACT_MUST_FIX_THRESHOLD = 5
COMPACT_ERRORS_THRESHOLD = 3

_PATTERN_PROMPT = (
    "An automated task runner encountered a repeated failure. Based on the error message "
    "below, suggest a single Python glob pattern (e.g. tests/test_api.py or src/**/*.py) "
    "for the test or source file most likely to fix this issue. "
    "Output ONLY the glob pattern — no explanation, no quotes, no extra text.\n\n"
    "Category: {category}\nPhase: {phase}\nError message: {message}"
)

_COMPRESS_PROMPT = (
    "Consolidate the following action items into at most {max_items} concise, unique bullet "
    "points. Remove near-duplicates and overly vague items. "
    "Output a JSON array of strings only — no other text, no markdown fences.\n\n"
    "Items:\n{items_json}"
)

_TIER_PROMPT = (
    "You classify the complexity of an autopilot development task into one of three tiers:\n"
    "  - lite: 1-2 files, no contract/schema/security impact, helper/test/doc edits.\n"
    "  - standard: 3-5 files, modest cross-module scope, no architecture/contract change.\n"
    "  - heavy: contract/schema/security/migration/architecture; >5 files; or ambiguous high-risk.\n"
    "\n"
    "Safety rules:\n"
    "  - Prefer standard over lite when uncertain.\n"
    "  - Prefer heavy if public contract / schema / security / data migration is involved.\n"
    "  - Return JSON only — no other text, no markdown fences.\n"
    "\n"
    "Static heuristic score (higher = more complex): {static_score}\n"
    "Heuristic reasons: {reasons}\n"
    "\n"
    "Task description: {task_direction}\n"
    "Files: {files}\n"
    "\n"
    "Respond with a JSON object:\n"
    "  {{\"tier\": \"lite|standard|heavy\", \"confidence\": 0.0-1.0, \"risk_flags\": [\"...\"], \"reason\": \"short\"}}\n"
)


def _advisor_api_key() -> str:
    return (
        os.environ.get("WORKFLOW_ADVISOR_API_KEY", "").strip()
        or os.environ.get("ANTHROPIC_API_KEY", "").strip()
    )


def model_is_enabled() -> bool:
    """Return True when model advisory is fully configured and not disabled."""
    gate = os.environ.get("WORKFLOW_MODEL_ADVISOR", "").strip().lower()
    if gate in {"0", "false", "no", "off"}:
        return False
    try:
        import anthropic as _a  # noqa: F401
    except ImportError:
        return False
    return bool(_advisor_api_key())


def _call_model(prompt: str, *, max_tokens: int = 256, timeout: float = _DEFAULT_TIMEOUT) -> str | None:
    """Make a single Sonnet call. Returns None on any failure."""
    if not model_is_enabled():
        return None
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=_advisor_api_key(), timeout=timeout)
        response = client.messages.create(
            model=_ADVISOR_MODEL,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return str(response.content[0].text).strip()
    except Exception as exc:
        logger.warning(
            "model_advisor: Sonnet call failed (%s: %s); falling back to heuristic",
            type(exc).__name__,
            str(exc)[:120],
        )
        return None


def _parse_json_list(raw: str | None) -> list[str] | None:
    """Parse a JSON array of strings from model output. Returns None on any parse failure."""
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        # Try stripping markdown fences the model may have added
        cleaned = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError:
            logger.warning("model_advisor: could not parse JSON list from response: %r", raw[:80])
            return None
    if not isinstance(parsed, list):
        return None
    return [str(item).strip() for item in parsed if str(item).strip()]


def _parse_tier_json(raw: str | None) -> dict[str, Any] | None:
    """Parse {tier, confidence, risk_flags, reason} JSON. None on any failure."""
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        cleaned = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError:
            logger.warning("model_advisor: could not parse tier JSON: %r", raw[:80])
            return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def suggest_tier(
    *,
    task_direction: str,
    files: list[str],
    static_score: int,
    reasons: list[str],
    timeout: float = _DEFAULT_TIMEOUT,
) -> dict[str, Any] | None:
    """Ask Sonnet for a tier classification on a gray-zone task.

    Returns {"tier", "confidence", "risk_flags", "reason"} on success, else None.
    None means "advisor disabled or call failed" — detector must fall back to
    its safe heuristic (default STANDARD).
    """
    if not model_is_enabled():
        return None
    prompt = _TIER_PROMPT.format(
        static_score=int(static_score),
        reasons=", ".join(str(r) for r in (reasons or [])[:10]) or "(none)",
        task_direction=str(task_direction or "")[:600],
        files=", ".join(str(f) for f in (files or [])[:20]) or "(none)",
    )
    raw = _call_model(prompt, max_tokens=128, timeout=timeout)
    parsed = _parse_tier_json(raw)
    if parsed is None:
        return None
    tier = str(parsed.get("tier") or "").strip().lower()
    if tier not in {"lite", "standard", "heavy"}:
        return None
    return {
        "tier": tier,
        "confidence": float(parsed.get("confidence") or 0.0),
        "risk_flags": list(parsed.get("risk_flags") or ()),
        "reason": str(parsed.get("reason") or "")[:200],
    }


def suggest_instinct_pattern(
    *,
    message: str,
    category: str,
    phase: str,
    timeout: float = _DEFAULT_TIMEOUT,
) -> str | None:
    """Ask Sonnet to suggest a glob pattern for the given repeated error.

    Returns a glob string (e.g. ``tests/test_api.py``) or None if the advisor
    is not enabled, the call fails, or the response looks invalid.
    """
    prompt = _PATTERN_PROMPT.format(
        category=str(category or "").strip(),
        phase=str(phase or "").strip(),
        message=str(message or "").strip()[:800],
    )
    raw = _call_model(prompt, max_tokens=64, timeout=timeout)
    if not raw:
        return None
    # Accept only the first line; reject if it looks like prose (spaces inside the path)
    first_line = raw.splitlines()[0].strip()
    if not first_line or len(first_line) > 120:
        logger.warning("model_advisor: pattern response too long or empty: %r", raw[:80])
        return None
    # A valid glob has no unescaped internal spaces (unlike a prose sentence)
    path_part = first_line.replace("\\ ", "")
    if " " in path_part:
        logger.warning("model_advisor: pattern response looks like prose: %r", first_line[:80])
        return None
    return first_line


def compress_compact_fields(
    *,
    must_fix: list[str],
    recent_errors: list[dict[str, Any]],
    max_must_fix: int = COMPACT_MUST_FIX_THRESHOLD,
    max_errors: int = COMPACT_ERRORS_THRESHOLD,
    timeout: float = _DEFAULT_TIMEOUT,
) -> dict[str, Any] | None:
    """Ask Sonnet to deduplicate/compress oversized compact fields.

    Returns a dict with compressed replacements for any oversized field, or None
    if the advisor is disabled, both fields are within budget, or all calls fail.
    The caller is responsible for merging returned keys into the compact payload.
    """
    result: dict[str, Any] = {}
    if len(must_fix) > max_must_fix:
        prompt = _COMPRESS_PROMPT.format(
            max_items=max_must_fix,
            items_json=json.dumps(must_fix[:20], ensure_ascii=False),
        )
        compressed = _parse_json_list(_call_model(prompt, max_tokens=512, timeout=timeout))
        if compressed is not None:
            result["must_fix"] = compressed
    if len(recent_errors) > max_errors:
        summaries = [
            str(e.get("message") or e.get("error") or "")
            for e in recent_errors[:10]
        ]
        summaries = [s for s in summaries if s]
        if summaries:
            prompt = _COMPRESS_PROMPT.format(
                max_items=max_errors,
                items_json=json.dumps(summaries, ensure_ascii=False),
            )
            compressed = _parse_json_list(_call_model(prompt, max_tokens=512, timeout=timeout))
            if compressed is not None:
                result["recent_errors_summary"] = compressed
    return result if result else None


__all__ = [
    "model_is_enabled",
    "suggest_instinct_pattern",
    "compress_compact_fields",
    "COMPACT_MUST_FIX_THRESHOLD",
    "COMPACT_ERRORS_THRESHOLD",
]
