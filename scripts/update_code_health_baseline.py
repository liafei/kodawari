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
from kodawari.gate.gate_ratchet import update_baseline_snapshot  # noqa: E402


def _read_json(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected object json: {path}")
    return payload


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
    baseline_path = Path(args.baseline).resolve()
    baseline = _read_json(baseline_path)
    if args.current:
        current = _read_json(Path(args.current).resolve())
    else:
        current = collect_code_health_snapshot(
            project_root=project_root,
            targets=_targets(project_root, list(args.src or [])),
        )
    updated, changes = update_baseline_snapshot(current, baseline)
    output = Path(args.output).resolve() if args.output else baseline_path
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(updated, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "status": "updated" if changes else "unchanged",
                "baseline": str(output),
                "changes": changes,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Lower code health baseline metrics when current values improve.")
    parser.add_argument("--project-root", default=str(REPO_ROOT))
    parser.add_argument("--baseline", default=str(REPO_ROOT / "planning" / "code_health_baseline.json"))
    parser.add_argument("--current", help="Optional current snapshot json path")
    parser.add_argument("--src", action="append", help="Target path(s) to scan when --current is omitted")
    parser.add_argument("--output", help="Optional output path; defaults to overwriting --baseline")
    return parser


if __name__ == "__main__":
    raise SystemExit(run(build_parser().parse_args()))
