from __future__ import annotations

from pathlib import Path

import kodawari


def test_top_level_public_api_is_explicit() -> None:
    assert kodawari.__all__ == ["__version__", "gate", "patterns", "safety", "spec_generator"]
    assert isinstance(kodawari.__version__, str)
    assert kodawari.__version__

    for name in ("gate", "patterns", "safety", "spec_generator"):
        module = getattr(kodawari, name)
        assert module.__name__ == f"kodawari.{name}"
        assert isinstance(module.__all__, list)


def test_stability_policy_documents_compatibility_window() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    text = (repo_root / "STABILITY.md").read_text(encoding="utf-8")

    assert "kodawari.__all__" in text
    assert "at least 90 days" in text
    assert "schema_version" in text
    assert "User commands" in text
