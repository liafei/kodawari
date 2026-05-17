"""Review package import-surface guards."""

from __future__ import annotations

import re
from pathlib import Path


SRC_ROOT = Path(__file__).resolve().parent.parent / "src"

_FLAT_REVIEW_IMPORT_RE = re.compile(
    r"^from kodawari\.autopilot\.(review_bridge|review_bundle|review_contract|review_precheck|cli_reviewer|codex_reviewer|plan_reviewer) import ",
    re.MULTILINE,
)


def _is_legacy_review_module(path: Path) -> bool:
    rel = path.relative_to(SRC_ROOT)
    if rel.parts[:3] == ("kodawari", "autopilot", "review"):
        return True
    if rel.parts[:2] != ("kodawari", "autopilot"):
        return False
    return rel.name in {
        "review_bridge.py",
        "review_bundle.py",
        "review_contract.py",
        "review_precheck.py",
        "cli_reviewer.py",
        "codex_reviewer.py",
        "plan_reviewer.py",
    }


def test_runtime_code_uses_review_package_import_surface() -> None:
    offenders: list[str] = []
    for path in SRC_ROOT.rglob("*.py"):
        if _is_legacy_review_module(path):
            continue
        text = path.read_text(encoding="utf-8")
        if _FLAT_REVIEW_IMPORT_RE.search(text):
            offenders.append(str(path.relative_to(SRC_ROOT)).replace("\\", "/"))
    assert offenders == []


def test_review_package_exports_expected_symbols() -> None:
    from kodawari.autopilot.review import (
        build_review_bundle,
        derive_runtime_review_evidence,
        summarize_peer_review,
    )
    from kodawari.autopilot.review.gateways.cli import REAL_REVIEW_MODES
    from kodawari.autopilot.review.gateways.codex import request_codex_review
    from kodawari.autopilot.review.gateways.peer_review import request_peer_review
    from kodawari.autopilot.review.gateways.plan import review_plan

    assert callable(build_review_bundle)
    assert callable(derive_runtime_review_evidence)
    assert callable(summarize_peer_review)
    assert "real_cli_reviewer" in REAL_REVIEW_MODES
    assert callable(request_codex_review)
    assert callable(request_peer_review)
    assert callable(review_plan)
