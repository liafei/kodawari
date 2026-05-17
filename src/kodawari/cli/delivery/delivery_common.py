"""Shared helpers for delivery workflow commands."""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kodawari.cli.artifact_versions import ArtifactSchemaVersionError
from kodawari.cli.contract.contract_first_schema import load_contract_first_artifact
from kodawari.cli.io_atomic import atomic_write_json, atomic_write_text
from kodawari.cli.contract.planning_conversation_compat import (
    load_planning_conversation,
    load_prd_intake_compatible,
)
from kodawari.cli.evidence.review_evidence_artifact import (
    REVIEW_EVIDENCE_FILENAME,
    ReviewEvidenceSchemaValidationError,
    coerce_review_evidence_payload,
    extract_review_evidence_from_compliance_report,
    load_review_evidence_artifact,
)
from kodawari.cli.evidence.verify_report import VERIFY_REPORT_FILENAME, load_verify_report_artifact

LEGACY_PLANNING_ARTIFACTS = (
    "PLAN.md",
    "TASKS.md",
    "ACCEPTANCE.md",
    "GATE.md",
)
CONTRACT_FIRST_PLANNING_ARTIFACTS = (
    "PLANNING_CONVERSATION.json",
    "PRD_INTAKE.json",
    "TASK_GRAPH.json",
    "TASK_CARD_ACTIVE.json",
)
DELIVERY_ARTIFACTS = (
    "DESIGN.md",
    "REVIEW.md",
    "QA_REPORT.md",
)
DEFAULT_REPLAY_GATE_RESULT = "REPLAY_GATE_RESULT.json"
DEFAULT_CANARY_GATE_RESULT = "CANARY_GATE_RESULT.json"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def resolve_planning_dir(*, project_root: Path, feature: str | None, planning_dir: str | None) -> tuple[Path, str]:
    if planning_dir:
        resolved = Path(planning_dir).resolve()
        inferred_feature = str(feature or resolved.name).strip() or resolved.name
        return resolved, inferred_feature
    if not str(feature or "").strip():
        raise ValueError("feature is required when planning_dir is not provided")
    resolved = (project_root / "planning" / str(feature).strip()).resolve()
    return resolved, str(feature).strip()


def _contract_first_active(planning_dir: Path) -> bool:
    return any((planning_dir / name).exists() for name in CONTRACT_FIRST_PLANNING_ARTIFACTS)


def _planning_artifact_mode(planning_dir: Path) -> str:
    return "contract_first" if _contract_first_active(planning_dir) else "legacy"


def _required_planning_artifacts_status(planning_dir: Path, *, include_delivery_artifacts: bool = True) -> dict[str, Any]:
    mode = _planning_artifact_mode(planning_dir)
    if mode == "contract_first":
        # Keep delivery-mode compatibility: legacy contract-first still accepts
        # PRD_INTAKE/TASK_GRAPH/TASK_CARD, while model-driven mode requires
        # PLANNING_CONVERSATION + REPO_INVENTORY + graph/card.
        if load_planning_conversation(planning_dir) is not None:
            required = (
                "PLANNING_CONVERSATION.json",
                "REPO_INVENTORY.json",
                "TASK_GRAPH.json",
                "TASK_CARD_ACTIVE.json",
            )
        else:
            required = (
                "PRD_INTAKE.json",
                "TASK_GRAPH.json",
                "TASK_CARD_ACTIVE.json",
            )
    else:
        required = LEGACY_PLANNING_ARTIFACTS
    required_all = tuple(required) + (DELIVERY_ARTIFACTS if include_delivery_artifacts else ())
    present: list[str] = []
    missing: list[str] = []
    invalid: list[str] = []
    schema_names = {
        "PLANNING_CONVERSATION.json": "planning_conversation",
        "PRD_INTAKE.json": "prd_intake",
        "REPO_INVENTORY.json": "repo_inventory",
        "ARCHITECTURE_PLAN.json": "architecture_plan",
        "TASK_GRAPH.json": "task_graph",
        "TASK_CARD_ACTIVE.json": "task_card",
    }
    for name in required_all:
        path = planning_dir / name
        if not path.exists():
            missing.append(name)
            continue
        if mode == "contract_first" and name in schema_names:
            try:
                load_contract_first_artifact(path, schema_name=schema_names[name])
            except ValueError:
                invalid.append(name)
                continue
        present.append(name)
    return {
        "mode": mode,
        "required": list(required_all),
        "present": present,
        "missing": missing,
        "invalid": invalid,
        "all_present": not missing and not invalid,
    }


def _load_contract_task_card(planning_dir: Path) -> dict[str, Any] | None:
    path = planning_dir / "TASK_CARD_ACTIVE.json"
    if not path.exists():
        return None
    return load_contract_first_artifact(path, schema_name="task_card")


def _load_contract_task_graph(planning_dir: Path) -> dict[str, Any] | None:
    path = planning_dir / "TASK_GRAPH.json"
    if not path.exists():
        return None
    return load_contract_first_artifact(path, schema_name="task_graph")


def _load_contract_prd_intake(planning_dir: Path) -> dict[str, Any] | None:
    return load_prd_intake_compatible(planning_dir)


def _load_contract_compliance_report(planning_dir: Path) -> dict[str, Any] | None:
    path = planning_dir / "COMPLIANCE_REPORT.json"
    if not path.exists():
        return None
    return load_contract_first_artifact(path, schema_name="compliance_report")


def _contract_first_completion_summary(planning_dir: Path) -> dict[str, Any]:
    task_graph = _load_contract_task_graph(planning_dir) or {}
    task_card = _load_contract_task_card(planning_dir) or {}
    tasks = [item for item in list(task_graph.get("tasks") or []) if isinstance(item, dict)]
    invariants = [str(item) for item in list(task_card.get("invariants") or []) if str(item).strip()]
    test_plan = str(task_card.get("test_plan") or "").strip()
    planning_artifacts = _required_planning_artifacts_status(planning_dir, include_delivery_artifacts=False)
    artifacts_present = bool(planning_artifacts.get("all_present"))
    tasks_summary = {
        "total": len(tasks),
        "done": len(tasks) if artifacts_present else 0,
        "status": "CONTRACT_FIRST_READY" if artifacts_present else "CONTRACT_FIRST_MISSING",
    }
    acceptance_ready = bool(invariants and test_plan)
    acceptance_summary = {
        "total": 1 if task_card else 0,
        "done": 1 if acceptance_ready else 0,
        "status": "CONTRACT_FIRST_READY" if acceptance_ready else "CONTRACT_FIRST_MISSING",
    }
    complete = artifacts_present and acceptance_ready
    return {
        "status": "PASS" if complete else "WARN",
        "tasks": tasks_summary,
        "acceptance": acceptance_summary,
        "details": (
            "Contract-first planning artifacts present and directly consumable."
            if complete
            else "Contract-first planning artifacts are incomplete."
        ),
    }


def _contract_scope_hints(planning_dir: Path) -> tuple[list[str], str]:
    task_graph = _load_contract_task_graph(planning_dir) or {}
    graph_tasks = [item for item in list(task_graph.get("tasks") or []) if isinstance(item, dict)]
    union_files: list[str] = []
    for task in graph_tasks:
        for raw in list(task.get("core_files") or []):
            text = _normalize_relpath(str(raw))
            if text and text not in union_files:
                union_files.append(text)
    if union_files:
        return union_files, "TASK_GRAPH.json"
    task_card = _load_contract_task_card(planning_dir) or {}
    files = [str(item) for item in list(task_card.get("files_to_change") or []) if str(item).strip()]
    if files:
        return files, "TASK_CARD_ACTIVE.json"
    return [], ""


def _task_run_payload(planning_dir: Path) -> dict[str, Any] | None:
    return _load_json_dict(planning_dir / ".task_run_result.json")


def _review_result_payload(planning_dir: Path) -> dict[str, Any] | None:
    return _load_json_dict(planning_dir / ".review_result.json")


def _load_verify_report(planning_dir: Path) -> dict[str, Any] | None:
    path = planning_dir / VERIFY_REPORT_FILENAME
    if not path.exists():
        return None
    return load_verify_report_artifact(path)


def _load_review_evidence_artifact_payload(planning_dir: Path) -> dict[str, Any] | None:
    path = planning_dir / REVIEW_EVIDENCE_FILENAME
    if not path.exists():
        return None
    try:
        payload = load_review_evidence_artifact(path)
    except (ArtifactSchemaVersionError, ValueError, ReviewEvidenceSchemaValidationError) as exc:
        return coerce_review_evidence_payload(
            {
                "status": "FAIL",
                "blocking_reason": f"invalid canonical review evidence artifact: {exc}",
                "issues": [f"invalid canonical review evidence artifact: {exc}"],
                "checks": {
                    "self_review_count": 0,
                    "peer_review_count": 0,
                    "must_fix_remaining": 0,
                },
                "evidence": [
                    {
                        "file": REVIEW_EVIDENCE_FILENAME,
                        "rule": "review_evidence.invalid_artifact",
                        "hit": str(exc),
                        "confidence": 1.0,
                    }
                ],
            },
            source=REVIEW_EVIDENCE_FILENAME,
            explicit=True,
        )
    return coerce_review_evidence_payload(payload, source=REVIEW_EVIDENCE_FILENAME, explicit=True)


def _review_payload_candidate(review_payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(review_payload, dict):
        return None
    direct = review_payload.get("review_evidence")
    if not isinstance(direct, dict):
        return None
    nested = direct.get("review_evidence_payload")
    if isinstance(nested, dict):
        source = (
            str(
                direct.get("review_evidence_source")
                or review_payload.get("review_evidence_source")
                or ".review_result.json.review_evidence"
            ).strip()
            or ".review_result.json.review_evidence"
        )
        explicit = bool(
            direct.get("explicit_review_evidence")
            if "explicit_review_evidence" in direct
            else review_payload.get("explicit_review_evidence")
            if "explicit_review_evidence" in review_payload
            else source not in {"summary_fallback", "COMPLIANCE_REPORT.json.review_evidence", ""}
        )
        return coerce_review_evidence_payload(nested, source=source, explicit=explicit)
    if {"status", "checks", "blocking_reason", "issues"} & set(direct):
        return coerce_review_evidence_payload(direct, source=".review_result.json.review_evidence", explicit=True)
    return None


def _load_legacy_review_evidence(
    *,
    planning_dir: Path,
    review_payload: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    task_run = _task_run_payload(planning_dir) or {}
    direct = task_run.get("review_evidence")
    if isinstance(direct, dict):
        return coerce_review_evidence_payload(
            direct,
            source=".task_run_result.json.review_evidence",
            explicit=True,
        )
    provided = _review_payload_candidate(review_payload)
    if provided is not None and bool(provided.get("explicit")):
        return provided
    compliance_from_task_run = extract_review_evidence_from_compliance_report(
        task_run.get("compliance_report"),
        source=".task_run_result.json.compliance_report.review_evidence",
        explicit=False,
    )
    if compliance_from_task_run is not None:
        return compliance_from_task_run
    if provided is not None:
        return provided
    compliance = _load_contract_compliance_report(planning_dir)
    if compliance is not None:
        return extract_review_evidence_from_compliance_report(
            compliance,
            source="COMPLIANCE_REPORT.json.review_evidence",
            explicit=False,
        )
    return None


def _load_json_dict(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_json(path, payload)


def _payload_digest(payload: dict[str, Any]) -> str:
    canonical_payload = {
        key: value
        for key, value in payload.items()
        if key not in {"payload_digest", "digest_algorithm"}
    }
    encoded = json.dumps(
        canonical_payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _attach_payload_digest(payload: dict[str, Any]) -> dict[str, Any]:
    payload["digest_algorithm"] = "sha256"
    payload["payload_digest"] = _payload_digest(payload)
    return payload


def _normalize_relpath(value: str) -> str:
    normalized = str(value or "").strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _git_diff_files(*, project_root: Path, base_branch: str) -> list[str]:
    command = [
        "git",
        "-C",
        str(project_root),
        "diff",
        "--name-only",
        "--diff-filter=ACMR",
        f"{base_branch}...HEAD",
    ]
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True, encoding="utf-8", errors="replace")
    except (FileNotFoundError, subprocess.CalledProcessError):
        return []
    files: list[str] = []
    for line in result.stdout.splitlines():
        normalized = _normalize_relpath(line)
        if normalized:
            files.append(normalized)
    return sorted(dict.fromkeys(files))


def _ensure_design_artifact(
    *,
    planning_dir: Path,
    feature: str,
    state_payload: dict[str, Any] | None,
    policy: Any | None = None,
) -> None:
    from kodawari.autopilot.engine.workflow_policy import should_emit_artifact

    if not should_emit_artifact("DESIGN.md", policy):
        return
    path = planning_dir / "DESIGN.md"
    if path.exists():
        return
    decisions = list((state_payload or {}).get("architecture_decisions") or [])
    lines = [
        f"# DESIGN ({feature})",
        "",
        "## Source",
        "- generated_by: kodawari delivery workflow",
        "",
        "## Architecture Decisions",
    ]
    if not decisions:
        lines.append("- (none recorded)")
    else:
        for item in decisions:
            payload = dict(item) if isinstance(item, dict) else {}
            decision = str(payload.get("decision") or "").strip()
            rationale = str(payload.get("rationale") or "").strip()
            if decision:
                lines.append(f"- {decision}")
                if rationale:
                    lines.append(f"  rationale: {rationale}")
    atomic_write_text(path, "\n".join(lines) + "\n")


def _ensure_placeholder_markdown(
    path: Path,
    *,
    title: str,
    policy: Any | None = None,
) -> None:
    from kodawari.autopilot.engine.workflow_policy import should_emit_artifact

    if not should_emit_artifact(path.name, policy):
        return
    if path.exists():
        return
    atomic_write_text(
        path,
        "\n".join(
            [
                f"# {title}",
                "",
                "- status: pending_generation",
                "- note: generated as placeholder by ship-readiness command.",
            ]
        )
        + "\n",
    )


__all__ = [
    "CONTRACT_FIRST_PLANNING_ARTIFACTS",
    "DEFAULT_CANARY_GATE_RESULT",
    "DEFAULT_REPLAY_GATE_RESULT",
    "DELIVERY_ARTIFACTS",
    "LEGACY_PLANNING_ARTIFACTS",
    "REVIEW_EVIDENCE_FILENAME",
    "VERIFY_REPORT_FILENAME",
    "_contract_first_active",
    "_contract_first_completion_summary",
    "_contract_scope_hints",
    "_ensure_design_artifact",
    "_ensure_placeholder_markdown",
    "_git_diff_files",
    "_load_contract_compliance_report",
    "_load_contract_prd_intake",
    "_load_contract_task_card",
    "_load_contract_task_graph",
    "_load_json_dict",
    "_load_legacy_review_evidence",
    "_load_review_evidence_artifact_payload",
    "_load_verify_report",
    "_normalize_relpath",
    "_planning_artifact_mode",
    "_required_planning_artifacts_status",
    "_review_result_payload",
    "_task_run_payload",
    "_attach_payload_digest",
    "_utc_now_iso",
    "_write_json",
    "resolve_planning_dir",
]


