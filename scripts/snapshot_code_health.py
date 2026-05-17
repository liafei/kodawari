#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from kodawari.gate.code_health import collect_code_health_snapshot  # noqa: E402


def _targets(project_root: Path, raw_paths: list[str]) -> list[Path]:
    if not raw_paths:
        return [(project_root / "src").resolve()]
    items: list[Path] = []
    for raw in raw_paths:
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = project_root / candidate
        items.append(candidate.resolve())
    return items


def run(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root).resolve()
    output = Path(args.output).resolve()
    payload = collect_code_health_snapshot(
        project_root=project_root,
        targets=_targets(project_root, list(args.src or [])),
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate a code health baseline snapshot.")
    parser.add_argument("--project-root", default=str(REPO_ROOT))
    parser.add_argument("--src", action="append", help="Target path(s) to scan; defaults to ./src")
    parser.add_argument("--output", default=str(REPO_ROOT / "planning" / "code_health_baseline.json"))
    return parser


if __name__ == "__main__":
    raise SystemExit(run(build_parser().parse_args()))
