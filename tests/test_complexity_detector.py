"""Tests for the complexity detector (C2).

Covers:
  - explicit user --tier
  - hard-rule heavy triggers (path tokens, file suffixes, keywords, path_type)
  - strong-lite shortcut (single source+test, all docs)
  - static scoring bands
  - LLM gray-zone classifier injection + failure fallback
  - learned adjustments (placeholder)
  - safety: gray zone without LLM defaults to STANDARD (not lite)
"""

from __future__ import annotations

from typing import Any

import pytest

from kodawari.autopilot.complexity_detector import (
    ComplexityInput,
    detect_complexity,
)


def _input(**kwargs: Any) -> ComplexityInput:
    base: dict = dict(feature="f1")
    base.update(kwargs)
    return ComplexityInput(**base)


# ---------------------------------------------------------------------------
# Explicit user --tier
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tier", ["lite", "standard", "heavy"])
def test_explicit_tier_user_overrides_everything(tier):
    inp = _input(
        task_direction="redesign whole architecture",
        source_of_truth_files=("backend/api/v1/routes/auth_routes.py",),
        path_type="schema_change",
    )
    decision = detect_complexity(inp, requested_tier=tier)
    assert decision.tier == tier
    assert decision.source == "explicit"
    assert decision.confidence == 1.0


def test_explicit_tier_invalid_falls_through_to_auto():
    inp = _input(task_direction="add helper")
    decision = detect_complexity(inp, requested_tier="garbage")
    assert decision.source != "explicit"


# ---------------------------------------------------------------------------
# Empty-input safety fallback — must produce STANDARD (not lite)
# ---------------------------------------------------------------------------


def test_empty_input_falls_back_to_standard():
    """Zero real signals must land in STANDARD, not lite."""
    # feature is always truthy from CLI, so _input() keeps feature="f1"
    # but every other signal field is left empty.
    inp = _input()
    decision = detect_complexity(inp)
    assert decision.tier == "standard"
    assert decision.source == "empty_input_fallback"
    assert "no_input_signals" in decision.reasons


def test_empty_input_fallback_ignores_feature_name():
    """Even with feature set, empty signals => standard fallback (feature alone is not a signal)."""
    inp = _input(feature="some-very-important-feature")
    decision = detect_complexity(inp)
    assert decision.source == "empty_input_fallback"


def test_empty_input_fallback_ignores_learned_hints_only():
    """Learned hints without any other signal must NOT block the fallback."""
    inp = _input(learned_hints=({"pattern": "f1", "score_delta": 5},))
    decision = detect_complexity(inp)
    assert decision.source == "empty_input_fallback"


def test_single_source_of_truth_file_bypasses_empty_fallback():
    """A single source_of_truth_files entry is a real signal: must NOT short-circuit."""
    inp = _input(source_of_truth_files=("docs/readme.md",))
    decision = detect_complexity(inp)
    assert decision.source != "empty_input_fallback"
    # all-docs single file => strong_lite
    assert decision.source == "strong_lite"


def test_changed_files_alone_bypasses_empty_fallback():
    """changed_files signal alone must route through scoring, not fallback."""
    inp = _input(changed_files=("backend/services/foo.py",))
    decision = detect_complexity(inp)
    assert decision.source != "empty_input_fallback"


# ---------------------------------------------------------------------------
# Hard rules — must produce HEAVY
# ---------------------------------------------------------------------------


def test_path_type_does_not_force_heavy():
    # The PRD contract's path_type enum is {"read", "write", "both"} and
    # does not carry a "this is a schema change" semantic, so path_type
    # alone must not force heavy. Contract/schema risk is covered by the
    # file-path and keyword hard-rules.
    decision = detect_complexity(_input(path_type="write"))
    assert decision.source != "hard_rule"


def test_hard_rule_migration_file():
    inp = _input(source_of_truth_files=("backend/db/migration_sql/001_init.sql",))
    decision = detect_complexity(inp)
    assert decision.tier == "heavy"
    assert "migration" in decision.risk_flags


def test_hard_rule_sql_file_suffix():
    inp = _input(source_of_truth_files=("backend/seeds/data.sql",))
    decision = detect_complexity(inp)
    assert decision.tier == "heavy"


def test_hard_rule_auth_path():
    inp = _input(source_of_truth_files=("backend/api/v1/services/auth_service.py",))
    decision = detect_complexity(inp)
    assert decision.tier == "heavy"
    assert "security" in decision.risk_flags


def test_hard_rule_credential_path():
    inp = _input(source_of_truth_files=("backend/api/v1/services/credential_store.py",))
    decision = detect_complexity(inp)
    assert decision.tier == "heavy"


def test_hard_rule_public_routes_path():
    inp = _input(source_of_truth_files=("backend/api/v1/routes/daily_routes.py",))
    decision = detect_complexity(inp)
    assert decision.tier == "heavy"
    assert "contract" in decision.risk_flags


def test_hard_rule_kodawari_core():
    inp = _input(source_of_truth_files=("kodawari/src/kodawari/autopilot/execution_artifacts.py",))
    decision = detect_complexity(inp)
    assert decision.tier == "heavy"
    assert "core" in decision.risk_flags


def test_hard_rule_breaking_change_keyword():
    inp = _input(task_direction="introduce breaking change to public API contract")
    decision = detect_complexity(inp)
    assert decision.tier == "heavy"
    assert decision.hard_rule.startswith("hard:keyword=")


def test_hard_rule_redesign_keyword():
    decision = detect_complexity(_input(task_direction="redesign daily edition pipeline"))
    assert decision.tier == "heavy"


def test_hard_rule_data_migration_keyword_in_requirements():
    decision = detect_complexity(_input(
        task_direction="upgrade backfill",
        requirements_text="this involves a data migration of all events",
    ))
    assert decision.tier == "heavy"


# ---------------------------------------------------------------------------
# Strong-lite shortcut
# ---------------------------------------------------------------------------


def test_strong_lite_single_source_and_test_pair():
    inp = _input(source_of_truth_files=(
        "backend/api/v1/services/top_list_service.py",
        "tests/test_t063_format_score_display.py",
    ))
    decision = detect_complexity(inp)
    assert decision.tier == "lite"
    assert decision.source == "strong_lite"


def test_strong_lite_all_docs():
    inp = _input(source_of_truth_files=("README.md", "docs/runbook.md"))
    decision = detect_complexity(inp)
    assert decision.tier == "lite"
    assert "all_docs" in decision.reasons


def test_strong_lite_two_sources_no_test_does_not_qualify():
    inp = _input(source_of_truth_files=(
        "backend/service_a.py",
        "backend/service_b.py",
    ))
    decision = detect_complexity(inp)
    # Falls through; no strong_lite source
    assert decision.source != "strong_lite"


def test_strong_lite_does_not_override_hard_rule():
    """Auth path beats single-source-test pattern."""
    inp = _input(source_of_truth_files=(
        "backend/api/v1/services/auth_service.py",
        "tests/test_auth_service.py",
    ))
    decision = detect_complexity(inp)
    assert decision.tier == "heavy"
    assert decision.source == "hard_rule"


# ---------------------------------------------------------------------------
# Static scoring bands
# ---------------------------------------------------------------------------


def test_static_score_lite_for_helper_addition():
    inp = _input(
        task_direction="add helper function for percentage formatting",
        source_of_truth_files=("backend/services/util.py",),  # 1 file -> -20
    )
    decision = detect_complexity(inp)
    assert decision.tier == "lite"
    assert decision.source == "static_score"


def test_static_score_heavy_for_many_files():
    inp = _input(
        task_direction="add support across components",
        source_of_truth_files=tuple(f"backend/m{i}.py" for i in range(12)),  # files>10 -> +60
        layers=("service", "repository", "frontend"),  # +25 +30
    )
    decision = detect_complexity(inp)
    assert decision.tier == "heavy"
    assert decision.source == "static_score"


def test_static_score_gray_zone_falls_back_to_standard_when_no_llm():
    """SAFETY: gray zone without LLM goes to STANDARD, not LITE."""
    # 3 files=+10, service path=+10, refactor keyword=+40 => score 60 (gray zone)
    inp = _input(
        task_direction="refactor the cache strategy across services",
        source_of_truth_files=(
            "backend/cache/handler.py",
            "backend/cache/loader.py",
            "backend/cache/index.py",
        ),
        layers=("service",),
    )
    decision = detect_complexity(inp)
    assert 26 <= decision.static_score <= 69, f"setup invalid: score={decision.static_score}"
    assert decision.tier == "standard"
    assert decision.source == "fallback_gray_zone_no_llm"
    assert any("default_standard" in r for r in decision.reasons)


# ---------------------------------------------------------------------------
# LLM classifier injection
# ---------------------------------------------------------------------------


def test_llm_classifier_called_for_gray_zone():
    """LLM is called when score is in 26-69 band."""
    calls: list[tuple] = []

    def fake_llm(inp, score, reasons):
        calls.append((inp.feature, score))
        return {"tier": "lite", "confidence": 0.8, "reason": "small_local_scope", "risk_flags": []}

    inp = _input(
        feature="gray-zone-feature",
        task_direction="refactor cache loader",
        source_of_truth_files=(
            "backend/cache/handler.py",
            "backend/cache/loader.py",
            "backend/cache/index.py",
        ),
    )
    decision = detect_complexity(inp, llm_classifier=fake_llm)
    assert len(calls) == 1
    assert decision.tier == "lite"
    assert decision.source == "llm_gray_zone"
    assert decision.llm_used is True


def test_llm_classifier_not_called_for_strong_lite():
    """Strong-lite shortcut bypasses LLM."""
    calls: list[tuple] = []

    def fake_llm(inp, score, reasons):
        calls.append((inp.feature,))
        return {"tier": "heavy"}

    inp = _input(source_of_truth_files=(
        "backend/services/util.py",
        "tests/test_util.py",
    ))
    decision = detect_complexity(inp, llm_classifier=fake_llm)
    assert len(calls) == 0
    assert decision.tier == "lite"


def test_llm_classifier_not_called_for_hard_rule():
    """Hard rule bypasses LLM."""
    calls: list[tuple] = []

    def fake_llm(inp, score, reasons):
        calls.append((inp.feature,))
        return {"tier": "lite"}

    inp = _input(source_of_truth_files=("backend/db/migration_sql/001.sql",))
    decision = detect_complexity(inp, llm_classifier=fake_llm)
    assert len(calls) == 0
    assert decision.tier == "heavy"


def test_llm_classifier_failure_falls_back_to_standard():
    def broken_llm(inp, score, reasons):
        raise RuntimeError("model timeout")

    inp = _input(
        task_direction="refactor cache loader",
        source_of_truth_files=(
            "backend/cache/handler.py",
            "backend/cache/loader.py",
            "backend/cache/index.py",
        ),
    )
    decision = detect_complexity(inp, llm_classifier=broken_llm)
    assert decision.tier == "standard"
    assert decision.source == "fallback_llm_failed"


def test_llm_classifier_invalid_tier_coerced_to_standard():
    def malformed_llm(inp, score, reasons):
        return {"tier": "extra-spicy", "confidence": 0.5}

    inp = _input(
        task_direction="refactor cache loader",
        source_of_truth_files=(
            "backend/cache/handler.py",
            "backend/cache/loader.py",
            "backend/cache/index.py",
        ),
    )
    decision = detect_complexity(inp, llm_classifier=malformed_llm)
    assert decision.tier == "standard"


# ---------------------------------------------------------------------------
# Learned adjustments (placeholder for C5 wiring)
# ---------------------------------------------------------------------------


def test_learned_hints_adjust_score_upward_into_heavy():
    # Base score: refactor kw (+40) + files=3-5 (+10) = 50 (standard band).
    # Learned +130 caps at +40 → 90 (heavy). Asserts cap is enforced and the
    # learning signal still lifts a borderline-standard task into heavy.
    inp = _input(
        feature="auth-rewrite-task",
        task_direction="refactor authentication subsystem",
        source_of_truth_files=(
            "backend/services/auth.py",
            "backend/services/session.py",
            "backend/services/token.py",
            "backend/services/cookie.py",
        ),
        learned_hints=(
            {"pattern": "auth-rewrite-task", "score_delta": 130,
             "last_seen": "2026-05-01T00:00:00+00:00"},
        ),
    )
    decision = detect_complexity(inp)
    assert decision.tier == "heavy"
    assert decision.learned_adjustments == ("learned:auth-rewrite-task:+40",)


def test_learned_hints_do_not_apply_when_pattern_not_matched():
    inp = _input(
        feature="tiny-helper-task",
        task_direction="add helper function",
        source_of_truth_files=("backend/services/util.py",),
        learned_hints=({"pattern": "auth-rewrite", "score_delta": -40},),
    )
    decision = detect_complexity(inp)
    assert decision.tier == "lite"
    assert decision.learned_adjustments == ()


def test_learned_hints_require_exact_feature_match_not_prefix():
    inp = _input(
        feature="auth-rewrite-v2",
        task_direction="add helper function",
        source_of_truth_files=("backend/services/util.py",),
        learned_hints=({"pattern": "auth-rewrite", "score_delta": 120},),
    )
    decision = detect_complexity(inp)
    assert decision.tier == "lite"
    assert decision.learned_adjustments == ()


def test_learned_hints_no_delta_no_adjustment():
    inp = _input(
        task_direction="add helper",
        source_of_truth_files=("backend/util.py",),
        learned_hints=({"pattern": "noop_hint", "score_delta": 0},),
    )
    decision = detect_complexity(inp)
    assert decision.tier == "lite"
    assert decision.learned_adjustments == ()


def test_learned_hints_opposite_directions_dedupe_to_newest():
    """Same pattern with over -20 + under +20 must NOT silently sum to 0.

    Pre-fix bug: real lane store had both directions promoted for
    `prd11-admin-token-auth-real4` and they cancelled to net delta 0,
    making lane learning effectively dead. Newest last_seen wins.
    """
    inp = _input(
        feature="auth-rewrite",
        task_direction="adjust auth flow",
        source_of_truth_files=("backend/services/auth.py",),
        learned_hints=(
            {"pattern": "auth-rewrite", "score_delta": -20,
             "last_seen": "2026-04-01T00:00:00+00:00", "confidence": 0.9},
            {"pattern": "auth-rewrite", "score_delta": 20,
             "last_seen": "2026-05-01T00:00:00+00:00", "confidence": 0.7},
        ),
    )
    decision = detect_complexity(inp)
    assert decision.learned_adjustments == ("learned:auth-rewrite:+20",)


def test_learned_hints_dedupe_breaks_tie_by_confidence():
    """When last_seen ties, higher-confidence hint wins."""
    inp = _input(
        feature="auth-rewrite",
        task_direction="adjust auth flow",
        source_of_truth_files=("backend/services/auth.py",),
        learned_hints=(
            {"pattern": "auth-rewrite", "score_delta": -20,
             "last_seen": "2026-05-01T00:00:00+00:00", "confidence": 0.7},
            {"pattern": "auth-rewrite", "score_delta": 20,
             "last_seen": "2026-05-01T00:00:00+00:00", "confidence": 0.95},
        ),
    )
    decision = detect_complexity(inp)
    assert decision.learned_adjustments == ("learned:auth-rewrite:+20",)


def test_learned_hints_total_delta_capped_at_threshold():
    """A single high-magnitude hint must clamp to ±_LANE_DELTA_CAP=40."""
    inp = _input(
        feature="auth-rewrite",
        task_direction="adjust auth flow",
        source_of_truth_files=("backend/services/auth.py",),
        learned_hints=(
            {"pattern": "auth-rewrite", "score_delta": 130,
             "last_seen": "2026-05-01T00:00:00+00:00"},
        ),
    )
    decision = detect_complexity(inp)
    assert decision.learned_adjustments == ("learned:auth-rewrite:+40",)


def test_learned_hints_kill_switch_disables_learning(monkeypatch):
    """WORKFLOW_LANE_LEARNING_DISABLED=1 short-circuits all lane adjustments."""
    monkeypatch.setenv("WORKFLOW_LANE_LEARNING_DISABLED", "1")
    inp = _input(
        feature="helper-format-task",
        task_direction="add helper function for percentage formatting",
        source_of_truth_files=("backend/services/util.py",),
        learned_hints=(
            {"pattern": "helper-format-task", "score_delta": 130,
             "last_seen": "2026-05-01T00:00:00+00:00"},
        ),
    )
    decision = detect_complexity(inp)
    assert decision.learned_adjustments == ()
    assert decision.tier == "lite"


# ---------------------------------------------------------------------------
# ComplexityInput utility
# ---------------------------------------------------------------------------


def test_complexity_input_all_files_dedupes_and_orders():
    inp = ComplexityInput(
        feature="f1",
        source_of_truth_files=("a.py", "b.py"),
        task_card_files=("b.py", "c.py"),
        changed_files=("c.py", "d.py"),
    )
    assert inp.all_files() == ("a.py", "b.py", "c.py", "d.py")


def test_complexity_input_all_files_handles_empty():
    assert ComplexityInput(feature="f1").all_files() == ()


# ---------------------------------------------------------------------------
# Decision shape sanity
# ---------------------------------------------------------------------------


def test_every_decision_has_score_and_reasons():
    samples = [
        _input(task_direction="add helper", source_of_truth_files=("util.py",)),
        _input(path_type="schema_change"),
        _input(source_of_truth_files=("README.md",)),
    ]
    for inp in samples:
        d = detect_complexity(inp)
        assert d.tier in {"lite", "standard", "heavy"}
        assert isinstance(d.static_score, int)
        assert isinstance(d.reasons, tuple)


# ---------------------------------------------------------------------------
# ComplexityInput signal completeness (§1 test coverage)
# ---------------------------------------------------------------------------


def test_complexity_detector_uses_task_card_files_for_hard_rules():
    """Verify that task_card_files with /routes/ triggers heavy tier."""
    inp = _input(
        task_card_files=("backend/api/v1/routes/new_endpoint.py",),
        changed_files=(),  # 关键：git 没检到，只有 task_card
    )
    decision = detect_complexity(inp, requested_tier="auto")
    assert decision.tier == "heavy"  # 因为 /routes/ 命中 hard rule


def test_complexity_detector_uses_source_of_truth_files():
    """Verify that source_of_truth_files contributes to scoring."""
    # Use 3+ files so we get positive file_score (+10) instead of negative (-20)
    inp_with_many_truth = _input(
        source_of_truth_files=("src/main.py", "src/utils.py", "src/config.py"),
        changed_files=(),
    )
    inp_with_one_truth = _input(
        source_of_truth_files=("src/main.py",),  # 1 .py file (not all_docs)
        changed_files=(),
    )
    decision_many = detect_complexity(inp_with_many_truth, requested_tier="auto")
    decision_one = detect_complexity(inp_with_one_truth, requested_tier="auto")
    # Both should have reasons mentioning "files" from source_of_truth_files scoring
    assert "files" in str(decision_many.reasons)
    assert "files" in str(decision_one.reasons)
    # 3 files should score higher (+10) than 1 file (-20)
    assert decision_many.static_score > decision_one.static_score


# ---------------------------------------------------------------------------
# Path-signal unification: file-count and strong-lite must respect
# task_card_files / changed_files, not just source_of_truth_files. This
# guards against the regression where _build_complexity_input moved the
# path signal to task_card + changed_files but the detector still only
# counted source_of_truth_files.
# ---------------------------------------------------------------------------


def test_file_count_score_uses_task_card_files():
    # 12 task_card files must score as files>10 (+60), not files<=2 (-20).
    inp = _input(
        task_card_files=tuple(f"backend/m{i}.py" for i in range(12)),
        source_of_truth_files=(),
        changed_files=(),
    )
    decision = detect_complexity(inp, requested_tier="auto")
    assert "files>10:+60" in decision.reasons
    assert "files<=2:-20" not in decision.reasons


def test_file_count_score_uses_changed_files():
    inp = _input(
        task_card_files=(),
        source_of_truth_files=(),
        changed_files=tuple(f"src/a{i}.py" for i in range(6)),
    )
    decision = detect_complexity(inp, requested_tier="auto")
    assert "files=6-10:+35" in decision.reasons


def test_strong_lite_fires_on_task_card_impl_test_pair():
    # src/app.py + tests/test_app.py living only on task_card_files
    # must still trigger strong_lite.
    inp = _input(
        source_of_truth_files=(),
        task_card_files=("src/app.py", "tests/test_app.py"),
        changed_files=(),
    )
    decision = detect_complexity(inp, requested_tier="auto")
    assert decision.source == "strong_lite"
    assert decision.tier == "lite"
    assert "single_source_and_test_pair" in decision.reasons


def test_strong_lite_all_docs_fires_on_changed_files():
    inp = _input(
        source_of_truth_files=(),
        task_card_files=(),
        changed_files=("README.md", "docs/guide.md"),
    )
    decision = detect_complexity(inp, requested_tier="auto")
    assert decision.source == "strong_lite"
    assert decision.tier == "lite"
    assert "all_docs" in decision.reasons
