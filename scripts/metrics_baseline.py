"""Frozen quality baseline for kodawari.

Produces a reproducible JSON snapshot of complexity / nesting / file-size
metrics across src/kodawari/. Used by:
- P0 of the quality plan: baseline for later PRs to diff against
- P4 ratchet gate: prevent regression

Tool choice (frozen):
- Complexity: radon.complexity.cc_visit (industry standard Python CC)
- Nesting: ast.NodeVisitor tracking control-flow node depth
  (If / For / While / With / Try / ExceptHandler / AsyncFor / AsyncWith)
- File size: len(text.splitlines()) incl. blanks and comments
- Shim detection: string match 'sys.modules[__name__]' in file text
- Dedup: flat files whose name also exists in a subpackage are excluded
  from dedup_count (since they are or will become shims)

Scope: src/kodawari/ only. Tests not included.
"""
from __future__ import annotations

import ast
import json
import pathlib
import sys
from datetime import datetime, timezone
from typing import Any

try:
    import radon
    from radon.complexity import cc_visit
    RADON_VERSION = radon.__version__
except ImportError:
    print("ERROR: radon not installed. Run: pip install radon", file=sys.stderr)
    sys.exit(1)

REPO = pathlib.Path(__file__).resolve().parents[1]
SRC = REPO / "src" / "kodawari"

# Thresholds come from code_redline (single source of truth).
# Hardcoded here as fallback if code_redline import fails at baseline time.
try:
    from code_redline import REDLINE
    COMPLEXITY_BLOCK = REDLINE.complexity_block
    COMPLEXITY_WARN = REDLINE.complexity_warn
    NESTING_MAX = REDLINE.nesting_max
    FILE_BLOCK_LINES = REDLINE.file_complexity_block_lines
    FILE_WARN_LINES = REDLINE.file_complexity_warn_lines
except ImportError:
    COMPLEXITY_BLOCK = 10
    COMPLEXITY_WARN = 7
    NESTING_MAX = 4
    FILE_BLOCK_LINES = 1500
    FILE_WARN_LINES = 1000


class NestingVisitor(ast.NodeVisitor):
    """Track max nesting depth across control-flow statements."""

    # Statements that increase nesting
    _NESTING_NODES = (
        ast.If, ast.For, ast.While, ast.With, ast.Try,
        ast.ExceptHandler, ast.AsyncFor, ast.AsyncWith,
    )

    def __init__(self) -> None:
        self.current = 0
        self.max_depth = 0

    def _enter(self, node: ast.AST) -> None:
        self.current += 1
        if self.current > self.max_depth:
            self.max_depth = self.current
        self.generic_visit(node)
        self.current -= 1

    def visit_If(self, node: ast.If) -> None: self._enter(node)
    def visit_For(self, node: ast.For) -> None: self._enter(node)
    def visit_While(self, node: ast.While) -> None: self._enter(node)
    def visit_With(self, node: ast.With) -> None: self._enter(node)
    def visit_Try(self, node: ast.Try) -> None: self._enter(node)
    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None: self._enter(node)
    def visit_AsyncFor(self, node: ast.AsyncFor) -> None: self._enter(node)
    def visit_AsyncWith(self, node: ast.AsyncWith) -> None: self._enter(node)


def is_shim(text: str) -> bool:
    """A file is treated as a shim iff it contains the sys.modules[__name__] marker."""
    return "sys.modules[__name__]" in text


def is_flat_with_sub_duplicate(path: pathlib.Path, all_rels: set[pathlib.PurePosixPath]) -> bool:
    """True if this is a top-level flat file under cli/ or autopilot/ whose
    basename also exists in a subpackage under the same domain."""
    rel = path.relative_to(SRC)
    parts = rel.parts
    if len(parts) != 2:
        return False  # must be domain/file.py (flat level)
    domain, fname = parts
    if domain not in {"cli", "autopilot"}:
        return False
    # Look for any sub-file with the same filename at depth >= 3
    for other in all_rels:
        other_parts = other.parts
        if len(other_parts) >= 3 and other_parts[0] == domain and other_parts[-1] == fname:
            return True
    return False


def collect_files() -> list[pathlib.Path]:
    return sorted(p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts)


def analyze() -> dict[str, Any]:
    files = collect_files()
    all_rels = {pathlib.PurePosixPath(*p.relative_to(SRC).parts) for p in files}

    total_files = len(files)
    shim_files: list[str] = []
    real_files: list[pathlib.Path] = []

    # Per-file records
    file_records: dict[str, dict[str, Any]] = {}

    complexity_raw: list[dict[str, Any]] = []   # includes flat+sub duplicates
    complexity_dedup: list[dict[str, Any]] = []  # flat-with-sub-duplicate excluded

    nesting_raw: list[dict[str, Any]] = []
    nesting_dedup: list[dict[str, Any]] = []

    over1000: list[dict[str, Any]] = []
    over500: list[dict[str, Any]] = []

    for p in files:
        rel = str(p.relative_to(SRC)).replace("\\", "/")
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            file_records[rel] = {"error": str(exc)}
            continue

        if is_shim(text):
            shim_files.append(rel)
            continue

        real_files.append(p)
        is_dup = is_flat_with_sub_duplicate(p, all_rels)

        loc = len(text.splitlines())
        if loc > FILE_BLOCK_LINES:
            over1000.append({"file": rel, "lines": loc, "is_flat_dup": is_dup})
        if loc > FILE_WARN_LINES:
            over500.append({"file": rel, "lines": loc, "is_flat_dup": is_dup})

        # Complexity via radon
        try:
            tree = ast.parse(text)
            for fn in cc_visit(text):
                if fn.complexity >= COMPLEXITY_BLOCK:
                    rec = {
                        "file": rel,
                        "name": fn.name,
                        "complexity": fn.complexity,
                        "lineno": fn.lineno,
                        "is_flat_dup": is_dup,
                    }
                    complexity_raw.append(rec)
                    if not is_dup:
                        complexity_dedup.append(rec)
        except SyntaxError:
            pass

        # Nesting via AST
        try:
            tree2 = ast.parse(text)
            v = NestingVisitor()
            v.visit(tree2)
            if v.max_depth > NESTING_MAX:
                rec = {"file": rel, "depth": v.max_depth, "is_flat_dup": is_dup}
                nesting_raw.append(rec)
                if not is_dup:
                    nesting_dedup.append(rec)
        except SyntaxError:
            pass

    # Sort descending
    complexity_raw.sort(key=lambda r: -r["complexity"])
    complexity_dedup.sort(key=lambda r: -r["complexity"])
    nesting_raw.sort(key=lambda r: -r["depth"])
    nesting_dedup.sort(key=lambda r: -r["depth"])
    over1000.sort(key=lambda r: -r["lines"])
    over500.sort(key=lambda r: -r["lines"])

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tool": {
            "complexity": f"radon {RADON_VERSION}",
            "nesting": "ast.NodeVisitor (If/For/While/With/Try/ExceptHandler/AsyncFor/AsyncWith)",
            "file_size": "len(text.splitlines())",
        },
        "redline": {
            "complexity_block": COMPLEXITY_BLOCK,
            "complexity_warn": COMPLEXITY_WARN,
            "nesting_max": NESTING_MAX,
            "file_block_lines": FILE_BLOCK_LINES,
            "file_warn_lines": FILE_WARN_LINES,
        },
        "scope": {
            "root": str(SRC.relative_to(REPO)).replace("\\", "/"),
            "tests_included": False,
        },
        "files": {
            "total": total_files,
            "shims": len(shim_files),
            "real": len(real_files),
        },
        "complexity": {
            "violations_raw": len(complexity_raw),
            "violations_dedup": len(complexity_dedup),
            "top_raw": complexity_raw[:30],
            "top_dedup": complexity_dedup[:30],
        },
        "nesting": {
            "violations_raw": len(nesting_raw),
            "violations_dedup": len(nesting_dedup),
            "top_raw": nesting_raw[:20],
            "top_dedup": nesting_dedup[:20],
        },
        "file_size": {
            "over_block": over1000,
            "over_warn": over500[:30],
            "over_warn_total": len(over500),
        },
    }


def main() -> None:
    data = analyze()
    out = REPO / "_baseline" / "metrics_baseline.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    # Also print summary to stdout
    print(f"Wrote {out}")
    print(f"Files: total={data['files']['total']} shims={data['files']['shims']} real={data['files']['real']}")
    print(f"Complexity violations (>={COMPLEXITY_BLOCK}): raw={data['complexity']['violations_raw']} dedup={data['complexity']['violations_dedup']}")
    print(f"Nesting violations (>{NESTING_MAX}): raw={data['nesting']['violations_raw']} dedup={data['nesting']['violations_dedup']}")
    print(f"File size >{FILE_BLOCK_LINES}: {len(data['file_size']['over_block'])}")
    print(f"File size >{FILE_WARN_LINES}: {data['file_size']['over_warn_total']}")


if __name__ == "__main__":
    main()
