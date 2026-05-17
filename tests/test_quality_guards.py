"""P4 quality ratchet guards.

These tests enforce that quality metrics do not regress:
  1. Cyclomatic complexity ratchet — no new functions with CC >= 10.
  2. CLI shim guard — any flat CLI file that has a sub counterpart must be a shim.
  3. Sub-package back-reference lint — sub files must not import flat siblings
     when a sub-path exists.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

_SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "kodawari"
_BASELINE_PATH = Path(__file__).resolve().parents[1] / "_baseline" / "metrics_baseline.json"


# ---------------------------------------------------------------------------
# Guard 1: Cyclomatic complexity ratchet
# ---------------------------------------------------------------------------


def _count_cc_violations(src_root: Path) -> int:
    try:
        import radon.complexity as rc  # type: ignore[import-untyped]
    except ImportError:
        pytest.skip("radon not installed")

    count = 0
    for pyfile in src_root.rglob("*.py"):
        try:
            # Use utf-8 (not utf-8-sig) to match metrics_baseline.py behaviour;
            # BOM files cause SyntaxError in cc_visit and are skipped.
            src = pyfile.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "sys.modules[__name__]" in src:
            continue
        try:
            count += sum(1 for r in rc.cc_visit(src) if r.complexity >= 10)
        except SyntaxError:
            continue
        except Exception:
            continue
    return count


def _baseline_dedup() -> int:
    if not _BASELINE_PATH.exists():
        pytest.skip("metrics_baseline.json not found")
    data = json.loads(_BASELINE_PATH.read_text(encoding="utf-8"))
    return int(data["complexity"]["violations_dedup"])


def test_complexity_ratchet_does_not_regress() -> None:
    """Total CC>=10 violation count must not exceed the frozen baseline."""
    baseline = _baseline_dedup()
    current = _count_cc_violations(_SRC_ROOT)
    assert current <= baseline, (
        f"Complexity ratchet exceeded: {current} violations (baseline={baseline}). "
        "Run scripts/metrics_baseline.py after reducing violations to update the floor."
    )


# ---------------------------------------------------------------------------
# Guard 2: CLI shim guard
# ---------------------------------------------------------------------------


def _is_shim(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return False
    return "sys.modules[__name__]" in text


def _cli_flat_files() -> list[Path]:
    cli_dir = _SRC_ROOT / "cli"
    sub_dirs = {p for p in cli_dir.iterdir() if p.is_dir() and not p.name.startswith("_")}
    sub_names: set[str] = set()
    for sub in sub_dirs:
        for pyfile in sub.glob("*.py"):
            if not pyfile.name.startswith("_"):
                sub_names.add(pyfile.name)
    flat_with_sub: list[Path] = []
    for flat in cli_dir.glob("*.py"):
        if flat.name.startswith("_"):
            continue
        if flat.name in sub_names:
            flat_with_sub.append(flat)
    return flat_with_sub


def test_cli_flat_files_with_sub_counterpart_are_shims() -> None:
    """Every flat CLI file that has a same-name sub file must be a sys.modules shim."""
    non_shims = [p for p in _cli_flat_files() if not _is_shim(p)]
    assert not non_shims, (
        "The following flat CLI files have sub counterparts but are NOT shims:\n"
        + "\n".join(f"  {p.relative_to(_SRC_ROOT)}" for p in sorted(non_shims))
        + "\nConvert them to sys.modules shims or remove the flat copy."
    )


# ---------------------------------------------------------------------------
# Guard 3: Sub-package back-reference lint
# ---------------------------------------------------------------------------


_DOMAIN_ROOTS = ("autopilot", "cli", "gate", "instincts", "safety")


def _has_sub_counterpart(flat_name: str, domain: str) -> bool:
    """Return True if flat_name.py exists in any sub-directory of domain."""
    domain_dir = _SRC_ROOT / domain
    for sub_dir in domain_dir.iterdir():
        if not sub_dir.is_dir() or sub_dir.name.startswith("_"):
            continue
        if (sub_dir / f"{flat_name}.py").exists():
            return True
    return False


def _sub_has_flat_sibling_with_sub_copy(import_name: str, domain: str) -> bool:
    """Return True if import_name is a flat module that also has a sub counterpart.

    Intentional flat-only modules (no sub copy) are not flagged.
    """
    prefix = f"kodawari.{domain}."
    if not import_name.startswith(prefix):
        return False
    remainder = import_name[len(prefix):]
    if "." in remainder:
        return False
    flat_path = _SRC_ROOT / domain / f"{remainder}.py"
    return flat_path.exists() and _has_sub_counterpart(remainder, domain)


def _find_back_references(src_root: Path) -> list[tuple[Path, str]]:
    violations: list[tuple[Path, str]] = []
    for domain in _DOMAIN_ROOTS:
        domain_dir = src_root / domain
        if not domain_dir.exists():
            continue
        for sub_dir in domain_dir.iterdir():
            if not sub_dir.is_dir() or sub_dir.name.startswith("_"):
                continue
            for pyfile in sub_dir.glob("*.py"):
                try:
                    src_text = pyfile.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                if "sys.modules[__name__]" in src_text:
                    continue
                try:
                    tree = ast.parse(src_text)
                except SyntaxError:
                    continue
                for node in ast.walk(tree):
                    if isinstance(node, ast.ImportFrom) and node.module:
                        if _sub_has_flat_sibling_with_sub_copy(node.module, domain):
                            flat_name = node.module.split(".")[-1]
                            flat_path = src_root / domain / f"{flat_name}.py"
                            if not _is_shim(flat_path):
                                violations.append((pyfile, node.module))
    return violations


def test_subpackage_files_do_not_import_non_shim_flat_siblings() -> None:
    """Sub-package files must not import flat siblings that are not yet shims."""
    violations = _find_back_references(_SRC_ROOT)
    if not violations:
        return
    lines = [f"  {p.relative_to(_SRC_ROOT)}: imports flat '{m}'" for p, m in sorted(violations)]
    pytest.fail(
        "Sub-package files back-reference flat modules that are not shims:\n"
        + "\n".join(lines)
        + "\nConvert the flat module to a shim first."
    )
