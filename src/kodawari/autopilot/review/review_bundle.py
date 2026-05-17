"""Peer-review bundle artifact helpers."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
from typing import Any

from jsonschema import Draft7Validator

from kodawari.autopilot.core.repo_path_guard import filter_repo_read_paths, guard_repo_read_path
from kodawari.infra.io_atomic import atomic_write_json


REVIEW_BUNDLE_SCHEMA_VERSION = "review.bundle.v1"
REVIEW_BUNDLE_FILENAME = ".review_bundle.json"
PEER_REVIEW_RESPONSE_SCHEMA = "peer_review_response"

_SCHEMA_CACHE: dict[str, dict[str, Any]] = {}


class ReviewBundleError(ValueError):
    """Raised when review bundle or response payloads are invalid."""


def _schema_path(name: str) -> Path:
    return Path(__file__).resolve().parents[2] / "schemas" / "runtime" / f"{name}.schema.json"


def _schema(name: str) -> dict[str, Any]:
    cached = _SCHEMA_CACHE.get(name)
    if cached is not None:
        return cached
    payload = json.loads(_schema_path(name).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"invalid schema payload for {name}")
    _SCHEMA_CACHE[name] = payload
    return payload


def _validate(name: str, payload: dict[str, Any]) -> dict[str, Any]:
    validator = Draft7Validator(_schema(name))
    errors = sorted(validator.iter_errors(payload), key=lambda item: list(item.path))
    if errors:
        details = []
        for error in errors:
            field = ".".join(str(item) for item in error.path) or "<root>"
            details.append(f"{field}: {error.message}")
        raise ReviewBundleError("; ".join(details))
    return payload


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _load_text(path: Path, *, max_chars: int = 2000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except FileNotFoundError:
        return ""
    stripped = text.strip()
    return stripped[:max_chars]


def _load_jsonl(path: Path, *, limit: int = 20) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return []
    out: list[dict[str, Any]] = []
    for line in lines[-max(1, int(limit)):]:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            out.append(payload)
    return out


def _execution_tool_evidence(planning_dir: Path) -> dict[str, Any]:
    manifest = _load_json(planning_dir / ".execution_tool_manifest.json")
    tool_calls = _load_jsonl(planning_dir / ".execution_tool_calls.jsonl", limit=30)
    patch_attempts = _load_jsonl(planning_dir / ".execution_patch_attempts.jsonl", limit=30)
    if not manifest and not tool_calls and not patch_attempts:
        return {}
    return {
        "manifest": manifest,
        "tool_calls_tail": tool_calls,
        "patch_attempts_tail": patch_attempts,
    }


def _git_diff(project_root: Path, changed_files: list[str], *, max_chars: int = 20000) -> str:
    safe_changed_files = _safe_changed_files(project_root, changed_files)
    if not safe_changed_files:
        return ""
    try:
        run = subprocess.run(
            ["git", "-C", str(project_root), "diff", "--no-ext-diff", "--unified=3", "--", *safe_changed_files],
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if run.returncode not in {0, 1}:
        return ""
    return str(run.stdout or "").strip()[:max_chars]


def _safe_changed_files(project_root: Path, changed_files: list[str]) -> list[str]:
    safe, _rejected = filter_repo_read_paths(
        project_root=project_root,
        paths=[str(item) for item in changed_files if str(item).strip()],
        require_file=False,
    )
    return safe


def _changed_file_path_guard_findings(project_root: Path, changed_files: list[str]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for item in changed_files:
        raw = str(item or "").strip()
        if not raw:
            continue
        result = guard_repo_read_path(project_root=project_root, path=raw, require_file=False)
        if not result.allowed:
            findings.append(result.to_dict())
    return findings


def _changed_file_snippets(project_root: Path, changed_files: list[str], *, max_chars: int = 3000) -> list[dict[str, str]]:
    snippets: list[dict[str, str]] = []
    for item in changed_files:
        result = guard_repo_read_path(project_root=project_root, path=item)
        if not result.allowed or result.resolved_path is None:
            continue
        path = result.resolved_path
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        snippets.append({"path": result.path, "snippet": text[:max_chars]})
    return snippets


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _extract_module_boundaries(source: dict[str, Any], key: str = "module_boundaries") -> list[dict[str, Any]]:
    return [
        {
            "name": str(item.get("name") or "").strip(),
            "surface": str(item.get("surface") or "").strip(),
            "roots": _string_list(item.get("roots")),
            "layers": _string_list(item.get("layers")),
        }
        for item in list(source.get(key) or [])
        if isinstance(item, dict)
    ]


def _extract_verify_recipes(source: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "surface": str(item.get("surface") or "").strip(),
            "command": str(item.get("command") or "").strip(),
            "required": bool(item.get("required", False)),
            "roots": _string_list(item.get("roots")),
        }
        for item in list(source.get("verify_recipes") or [])
        if isinstance(item, dict)
    ]


def _extract_approval_points(source: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "name": str(item.get("name") or "").strip(),
            "required": bool(item.get("required", False)),
            "reason": str(item.get("reason") or "").strip(),
        }
        for item in list(source.get("approval_points") or [])
        if isinstance(item, dict)
    ]


def _extract_task_graph_section(
    tasks: list[dict[str, Any]],
    graph: dict[str, Any],
    task_hit: dict[str, Any],
    changed_files: list[str],
) -> dict[str, Any]:
    return {
        "task_count": len(tasks),
        "executability_status": str(dict(graph.get("executability") or {}).get("status") or "").strip(),
        "current_task": {
            "task_id": str(task_hit.get("task_id") or "").strip(),
            "task_name": str(task_hit.get("task_name") or "").strip(),
            "layer_owner": str(task_hit.get("layer_owner") or "").strip(),
            "core_files": _string_list(task_hit.get("core_files")),
            "depends_on": _string_list(task_hit.get("depends_on")),
        },
        "changed_files": [str(item).strip() for item in changed_files if str(item).strip()],
    }


def _build_contract_from_conversation(
    conversation: dict[str, Any],
    graph: dict[str, Any],
    tasks: list[dict[str, Any]],
    task_hit: dict[str, Any],
    changed_files: list[str],
) -> dict[str, Any]:
    return {
        "prd_intake": {
            "business_outcome": str(conversation.get("business_outcome") or "").strip(),
            "source_of_truth": _string_list(conversation.get("source_of_truth")),
            "source_of_truth_canonical": _string_list(conversation.get("source_of_truth_canonical")),
            "out_of_scope": _string_list(conversation.get("out_of_scope")),
        },
        "architecture_plan": {
            "archetype": str(conversation.get("archetype") or "").strip(),
            "capabilities": _string_list(conversation.get("capabilities")),
            "module_boundaries_excerpt": _extract_module_boundaries(conversation)[:12],
            "verify_recipes_excerpt": _extract_verify_recipes(conversation)[:12],
            "approval_points_excerpt": _extract_approval_points(conversation)[:12],
            "execution_constraints_excerpt": dict(conversation.get("execution_constraints") or {}),
        },
        "task_graph": _extract_task_graph_section(tasks, graph, task_hit, changed_files),
    }


def _build_contract_from_artifacts(
    prd: dict[str, Any],
    architecture: dict[str, Any],
    graph: dict[str, Any],
    tasks: list[dict[str, Any]],
    task_hit: dict[str, Any],
    changed_files: list[str],
) -> dict[str, Any]:
    return {
        "prd_intake": {
            "business_outcome": str(prd.get("business_outcome") or "").strip(),
            "source_of_truth": _string_list(prd.get("source_of_truth")),
            "source_of_truth_canonical": _string_list(prd.get("source_of_truth_canonical")),
            "out_of_scope": _string_list(prd.get("out_of_scope")),
        },
        "architecture_plan": {
            "archetype": str(architecture.get("archetype") or "").strip(),
            "capabilities": _string_list(architecture.get("capabilities")),
            "module_boundaries_excerpt": _extract_module_boundaries(architecture)[:12],
            "verify_recipes_excerpt": _extract_verify_recipes(architecture)[:12],
            "approval_points_excerpt": _extract_approval_points(architecture)[:12],
            "execution_constraints_excerpt": dict(architecture.get("execution_constraints") or {}),
        },
        "task_graph": _extract_task_graph_section(tasks, graph, task_hit, changed_files),
    }


def _normalize_task_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_id": str(item.get("task_id") or "").strip(),
        "task_name": str(item.get("task_name") or "").strip(),
        "layer_owner": str(item.get("layer_owner") or "").strip(),
        "core_files": _string_list(item.get("files_to_change") or item.get("core_files")),
        "depends_on": _string_list(item.get("depends_on")),
    }


def _graph_tasks(graph: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(item) for item in list(graph.get("tasks") or []) if isinstance(item, dict)]


def _conversation_fallback_tasks(conversation: dict[str, Any]) -> list[dict[str, Any]]:
    plan_tasks = list(dict(conversation.get("final_plan") or {}).get("tasks") or [])
    return [_normalize_task_item(item) for item in plan_tasks if isinstance(item, dict)]


def _resolve_tasks_from_graph_or_conversation(
    graph: dict[str, Any], conversation: dict[str, Any]
) -> list[dict[str, Any]]:
    tasks = _graph_tasks(graph)
    if tasks or not conversation:
        return tasks
    return _conversation_fallback_tasks(conversation)


def _contract_excerpt(
    *,
    planning_dir: Path,
    task_id: str,
    changed_files: list[str],
) -> dict[str, Any]:
    conversation = _load_json(planning_dir / "PLANNING_CONVERSATION.json")
    prd = _load_json(planning_dir / "PRD_INTAKE.json")
    architecture = _load_json(planning_dir / "ARCHITECTURE_PLAN.json")
    graph = _load_json(planning_dir / "TASK_GRAPH.json")
    tasks = _resolve_tasks_from_graph_or_conversation(graph, conversation)
    task_hit = next((item for item in tasks if str(item.get("task_id") or "").strip() == task_id), {})
    if conversation:
        return _build_contract_from_conversation(conversation, graph, tasks, task_hit, changed_files)
    return _build_contract_from_artifacts(prd, architecture, graph, tasks, task_hit, changed_files)


def _implementer_note(planning_dir: Path) -> dict[str, Any]:
    execution_result = _load_json(planning_dir / ".execution_result.json")
    raw = execution_result.get("implementer_note")
    if not isinstance(raw, dict):
        return {}
    note = {
        "claimed_intent": str(raw.get("claimed_intent") or "").strip(),
        "claimed_invariants_preserved": _string_list(raw.get("claimed_invariants_preserved")),
        "claimed_risks": _string_list(raw.get("claimed_risks")),
        "non_authoritative": True,
    }
    if not any([note["claimed_intent"], note["claimed_invariants_preserved"], note["claimed_risks"]]):
        return {}
    return note


def _runtime_dict(context: dict[str, Any], key: str) -> dict[str, Any]:
    value = context.get(key)
    return dict(value) if isinstance(value, dict) else {}


def _build_verify_summary(verify_report: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    runtime_check = _runtime_dict(dict(context or {}), "runtime_verify_check")
    if runtime_check:
        return {
            "status": str(runtime_check.get("status") or "").strip(),
            "passed": bool(runtime_check.get("passed", False)),
            "input_confidence": "runtime",
            "summary": str(runtime_check.get("summary") or "").strip(),
            "source": "runtime_verify_check",
            "verify_cmd": str(runtime_check.get("verify_cmd") or runtime_check.get("verify_cmd_resolved") or "").strip(),
            "verify_cmd_resolved": str(runtime_check.get("verify_cmd_resolved") or "").strip(),
            "verify_target_source": str(runtime_check.get("verify_target_source") or "").strip(),
            "verify_targets": _string_list(runtime_check.get("verify_targets")),
            "mode": str(runtime_check.get("mode") or "").strip(),
            "command_executed": bool(runtime_check.get("command_executed", False)),
            "returncode": runtime_check.get("returncode"),
        }
    check = dict(verify_report.get("verify_check") or {})
    return {
        "status": str(verify_report.get("status") or "").strip(),
        "input_confidence": str(verify_report.get("input_confidence") or "").strip(),
        "summary": str(check.get("summary") or verify_report.get("summary") or "").strip(),
        "source": ".verify_report.json",
    }


def _build_compliance_summary(compliance_report: dict[str, Any]) -> dict[str, Any]:
    checks = [
        str(item.get("check_name") or "")
        for item in list(compliance_report.get("checks") or [])
        if isinstance(item, dict)
    ]
    return {"status": str(compliance_report.get("status") or "").strip(), "checks": checks}


def _build_gate_summary(gate_result: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    runtime_gate = _runtime_dict(dict(context or {}), "runtime_gate_check")
    if runtime_gate:
        return {
            "status": str(runtime_gate.get("total_status") or runtime_gate.get("status") or "").strip(),
            "blocking_reason": str(runtime_gate.get("blocking_reason") or "").strip(),
            "source": "runtime_gate_check",
        }
    return {
        "status": str(gate_result.get("total_status") or "").strip(),
        "blocking_reason": str(gate_result.get("blocking_reason") or "").strip(),
        "source": ".gate_result.json",
    }


def _build_execution_summary(planning_dir: Path) -> dict[str, Any]:
    result = _load_json(planning_dir / ".execution_result.json")
    if not result:
        return {}
    verify_summary = result.get("verify_summary")
    verify_payload = dict(verify_summary) if isinstance(verify_summary, dict) else {}
    return {
        "status": str(result.get("status") or "").strip(),
        "backend": str(result.get("backend") or "").strip(),
        "changed_files": _string_list(result.get("changed_files")),
        "changed_files_count": len(_string_list(result.get("changed_files"))),
        "verification_only_noop": bool(result.get("verification_only_noop", False)),
        "returncode": result.get("returncode"),
        "summary": str(result.get("summary") or "").strip(),
        "verify_summary": {
            "status": str(verify_payload.get("status") or "").strip(),
            "passed": bool(verify_payload.get("passed", False)),
            "command_executed": bool(verify_payload.get("command_executed", False)),
            "returncode": verify_payload.get("returncode"),
            "verify_cmd": str(verify_payload.get("verify_cmd") or "").strip(),
            "summary": str(verify_payload.get("summary") or "").strip(),
        },
    }


def _resolve_bundle_task_id(context: dict[str, Any], task_card: dict[str, Any]) -> str:
    return str(context.get("task_id") or task_card.get("task_id") or "").strip()


def _resolve_bundle_invariants(context: dict[str, Any], task_card: dict[str, Any]) -> list[str]:
    items = list(task_card.get("invariants") or context.get("task_invariants") or [])
    return [str(item) for item in items if str(item).strip()]


def _apply_optional_bundle_fields(
    payload: dict[str, Any],
    deterministic_findings: dict[str, Any] | None,
    implementer_note: dict[str, Any],
) -> None:
    if isinstance(deterministic_findings, dict):
        payload["deterministic_findings"] = dict(deterministic_findings)
    if implementer_note:
        payload["implementer_note"] = implementer_note


def _verified_test_snippets(
    project_root: Path,
    deterministic_findings: dict[str, Any] | None,
) -> list[dict[str, str]]:
    if not isinstance(deterministic_findings, dict):
        return []
    verified_files = _string_list(deterministic_findings.get("verified_test_files"))
    if not verified_files:
        return []
    return _changed_file_snippets(project_root, verified_files)


def build_review_bundle(
    *,
    feature: str,
    task: str,
    project_root: Path,
    planning_dir: Path,
    context: dict[str, Any],
    changed_files: list[str],
    review_iteration: int,
    deterministic_findings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    # Prefer task_card from the engine's context (carries the explicit
    # --card payload). Falling back to TASK_CARD_ACTIVE.json is only safe when
    # context truly lacks a task_card (legacy/non-engine callers); when the
    # engine populated context.task_card, ACTIVE may belong to a *different*
    # task and would silently corrupt the review bundle's task_id/scope.
    context_task_card = context.get("task_card")
    if isinstance(context_task_card, dict) and context_task_card:
        task_card = dict(context_task_card)
    else:
        task_card = dict(_load_json(planning_dir / "TASK_CARD_ACTIVE.json") or {})
    verify_report = _load_json(planning_dir / ".verify_report.json")
    compliance_report = _load_json(planning_dir / "COMPLIANCE_REPORT.json")
    gate_result = _load_json(planning_dir / ".gate_result.json")
    review_result = _load_json(planning_dir / ".review_result.json")
    task_id = _resolve_bundle_task_id(context, task_card)
    safe_changed_files = _safe_changed_files(project_root, changed_files)
    payload: dict[str, Any] = {
        "schema_version": REVIEW_BUNDLE_SCHEMA_VERSION,
        "feature": str(feature or "").strip(),
        "task": str(task or "").strip(),
        "task_id": task_id,
        "review_iteration": int(review_iteration),
        "workspace_root": str(project_root.resolve()),
        "changed_files": safe_changed_files,
        "invariants": _resolve_bundle_invariants(context, task_card),
        "task_card": task_card,
        "task_scope": str(context.get("task_scope") or "").strip(),
        "contract_excerpt": _contract_excerpt(
            planning_dir=planning_dir,
            task_id=task_id,
            changed_files=safe_changed_files,
        ),
        "verify_summary": _build_verify_summary(verify_report, context),
        "compliance_summary": _build_compliance_summary(compliance_report),
        "gate_summary": _build_gate_summary(gate_result, context),
        "execution_summary": _build_execution_summary(planning_dir),
        "design_summary": _load_text(planning_dir / "DESIGN.md"),
        "review_summary": _load_text(planning_dir / "REVIEW.md"),
        "task_graph_summary": _load_text(planning_dir / "TASK_GRAPH.json"),
        "git_diff": _git_diff(project_root, safe_changed_files),
        "changed_file_snippets": _changed_file_snippets(project_root, safe_changed_files),
        "verified_test_snippets": _verified_test_snippets(project_root, deterministic_findings),
        "changed_file_path_guard_findings": _changed_file_path_guard_findings(project_root, changed_files),
        "execution_tool_evidence": _execution_tool_evidence(planning_dir),
        "existing_review_status": str(review_result.get("status") or "").strip(),
    }
    _apply_optional_bundle_fields(payload, deterministic_findings, _implementer_note(planning_dir))
    return _validate("review_bundle", payload)


def write_review_bundle(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_json(path, _validate("review_bundle", dict(payload)))


def validate_peer_review_response(payload: dict[str, Any]) -> dict[str, Any]:
    return _validate(PEER_REVIEW_RESPONSE_SCHEMA, dict(payload))


__all__ = [
    "REVIEW_BUNDLE_FILENAME",
    "REVIEW_BUNDLE_SCHEMA_VERSION",
    "ReviewBundleError",
    "build_review_bundle",
    "validate_peer_review_response",
    "write_review_bundle",
]
