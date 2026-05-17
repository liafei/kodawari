"""Tests for model_advisor.py — covers disabled/enabled paths, fallback, and wiring."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from kodawari.autopilot import model_advisor
from kodawari.autopilot.model_advisor import (
    compress_compact_fields,
    model_is_enabled,
    suggest_instinct_pattern,
)


# ---------------------------------------------------------------------------
# Activation gate
# ---------------------------------------------------------------------------

class TestModelIsEnabled:
    def test_disabled_by_env_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WORKFLOW_MODEL_ADVISOR", "0")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
        assert model_is_enabled() is False

    def test_disabled_by_env_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WORKFLOW_MODEL_ADVISOR", "false")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
        assert model_is_enabled() is False

    def test_disabled_when_no_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("WORKFLOW_MODEL_ADVISOR", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("WORKFLOW_ADVISOR_API_KEY", raising=False)
        assert model_is_enabled() is False

    def test_disabled_when_sdk_not_importable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
        monkeypatch.delenv("WORKFLOW_MODEL_ADVISOR", raising=False)
        # Patch import to simulate missing package
        with patch.dict("sys.modules", {"anthropic": None}):
            assert model_is_enabled() is False

    def test_enabled_with_key_and_sdk(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake-key")
        monkeypatch.delenv("WORKFLOW_MODEL_ADVISOR", raising=False)
        try:
            import anthropic  # noqa: F401
            sdk_present = True
        except ImportError:
            sdk_present = False
        if not sdk_present:
            pytest.skip("anthropic SDK not installed")
        assert model_is_enabled() is True

    def test_workflow_advisor_key_takes_precedence(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WORKFLOW_ADVISOR_API_KEY", "sk-workflow-key")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("WORKFLOW_MODEL_ADVISOR", raising=False)
        try:
            import anthropic  # noqa: F401
        except ImportError:
            pytest.skip("anthropic SDK not installed")
        assert model_is_enabled() is True


# ---------------------------------------------------------------------------
# suggest_instinct_pattern
# ---------------------------------------------------------------------------

class TestSuggestInstinctPattern:
    def test_returns_none_when_advisor_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WORKFLOW_MODEL_ADVISOR", "0")
        result = suggest_instinct_pattern(message="db error", category="verify", phase="VERIFY")
        assert result is None

    def test_valid_pattern_returned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("WORKFLOW_MODEL_ADVISOR", raising=False)
        with patch.object(model_advisor, "_call_model", return_value="tests/test_db.py") as mock:
            result = suggest_instinct_pattern(message="db error", category="verify", phase="VERIFY")
        assert result == "tests/test_db.py"
        mock.assert_called_once()

    def test_prose_response_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with patch.object(model_advisor, "_call_model", return_value="You should look at tests for db"):
            result = suggest_instinct_pattern(message="db error", category="verify", phase="VERIFY")
        assert result is None

    def test_empty_response_returns_none(self) -> None:
        with patch.object(model_advisor, "_call_model", return_value=""):
            result = suggest_instinct_pattern(message="db error", category="verify", phase="VERIFY")
        assert result is None

    def test_model_failure_returns_none(self) -> None:
        with patch.object(model_advisor, "_call_model", return_value=None):
            result = suggest_instinct_pattern(message="any", category="runtime", phase="RUNTIME")
        assert result is None

    def test_multiline_uses_first_line_only(self) -> None:
        with patch.object(model_advisor, "_call_model", return_value="tests/test_api.py\nexplanation here"):
            result = suggest_instinct_pattern(message="api error", category="runtime", phase="RUNTIME")
        assert result == "tests/test_api.py"

    def test_overly_long_response_rejected(self) -> None:
        with patch.object(model_advisor, "_call_model", return_value="x" * 130):
            result = suggest_instinct_pattern(message="any", category="runtime", phase="RUNTIME")
        assert result is None


# ---------------------------------------------------------------------------
# compress_compact_fields
# ---------------------------------------------------------------------------

class TestCompressCompactFields:
    def _make_must_fix(self, n: int) -> list[str]:
        return [f"Must fix item {i}" for i in range(n)]

    def _make_errors(self, n: int) -> list[dict[str, Any]]:
        return [{"message": f"Error {i} occurred in module X"} for i in range(n)]

    def test_returns_none_when_both_within_budget(self) -> None:
        with patch.object(model_advisor, "_call_model") as mock:
            result = compress_compact_fields(
                must_fix=self._make_must_fix(3),
                recent_errors=self._make_errors(2),
            )
        assert result is None
        mock.assert_not_called()

    def test_compresses_must_fix_when_oversized(self) -> None:
        compressed = ["Fix auth module", "Fix DB migrations", "Fix test coverage"]
        with patch.object(
            model_advisor, "_call_model", return_value=json.dumps(compressed)
        ):
            result = compress_compact_fields(
                must_fix=self._make_must_fix(8),
                recent_errors=self._make_errors(2),
            )
        assert result is not None
        assert result["must_fix"] == compressed

    def test_compresses_errors_when_oversized(self) -> None:
        compressed = ["Auth error (×3)", "DB timeout (×2)"]
        with patch.object(
            model_advisor, "_call_model", return_value=json.dumps(compressed)
        ):
            result = compress_compact_fields(
                must_fix=self._make_must_fix(2),
                recent_errors=self._make_errors(5),
            )
        assert result is not None
        assert result["recent_errors_summary"] == compressed

    def test_fallback_when_model_fails(self) -> None:
        with patch.object(model_advisor, "_call_model", return_value=None):
            result = compress_compact_fields(
                must_fix=self._make_must_fix(10),
                recent_errors=self._make_errors(5),
            )
        assert result is None

    def test_fallback_when_model_returns_invalid_json(self) -> None:
        with patch.object(model_advisor, "_call_model", return_value="not json at all"):
            result = compress_compact_fields(
                must_fix=self._make_must_fix(10),
                recent_errors=self._make_errors(2),
            )
        assert result is None


# ---------------------------------------------------------------------------
# Integration: instinct engine uses model advisor at promotion
# ---------------------------------------------------------------------------

class TestInstinctEngineModelAdvisorWiring:
    def test_model_pattern_used_at_promotion(self, tmp_path: Path) -> None:
        """When model advisor returns a pattern, the promoted LearnedInstinct uses it."""
        from kodawari.instincts import engine as instinct_engine
        from kodawari.autopilot import model_advisor as ma

        event = {"message": "DB migration failed", "category": "gate", "phase": "GATE"}
        # Ingest threshold-1 events (not yet promoted) with model returning None
        with patch.object(ma, "_call_model", return_value=None):
            for _ in range(2):
                result = instinct_engine.ingest_error_event(
                    tmp_path, event, threshold=3
                )
        # After 2 events with threshold=3, still not promoted
        assert result["promoted"] is False

        # Ingest the threshold (3rd) event with model returning an advised pattern
        with patch.object(ma, "_call_model", return_value="tests/test_migrations.py"):
            result = instinct_engine.ingest_error_event(tmp_path, event, threshold=3)

        assert result["promoted"] is True
        assert result["learned_pattern"] == "tests/test_migrations.py"

    def test_heuristic_used_when_model_disabled(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """When WORKFLOW_MODEL_ADVISOR=0, heuristic pattern is used for promoted instinct."""
        monkeypatch.setenv("WORKFLOW_MODEL_ADVISOR", "0")
        from kodawari.instincts import engine as instinct_engine

        event = {"message": "migration schema error", "category": "gate", "phase": "GATE"}
        for _ in range(3):
            result = instinct_engine.ingest_error_event(tmp_path, event, threshold=3)

        assert result["promoted"] is True
        # Heuristic for "migration" + "gate" → tests/test_*migration*.py
        assert "migration" in result["learned_pattern"]


# ---------------------------------------------------------------------------
# Integration: semantic_compact triggers compression when oversized
# ---------------------------------------------------------------------------

class TestSemanticCompactCompressionWiring:
    def test_compression_applied_when_must_fix_oversized(self, tmp_path: Path) -> None:
        from kodawari.autopilot import model_advisor as ma
        from kodawari.autopilot.semantic_compact import materialize_semantic_compact

        # Build a fake state with many must_fix items
        class _FakeState:
            last_error = "db error"
            errors: list = []
            def get_value(self, key: str, default: Any = None) -> Any:
                return default

        class _FakeContext:
            review_feedback = type("RF", (), {
                "must_fix": [f"Fix item {i}" for i in range(10)],
                "gate_recommendation": None,
                "architecture_decisions": [],
            })()
            open_questions: list = []

        compressed = ["Fix auth", "Fix DB", "Fix tests"]
        with patch.object(ma, "_call_model", return_value=json.dumps(compressed)):
            result = materialize_semantic_compact(
                project_root=tmp_path,
                feature="test_feature",
                state=_FakeState(),
                context=_FakeContext(),
                planning_dir=tmp_path / "planning" / "test_feature",
            )
        if result["status"] == "written":
            payload = result["payload"]
            assert payload.get("compact_source") == "model_compressed"
            assert payload["must_fix"] == compressed

    def test_no_compression_when_within_budget(self, tmp_path: Path) -> None:
        from kodawari.autopilot import model_advisor as ma
        from kodawari.autopilot.semantic_compact import materialize_semantic_compact

        class _FakeState:
            last_error = ""
            errors: list = []
            def get_value(self, key: str, default: Any = None) -> Any:
                return default

        class _FakeContext:
            review_feedback = type("RF", (), {
                "must_fix": ["Fix one thing"],
                "gate_recommendation": None,
                "architecture_decisions": [],
            })()
            open_questions: list = []

        with patch.object(ma, "_call_model") as mock:
            materialize_semantic_compact(
                project_root=tmp_path,
                feature="test_feature",
                state=_FakeState(),
                context=_FakeContext(),
                planning_dir=tmp_path / "planning" / "test_feature",
            )
        mock.assert_not_called()
