from __future__ import annotations

from pathlib import Path


def _doc(name: str) -> str:
    repo_root = Path(__file__).resolve().parents[1]
    return (repo_root / "docs" / name).read_text(encoding="utf-8")


def test_product_docs_cover_user_operator_and_capability_paths() -> None:
    user_guide = _doc("USER_GUIDE.md")
    operator_runbook = _doc("OPERATOR_RUNBOOK.md")
    index = _doc("README.md")

    assert "CAPABILITY_MAP.md" in user_guide
    assert "kodawari work-all" in user_guide
    assert "kodawari status" in user_guide
    assert "noop_test_only" in user_guide
    assert "Do not use `kodawari`" in user_guide

    assert "simulate_local" in operator_runbook
    assert "real CLI review" in operator_runbook
    assert "WORKFLOW_REVIEWER_BACKEND" in operator_runbook
    assert "schema_version" in operator_runbook
    assert "release_audit.py" in operator_runbook

    assert "USER_GUIDE.md" in index
    assert "OPERATOR_RUNBOOK.md" in index
