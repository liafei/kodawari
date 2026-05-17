"""Ensure MERGED_CONTRACT_VERSION is assigned in exactly one place.

Invariant: only src/kodawari/infra/contract_version.py may contain
``MERGED_CONTRACT_VERSION = "<value>"``. Any other file doing so is a
dual-source bug and will make this test fail.
"""

from __future__ import annotations

import re
from pathlib import Path

# The one canonical source file (relative to src root)
_CANONICAL = Path("kodawari/infra/contract_version.py")

# Pattern that catches any bare assignment (both single and double quotes)
_PATTERN = re.compile(r"MERGED_CONTRACT_VERSION\s*=\s*[\"']")

_SRC_ROOT = Path(__file__).parent.parent / "src"


def _find_duplicates() -> list[Path]:
    """Return all src/ files that assign MERGED_CONTRACT_VERSION, excluding the canonical one."""
    duplicates: list[Path] = []
    for py_file in _SRC_ROOT.rglob("*.py"):
        rel = py_file.relative_to(_SRC_ROOT)
        if rel == _CANONICAL:
            continue
        try:
            text = py_file.read_text(encoding="utf-8")
        except OSError:
            continue
        if _PATTERN.search(text):
            duplicates.append(rel)
    return duplicates


def test_no_duplicate_merged_contract_version() -> None:
    """MERGED_CONTRACT_VERSION must be assigned only in infra/contract_version.py."""
    duplicates = _find_duplicates()
    assert duplicates == [], (
        "MERGED_CONTRACT_VERSION is assigned outside the canonical source. "
        f"Fix these files: {[str(p) for p in duplicates]}"
    )


def test_canonical_source_exists() -> None:
    """The canonical source file must itself define the constant."""
    canonical_path = _SRC_ROOT / _CANONICAL
    assert canonical_path.exists(), f"Canonical file missing: {canonical_path}"
    text = canonical_path.read_text(encoding="utf-8")
    assert _PATTERN.search(text), (
        f"Canonical file {_CANONICAL} does not define MERGED_CONTRACT_VERSION"
    )


def test_import_resolves() -> None:
    """The constant must be importable from the canonical module."""
    from kodawari.infra.contract_version import MERGED_CONTRACT_VERSION  # noqa: F401

    assert isinstance(MERGED_CONTRACT_VERSION, str) and MERGED_CONTRACT_VERSION
