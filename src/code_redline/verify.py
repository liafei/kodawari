"""Single-source guard — consumer projects run this to detect redline
numbers that were hardcoded instead of imported from this package.

Example consumer test::

    from code_redline.verify import find_hardcoded_copies

    def test_no_hardcoded_redline_numbers():
        hits = find_hardcoded_copies(project_root="src", exclude=["vendor/"])
        assert not hits, f"Hardcoded redline values found: {hits}"
"""
from __future__ import annotations

import re
from pathlib import Path

from code_redline.standard import REDLINE, RedlineStandard


def _numbers_to_guard(std: RedlineStandard) -> list[int]:
    """The specific integers that identify redline thresholds. We guard
    these as word-boundary matches so ``= 1000`` or ``: 1000`` fires
    but ``= 10000`` does not."""
    return [
        std.file_complexity_warn_lines,  # 1000
        std.file_complexity_block_lines,  # 1500
        std.max_violations,  # 50
        std.file_complexity_block_sum,  # 30
        std.file_complexity_warn_sum,  # 20
        std.complexity_block,  # 10
        std.complexity_warn,  # 7
        std.nesting_max,  # 4
    ]


def find_hardcoded_copies(
    *,
    project_root: str | Path,
    exclude: list[str] | None = None,
    std: RedlineStandard = REDLINE,
) -> list[tuple[Path, int, str]]:
    """Walk ``project_root`` (*.py only) and return ``(path, line_no, text)``
    for lines that contain a hardcoded redline threshold literal.

    The check is intentionally strict: any assignment like
    ``foo = 1500``, ``limit = 50``, or a dict entry like
    ``{"file_max_lines": 1500}`` will match. Legitimate uses
    (e.g. ``retry_attempts = 4`` for unrelated counters) will collide
    with the guard — consumers should either use a different number
    or import from code_redline.

    Returns an empty list when clean.
    """
    root = Path(project_root).resolve()
    exclude_list = [str(root / ex) for ex in (exclude or [])]
    numbers = _numbers_to_guard(std)
    # Match assignment or dict-literal use of one of the guarded integers,
    # as a whole word.
    pattern = re.compile(
        r"(?:=|:)\s*(" + "|".join(str(n) for n in numbers) + r")\b(?!\s*[.0-9])"
    )
    hits: list[tuple[Path, int, str]] = []
    for py in root.rglob("*.py"):
        spy = str(py)
        if any(spy.startswith(ex) for ex in exclude_list):
            continue
        try:
            text = py.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for idx, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                hits.append((py.relative_to(root), idx, line.strip()))
    return hits
