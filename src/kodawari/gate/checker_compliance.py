"""Compliance report assembly for contract-first gate checks."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from kodawari.gate.ast_checker import check_layer_boundary_ast
from kodawari.gate.checker_duplication import run_duplication_checker
from kodawari.gate.checker_import_rules import (
    find_module_ownership_file,
    load_module_ownership_modules,
    run_import_rules_checker,
)
from kodawari.gate.checker_scope_contract import (
    _string_list,
    _task_graph_allowed_files,
    check_layer_boundary_simple,
    check_scope_drift,
    run_source_of_truth_conflict_check,
)
from kodawari.gate.checker_semantics import (
    check_cache_consistency,
    check_invariant_proof,
    check_prd_coverage,
    check_runtime_contract_scatter,
)
from kodawari.gate.models import ComplianceCheck, ComplianceEvidence, ComplianceReport
from kodawari.source_of_truth import canonicalize_source_of_truth, load_domain_source_of_truth


def _check_status(value: str) -> str:
    normalized = str(value or "").upper()
    if normalized not in {"PASS", "FAIL", "WARN"}:
        return "WARN"
    return normalized


def _safe_confidence(value: Any) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return 0.0
    if confidence < 0.0:
        return 0.0
    if confidence > 1.0:
        return 1.0
    return confidence


def _message_to_file(message: str) -> str:
    text = str(message or "").strip()
    if ":" not in text:
        return ""
    prefix = text.split(":", 1)[0].strip()
    return prefix if prefix.endswith(".py") else ""


def _coerce_line(line_raw: Any) -> int | None:
    if line_raw is None or not str(line_raw).strip():
        return None
    try:
        return int(line_raw)
    except (TypeError, ValueError):
        return None


def _coerce_evidence_item(
    item: Any,
    *,
    default_rule: str,
) -> ComplianceEvidence | None:
    if not isinstance(item, dict):
        return None
    file_value = str(item.get("file") or "").strip() or "<unknown>"
    rule_value = str(item.get("rule") or default_rule).strip() or default_rule
    hit_value = str(item.get("hit") or "").strip()
    if not hit_value:
        return None
    metadata = item.get("metadata")
    return ComplianceEvidence(
        file=file_value,
        rule=rule_value,
        hit=hit_value,
        confidence=_safe_confidence(item.get("confidence")),
        line=_coerce_line(item.get("line")),
        metadata=dict(metadata) if isinstance(metadata, dict) else {},
    )


def _evidence_from_file_paths(
    name: str, paths: list[Any], rule_suffix: str, hit: str, confidence: float
) -> list[ComplianceEvidence]:
    return [
        ComplianceEvidence(file=str(p), rule=f"{name}.{rule_suffix}", hit=hit, confidence=confidence)
        for p in paths
    ]


def _evidence_from_messages(
    name: str, messages: list[Any], rule_suffix: str, confidence: float, file_src: str = ""
) -> list[ComplianceEvidence]:
    evidence: list[ComplianceEvidence] = []
    for item in messages:
        msg = str(item or "").strip()
        if not msg:
            continue
        evidence.append(ComplianceEvidence(
            file=file_src or _message_to_file(msg) or "<unknown>",
            rule=f"{name}.{rule_suffix}",
            hit=msg,
            confidence=confidence,
        ))
    return evidence


def _str_list_from(items: Any) -> list[str]:
    return [str(v) for v in list(items or []) if str(v).strip()]


def _boundary_debt_hit(layers: list[str], severity: str, rec_split: list[str]) -> str:
    parts = [f"shared by layers {layers}"]
    if severity:
        parts.append(f"severity={severity}")
    if rec_split:
        parts.append(f"recommended_split={rec_split}")
    return "; ".join(parts)


def _boundary_debt_meta(layers: list[str], tasks: list[str], severity: str, rec_split: list[str]) -> dict[str, Any]:
    meta: dict[str, Any] = {"layers": layers, "tasks": tasks}
    if severity:
        meta["severity"] = severity
    if rec_split:
        meta["recommended_split"] = rec_split
    return meta


def _boundary_debt_item(name: str, item: dict[str, Any]) -> ComplianceEvidence | None:
    file_path = str(item.get("file") or "").strip()
    layers = _str_list_from(item.get("layers"))
    if not file_path or len(layers) <= 1:
        return None
    severity = str(item.get("severity") or "").strip().lower()
    rec_split = _str_list_from(item.get("recommended_split"))
    tasks = _str_list_from(item.get("tasks"))
    return ComplianceEvidence(
        file=file_path,
        rule=f"{name}.boundary_debt",
        hit=_boundary_debt_hit(layers, severity, rec_split),
        confidence=0.88,
        metadata=_boundary_debt_meta(layers, tasks, severity, rec_split),
    )


def _evidence_from_boundary_debt(name: str, items: list[Any]) -> list[ComplianceEvidence]:
    evidence: list[ComplianceEvidence] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        entry = _boundary_debt_item(name, item)
        if entry is not None:
            evidence.append(entry)
    return evidence


def _evidence_from_conflict_files(name: str, conflict_files: dict[str, Any]) -> list[ComplianceEvidence]:
    evidence: list[ComplianceEvidence] = []
    for field, files in conflict_files.items():
        file_list = [str(item) for item in list(files or []) if str(item).strip()]
        if not file_list:
            continue
        evidence.append(ComplianceEvidence(
            file=", ".join(file_list),
            rule=f"{name}.field_conflict",
            hit=f"field '{field}' has conflicting definitions",
            confidence=0.95,
            metadata={"field": str(field), "files": file_list},
        ))
    return evidence


def _pl(payload: dict[str, Any], key: str) -> list[Any]:
    return list(payload.get(key) or [])


def _derive_evidence_from_payload(name: str, payload: dict[str, Any]) -> list[ComplianceEvidence]:
    evidence: list[ComplianceEvidence] = []
    evidence += _evidence_from_file_paths(name, _pl(payload, "out_of_scope_files"),
                                          "scope_drift", "file changed outside allowed scope", 0.98)
    evidence += _evidence_from_file_paths(name, _pl(payload, "suspicious_files"),
                                          "suspicious_file", "write path may be inconsistent with cache invalidation", 0.78)
    evidence += _evidence_from_messages(name, _pl(payload, "violations"), "violation", 0.9)
    evidence += _evidence_from_messages(name, _pl(payload, "simple"), "layer_simple", 0.85)
    evidence += _evidence_from_messages(name, _pl(payload, "ast"), "layer_ast", 0.9)
    evidence += _evidence_from_messages(name, _pl(payload, "issues"), "review_issue", 0.95, "<runtime>")
    evidence += _evidence_from_boundary_debt(name, _pl(payload, "items"))
    conflict_files = payload.get("conflict_files")
    if isinstance(conflict_files, dict):
        evidence += _evidence_from_conflict_files(name, conflict_files)
    details = str(payload.get("details") or "").strip()
    if not evidence and details:
        evidence.append(ComplianceEvidence(file="<unknown>", rule=f"{name}.derived", hit=details, confidence=0.0))
    return evidence


def _collect_evidence(name: str, payload: dict[str, Any]) -> tuple[list[ComplianceEvidence], bool]:
    raw = payload.get("evidence")
    evidence: list[ComplianceEvidence] = []
    if isinstance(raw, list):
        for item in raw:
            normalized = _coerce_evidence_item(item, default_rule=f"{name}.evidence")
            if normalized is not None:
                evidence.append(normalized)
    elif isinstance(raw, dict):
        normalized = _coerce_evidence_item(raw, default_rule=f"{name}.evidence")
        if normalized is not None:
            evidence.append(normalized)
    if evidence:
        return evidence, True
    return _derive_evidence_from_payload(name, payload), False


_DERIVED_EVIDENCE_THRESHOLD = 0.5
_DOMAIN_SOT_STOPWORDS = {"logic", "rules", "rule", "module", "service", "layer"}


def _evidence_sufficient(evidence: list[ComplianceEvidence], explicit: bool) -> bool:
    if explicit:
        return True
    return any(item.confidence >= _DERIVED_EVIDENCE_THRESHOLD for item in evidence)


def _sot_tokens_for_semantic(semantic: str, canonical_module: str) -> list[str]:
    tokens = [t for t in str(semantic).lower().replace("-", " ").split()
              if len(t) >= 4 and t not in _DOMAIN_SOT_STOPWORDS]
    if not tokens:
        tokens = [t for t in str(canonical_module).lower().replace("_", " ").split() if len(t) >= 4]
    return tokens


def _check_file_against_sot(
    changed: str, canonical_path: str, tokens: list[str],
    semantic: str, canonical_module: str,
) -> dict[str, Any] | None:
    normalized = str(changed).strip().replace("\\", "/")
    if not normalized or normalized == canonical_path:
        return None
    if not tokens or not any(token in normalized.lower() for token in tokens):
        return None
    return {
        "file": normalized,
        "rule": "domain_source_of_truth.canonical_hint",
        "hit": (
            f"changed file may be reimplementing '{semantic}' outside canonical module "
            f"'{canonical_module}' ({canonical_path or 'path unknown'})"
        ),
        "confidence": 0.55,
        "metadata": {"semantic": semantic, "canonical_module": canonical_module, "canonical_path": canonical_path},
    }


def _path_by_module_map(modules: list[dict[str, Any]]) -> dict[str, str]:
    return {str(m.get("module") or "").strip(): str(m.get("path") or "").strip() for m in modules}


def _sot_status_details(evidence: list[Any]) -> tuple[str, str]:
    if evidence:
        return "WARN", "Potential domain source-of-truth drift detected."
    return "PASS", "No domain source-of-truth drift detected."


def _domain_sot_payload(project_root: Path, changed_files: list[str]) -> dict[str, Any]:
    ownership_path = find_module_ownership_file(project_root)
    if ownership_path is None:
        return {"status": "PASS", "details": "domain source-of-truth manifest not configured", "evidence": []}
    mapping = load_domain_source_of_truth(ownership_path)
    modules = load_module_ownership_modules(project_root=project_root, ownership_path=ownership_path)
    path_by_module = _path_by_module_map(modules)
    evidence: list[dict[str, Any]] = []
    for semantic, canonical_module in mapping.items():
        canonical_path = path_by_module.get(canonical_module, "")
        tokens = _sot_tokens_for_semantic(semantic, canonical_module)
        for changed in changed_files:
            entry = _check_file_against_sot(changed, canonical_path, tokens, semantic, canonical_module)
            if entry is not None:
                evidence.append(entry)
    status, details = _sot_status_details(evidence)
    return {"status": status, "details": details, "evidence": evidence}


def _placeholder_evidence(name: str, hit: str) -> list[ComplianceEvidence]:
    return [ComplianceEvidence(file="<unknown>", rule=f"{name}.evidence_required", hit=hit, confidence=0.0)]


def _append_detail(existing: str, msg: str) -> str:
    return (existing + " " if existing else "") + msg


def _compute_evidence_sufficient(evidence: list[ComplianceEvidence], explicit: bool) -> bool:
    return bool(evidence) and (explicit or any(item.confidence > 0.0 for item in evidence))


def _apply_evidence_policy(
    name: str,
    status: str,
    details: str,
    evidence: list[ComplianceEvidence],
    explicit: bool,
) -> tuple[str, str, list[ComplianceEvidence]]:
    if status == "FAIL" and not _evidence_sufficient(evidence, explicit):
        return (
            "WARN",
            _append_detail(details, "Downgraded from FAIL because no evidence was provided."),
            _placeholder_evidence(name, "Blocking verdict removed due to missing evidence."),
        )
    if status == "WARN" and not _evidence_sufficient(evidence, explicit):
        return status, details, _placeholder_evidence(name, "Warning issued without concrete evidence.")
    return status, details, evidence


def _to_compliance_check(name: str, payload: dict[str, Any]) -> ComplianceCheck:
    status = _check_status(str(payload.get("status") or "WARN"))
    details = str(payload.get("details") or "").strip()
    evidence, explicit = _collect_evidence(name, payload)
    status, details, evidence = _apply_evidence_policy(name, status, details, evidence, explicit)
    evidence_sufficient = _compute_evidence_sufficient(evidence, explicit)
    return ComplianceCheck(
        check_name=name,
        status=status,
        details=details,
        evidence=evidence,
        evidence_sufficient=evidence_sufficient,
        blocking_eligible=(status == "FAIL" and evidence_sufficient),
    )


def _duplication_status(duplication_dict: dict[str, Any], duplicate_blocks: int) -> str:
    if duplicate_blocks:
        return "WARN"
    return "WARN" if str(duplication_dict.get("status") or "").upper() == "WARN" else "PASS"


def _duplication_details(duplication_dict: dict[str, Any], duplicate_blocks: int) -> str:
    if duplicate_blocks:
        return f"Detected {duplicate_blocks} duplicate-code block(s) in changed files."
    return str(duplication_dict.get("details") or "No duplicate-code blocks reported for changed files.")


def _build_duplication_payload(changed_files: list[str], project_root: Path) -> dict[str, Any]:
    duplication_paths = [(project_root / item).resolve() for item in changed_files if str(item).strip()]
    if not duplication_paths:
        return {"status": "PASS", "details": "No duplicate-code blocks reported for changed files.", "evidence": []}
    duplication_report = run_duplication_checker(duplication_paths, project_root=project_root)
    if duplication_report is None:
        return {"status": "PASS", "details": "No duplicate-code blocks reported for changed files.", "evidence": []}
    duplication_dict = duplication_report.to_dict()
    duplicate_blocks = int(duplication_dict.get("duplicate_block_count") or 0)
    return {
        "status": _duplication_status(duplication_dict, duplicate_blocks),
        "details": _duplication_details(duplication_dict, duplicate_blocks),
        "evidence": list(duplication_dict.get("evidence") or []),
    }


def _review_status(review_payload: dict[str, Any]) -> str:
    status_raw = str(review_payload.get("status") or "").upper()
    return "PASS" if status_raw in {"PASS", "SKIP"} else _check_status(status_raw or "WARN")


def _review_details(review_payload: dict[str, Any], issues: list[str]) -> str:
    details = str(review_payload.get("blocking_reason") or review_payload.get("details") or "").strip()
    return details or (issues[0] if issues else "")


def _apply_missing_review_evidence(
    review_payload: dict[str, Any],
    issues: list[str],
    entries: list[Any],
) -> tuple[str, str, list[str], list[Any]]:
    source = str(review_payload.get("source") or "unknown").strip() or "unknown"
    details = f"explicit review evidence missing (source={source})"
    if "explicit review evidence missing" not in issues:
        issues = [*issues, "explicit review evidence missing"]
    if not entries:
        entries = [{"file": "<runtime>", "rule": "review_evidence.explicit_missing",
                    "hit": details, "confidence": 0.95}]
    return "WARN", details, issues, entries


def _build_review_check(review_evidence: dict[str, Any] | None) -> dict[str, Any]:
    review_payload = dict(review_evidence or {})
    explicit = bool(review_payload.get("explicit")) if "explicit" in review_payload else True
    status = _review_status(review_payload)
    issues = [str(item) for item in list(review_payload.get("issues") or []) if str(item).strip()]
    details = _review_details(review_payload, issues)
    entries = list(review_payload.get("evidence") or [])
    if not review_payload or not explicit:
        status, details, issues, entries = _apply_missing_review_evidence(review_payload, issues, entries)
    return {"status": status, "details": details, "issues": issues, "evidence": entries}


def _resolve_allowed_files(
    allowed_files: list[str] | None,
    task_graph: dict[str, Any],
    task_card: dict[str, Any] | None,
) -> list[str]:
    resolved: list[str] = []
    for source in (
        list(allowed_files or []),
        _task_graph_allowed_files(task_graph),
        _string_list((task_card or {}).get("files_to_change")),
    ):
        for item in source:
            text = str(item or "").strip()
            if text and text not in resolved:
                resolved.append(text)
    return resolved


def _resolve_sot_canonical(prd: dict[str, Any], sot: list[str]) -> list[str]:
    return _string_list(prd.get("source_of_truth_canonical")) or canonicalize_source_of_truth(sot)


def _resolve_compliance_inputs(
    task_graph: dict[str, Any] | None,
    tasks: list[dict[str, Any]] | None,
    prd_intake: dict[str, Any] | None,
    allowed_files: list[str] | None,
    declared_sot: list[str] | None,
    task_card: dict[str, Any] | None,
) -> tuple[dict[str, Any], list[Any], dict[str, Any], list[str], list[str], list[str]]:
    resolved_task_graph = dict(task_graph or {})
    resolved_tasks = list(tasks or resolved_task_graph.get("tasks") or [])
    resolved_prd = dict(prd_intake or {})
    resolved_allowed_files = _resolve_allowed_files(allowed_files, resolved_task_graph, task_card)
    resolved_sot = list(declared_sot or _string_list(resolved_prd.get("source_of_truth")))
    resolved_sot_canonical = _resolve_sot_canonical(resolved_prd, resolved_sot)
    return resolved_task_graph, resolved_tasks, resolved_prd, resolved_allowed_files, resolved_sot, resolved_sot_canonical


def _build_layer_payload(layer_simple: list[Any], layer_ast: list[Any]) -> dict[str, Any]:
    violations = bool(layer_simple or layer_ast)
    return {
        "status": "FAIL" if violations else "PASS",
        "details": "Layer boundary violations detected." if violations else "No layer boundary violations.",
        "simple": layer_simple,
        "ast": layer_ast,
    }


def _resolve_layer_boundary_debt(resolved_task_graph: dict[str, Any]) -> dict[str, Any]:
    debt = dict(resolved_task_graph.get("boundary_debt") or {})
    if not str(debt.get("status") or "").upper():
        return {"status": "PASS", "details": "No physical layer-boundary debt detected.", "items": []}
    return debt


def _build_sot_payload(sot_violations: list[Any]) -> dict[str, Any]:
    fail = bool(sot_violations)
    return {
        "status": "FAIL" if fail else "PASS",
        "details": "Writes outside declared source_of_truth detected." if fail else "No source_of_truth violations.",
        "violations": sot_violations,
    }


def build_contract_compliance_report(
    *,
    changed_files: list[str],
    project_root: Path,
    task_card: dict[str, Any] | None = None,
    task_graph: dict[str, Any] | None = None,
    prd_intake: dict[str, Any] | None = None,
    review_evidence: dict[str, Any] | None = None,
    allowed_files: list[str] | None = None,
    declared_sot: list[str] | None = None,
    tasks: list[dict[str, Any]] | None = None,
    schema_files: list[str] | None = None,
    include_ast_checks: bool = True,
) -> dict[str, Any]:
    resolved_task_graph, resolved_tasks, resolved_prd, resolved_allowed_files, resolved_sot, resolved_sot_canonical = (
        _resolve_compliance_inputs(task_graph, tasks, prd_intake, allowed_files, declared_sot, task_card)
    )
    scope = check_scope_drift(changed_files, resolved_allowed_files, project_root=project_root)
    layer_simple = check_layer_boundary_simple(changed_files, project_root)
    layer_ast = check_layer_boundary_ast(changed_files, project_root) if include_ast_checks else []
    layer_payload = _build_layer_payload(layer_simple, layer_ast)
    layer_boundary_debt = _resolve_layer_boundary_debt(resolved_task_graph)
    sot_violations = run_source_of_truth_conflict_check(
        changed_files, project_root, resolved_sot, declared_sot_canonical=resolved_sot_canonical,
    )
    sot_payload = _build_sot_payload(sot_violations)
    prd_coverage = check_prd_coverage(resolved_tasks, resolved_prd)
    invariant_proof = check_invariant_proof(resolved_tasks)
    cache_consistency = check_cache_consistency(changed_files, project_root)
    runtime_contract_scatter = check_runtime_contract_scatter(list(schema_files or []))
    duplication_payload = _build_duplication_payload(changed_files, project_root)
    import_rules_payload = run_import_rules_checker(changed_files, project_root)
    domain_sot_payload = _domain_sot_payload(project_root, changed_files)
    review_check = _build_review_check(review_evidence)

    checks = [
        _to_compliance_check("scope_drift", scope),
        _to_compliance_check("layer_boundary", layer_payload),
        _to_compliance_check("layer_boundary_debt", layer_boundary_debt),
        _to_compliance_check("source_of_truth_conflict", sot_payload),
        _to_compliance_check("invariant_proof", invariant_proof),
        _to_compliance_check("prd_coverage", prd_coverage),
        _to_compliance_check("cache_consistency", cache_consistency),
        _to_compliance_check("runtime_contract_scatter", runtime_contract_scatter),
        _to_compliance_check("duplication", duplication_payload),
        _to_compliance_check("import_rules", import_rules_payload),
        _to_compliance_check("domain_source_of_truth", domain_sot_payload),
        _to_compliance_check("review_evidence", review_check),
    ]
    status = "PASS"
    if any(item.status == "FAIL" for item in checks):
        status = "FAIL"
    return ComplianceReport(status=status, checks=checks, mode="contract_first_mvp").to_dict()


__all__ = ["build_contract_compliance_report"]
