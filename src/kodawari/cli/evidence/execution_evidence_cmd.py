"""CLI command for writing canonical manual execution artifacts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from kodawari.autopilot.execution.execution_artifacts import (
    EXECUTION_RESULT_FILENAME,
    ExecutionArtifactError,
    MANUAL_BACKEND,
    build_execution_result,
    write_execution_result,
)
from kodawari.cli.artifact_versions import ArtifactSchemaVersionError
from kodawari.cli.contract.command_contract import build_error_payload, normalize_mutating_payload
from kodawari.cli.delivery.delivery_common import (
    _load_contract_task_card,
    _load_verify_report,
    _normalize_relpath,
)
from kodawari.cli.io_atomic import CorruptArtifactError, load_json_dict
from kodawari.cli.provenance import build_cli_provenance
from kodawari.cli.evidence.verify_report import VerifyReportSchemaValidationError


def _emit(payload: dict[str, Any]) -> int:
    normalized = normalize_mutating_payload(payload)
    print(json.dumps(normalized, ensure_ascii=False, indent=2))
    return int(normalized.get("_rc", 0) or 0)


def _resolve_planning_dir(project_root: Path, feature: str | None, planning_dir: str | None) -> tuple[Path, str]:
    if str(planning_dir or "").strip():
        resolved = Path(str(planning_dir)).resolve()
        inferred_feature = str(feature or resolved.name).strip() or resolved.name
        return resolved, inferred_feature
    if not str(feature or "").strip():
        raise ValueError("feature is required when planning_dir is not provided")
    resolved = (project_root / "planning" / str(feature).strip()).resolve()
    return resolved, str(feature).strip()


def _normalize_changed_files(values: list[str] | None) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in list(values or []):
        item = _normalize_relpath(str(raw))
        if not item:
            continue
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(item)
    return normalized


def _same_changed_files(left: list[str], right: list[str]) -> bool:
    return {item.lower() for item in left} == {item.lower() for item in right}


def _review_changed_files(planning_dir: Path) -> list[str]:
    payload = load_json_dict(planning_dir / ".review_result.json", required=False, quarantine_on_error=True)
    if not isinstance(payload, dict):
        return []
    changed = dict(payload.get("changed_files") or {})
    return _normalize_changed_files(list(changed.get("items") or []))


def _verify_changed_files(planning_dir: Path) -> list[str]:
    payload = _load_verify_report(planning_dir)
    if not isinstance(payload, dict):
        return []
    changed = dict(payload.get("changed_files") or {})
    return _normalize_changed_files(list(changed.get("items") or []))


def _read_optional_text(project_root: Path, value: str | None) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    path = Path(text)
    if not path.is_absolute():
        path = (project_root / path).resolve()
    else:
        path = path.resolve()
    return path.read_text(encoding="utf-8")


def _normalize_artifacts(project_root: Path, values: list[str] | None) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    root = project_root.resolve()
    for raw in list(values or []):
        text = str(raw or "").strip()
        if not text:
            continue
        candidate = Path(text)
        if candidate.is_absolute():
            resolved = candidate.resolve()
        else:
            resolved = (root / candidate).resolve()
        try:
            item = str(resolved.relative_to(root)).replace("\\", "/")
        except ValueError:
            item = str(resolved)
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(item)
    return normalized


def _task_label(planning_dir: Path, feature: str) -> str:
    task_card = _load_contract_task_card(planning_dir) or {}
    task_id = str(task_card.get("task_id") or "").strip()
    task_name = str(task_card.get("task_name") or "").strip()
    if task_id and task_name:
        return f"{task_id}: {task_name}"
    if task_id:
        return task_id
    return feature


def _consistency_mismatch_payload(
    *,
    command: str,
    project_root: Path,
    planning_dir: Path,
    feature: str,
    changed_files: list[str],
    compared_name: str,
    compared_source: str,
    compared_files: list[str],
) -> dict[str, Any]:
    details = (
        f"execution changed files {changed_files} do not match {compared_source} changed files {compared_files}"
    )
    return build_error_payload(
        command=command,
        project_root=project_root,
        planning_dir=planning_dir,
        module_file=Path(__file__),
        error=details,
        error_code=compared_name,
        blocking_reason=details,
        remediation=[
            "Regenerate execution evidence with the same changed files truth used by review and verify.",
            f"Execution changed files: {changed_files}",
            f"{compared_source} changed files: {compared_files}",
        ],
        next_action="Align execution/review/verify changed files, then rerun `kodawari execution-evidence`.",
        extra={
            "_rc": 2,
            "status": "BLOCKED",
            "entrypoint": "kodawari execution-evidence",
            "feature": feature,
            "planning_dir": str(planning_dir),
            "changed_files": changed_files,
            "compared_source": compared_source,
            "compared_files": compared_files,
        },
    )


def run_execution_evidence_command(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root).resolve()
    planning_dir: Path | None = None
    feature = str(getattr(args, "feature", None) or "").strip()
    try:
        planning_dir, feature = _resolve_planning_dir(
            project_root=project_root,
            feature=getattr(args, "feature", None),
            planning_dir=getattr(args, "planning_dir", None),
        )
        planning_dir.mkdir(parents=True, exist_ok=True)
        backend = str(getattr(args, "backend", "") or "").strip().lower()
        if backend != MANUAL_BACKEND:
            raise ValueError("execution-evidence only supports --backend manual")
        changed_files = _normalize_changed_files(list(getattr(args, "changed_file", []) or []))
        if not changed_files:
            raise ValueError("execution-evidence requires at least one --changed-file")

        review_changed = _review_changed_files(planning_dir)
        if review_changed and not _same_changed_files(changed_files, review_changed):
            return _emit(
                _consistency_mismatch_payload(
                    command="execution-evidence",
                    project_root=project_root,
                    planning_dir=planning_dir,
                    feature=feature,
                    changed_files=changed_files,
                    compared_name="execution_vs_review_changed_files",
                    compared_source=".review_result.json",
                    compared_files=review_changed,
                )
            )

        verify_changed = _verify_changed_files(planning_dir)
        if verify_changed and not _same_changed_files(changed_files, verify_changed):
            return _emit(
                _consistency_mismatch_payload(
                    command="execution-evidence",
                    project_root=project_root,
                    planning_dir=planning_dir,
                    feature=feature,
                    changed_files=changed_files,
                    compared_name="execution_vs_verify_changed_files",
                    compared_source=".verify_report.json",
                    compared_files=verify_changed,
                )
            )

        status = str(getattr(args, "status", "PASS") or "PASS").strip().upper()
        returncode = getattr(args, "returncode", None)
        if returncode is None and status == "PASS":
            returncode = 0
        stdout_excerpt = _read_optional_text(project_root, getattr(args, "stdout_file", None))
        stderr_excerpt = _read_optional_text(project_root, getattr(args, "stderr_file", None))
        summary = str(getattr(args, "summary", "") or "").strip()
        if not summary:
            summary = "manual execution evidence recorded" if status == "PASS" else "manual execution evidence recorded as non-pass"
        error_code = ""
        blocking_reason = ""
        if status == "BLOCKED":
            error_code = "MANUAL_EXECUTION_BLOCKED"
            blocking_reason = summary
        elif status == "FAIL":
            error_code = "MANUAL_EXECUTION_FAILED"
            blocking_reason = summary
        artifact = build_execution_result(
            feature=feature,
            task=_task_label(planning_dir, feature),
            backend=backend,
            status=status,
            changed_files=changed_files,
            stdout_excerpt=stdout_excerpt,
            stderr_excerpt=stderr_excerpt,
            returncode=returncode,
            artifacts=_normalize_artifacts(project_root, list(getattr(args, "artifact", []) or [])),
            error_code=error_code,
            blocking_reason=blocking_reason,
            summary=summary,
        )
        artifact_path = planning_dir / EXECUTION_RESULT_FILENAME
        write_execution_result(artifact_path, artifact)
        return _emit(
            {
                "_rc": 0,
                "status": "PASS",
                "entrypoint": "kodawari execution-evidence",
                "feature": feature,
                "planning_dir": str(planning_dir),
                "execution_status": str(artifact.get("status") or "").upper(),
                "changed_files": list(artifact.get("changed_files") or []),
                "artifacts": {EXECUTION_RESULT_FILENAME: str(artifact_path.resolve())},
                "remediation": [],
                "next_action": "",
                "provenance": build_cli_provenance(
                    command="execution-evidence",
                    project_root=project_root,
                    planning_dir=planning_dir,
                    module_file=Path(__file__),
                ),
            }
        )
    except (
        ArtifactSchemaVersionError,
        CorruptArtifactError,
        ExecutionArtifactError,
        VerifyReportSchemaValidationError,
        FileNotFoundError,
        ValueError,
    ) as exc:
        error_code = "execution_evidence_failed"
        remediation = [
            "Provide manual execution evidence with --backend manual and at least one --changed-file.",
            "Rerun `kodawari execution-evidence --project-root <root> --feature <feature> --backend manual --changed-file <path>` after fixing the input.",
        ]
        if isinstance(exc, ArtifactSchemaVersionError):
            error_code = "artifact_schema_version_invalid"
        elif isinstance(exc, CorruptArtifactError):
            error_code = "artifact_corrupt"
            if exc.quarantine_path is not None:
                remediation.append(f"Quarantined copy: {exc.quarantine_path}")
        elif isinstance(exc, VerifyReportSchemaValidationError):
            error_code = "artifact_schema_invalid"
        payload = build_error_payload(
            command="execution-evidence",
            project_root=project_root,
            planning_dir=planning_dir,
            module_file=Path(__file__),
            error=str(exc),
            error_code=error_code,
            remediation=remediation,
            next_action="Fix the execution evidence input, then rerun `kodawari execution-evidence`.",
            extra={
                "_rc": 2,
                "status": "FAIL",
                "entrypoint": "kodawari execution-evidence",
                "feature": feature,
                "planning_dir": str(planning_dir) if planning_dir is not None else "",
            },
        )
        return _emit(payload)


