"""Semantic gate checks for PRD/task/runtime consistency."""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

from kodawari.autopilot.prd_contract import prd_coverage_check
from kodawari.gate.ast_checker import check_cache_consistency_ast

_CONTRACT_FIELD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def check_prd_coverage(tasks: list[dict[str, Any]], prd_intake: dict[str, Any]) -> dict[str, Any]:
    return prd_coverage_check(tasks=tasks, prd_intake=prd_intake)


def _has_valid_invariants(invariants: Any) -> bool:
    return isinstance(invariants, list) and any(str(x).strip() for x in invariants)


def check_invariant_proof(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    missing_invariants: list[str] = []
    missing_proof: list[str] = []
    for item in tasks:
        if not isinstance(item, dict):
            continue
        task_id = str(item.get("task_id") or "UNKNOWN")
        if not _has_valid_invariants(item.get("invariants")):
            missing_invariants.append(task_id)
        proof = str(item.get("test_proof") or "").strip()
        if not proof:
            missing_proof.append(task_id)
    if missing_invariants or missing_proof:
        return {
            "status": "FAIL",
            "missing_invariants": missing_invariants,
            "missing_test_proof": missing_proof,
            "details": "Some tasks are missing invariants or test_proof.",
        }
    return {
        "status": "PASS",
        "missing_invariants": [],
        "missing_test_proof": [],
        "details": "All tasks include invariants and test_proof.",
    }


def _sorted_file_set(ast_payload: dict[str, Any], key: str) -> list[str]:
    return sorted(set(str(item) for item in ast_payload.get(key, []) if str(item).strip()))


def check_cache_consistency(changed_files: list[str], project_root: Path) -> dict[str, Any]:
    ast_payload = check_cache_consistency_ast(changed_files, project_root)
    fail_files = _sorted_file_set(ast_payload, "fail_files")
    warn_files = _sorted_file_set(ast_payload, "warn_files")
    return {
        "status": str(ast_payload.get("status") or "WARN"),
        "mode": "ast_association_v2",
        "suspicious_files": sorted(set(fail_files + warn_files)),
        "fail_files": fail_files,
        "warn_files": warn_files,
        "pass_files": _sorted_file_set(ast_payload, "pass_files"),
        "analysis": list(ast_payload.get("analysis") or []),
        "profile_behavior": {"PASS": "no risk", "WARN": "ambiguous risk", "FAIL": "high-confidence risk"},
        "details": str(ast_payload.get("details") or ""),
        "evidence": list(ast_payload.get("evidence") or []),
    }


def _load_schema_document(path: Path) -> tuple[dict[str, Any], str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except UnicodeDecodeError:
        return {}, "schema file is not utf-8 decodable"
    except json.JSONDecodeError:
        return {}, "schema file is not valid JSON"
    except OSError:
        return {}, "schema file cannot be read"
    if not isinstance(payload, dict):
        return {}, "schema root is not an object"
    return payload, ""


def _normalize_schema_shape(definition: Any, *, required: bool) -> dict[str, Any]:
    payload = definition if isinstance(definition, dict) else {}
    enum_values = payload.get("enum")
    normalized_enum = (
        sorted(str(item) for item in enum_values if str(item).strip()) if isinstance(enum_values, list) else []
    )
    return {
        "type": payload.get("type"),
        "required": bool(required),
        "enum": normalized_enum,
        "description": str(payload.get("description") or "").strip(),
    }


def _collect_field_records(
    schema_files: list[str],
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]], list[dict[str, Any]]]:
    field_records: dict[str, list[dict[str, Any]]] = {}
    parse_issues: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    for raw in schema_files:
        path = Path(raw).resolve()
        document, issue = _load_schema_document(path)
        if issue:
            parse_issues.append({"file": str(path), "issue": issue})
            evidence.append({"file": str(path), "rule": "runtime_contract_scatter.schema_parse", "hit": issue, "confidence": 0.35})
            continue
        required_fields = {str(item).strip() for item in list(document.get("required") or []) if str(item).strip()}
        properties = document.get("properties")
        if not isinstance(properties, dict):
            continue
        for field_name, definition in properties.items():
            if not _CONTRACT_FIELD_RE.match(str(field_name)):
                continue
            field = str(field_name)
            field_records.setdefault(field, []).append(
                {"file": str(path), "shape": _normalize_schema_shape(definition, required=field in required_fields)}
            )
    return field_records, parse_issues, evidence


def _conflict_dimension_values(records: list[dict[str, Any]], dimension: str) -> set[str]:
    return {json.dumps(item.get("shape", {}).get(dimension), ensure_ascii=False, sort_keys=True) for item in records}


def _field_identity(field: str) -> str:
    if field in {"schema_version", "status", "mode", "generated_at", "feature", "details"}:
        return f"generic:{field}"
    return f"contract:{field}"


def _conflict_dimensions(records: list[dict[str, Any]]) -> list[str]:
    dimensions: list[str] = []
    for dimension in ("type", "enum"):
        if len(_conflict_dimension_values(records, dimension)) > 1:
            dimensions.append(dimension)
    return dimensions


def _drift_dimensions(records: list[dict[str, Any]]) -> list[str]:
    dimensions: list[str] = []
    for dimension in ("required", "description"):
        if len(_conflict_dimension_values(records, dimension)) > 1:
            dimensions.append(dimension)
    return dimensions


def _record_file_set(records: list[dict[str, Any]]) -> list[str]:
    return sorted({str(item.get("file") or "") for item in records if str(item.get("file") or "").strip()})


def _record_shape_map(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(item.get("file") or ""): dict(item.get("shape") or {}) for item in records if str(item.get("file") or "").strip()}


def _analyze_field_conflicts(
    field_records: dict[str, list[dict[str, Any]]],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    conflicts: dict[str, dict[str, Any]] = {}
    drifts: dict[str, dict[str, Any]] = {}
    evidence: list[dict[str, Any]] = []
    drift_evidence: list[dict[str, Any]] = []
    for field, records in field_records.items():
        if len(records) <= 1:
            continue
        if _field_identity(field).startswith("generic:"):
            continue
        dimensions = _conflict_dimensions(records)
        files = _record_file_set(records)
        shape_map = _record_shape_map(records)
        if dimensions:
            conflicts[field] = {"dimensions": dimensions, "files": files, "shapes": shape_map}
            evidence.append({
                "file": ", ".join(files),
                "rule": "runtime_contract_scatter.structural_conflict",
                "hit": f"field '{field}' conflicts on {dimensions}",
                "confidence": 0.95,
                "metadata": {"field": field, "dimensions": dimensions, "files": files},
            })
            continue
        drift_dimensions = _drift_dimensions(records)
        if drift_dimensions:
            drifts[field] = {"dimensions": drift_dimensions, "files": files, "shapes": shape_map}
            drift_evidence.append({
                "file": ", ".join(files),
                "rule": "runtime_contract_scatter.metadata_drift",
                "hit": f"field '{field}' differs on non-blocking metadata {drift_dimensions}",
                "confidence": 0.55,
                "metadata": {"field": field, "dimensions": drift_dimensions, "files": files},
            })
    return conflicts, drifts, evidence, drift_evidence


def _contract_scatter_status(conflicts: dict[str, Any], parse_issues: list[Any]) -> str:
    if conflicts:
        return "FAIL"
    if parse_issues:
        return "WARN"
    return "PASS"


def check_runtime_contract_scatter(schema_files: list[str]) -> dict[str, Any]:
    field_records, parse_issues, parse_evidence = _collect_field_records(schema_files)
    conflicts, metadata_drifts, conflict_evidence, drift_evidence = _analyze_field_conflicts(field_records)
    evidence = parse_evidence + conflict_evidence + drift_evidence
    status = _contract_scatter_status(conflicts, parse_issues)
    conflict_files = {field: list(payload["files"]) for field, payload in conflicts.items()}
    return {
        "status": status,
        "mode": "structural_rule_v3",
        "conflict_fields": sorted(conflicts),
        "conflict_files": conflict_files,
        "conflicts": conflicts,
        "metadata_drifts": metadata_drifts,
        "parse_issues": parse_issues,
        "details": (
            "Detected structural conflicts in runtime contract fields."
            if conflicts
            else "No structural runtime contract conflicts detected."
        ),
        "evidence": evidence,
    }


__all__ = [
    "check_cache_consistency",
    "check_invariant_proof",
    "check_prd_coverage",
    "check_runtime_contract_scatter",
]
