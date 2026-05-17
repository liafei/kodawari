"""CLI command for writing canonical dual-review evidence artifacts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from kodawari.cli.artifact_versions import ArtifactSchemaVersionError
from kodawari.cli.contract.command_contract import build_error_payload, normalize_mutating_payload
from kodawari.cli.io_atomic import CorruptArtifactError, load_json_dict
from kodawari.cli.provenance import build_cli_provenance
from kodawari.cli.evidence.review_evidence_artifact import (
    REVIEW_EVIDENCE_FILENAME,
    ReviewEvidenceSchemaValidationError,
    build_review_evidence_artifact,
    write_review_evidence_artifact,
)


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


def run_review_evidence_command(args: argparse.Namespace) -> int:
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
        input_path = Path(str(getattr(args, "input"))).resolve()
        input_payload = load_json_dict(input_path, required=True, quarantine_on_error=True)
        if not isinstance(input_payload, dict):
            raise ValueError(f"review evidence input must be an object JSON payload: {input_path}")
        artifact = build_review_evidence_artifact(
            feature=feature,
            planning_dir=planning_dir,
            review_evidence=input_payload,
            entrypoint="kodawari review-evidence",
        )
        artifact_path = planning_dir / REVIEW_EVIDENCE_FILENAME
        write_review_evidence_artifact(artifact_path, artifact)
        return _emit(
            {
                "_rc": 0,
                "status": "PASS",
                "entrypoint": "kodawari review-evidence",
                "feature": feature,
                "planning_dir": str(planning_dir),
                "artifacts": {REVIEW_EVIDENCE_FILENAME: str(artifact_path.resolve())},
                "review_evidence_status": str(artifact.get("status") or "UNKNOWN").upper(),
                "remediation": [],
                "next_action": "",
                "provenance": build_cli_provenance(
                    command="review-evidence",
                    project_root=project_root,
                    planning_dir=planning_dir,
                    module_file=Path(__file__),
                ),
            }
        )
    except (
        ArtifactSchemaVersionError,
        ReviewEvidenceSchemaValidationError,
        CorruptArtifactError,
        FileNotFoundError,
        ValueError,
    ) as exc:
        error_code = "review_evidence_failed"
        validation_errors: list[dict[str, str]] = []
        remediation = [
            "Provide a canonical review-evidence JSON payload with status/checks/issues/evidence fields.",
            "Rerun `kodawari review-evidence --project-root <root> --feature <feature> --input REVIEW_EVIDENCE_INPUT.json` after fixing the input.",
        ]
        if isinstance(exc, ArtifactSchemaVersionError):
            error_code = "artifact_schema_version_invalid"
        elif isinstance(exc, ReviewEvidenceSchemaValidationError):
            error_code = "artifact_schema_invalid"
            validation_errors = list(exc.errors)
        elif isinstance(exc, CorruptArtifactError):
            error_code = "artifact_corrupt"
            if exc.quarantine_path is not None:
                remediation.append(f"Quarantined copy: {exc.quarantine_path}")
        payload = build_error_payload(
            command="review-evidence",
            project_root=project_root,
            planning_dir=planning_dir,
            module_file=Path(__file__),
            error=str(exc),
            error_code=error_code,
            remediation=remediation,
            next_action="Fix the review-evidence payload, then rerun `kodawari review-evidence`.",
            extra={
                "_rc": 2,
                "status": "FAIL",
                "entrypoint": "kodawari review-evidence",
                "feature": feature,
                "planning_dir": str(planning_dir) if planning_dir is not None else "",
                **({"validation_errors": validation_errors} if validation_errors else {}),
            },
        )
        return _emit(payload)

