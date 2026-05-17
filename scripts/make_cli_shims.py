"""P1: Convert CLI flat files to sys.modules shims.

Steps:
1. git mv main_support.py -> cli/core/main_support.py
2. git mv provenance.py  -> cli/evidence/provenance.py
3. Write shims for all 83 flat CLI locations (81 pairs + 2 moved files)
"""
from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
CLI = REPO / "src" / "kodawari" / "cli"
BASELINE = REPO / "_baseline" / "cli_fork_report.json"

SHIM_TEMPLATE = (
    '"""Shim: real implementation lives at {target}."""\n'
    "import sys as _sys, importlib as _importlib\n"
    '_sys.modules[__name__] = _importlib.import_module("{target}")\n'
)


def git_mv(src: pathlib.Path, dst: pathlib.Path) -> None:
    result = subprocess.run(
        ["git", "mv", str(src), str(dst)],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"  ERROR git mv {src} -> {dst}: {result.stderr}", file=sys.stderr)
        sys.exit(1)
    print(f"  git mv {src.relative_to(REPO)} -> {dst.relative_to(REPO)}")


def write_shim(flat_path: pathlib.Path, target_module: str) -> None:
    content = SHIM_TEMPLATE.format(target=target_module)
    flat_path.write_text(content, encoding="utf-8")
    print(f"  shim {flat_path.relative_to(REPO)} -> {target_module}")


def main() -> None:
    data = json.loads(BASELINE.read_text(encoding="utf-8"))

    # Step 1: Move flat-only files that need a subpackage home
    moves = [
        (CLI / "main_support.py", CLI / "core" / "main_support.py", "kodawari.cli.core.main_support"),
        (CLI / "provenance.py", CLI / "evidence" / "provenance.py", "kodawari.cli.evidence.provenance"),
    ]
    print("=== P1.1: git mv flat-only files to subpackages ===")
    for src, dst, target in moves:
        git_mv(src, dst)
        write_shim(src, target)

    # Step 2: Build flat -> target_module mapping from all 81 pairs
    print("\n=== P1.2: Write shims for 81 flat/sub pairs ===")
    mapping: dict[str, str] = {}
    for cat in ["identical", "import_only", "real_divergence"]:
        for r in data["pairs"][cat]:
            flat = r["flat"]   # e.g. src/kodawari/cli/foo.py
            sub = r["sub"]     # e.g. src/kodawari/cli/evidence/foo.py
            m = re.search(r"cli/(\w+)/(\w+)\.py$", sub)
            if not m:
                print(f"  SKIP unrecognized sub path: {sub}", file=sys.stderr)
                continue
            subpkg, modname = m.group(1), m.group(2)
            flat_name_m = re.search(r"cli/(\w+)\.py$", flat)
            if not flat_name_m:
                print(f"  SKIP unrecognized flat path: {flat}", file=sys.stderr)
                continue
            flat_path = REPO / flat
            target = f"kodawari.cli.{subpkg}.{modname}"
            mapping[str(flat_path)] = target

    for flat_str, target in sorted(mapping.items()):
        flat_path = pathlib.Path(flat_str)
        write_shim(flat_path, target)

    total = len(moves) + len(mapping)
    print(f"\nDone. {total} shims written ({len(moves)} new files after git mv, {len(mapping)} replacements).")


if __name__ == "__main__":
    main()
