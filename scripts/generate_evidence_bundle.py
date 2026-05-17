#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from kodawari.autopilot.execution_backend import (  # noqa: E402
    execution_backend_capabilities,
    execution_backend_capability_truth,
)

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        text = path.read_text(encoding="utf-8-sig", errors="ignore")
    if text.startswith("\ufeff"):
        text = text.lstrip("\ufeff")
    payload = json.loads(text)
    return payload if isinstance(payload, dict) else None


def _relpath(path: Path, project_root: Path) -> str:
    try:
        return str(path.resolve().relative_to(project_root.resolve())).replace("\\", "/")
    except ValueError:
        return str(path.resolve())


def _git_head(project_root: Path) -> str:
    try:
        run = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            check=False,
        )
        if run.returncode == 0:
            text = run.stdout.strip()
            if text:
                return text
    except OSError:
        pass
    return "UNKNOWN"


@dataclass(frozen=True)
class EvidenceRow:
    filename: str
    title: str
    command: str
    input_artifacts: list[str]
    verdict: str
    summary: str


def _lane_status(planning_dir: Path, lane: str) -> tuple[str, Path]:
    path = planning_dir / f"lane_stability_{lane}.json"
    payload = _read_json(path)
    if not payload:
        return "MISSING", path
    return str(payload.get("status") or "UNKNOWN").upper(), path


def _gate_status(project_root: Path) -> tuple[str, Path]:
    path = project_root / "planning" / "ci_repo_health_src" / ".gate_result.json"
    payload = _read_json(path)
    if not payload:
        return "MISSING", path
    return str(payload.get("total_status") or payload.get("status") or "UNKNOWN").upper(), path


def _has_blocked_state(project_root: Path) -> tuple[bool, list[Path]]:
    matches: list[Path] = []
    for path in (project_root / "planning").rglob(".autopilot_state.json"):
        payload = _read_json(path)
        if not payload:
            continue
        normalized = json.dumps(payload, ensure_ascii=False).lower()
        if "blocked" in normalized:
            matches.append(path)
    return bool(matches), matches


def _backend_capability_evidence(project_root: Path) -> tuple[str, list[str], list[str]]:
    backends = ["codex_cli", "claude_code"]
    required_capabilities = (
        "implemented",
        "supports_deterministic_changed_files",
        "supports_agent_teams",
        "supports_worktree_isolation",
        "supports_hooks",
        "supports_memory",
    )
    lines: list[str] = []
    verdict = "PASS"
    for backend in backends:
        capabilities = execution_backend_capabilities(backend)
        truth = execution_backend_capability_truth(backend)
        backend_ok = True
        for field in required_capabilities:
            if field not in truth:
                backend_ok = False
                break
            if bool(capabilities.get(field)) != bool(dict(truth.get(field) or {}).get("descriptor_value")):
                backend_ok = False
                break
        state = "PASS" if backend_ok else "FAIL"
        lines.append(f"{backend}={state}")
        if not backend_ok:
            verdict = "FAIL"
    artifacts = [
        "src/kodawari/autopilot/execution_backend.py",
        "tests/test_execution_backend_capability_honesty.py",
    ]
    return verdict, artifacts, lines


def _build_rows(project_root: Path, planning_dir: Path) -> list[EvidenceRow]:
    always_status, always_path = _lane_status(planning_dir, "always-on")
    integration_status, integration_path = _lane_status(planning_dir, "integration")
    lane_verdict = (
        "PASS"
        if always_status == "PASS" and integration_status == "PASS"
        else ("MISSING" if "MISSING" in {always_status, integration_status} else "BLOCKED")
    )
    gate_verdict, gate_path = _gate_status(project_root)

    blocked_found, blocked_paths = _has_blocked_state(project_root)
    blocked_verdict = "PASS" if blocked_found else "MISSING"

    backend_capability_verdict, backend_capability_artifacts, backend_capability_lines = _backend_capability_evidence(
        project_root
    )

    happy_verdict = "PASS" if always_status == "PASS" else ("MISSING" if always_status == "MISSING" else "BLOCKED")
    happy_summary = f"always-on lane status={always_status}"

    rows = [
        EvidenceRow(
            filename="happy-path.md",
            title="Happy Path",
            command="powershell -ExecutionPolicy Bypass -File .\\scripts\\run_always_on_lane.ps1 -Repeat 2",
            input_artifacts=[_relpath(always_path, project_root)] if always_path.exists() else [],
            verdict=happy_verdict,
            summary=happy_summary,
        ),
        EvidenceRow(
            filename="blocked-recovery.md",
            title="Blocked Recovery",
            command="powershell -ExecutionPolicy Bypass -File .\\scripts\\kodawari.ps1 work-all --project-root . --feature <feature>",
            input_artifacts=[_relpath(path, project_root) for path in blocked_paths],
            verdict=blocked_verdict,
            summary="blocked autopilot states detected" if blocked_found else "no blocked/recovery state artifact found",
        ),
        EvidenceRow(
            filename="backend-capabilities.md",
            title="Backend Capabilities",
            command="python -m pytest -q tests/test_execution_backend_capability_honesty.py",
            input_artifacts=backend_capability_artifacts,
            verdict=backend_capability_verdict,
            summary="; ".join(backend_capability_lines),
        ),
        EvidenceRow(
            filename="lane-stability.md",
            title="Lane Stability",
            command="powershell -ExecutionPolicy Bypass -File .\\scripts\\run_lane_stability.ps1 -Lane always-on -Repeat 2",
            input_artifacts=[
                _relpath(always_path, project_root) if always_path.exists() else "",
                _relpath(integration_path, project_root) if integration_path.exists() else "",
            ],
            verdict=lane_verdict,
            summary=f"always-on={always_status}, integration={integration_status}",
        ),
        EvidenceRow(
            filename="gate-enforcement.md",
            title="Gate Enforcement",
            command="powershell -ExecutionPolicy Bypass -File .\\scripts\\kodawari.ps1 gate --project-root . --path .\\src --profile strict --fail-on-block",
            input_artifacts=[_relpath(gate_path, project_root)] if gate_path.exists() else [],
            verdict=gate_verdict if gate_verdict in {"PASS", "FAIL", "BLOCKED"} else "MISSING",
            summary=f"strict gate status={gate_verdict}",
        ),
    ]
    return rows


def _render_markdown(row: EvidenceRow, *, timestamp: str, source_commit: str) -> str:
    artifacts = row.input_artifacts or ["(none)"]
    lines = [
        f"# Evidence: {row.title}",
        "",
        f"- command: `{row.command}`",
        "- input_artifacts:",
    ]
    lines.extend([f"  - `{item}`" for item in artifacts])
    lines.extend(
        [
            f"- verdict: `{row.verdict}`",
            f"- timestamp: `{timestamp}`",
            f"- source_commit: `{source_commit}`",
            f"- summary: {row.summary}",
            "",
            "## Notes",
            "- This file is auto-generated by `scripts/generate_evidence_bundle.py`.",
            "- JSON truth and lane/gate artifacts remain the authoritative source.",
            "",
        ]
    )
    return "\n".join(lines)


def run(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root).resolve()
    planning_dir = Path(args.planning_dir).resolve()
    output_dir = Path(args.output_dir).resolve()
    source_commit = str(args.source_commit or "").strip() or _git_head(project_root)
    timestamp = str(args.timestamp_utc or "").strip() or _utc_now_iso()

    rows = _build_rows(project_root=project_root, planning_dir=planning_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    manifest_rows: list[dict[str, Any]] = []
    for row in rows:
        body = _render_markdown(row, timestamp=timestamp, source_commit=source_commit)
        target = output_dir / row.filename
        target.write_text(body, encoding="utf-8")
        manifest_rows.append(
            {
                "file": row.filename,
                "command": row.command,
                "input_artifacts": row.input_artifacts,
                "verdict": row.verdict,
                "timestamp": timestamp,
                "source_commit": source_commit,
                "summary": row.summary,
            }
        )

    manifest = {
        "schema_version": "evidence.bundle.v1",
        "generated_at_utc": timestamp,
        "source_commit": source_commit,
        "project_root": str(project_root),
        "evidence_total": len(manifest_rows),
        "items": manifest_rows,
    }
    manifest_path = output_dir / "evidence_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": "PASS", "output_dir": str(output_dir), "manifest": str(manifest_path)}, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate five evidence markdown packs from existing workflow artifacts.")
    parser.add_argument("--project-root", default=".", help="Kodawari project root path.")
    parser.add_argument("--planning-dir", default="planning", help="Planning artifact directory.")
    parser.add_argument("--output-dir", default="evidence", help="Output evidence directory.")
    parser.add_argument("--source-commit", default="", help="Optional source commit SHA override.")
    parser.add_argument("--timestamp-utc", default="", help="Optional UTC timestamp override.")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
