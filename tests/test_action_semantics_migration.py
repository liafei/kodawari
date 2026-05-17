"""P1a migration tests: action string normalizer correctness."""

from __future__ import annotations

import pytest

from kodawari.autopilot.action_semantics import (
    _LEGACY_TO_CANONICAL,
    normalize_action,
    normalize_requested_action,
)
from kodawari.autopilot.execution_prompt_common import render_fix_round_preamble


def test_every_legacy_value_has_canonical() -> None:
    expected_legacy = {
        "opus_design",
        "codex_implement",
        "opus_review",
        "codex_self_review",
        "codex_fix",
    }
    assert set(_LEGACY_TO_CANONICAL.keys()) == expected_legacy


def test_normalize_all_legacy_values() -> None:
    assert normalize_action("opus_design") == "design"
    assert normalize_action("codex_implement") == "implement"
    assert normalize_action("opus_review") == "peer_review"
    assert normalize_action("codex_self_review") == "self_review"
    assert normalize_action("codex_fix") == "fix_round"


def test_canonical_values_pass_through() -> None:
    for canonical in ("design", "implement", "peer_review", "self_review", "fix_round",
                      "verify", "rules_gate", "proceed_to_gate", "finish"):
        assert normalize_action(canonical) == canonical


def test_none_and_empty_normalize_to_empty() -> None:
    assert normalize_action(None) == ""
    assert normalize_action("") == ""
    assert normalize_action("  ") == ""


def test_read_legacy_state_file() -> None:
    """Old state dict with 'codex_fix' must be recognized as fix_round."""
    assert normalize_requested_action("codex_fix") == "fix_round"


def test_write_canonical_only() -> None:
    """New writes use canonical values; normalizer is identity on them."""
    assert normalize_action("fix_round") == "fix_round"
    assert normalize_action("peer_review") == "peer_review"


def test_fix_round_preamble_triggered_by_legacy_action() -> None:
    """render_fix_round_preamble must fire for legacy 'codex_fix' action."""
    payload = {"requested_action": "codex_fix", "must_fix": ["item1"]}
    lines = render_fix_round_preamble(payload)
    assert len(lines) >= 2
    assert any("item1" in line for line in lines)


def test_fix_round_preamble_triggered_by_canonical_action() -> None:
    """render_fix_round_preamble must fire for canonical 'fix_round' action."""
    payload = {"requested_action": "fix_round", "must_fix": ["item2"]}
    lines = render_fix_round_preamble(payload)
    assert len(lines) >= 2
    assert any("item2" in line for line in lines)


def test_fix_round_preamble_not_triggered_for_other_actions() -> None:
    payload = {"requested_action": "implement", "must_fix": ["item"]}
    assert render_fix_round_preamble(payload) == []
