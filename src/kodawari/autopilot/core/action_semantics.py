"""Canonical action string normalizer for collaboration wire values.

All producers (collaboration_flow.py) and consumers (local_adapter.py,
execution_prompt_common.py, state dispatch) must pass values through
normalize_action() so legacy state files with vendor-prefixed strings
remain readable while new writes emit only canonical values.
"""

from __future__ import annotations


_LEGACY_TO_CANONICAL: dict[str, str] = {
    "opus_design": "design",
    "codex_implement": "implement",
    "opus_review": "peer_review",
    "codex_self_review": "self_review",
    "codex_fix": "fix_round",
}

_CANONICAL: frozenset[str] = frozenset(_LEGACY_TO_CANONICAL.values()) | frozenset(
    {"verify", "rules_gate", "proceed_to_gate", "finish"}
)


def normalize_action(raw: str | None) -> str:
    s = (raw or "").strip()
    return _LEGACY_TO_CANONICAL.get(s, s)


def normalize_requested_action(raw: str | None) -> str:
    return normalize_action(raw)


def is_canonical_action(value: str) -> bool:
    return value in _CANONICAL
