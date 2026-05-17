"""Verify LocalCodexAdapterConfig __post_init__ dual-field aliasing behaviour.

Four cases per the migration spec (P1.1.2.1):
1. Only canonical reviewer_* fields → no warning, works.
2. Only legacy opus_gateway_* credential fields → DeprecationWarning emitted,
   canonical fields are synced.
3. Both canonical and legacy passed with different values → canonical wins,
   legacy is ignored, warning still emitted.
4. Neither passed → defaults, no warning.
"""
from __future__ import annotations

import warnings

import pytest


def _make_config(**kwargs):  # type: ignore[no-untyped-def]
    from kodawari.autopilot.local_adapter import LocalCodexAdapterConfig

    return LocalCodexAdapterConfig(**kwargs)


def test_canonical_only_no_warning() -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        cfg = _make_config(reviewer_base_url="https://canonical.test", reviewer_api_key="key-canonical")

    dep_warnings = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert not dep_warnings, "No DeprecationWarning expected when only canonical fields are used"
    assert cfg.reviewer_base_url == "https://canonical.test"
    assert cfg.reviewer_api_key == "key-canonical"


def test_legacy_only_warns_and_syncs() -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        cfg = _make_config(opus_gateway_base_url="https://legacy.test", opus_gateway_api_key="key-legacy")

    dep_warnings = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert dep_warnings, "DeprecationWarning expected when legacy opus_gateway_* fields are used"
    assert "opus_gateway" in str(dep_warnings[0].message).lower()
    assert cfg.reviewer_base_url == "https://legacy.test", "Legacy value must be synced to canonical"
    assert cfg.reviewer_api_key == "key-legacy", "Legacy api_key must be synced to canonical"


def test_canonical_wins_over_legacy() -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        cfg = _make_config(
            reviewer_base_url="https://canonical.test",
            reviewer_api_key="key-canonical",
            opus_gateway_base_url="https://legacy.test",
            opus_gateway_api_key="key-legacy",
        )

    dep_warnings = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    # Warning is still emitted because legacy fields were explicitly set
    assert dep_warnings, "DeprecationWarning should fire even when canonical also set"
    assert cfg.reviewer_base_url == "https://canonical.test", "Canonical must win over legacy"
    assert cfg.reviewer_api_key == "key-canonical", "Canonical api_key must win over legacy"


def test_neither_passed_no_warning() -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        cfg = _make_config()

    dep_warnings = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert not dep_warnings, "No DeprecationWarning when neither set"
    assert cfg.reviewer_base_url == ""
    assert cfg.reviewer_api_key == ""


# --- model / api_format coverage (GPT review follow-up) ---


def test_legacy_model_only_warns_and_syncs_to_reviewer_model() -> None:
    """Regression: passing only opus_gateway_model must warn AND propagate
    the value into reviewer_model so CLI/Codex reviewer paths see it."""
    from kodawari.autopilot.local_adapter import LocalCodexAdapter

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        cfg = _make_config(opus_gateway_model="legacy-model-only", opus_reviewer_backend="cli")

    dep_warnings = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert dep_warnings, "DeprecationWarning expected when only opus_gateway_model is set"
    assert "opus_gateway_model" in str(dep_warnings[0].message)
    assert cfg.reviewer_model == "legacy-model-only"

    adapter = LocalCodexAdapter(cfg)
    assert adapter._cli_reviewer_config().model == "legacy-model-only"
    assert adapter._codex_reviewer_config().model == "legacy-model-only"
    assert adapter._review_runtime_gateway().get("model") == "legacy-model-only"


def test_legacy_api_format_only_warns_and_syncs() -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        cfg = _make_config(opus_gateway_api_format="anthropic")

    dep_warnings = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert dep_warnings, "DeprecationWarning expected when only opus_gateway_api_format is set"
    assert "opus_gateway_api_format" in str(dep_warnings[0].message)
    assert cfg.reviewer_api_format == "anthropic"


def test_canonical_model_wins_over_legacy() -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        cfg = _make_config(
            reviewer_model="canonical-model",
            opus_gateway_model="legacy-model",
        )

    dep_warnings = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert dep_warnings, "Warning still emitted when both set"
    assert cfg.reviewer_model == "canonical-model"


def test_default_model_does_not_warn() -> None:
    """opus_gateway_model carries the non-empty default 'claude-opus-4.1';
    that default must not trigger a DeprecationWarning."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        cfg = _make_config()

    dep_warnings = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert not dep_warnings
    assert cfg.opus_gateway_model == "claude-opus-4.1"
    assert cfg.opus_gateway_api_format == "auto"
