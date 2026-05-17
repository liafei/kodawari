"""Tests for C6 — model_advisor tier classifier + detect_complexity bridge.

Covers:
  - _parse_tier_json handles valid/invalid/fenced JSON
  - suggest_tier returns None when advisor disabled
  - suggest_tier returns None for invalid tier values
  - suggest_tier returns structured dict when model yields valid JSON
  - model_advisor_tier_classifier raises RuntimeError when disabled (fallback)
  - detect_complexity uses bridge; falls back to STANDARD on RuntimeError
"""

from __future__ import annotations

from typing import Any

import pytest

from kodawari.autopilot import model_advisor
from kodawari.autopilot.complexity_detector import (
    ComplexityInput,
    detect_complexity,
    model_advisor_tier_classifier,
)


# ---------------------------------------------------------------------------
# _parse_tier_json
# ---------------------------------------------------------------------------


def test_parse_tier_json_returns_dict():
    raw = '{"tier":"lite","confidence":0.8,"risk_flags":[],"reason":"small"}'
    parsed = model_advisor._parse_tier_json(raw)
    assert parsed == {"tier": "lite", "confidence": 0.8, "risk_flags": [], "reason": "small"}


def test_parse_tier_json_strips_markdown_fences():
    raw = '```json\n{"tier":"heavy"}\n```'
    assert model_advisor._parse_tier_json(raw) == {"tier": "heavy"}


def test_parse_tier_json_returns_none_for_invalid():
    assert model_advisor._parse_tier_json(None) is None
    assert model_advisor._parse_tier_json("") is None
    assert model_advisor._parse_tier_json("not json") is None
    assert model_advisor._parse_tier_json('["tier","lite"]') is None  # list not dict


# ---------------------------------------------------------------------------
# suggest_tier
# ---------------------------------------------------------------------------


def test_suggest_tier_returns_none_when_advisor_disabled(monkeypatch):
    monkeypatch.setattr(model_advisor, "model_is_enabled", lambda: False)
    result = model_advisor.suggest_tier(
        task_direction="add helper", files=["a.py"], static_score=10, reasons=[],
    )
    assert result is None


def test_suggest_tier_returns_none_when_call_returns_none(monkeypatch):
    monkeypatch.setattr(model_advisor, "model_is_enabled", lambda: True)
    monkeypatch.setattr(model_advisor, "_call_model", lambda *a, **kw: None)
    assert model_advisor.suggest_tier(
        task_direction="x", files=[], static_score=0, reasons=[],
    ) is None


def test_suggest_tier_returns_structured_dict_on_success(monkeypatch):
    monkeypatch.setattr(model_advisor, "model_is_enabled", lambda: True)
    monkeypatch.setattr(
        model_advisor, "_call_model",
        lambda *a, **kw: '{"tier":"standard","confidence":0.72,"risk_flags":["contract"],"reason":"cross-layer"}',
    )
    result = model_advisor.suggest_tier(
        task_direction="refactor cache", files=["a.py"], static_score=45, reasons=["refactor_keyword:+40"],
    )
    assert result == {
        "tier": "standard",
        "confidence": 0.72,
        "risk_flags": ["contract"],
        "reason": "cross-layer",
    }


def test_suggest_tier_rejects_invalid_tier(monkeypatch):
    monkeypatch.setattr(model_advisor, "model_is_enabled", lambda: True)
    monkeypatch.setattr(
        model_advisor, "_call_model",
        lambda *a, **kw: '{"tier":"extra_spicy","confidence":0.5}',
    )
    assert model_advisor.suggest_tier(
        task_direction="x", files=[], static_score=0, reasons=[],
    ) is None


# ---------------------------------------------------------------------------
# model_advisor_tier_classifier bridge
# ---------------------------------------------------------------------------


def test_model_advisor_bridge_raises_when_disabled(monkeypatch):
    """RuntimeError signals detect_complexity to use its fallback path."""
    monkeypatch.setattr(model_advisor, "suggest_tier", lambda **kw: None)
    inp = ComplexityInput(
        feature="f1", task_direction="x",
        source_of_truth_files=("a.py", "b.py", "c.py"),
    )
    with pytest.raises(RuntimeError, match="model_advisor_disabled_or_failed"):
        model_advisor_tier_classifier(inp, 50, ("files=3-5:+10",))


def test_model_advisor_bridge_returns_advisor_result(monkeypatch):
    monkeypatch.setattr(
        model_advisor, "suggest_tier",
        lambda **kw: {"tier": "lite", "confidence": 0.9, "risk_flags": [], "reason": "small scope"},
    )
    inp = ComplexityInput(feature="f1", task_direction="x")
    result = model_advisor_tier_classifier(inp, 40, ())
    assert result["tier"] == "lite"
    assert result["confidence"] == 0.9


# ---------------------------------------------------------------------------
# detect_complexity with bridge: fallback path when advisor disabled
# ---------------------------------------------------------------------------


def test_detect_complexity_gray_zone_falls_back_when_advisor_disabled(monkeypatch):
    """Bridge raises RuntimeError -> _classify_with_llm returns standard fallback."""
    monkeypatch.setattr(model_advisor, "suggest_tier", lambda **kw: None)
    inp = ComplexityInput(
        feature="f1",
        task_direction="refactor cache strategy",
        source_of_truth_files=(
            "backend/cache/a.py",
            "backend/cache/b.py",
            "backend/cache/c.py",
        ),
    )
    decision = detect_complexity(inp, llm_classifier=model_advisor_tier_classifier)
    assert decision.tier == "standard"
    assert decision.source == "fallback_llm_failed"


def test_detect_complexity_gray_zone_uses_advisor_when_enabled(monkeypatch):
    """When advisor works, detect_complexity returns LLM tier with source=llm_gray_zone."""
    monkeypatch.setattr(
        model_advisor, "suggest_tier",
        lambda **kw: {"tier": "heavy", "confidence": 0.85, "risk_flags": ["contract"], "reason": "routes touched"},
    )
    inp = ComplexityInput(
        feature="f1",
        task_direction="refactor cache strategy",
        source_of_truth_files=(
            "backend/cache/a.py",
            "backend/cache/b.py",
            "backend/cache/c.py",
        ),
    )
    decision = detect_complexity(inp, llm_classifier=model_advisor_tier_classifier)
    assert decision.tier == "heavy"
    assert decision.source == "llm_gray_zone"
    assert decision.llm_used is True


def test_detect_complexity_api_behavior_small_change_defaults_standard():
    inp = ComplexityInput(
        feature="social-api",
        task_direction="fix social api response behavior",
        changed_files=(
            "backend/api/v1/routes/social_routes.py",
            "tests/test_social_routes.py",
        ),
    )

    decision = detect_complexity(inp)

    assert decision.tier == "standard"
    assert decision.source == "api_behavior_floor"


def test_detect_complexity_public_api_contract_still_heavy():
    inp = ComplexityInput(
        feature="public-api",
        task_direction="public api contract change",
        changed_files=("backend/api/v1/routes/public_routes.py",),
    )

    decision = detect_complexity(inp)

    assert decision.tier == "heavy"
    assert decision.source == "hard_rule"
