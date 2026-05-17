"""Artifact migration command for versioned machine artifacts."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kodawari.cli.artifact_versions import migrate_payload_for_path
from kodawari.cli.contract.command_contract import build_error_payload, normalize_mutating_payload
from kodawari.cli.io_atomic import atomic_write_json, atomic_write_text, load_json_dict
from kodawari.cli.provenance import build_cli_provenance


FIELD_REPORT_HISTORY_SCHEMA_VERSION = "field.report.v1"
ROOT_LEVEL_ARTIFACTS = (
    "AUTOMATION_EVAL_REPORT.json",
    "AUTOMATION_EVAL_INPUT_LOCK.json",
)
PLANNING_LEVEL_ARTIFACTS = (
    ".autopilot_state.json",
    ".telemetry_snapshot.json",
    ".field_report.json",
    ".review_evidence.json",
    ".verify_report.json",
    ".execution_request.json",
    ".execution_result.json",
    ".review_bundle.json",
    ".worktree_baseline.json",
    "PRD_INTAKE.json",
    "TASK_GRAPH.json",
    "TASK_CARD_ACTIVE.json",
    "COMPLIANCE_REPORT.json",
)


def _provenance(*, command: str, project_root: Path, resolved_planning_dirs: list[Path]) -> dict[str, Any]:
    return build_cli_provenance(
        command=command,
        project_root=project_root,
        planning_dir=None,
        resolved_planning_dirs=resolved_planning_dirs,
        module_file=Path(__file__),
    )


def _timestamp_slug() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _resolve_planning_dirs(args: argparse.Namespace, project_root: Path) -> list[Path]:
    resolved: list[Path] = []
    seen: set[str] = set()

    def add(path: Path) -> None:
        key = str(path.resolve()).lower()
        if key in seen:
            return
        seen.add(key)
        resolved.append(path.resolve())

    feature = str(getattr(args, "feature", "") or "").strip()
    if feature:
        add((project_root / "planning" / feature).resolve())
    for run_id in list(getattr(args, "run_id", []) or []):
        add((project_root / "planning" / str(run_id).strip()).resolve())
    for raw in list(getattr(args, "planning_dir", []) or []):
        add(Path(raw).resolve())
    if bool(getattr(args, "all_runs", False)):
        planning_root = (project_root / "planning").resolve()
        if planning_root.exists():
            for candidate in sorted(item for item in planning_root.iterdir() if item.is_dir()):
                add(candidate)
    return [path for path in resolved if path.exists()]


def _candidate_paths(project_root: Path, planning_dirs: list[Path]) -> list[Path]:
    candidates: list[Path] = []
    for name in ROOT_LEVEL_ARTIFACTS:
        path = (project_root / name).resolve()
        if path.exists():
            candidates.append(path)
    for planning_dir in planning_dirs:
        for name in PLANNING_LEVEL_ARTIFACTS:
            path = (planning_dir / name).resolve()
            if path.exists():
                candidates.append(path)
        history_path = (planning_dir / ".field_reports.jsonl").resolve()
        if history_path.exists():
            candidates.append(history_path)
        for task_card in sorted(planning_dir.glob("TASK_CARD_*.json")):
            candidates.append(task_card.resolve())
    return candidates


def _backup_file(path: Path, backup_root: Path) -> Path:
    backup_root.mkdir(parents=True, exist_ok=True)
    target = backup_root / path.name
    target.write_bytes(path.read_bytes())
    return target


def _migrate_field_report_history(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"changed": False, "rows_total": 0, "rows_changed": 0, "content": ""}
    rows_changed = 0
    rows_total = 0
    rendered: list[str] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line:
            continue
        rows_total += 1
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            rendered.append(line)
            continue
        if not isinstance(payload, dict):
            rendered.append(line)
            continue
        if not str(payload.get("schema_version") or "").strip():
            payload["schema_version"] = FIELD_REPORT_HISTORY_SCHEMA_VERSION
            rows_changed += 1
        rendered.append(json.dumps(payload, ensure_ascii=False))
    return {
        "changed": rows_changed > 0,
        "rows_total": rows_total,
        "rows_changed": rows_changed,
        "content": ("\n".join(rendered) + "\n") if rendered else "",
    }


def run_migrate_artifacts_command(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root).resolve()
    planning_dirs = _resolve_planning_dirs(args, project_root)
    candidates = _candidate_paths(project_root, planning_dirs)
    backup_root: Path | None = None
    records: list[dict[str, Any]] = []

    try:
        if bool(getattr(args, "write", False)):
            backup_root = (project_root / ".artifact_backups" / _timestamp_slug()).resolve()
        for path in candidates:
            if path.name == ".field_reports.jsonl":
                migration = _migrate_field_report_history(path)
                record = {
                    "path": str(path),
                    "artifact_kind": "field_report_history",
                    "from_version": "mixed",
                    "to_version": FIELD_REPORT_HISTORY_SCHEMA_VERSION,
                    "changed": bool(migration["changed"]),
                    "rows_total": int(migration["rows_total"]),
                    "rows_changed": int(migration["rows_changed"]),
                }
                if bool(getattr(args, "write", False)) and migration["changed"]:
                    assert backup_root is not None
                    backup_path = _backup_file(path, backup_root)
                    atomic_write_text(path, str(migration["content"]))
                    record["backup_path"] = str(backup_path)
                records.append(record)
                continue

            payload = load_json_dict(path, required=True)
            if payload is None:
                continue
            migration = migrate_payload_for_path(path, payload)
            if migration is None:
                continue
            record = {
                "path": str(path),
                "artifact_kind": migration.artifact_kind,
                "from_version": migration.from_version,
                "to_version": migration.to_version,
                "changed": migration.changed,
            }
            if bool(getattr(args, "write", False)) and migration.changed:
                assert backup_root is not None
                backup_path = _backup_file(path, backup_root)
                atomic_write_json(path, migration.payload)
                record["backup_path"] = str(backup_path)
            records.append(record)

        changed_records = [item for item in records if bool(item.get("changed"))]
        payload = {
            "status": "PASS",
            "entrypoint": "kodawari migrate-artifacts",
            "project_root": str(project_root),
            "write_mode": bool(getattr(args, "write", False)),
            "planning_dirs": [str(path) for path in planning_dirs],
            "artifacts_scanned": len(records),
            "artifacts_changed": len(changed_records),
            "records": records,
            "backup_root": str(backup_root) if backup_root is not None else None,
            "provenance": _provenance(
                command="migrate-artifacts",
                project_root=project_root,
                resolved_planning_dirs=planning_dirs,
            ),
        }
        if changed_records and not bool(getattr(args, "write", False)):
            payload["next_action"] = "Rerun with --write to apply the pending migrations."
            payload["remediation"] = ["Dry-run detected migratable artifacts that still need schema_version upgrades."]
        normalized_payload = normalize_mutating_payload(payload)
        print(json.dumps(normalized_payload, ensure_ascii=False, indent=2))
        return 0
    except ValueError as exc:
        payload = build_error_payload(
            command="migrate-artifacts",
            project_root=project_root,
            planning_dir=None,
            module_file=Path(__file__),
            error=str(exc),
            error_code="artifact_migration_failed",
            remediation=["Inspect the failing artifact and rerun migrate-artifacts."],
            resolved_planning_dirs=planning_dirs,
        )
        print(json.dumps(normalize_mutating_payload(payload), ensure_ascii=False, indent=2))
        return 2

