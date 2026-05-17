"""CI guard: fail if any legacy module has a past REMOVE_AFTER date.

When a REMOVE_AFTER date is in the past, the team MUST either:
1. Delete the module, or
2. Extend the date (with a comment explaining why).

This test turns red automatically on the deadline date. Scans every
REMOVE_AFTER occurrence in the file — module docstrings, class/function
docstrings, or comments — so the guard can't be bypassed by tucking the
marker into a nested scope.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
import re

_REMOVE_AFTER_RE = re.compile(r"REMOVE_AFTER:\s*(\d{4}-\d{2}-\d{2})")

_SRC_ROOT = Path(__file__).resolve().parent.parent / "src"


def _iter_remove_after_matches() -> list[tuple[Path, str]]:
    out: list[tuple[Path, str]] = []
    for py_file in _SRC_ROOT.rglob("*.py"):
        try:
            text = py_file.read_text(encoding="utf-8")
        except OSError:
            continue
        for m in _REMOVE_AFTER_RE.finditer(text):
            out.append((py_file, m.group(1)))
    return out


def test_no_expired_legacy_modules() -> None:
    today = date.today()
    expired: list[tuple[Path, date]] = []
    for path, raw in _iter_remove_after_matches():
        remove_after = date.fromisoformat(raw)
        if remove_after < today:
            expired.append((path, remove_after))
    if expired:
        details = "\n".join(
            f"  {p.relative_to(_SRC_ROOT)} (expired {d})" for p, d in expired
        )
        raise AssertionError(
            f"Expired legacy modules — delete or extend the REMOVE_AFTER date:\n{details}"
        )


def test_remove_after_dates_are_parseable() -> None:
    """All REMOVE_AFTER values must be valid ISO dates."""
    for _path, raw in _iter_remove_after_matches():
        date.fromisoformat(raw)
