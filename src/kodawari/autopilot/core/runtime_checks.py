"""Runtime verify/gate helpers for the merged workflow chain."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from kodawari.gate import GateEngine, get_profile
from kodawari.autopilot.execution.verify_execution import maybe_execute_verify_command
from kodawari.autopilot.execution.verify_targeting import resolve_verify_targeting


def _no_fake_run_strict() -> bool:
    """No-fake-run policy gate: production strict mode is on when the
    operator explicitly opted in to real peer review and we are not
    running under pytest. Mirrors the Fix 3 gate in engine_review_mixin
    so the policy fires consistently across reviewer + verify paths."""
    review_enabled = str(os.environ.get("WORKFLOW_REVIEW_ENABLED", "")).strip().lower() in {
        "1", "true", "yes", "on",
    }
    if not review_enabled:
        return False
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    test_mode = str(os.environ.get("WORKFLOW_SDK_TEST_MODE", "")).strip().lower() in {
        "1", "true", "yes", "on",
    }
    if test_mode:
        return False
    return True


def build_verify_check(
    *,
    project_root: Path,
    feature: str,
    task_label: str,
    verify_cmd: str,
    changed_files: list[str],
    qa_payload: dict[str, Any] | None,
    instinct_hints: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    verify_targeting = resolve_verify_targeting(
        project_root=project_root,
        verify_cmd=verify_cmd,
        changed_files=changed_files,
        feature=feature,
        task_label=task_label,
        instinct_hints=instinct_hints,
    )
    command_payload = _execute_verify_command(
        project_root=project_root,
        feature=feature,
        task_label=task_label,
        changed_files=changed_files,
        verify_targeting=verify_targeting,
    )
    if command_payload is not None:
        return _merge_verify_targeting(command_payload, verify_targeting)
    return _build_compat_verify_payload(
        feature=feature,
        task_label=task_label,
        changed_files=changed_files,
        qa_payload=qa_payload,
        verify_targeting=verify_targeting,
    )


def _execute_verify_command(
    *,
    project_root: Path,
    feature: str,
    task_label: str,
    changed_files: list[str],
    verify_targeting: dict[str, Any],
) -> dict[str, Any] | None:
    return maybe_execute_verify_command(
        project_root=project_root,
        feature=feature,
        task_label=task_label,
        verify_cmd=str(verify_targeting["verify_cmd_resolved"]),
        changed_files=changed_files,
        # Plumb the resolved verify_targets + source so _execution_mode can
        # honor verify_targeting's smarter resolution instead of doing its own
        # (naive) check of changed_files. Without these, T4-style code-only
        # rounds whose matched test file lives under a different name
        # (test_api.py for app/main.py) silently skipped verify.
        verify_targets=list(verify_targeting.get("verify_targets") or []),
        verify_target_source=str(verify_targeting.get("verify_target_source") or ""),
    )


def _merge_verify_targeting(
    payload: dict[str, Any],
    verify_targeting: dict[str, Any],
) -> dict[str, Any]:
    payload["verify_cmd"] = str(verify_targeting["verify_cmd"])
    payload["verify_cmd_resolved"] = str(verify_targeting["verify_cmd_resolved"])
    payload["verify_target_source"] = str(verify_targeting["verify_target_source"])
    payload["verify_targets"] = list(verify_targeting["verify_targets"])
    instinct_patterns = list(verify_targeting.get("instinct_patterns") or [])
    if instinct_patterns:
        payload["instinct_patterns"] = [str(item) for item in instinct_patterns if str(item).strip()]
    instinct_reason = str(verify_targeting.get("instinct_reason") or "").strip()
    if instinct_reason:
        payload["instinct_reason"] = instinct_reason
    keyword_expression = str(verify_targeting.get("verify_keyword_expression") or "").strip()
    if keyword_expression:
        payload["verify_keyword_expression"] = keyword_expression
    keyword_source = str(verify_targeting.get("verify_keyword_source") or "").strip()
    if keyword_source:
        payload["verify_keyword_source"] = keyword_source
    keyword_match_count = int(verify_targeting.get("verify_keyword_match_count") or 0)
    if keyword_match_count > 0:
        payload["verify_keyword_match_count"] = keyword_match_count
    return payload


def _build_compat_verify_payload(
    *,
    feature: str,
    task_label: str,
    changed_files: list[str],
    qa_payload: dict[str, Any] | None,
    verify_targeting: dict[str, Any],
) -> dict[str, Any]:
    payload = dict(qa_payload or {})
    artifacts = _verify_artifacts(payload, changed_files)
    status = _verify_status(payload)
    summary = _verify_summary(payload)
    # No-fake-run policy Fix 10 (gated): this payload is built when
    # verify_cmd could not be executed (command_executed=False below).
    # Previously passed=True if the upstream post_execution_qa status
    # happened to be "PASS" — but that "PASS" came from heuristic
    # file-presence checks, not a real test run. The gate treated this
    # as verify-evidence and proceeded. Now: when production strict is
    # opted-in (WORKFLOW_REVIEW_ENABLED=1 AND non-test env), passed=False
    # so the gate surfaces "no_verify_command" rather than silently
    # accept. Dev/subscription-mode runs (without WORKFLOW_REVIEW_ENABLED)
    # keep the old behavior so local iteration without verify_cmd still
    # works.
    production_strict = _no_fake_run_strict()
    response = {
        "feature": feature,
        "task_label": task_label,
        "status": "NO_VERIFY_COMMAND" if production_strict else status,
        "passed": False if production_strict else (status == "PASS"),
        "mode": "compat_post_execution_qa",
        "source": "post_execution_qa",
        "verify_cmd": str(verify_targeting["verify_cmd"]),
        "verify_cmd_resolved": str(verify_targeting["verify_cmd_resolved"]),
        "verify_target_source": str(verify_targeting["verify_target_source"]),
        "verify_targets": list(verify_targeting["verify_targets"]),
        "artifacts": artifacts,
        "summary": summary,
        "blocking_reason": _verify_blocking_reason(status, summary),
        "command_executed": False,
        "returncode": None,
        "stdout_excerpt": "",
        "stderr_excerpt": "",
    }
    instinct_patterns = list(verify_targeting.get("instinct_patterns") or [])
    if instinct_patterns:
        response["instinct_patterns"] = [str(item) for item in instinct_patterns if str(item).strip()]
    instinct_reason = str(verify_targeting.get("instinct_reason") or "").strip()
    if instinct_reason:
        response["instinct_reason"] = instinct_reason
    keyword_expression = str(verify_targeting.get("verify_keyword_expression") or "").strip()
    if keyword_expression:
        response["verify_keyword_expression"] = keyword_expression
    keyword_source = str(verify_targeting.get("verify_keyword_source") or "").strip()
    if keyword_source:
        response["verify_keyword_source"] = keyword_source
    keyword_match_count = int(verify_targeting.get("verify_keyword_match_count") or 0)
    if keyword_match_count > 0:
        response["verify_keyword_match_count"] = keyword_match_count
    return response


def evaluate_runtime_gate(
    *,
    project_root: Path,
    changed_files: list[str],
    profile_name: str = "blocking",
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    scoped_targets = _existing_python_targets(root, changed_files)
    if not scoped_targets:
        profile = get_profile(profile_name)
        return {
            "profile": profile.to_dict(),
            "total_status": "PASS",
            "scanned_files": 0,
            "total_violations": 0,
            "max_violations": profile.thresholds.max_violations,
            "blocking_violations": 0,
            "items": [],
            "source": "runtime_loop",
            "scope": "changed_files",
            "targets": [],
            "passed": True,
            "blocking_reason": "",
            "reason": "no_existing_changed_python_files",
        }
    payload = GateEngine(project_root=root).evaluate(
        targets=scoped_targets,
        profile_name=profile_name,
    ).to_dict()
    payload["source"] = "runtime_loop"
    payload["scope"] = "changed_files"
    payload["targets"] = [str(path) for path in scoped_targets]
    payload["passed"] = str(payload.get("total_status") or "").upper() != "BLOCKED"
    payload["blocking_reason"] = gate_blocking_reason(payload)
    return payload


def gate_blocking_reason(payload: dict[str, Any]) -> str:
    if str(payload.get("total_status") or "").upper() != "BLOCKED":
        return ""
    first = next(_gate_violation_messages(payload), "")
    if first:
        return first
    return "Runtime gate blocked"


def gate_must_fix_items(payload: dict[str, Any], *, limit: int = 3) -> list[str]:
    fixes = _limited_violation_messages(payload, limit)
    if fixes:
        return fixes
    blocking = gate_blocking_reason(payload)
    return [blocking] if blocking else []


def _verify_artifacts(payload: dict[str, Any], changed_files: list[str]) -> list[str]:
    return _string_list(payload.get("artifacts")) or list(changed_files)


def _verify_status(payload: dict[str, Any]) -> str:
    return str(payload.get("status") or "PASS").strip().upper() or "PASS"


def _verify_summary(payload: dict[str, Any]) -> str:
    return str(payload.get("summary") or "").strip()


def _verify_blocking_reason(status: str, summary: str) -> str:
    if status == "PASS":
        return ""
    if summary:
        return summary
    return f"Verify stage returned {status}"


def _gate_violation_messages(payload: dict[str, Any]):
    for violation in _gate_violations(payload):
        message = _violation_message(violation)
        if message:
            yield message


def _gate_violations(payload: dict[str, Any]):
    for item in _gate_checker_items(payload):
        for violation in _checker_violations(item):
            yield violation


def _gate_checker_items(payload: dict[str, Any]):
    for item in list(payload.get("items") or []):
        if isinstance(item, dict):
            yield item


def _checker_violations(item: dict[str, Any]):
    for violation in list(item.get("violations") or []):
        if isinstance(violation, dict):
            yield violation


def _violation_message(violation: dict[str, Any]) -> str:
    message = str(violation.get("message") or "").strip()
    path = str(violation.get("path") or "").strip()
    base = f"{path}: {message}" if message and path else message
    hint = _remediation_hint(violation)
    return f"{base} Remediation: {hint}" if hint and base else base


def _remediation_hint(violation: dict[str, Any]) -> str:
    metric = str(violation.get("metric") or "").strip().lower()
    if metric == "complexity":
        return (
            "Extract the function into 2-3 smaller helpers, "
            "each with a single responsibility (one per input type or major branch)."
        )
    if metric == "nesting":
        return "Flatten nested conditionals using early returns or guard clauses."
    return ""


def _limited_violation_messages(payload: dict[str, Any], limit: int) -> list[str]:
    resolved_limit = max(1, int(limit))
    fixes: list[str] = []
    for message in _gate_violation_messages(payload):
        fixes.append(message)
        if len(fixes) >= resolved_limit:
            break
    return fixes


def _existing_python_targets(project_root: Path, changed_files: list[str]) -> list[Path]:
    targets: list[Path] = []
    for raw in changed_files:
        candidate = (project_root / str(raw)).resolve()
        if candidate.suffix != ".py":
            continue
        if candidate.exists():
            targets.append(candidate)
    return sorted({path for path in targets})


def _string_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    return [str(item) for item in values if str(item).strip()]

