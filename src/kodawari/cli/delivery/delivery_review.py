"""Review workflow helpers."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from kodawari.autopilot.review_runtime_policy import classify_review_runtime
from kodawari.cli.evidence.changed_files_truth import (
    filter_project_root_paths,
    git_base_branch_diff_files,
    load_worktree_baseline,
)
from kodawari.cli.evidence.artifact_truth import resolve_authoritative_changed_files
from kodawari.cli.delivery.delivery_common import (
    _attach_payload_digest,
    _contract_first_completion_summary,
    _contract_scope_hints,
    _ensure_design_artifact,
    _load_json_dict,
    _normalize_relpath,
    _planning_artifact_mode,
    _write_json,
    _utc_now_iso,
)
from kodawari.cli.main_support import _build_cli_provenance
from kodawari.cli.delivery.delivery_release import (
    _contract_layer_boundary_debt,
    _resolve_execution_check,
    _review_evidence,
)
from kodawari.cli.delivery.workflow_chain import load_workflow_chain_snapshot, parse_task_backlog
from kodawari.gate.checkers import check_scope_drift as contract_scope_drift
from kodawari.cli.io_atomic import atomic_write_text

CHECKBOX_RE = re.compile(r"^\s*-\s*\[( |x|X)\]\s*(.+?)\s*$")
PATH_HINT_RE = re.compile(r"([A-Za-z0-9_./\\-]+\.[A-Za-z0-9_]+)")


def _resolve_review_changed_files(
    *,
    project_root: Path,
    planning_dir: Path,
    base_branch: str,
    state_payload: dict[str, Any] | None,
    changed_files_override: list[str] | None,
) -> tuple[list[str], str, list[str]]:
    baseline = load_worktree_baseline(planning_dir)
    dirty = filter_project_root_paths(project_root, list((baseline or {}).get("dirty_files") or []))
    authoritative = resolve_authoritative_changed_files(
        project_root=project_root,
        planning_dir=planning_dir,
        state_payload=state_payload,
    )
    candidates: list[tuple[str, list[str]]] = [
        (str(authoritative.get("source") or "none"), list(authoritative.get("items") or [])),
        ("cli_override", list(changed_files_override or [])),
        ("git_diff:project_root", git_base_branch_diff_files(project_root, base_branch)),
    ]
    for source, values in candidates:
        normalized = filter_project_root_paths(project_root, [_normalize_relpath(item) for item in values])
        if normalized:
            return normalized, source, dirty
    return [], "none", dirty


def _dirty_worktree_check(dirty_files: list[str]) -> dict[str, Any]:
    if not dirty_files:
        return {"status": "PASS", "details": "No baseline dirty files detected.", "dirty_files": []}
    return {
        "status": "WARN",
        "details": "Pre-existing dirty files detected in baseline.",
        "dirty_files": sorted(dirty_files),
    }


def _task_scope_hints(tasks: list[dict[str, Any]]) -> set[str]:
    hints: set[str] = set()
    for task in tasks:
        scope = str(task.get("scope") or "")
        for match in PATH_HINT_RE.findall(scope):
            normalized = _normalize_relpath(match)
            if normalized:
                hints.add(normalized)
                hints.update(_derived_test_hints(normalized))
    return hints


def _derived_test_hints(path_hint: str) -> set[str]:
    normalized = _normalize_relpath(path_hint)
    stem = Path(normalized).stem
    values = {
        f"tests/test_{stem}.py",
    }
    if normalized.startswith("src/"):
        values.add("tests/")
    return {item for item in values if item}


def _scope_drift_check(
    *,
    planning_dir: Path,
    changed_files: list[str],
    additional_allowed: list[str],
) -> dict[str, Any]:
    scope_source = "TASKS.md"
    if _planning_artifact_mode(planning_dir) == "contract_first":
        scope_hints, scope_source = _contract_scope_hints(planning_dir)
    else:
        # Include completed tasks so upstream/finished scope remains allowed;
        # changed_files accumulates across the whole workflow, not just the
        # remaining backlog.
        tasks = parse_task_backlog(planning_dir / "TASKS.md", include_completed=True)
        scope_hints = sorted(_task_scope_hints(tasks))
    allowed = set(scope_hints)
    for item in additional_allowed:
        normalized = _normalize_relpath(item)
        if normalized:
            allowed.add(normalized)
    if not allowed:
        return {
            "status": "WARN",
            "allowed_hints": [],
            "out_of_scope_files": [],
            "scope_source": scope_source or "unknown",
            "details": (
                "No explicit file-scope hints found in contract-first planning artifacts; drift check skipped."
                if _planning_artifact_mode(planning_dir) == "contract_first"
                else "No explicit file-scope hints found in TASKS.md; drift check skipped."
            ),
        }
    payload = contract_scope_drift(changed_files, sorted(allowed))
    out_of_scope = list(payload.get("out_of_scope_files") or [])
    return {
        "status": "FAIL" if out_of_scope else "PASS",
        "allowed_hints": sorted(allowed),
        "out_of_scope_files": out_of_scope,
        "scope_source": scope_source or "TASKS.md",
        "details": (
            "Changed files exceed contract-first task scope."
            if out_of_scope and _planning_artifact_mode(planning_dir) == "contract_first"
            else "Changed files exceed task scope hints."
            if out_of_scope
            else "All changed files match task scope hints."
        ),
    }


def _is_source_python_file(path: str) -> bool:
    normalized = _normalize_relpath(path)
    if not normalized.endswith(".py"):
        return False
    name = Path(normalized).name
    lowered = normalized.lower()
    if name.startswith("test_"):
        return False
    if lowered.startswith("tests/") or "/tests/" in lowered:
        return False
    if lowered.startswith("docs/") or lowered.startswith("planning/"):
        return False
    return True


def _missing_tests_check(changed_files: list[str]) -> dict[str, Any]:
    normalized = [_normalize_relpath(item) for item in changed_files]
    changed_source = [item for item in normalized if _is_source_python_file(item)]
    changed_tests = [
        item
        for item in normalized
        if item.startswith("tests/") or "/tests/" in item or Path(item).name.startswith("test_")
    ]
    if changed_source and not changed_tests:
        return {
            "status": "FAIL",
            "changed_source_files": changed_source,
            "changed_test_files": [],
            "details": "Source files changed without scoped test updates.",
        }
    return {
        "status": "PASS",
        "changed_source_files": changed_source,
        "changed_test_files": changed_tests,
        "details": "Scoped tests are present for changed source files." if changed_source else "No source changes detected.",
    }


def _checkbox_completion(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"total": 0, "done": 0, "status": "MISSING"}
    total = 0
    done = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        match = CHECKBOX_RE.match(line)
        if match is None:
            continue
        total += 1
        if str(match.group(1)).strip().lower() == "x":
            done += 1
    status = "COMPLETE" if total > 0 and done == total else "INCOMPLETE"
    if total == 0:
        status = "EMPTY"
    return {"total": total, "done": done, "status": status}


def _completion_summary(tasks: dict[str, Any], acceptance: dict[str, Any]) -> dict[str, Any]:
    complete = tasks.get("status") == "COMPLETE" and acceptance.get("status") == "COMPLETE"
    status = "PASS" if complete else "WARN"
    return {
        "status": status,
        "tasks": tasks,
        "acceptance": acceptance,
        "details": "Checklist complete." if complete else "Checklist not fully complete.",
    }


def _review_summary(
    *,
    status: str,
    scope_status: str,
    tests_status: str,
    completion_status: str,
    dirty_status: str,
) -> str:
    return (
        f"review={status}; scope_drift={scope_status}; "
        f"missing_tests={tests_status}; dirty_worktree={dirty_status}; checklist={completion_status}"
    )


def _review_runtime_semantics(workflow_chain: dict[str, Any]) -> dict[str, Any]:
    upstream = dict(workflow_chain.get("upstream") or {})
    runtime = dict(upstream.get("peer_review_runtime") or {})
    raw_mode = str(runtime.get("mode") or "").strip()
    runtime_classification = classify_review_runtime(
        runtime,
        require_real_peer_review=bool(runtime.get("real_required")),
    )
    return {
        "review_mode": "real_peer_review" if runtime_classification.is_real_review else "simulated",
        "review_runtime_mode_raw": raw_mode,
        "real_review_requested": bool(runtime.get("real_requested")),
        "real_review_required": bool(runtime.get("real_required")),
        "fallback_used": bool(runtime.get("fallback_used")),
        "review_quality": runtime_classification.review_quality,
        "semantic_review_performed": runtime_classification.semantic_review_performed,
    }


def _write_review_markdown(path: Path, payload: dict[str, Any]) -> None:
    checks = dict(payload.get("checks") or {})
    scope = dict(checks.get("scope_drift") or {})
    tests = dict(checks.get("missing_tests") or {})
    execution = dict(checks.get("execution") or {})
    dirty = dict(checks.get("dirty_worktree") or {})
    completion = dict(checks.get("checklist_completion") or {})
    boundary_debt = dict(checks.get("layer_boundary_debt") or {})
    changed = dict(payload.get("changed_files") or {})
    execution_guard = dict(payload.get("execution_guard") or {})
    lines = [
        f"# REVIEW ({payload.get('feature', '')})",
        "",
        "## Diff Input",
        f"- base_branch: {payload.get('base_branch', '')}",
        f"- changed_files_source: {changed.get('source', '')}",
        f"- changed_files_count: {changed.get('count', 0)}",
        f"- execution_status: {payload.get('execution_status', '')}",
        f"- execution_source: {payload.get('execution_source', '')}",
        f"- execution_backend: {payload.get('execution_backend', '')}",
        f"- execution_guard_action: {execution_guard.get('action', '')}",
        f"- execution_guard_policy: {execution_guard.get('policy', '')}",
        f"- execution_guard_pattern: {execution_guard.get('pattern', '')}",
        f"- execution_guard_command: {execution_guard.get('command', '')}",
        f"- review_evidence_status: {payload.get('review_evidence_status', '')}",
        f"- review_evidence_source: {payload.get('review_evidence_source', '')}",
        "",
        "## Findings",
        f"- overall_status: {payload.get('status', '')}",
        f"- scope_drift: {scope.get('status', '')}",
        f"- missing_tests: {tests.get('status', '')}",
        f"- execution: {execution.get('status', '')}",
        f"- dirty_worktree: {dirty.get('status', '')}",
        f"- checklist_completion: {completion.get('status', '')}",
        f"- layer_boundary_debt: {boundary_debt.get('status', '')}",
        "",
        "## Changed Files",
    ]
    changed_items = list(changed.get("items") or [])
    if changed_items:
        lines.extend(f"- {item}" for item in changed_items)
    else:
        lines.append("- (none)")
    out_of_scope = list(scope.get("out_of_scope_files") or [])
    lines.extend(["", "## Scope Drift"])
    if out_of_scope:
        lines.extend(f"- {item}" for item in out_of_scope)
    else:
        lines.append("- (none)")
    lines.extend(["", "## Dirty Worktree"])
    dirty_files = list(dirty.get("dirty_files") or [])
    if dirty_files:
        lines.extend(f"- {item}" for item in dirty_files)
    else:
        lines.append(f"- {dirty.get('details', '(none)')}")
    lines.extend(["", "## Layer Boundary Debt"])
    debt_items = list(boundary_debt.get("items") or [])
    if debt_items:
        for item in debt_items:
            record = dict(item) if isinstance(item, dict) else {}
            detail = (
                f"- {record.get('file', '')}: "
                f"severity={record.get('severity', '')}; "
                f"layers={', '.join(record.get('layers') or [])}; "
                f"tasks={', '.join(record.get('tasks') or [])}; "
                f"recommended_split={'; '.join(record.get('recommended_split') or [])}"
            )
            lines.append(detail)
    else:
        lines.append(f"- {boundary_debt.get('details', '(none)')}")
    provenance = dict(payload.get("provenance") or {})
    lines.extend(
        [
            "",
            "## Mirror Provenance",
            "- source_json: .review_result.json",
            f"- digest_algorithm: {payload.get('digest_algorithm', '')}",
            f"- payload_digest: {payload.get('payload_digest', '')}",
            f"- generated_at: {payload.get('generated_at', '')}",
            f"- provenance.command: {provenance.get('command', '')}",
            f"- provenance.planning_dir: {provenance.get('planning_dir', '')}",
        ]
    )
    atomic_write_text(path, "\n".join(lines) + "\n")


def build_review_report(
    *,
    project_root: Path,
    planning_dir: Path,
    feature: str,
    base_branch: str,
    changed_files_override: list[str] | None = None,
    scope_allow: list[str] | None = None,
) -> dict[str, Any]:
    planning_dir.mkdir(parents=True, exist_ok=True)
    planning_mode = _planning_artifact_mode(planning_dir)
    state = _load_json_dict(planning_dir / ".autopilot_state.json")
    workflow_chain = load_workflow_chain_snapshot(planning_dir) or {}
    semantic_compact = _load_json_dict(planning_dir / "semantic_compact.json")
    gate_payload = _load_json_dict(planning_dir / ".gate_result.json")
    changed_files, changed_source, dirty_files = _resolve_review_changed_files(
        project_root=project_root,
        planning_dir=planning_dir,
        state_payload=state,
        base_branch=base_branch,
        changed_files_override=changed_files_override,
    )
    scope_result = _scope_drift_check(
        planning_dir=planning_dir,
        changed_files=changed_files,
        additional_allowed=scope_allow or [],
    )
    tests_result = _missing_tests_check(changed_files)
    if planning_mode == "contract_first":
        completion = _contract_first_completion_summary(planning_dir)
    else:
        tasks_completion = _checkbox_completion(planning_dir / "TASKS.md")
        acceptance_completion = _checkbox_completion(planning_dir / "ACCEPTANCE.md")
        completion = _completion_summary(tasks_completion, acceptance_completion)
    layer_boundary_debt = _contract_layer_boundary_debt(planning_dir) if planning_mode == "contract_first" else {
        "status": "PASS",
        "details": "Layer-boundary debt only applies to contract-first task graph.",
        "items": [],
    }
    execution_check = _resolve_execution_check(planning_dir)
    dirty_check = _dirty_worktree_check(dirty_files)
    review_evidence = _review_evidence(
        planning_dir=planning_dir,
        workflow_chain=workflow_chain,
        semantic_compact=semantic_compact,
        gate_payload=gate_payload,
    )
    review_runtime = _review_runtime_semantics(workflow_chain)
    failing_checks: list[tuple[str, dict[str, Any]]] = []
    for name, result in (
        ("scope_drift", scope_result),
        ("missing_tests", tests_result),
        ("dirty_worktree", dirty_check),
    ):
        if str(result.get("status") or "").upper() == "FAIL":
            failing_checks.append((name, result))
    blocked = bool(failing_checks)
    status = "BLOCKED" if blocked else "PASS"
    summary = _review_summary(
        status=status,
        scope_status=scope_result["status"],
        tests_status=tests_result["status"],
        completion_status=completion["status"],
        dirty_status=dirty_check["status"],
    )
    blocking_reason = ""
    remediation: list[str] = []
    if blocked:
        first_name, first_result = failing_checks[0]
        blocking_reason = f"{first_name}: {first_result.get('details', '')}".strip(": ")
        if any(name == "scope_drift" for name, _ in failing_checks):
            scope_source = str(scope_result.get("scope_source") or "TASKS.md")
            remediation.append(f"Keep changes inside {scope_source} scope hints or add explicit --scope-allow entries.")
        if any(name == "missing_tests" for name, _ in failing_checks):
            remediation.append("Add or update scoped tests for every changed source file before rerunning review.")
        if any(name == "dirty_worktree" for name, _ in failing_checks):
            remediation.append("Clean the pre-existing dirty worktree files or isolate them before rerunning review.")
    payload = {
        "status": status,
        "entrypoint": "kodawari review",
        "feature": feature,
        "planning_dir": str(planning_dir),
        "planning_artifact_mode": planning_mode,
        "base_branch": base_branch,
        "changed_files": {
            "source": changed_source,
            "items": changed_files,
            "count": len(changed_files),
        },
        "checks": {
            "scope_drift": scope_result,
            "missing_tests": tests_result,
            "checklist_completion": completion,
            "layer_boundary_debt": layer_boundary_debt,
            "execution": execution_check,
            "dirty_worktree": dirty_check,
        },
        "execution_status": str(execution_check.get("execution_status") or "").upper(),
        "execution_source": str(execution_check.get("source") or ""),
        "execution_backend": str(execution_check.get("backend") or ""),
        "execution_guard": dict(execution_check.get("execution_guard") or {}),
        "review_mode": review_runtime["review_mode"],
        "review_runtime_mode_raw": review_runtime["review_runtime_mode_raw"],
        "real_review_requested": review_runtime["real_review_requested"],
        "real_review_required": review_runtime["real_review_required"],
        "fallback_used": review_runtime["fallback_used"],
        "review_quality": review_runtime["review_quality"],
        "semantic_review_performed": review_runtime["semantic_review_performed"],
        "review_evidence_status": review_evidence["review_evidence_status"],
        "review_evidence_source": review_evidence["review_evidence_source"],
        "explicit_review_evidence": review_evidence["explicit_review_evidence"],
        "review_evidence": review_evidence,
        "summary": summary,
        "blocking_reason": blocking_reason,
        "remediation": remediation,
        "next_action": "" if not blocked else remediation[0],
        "generated_at": _utc_now_iso(),
        "provenance": _build_cli_provenance(
            command="review",
            project_root=project_root,
            planning_dir=planning_dir,
        ),
    }
    _attach_payload_digest(payload)
    _ensure_design_artifact(planning_dir=planning_dir, feature=feature, state_payload=state)
    _write_json(planning_dir / ".review_result.json", payload)
    _write_review_markdown(planning_dir / "REVIEW.md", payload)
    return payload


__all__ = ["build_review_report"]

