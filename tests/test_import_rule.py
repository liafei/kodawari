from __future__ import annotations

import ast
from pathlib import Path


def _module_name(path: Path, src_root: Path) -> str:
    relative = path.relative_to(src_root).with_suffix("")
    return ".".join(relative.parts)


def test_public_packages_do_not_cross_import_internal_modules() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    src_root = repo_root / "src"
    package_root = src_root / "kodawari"
    violations: list[str] = []

    for path in package_root.rglob("*.py"):
        module_name = _module_name(path, src_root)
        if module_name.startswith("kodawari._internal"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "kodawari._internal" or alias.name.startswith("kodawari._internal."):
                        violations.append(f"{path.relative_to(repo_root)}:{node.lineno} imports {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module == "kodawari._internal" or module.startswith("kodawari._internal."):
                    violations.append(f"{path.relative_to(repo_root)}:{node.lineno} imports {module}")

    assert violations == []
