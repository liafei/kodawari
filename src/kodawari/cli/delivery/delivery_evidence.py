"""Shared runtime evidence helpers for delivery workflow."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from kodawari.autopilot.execution.execution_backend import execution_backend_descriptor
from kodawari.cli.delivery.delivery_common import (
    _load_contract_compliance_report,
    _load_legacy_review_evidence,
    _load_review_evidence_artifact_payload,
    _planning_artifact_mode,
    _task_run_payload,
)
from kodawari.cli.evidence.review_evidence_artifact import coerce_review_evidence_payload


def _load_json_dict(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _resolve_gate_from_autopilot_rounds(planning_dir: Path) -> dict[str, Any] | None:
    # Autopilot's RULES_GATE round runs the real gate check but does not
    # write .gate_result.json (that artifact comes from `kodawari gate`
    # or contract-first task-run). When neither canonical artifact is
    # present, fall back to the rounds log so release_tail's QA sub-check
    # reflects the actual gate outcome instead of "gate result unavailable".
    # This is a READ-side fallback only; it does not create new artifacts.
    rounds_path = planning_dir / ".autopilot_rounds.jsonl"
    if not rounds_path.exists():
        return None
    try:
        lines = rounds_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    _gate_stages = {"RULES_GATE", "PROCEED_TO_GATE"}
    for line in reversed(lines):
        try:
            record = json.loads(line.strip())
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        if str(record.get("stage") or "").upper() not in _gate_stages:
            continue
        gate = dict((record.get("details") or {}).get("gate_check") or {})
        status = str(gate.get("total_status") or "").upper()
        if not status:
            continue
        return {
            "status": "PASS" if status == "PASS" else "FAIL",
            "gate_status": status,
            "source": ".autopilot_rounds.jsonl",
            "reason": "" if status == "PASS" else str(gate.get("blocking_reason") or status),
        }
    return None


def _review_required_flag(checks: dict[str, Any], name: str) -> bool:
    value = checks.get(name)
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"", "0", "false", "no"}


def _resolve_gate_check(*, planning_dir: Path, gate_payload: dict[str, Any] | None) -> dict[str, Any]:
    direct_gate = dict(gate_payload or {})
    gate_status = str(direct_gate.get("total_status") or "").upper()
    if gate_status:
        return {
            "status": "PASS" if gate_status == "PASS" else "FAIL",
            "gate_status": gate_status,
            "source": ".gate_result.json",
            "reason": "" if gate_status == "PASS" else gate_status,
        }
    task_run = _task_run_payload(planning_dir) or {}
    task_run_gate = dict(task_run.get("gate_check") or {})
    task_run_status = str(task_run_gate.get("total_status") or "").upper()
    if task_run_status:
        return {
            "status": "PASS" if task_run_status == "PASS" else "FAIL",
            "gate_status": task_run_status,
            "source": ".task_run_result.json.gate_check",
            "reason": "" if task_run_status == "PASS" else str(task_run_gate.get("details") or task_run_status),
        }
    compliance = _load_contract_compliance_report(planning_dir) or {}
    compliance_status = str(compliance.get("status") or "").upper()
    if compliance_status:
        return {
            "status": "PASS" if compliance_status == "PASS" else "FAIL",
            "gate_status": compliance_status,
            "source": "COMPLIANCE_REPORT.json",
            "reason": "" if compliance_status == "PASS" else "contract-first compliance report failed",
        }
    rounds_result = _resolve_gate_from_autopilot_rounds(planning_dir)
    if rounds_result:
        return rounds_result
    if _planning_artifact_mode(planning_dir) == "contract_first":
        return {
            "status": "FAIL",
            "gate_status": "MISSING",
            "source": ".task_run_result.json/COMPLIANCE_REPORT.json",
            "reason": "gate artifact not generated for contract-first flow",
        }
    return {
        "status": "FAIL",
        "gate_status": "UNKNOWN",
        "source": "",
        "reason": "gate result unavailable",
    }


def _must_fix_items(semantic_compact: dict[str, Any] | None) -> list[str]:
    values = list((semantic_compact or {}).get("must_fix") or [])
    return [str(item) for item in values if str(item).strip()]


def _int_value(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _runtime_review_snapshot(workflow_chain: dict[str, Any]) -> dict[str, Any]:
    upstream = dict(workflow_chain.get("upstream") or {})
    peer_summary = dict(upstream.get("peer_review_summary") or {})
    approvals = dict(upstream.get("approvals") or {})
    loop_outcome = dict(upstream.get("loop_outcome") or {})
    must_fix_remaining = _int_value(peer_summary.get("must_fix_remaining"), -1)
    if must_fix_remaining < 0:
        must_fix_remaining = _int_value(loop_outcome.get("must_fix_remaining"), -1)
    return {
        "approved": bool(peer_summary.get("approved")) or bool(approvals.get("peer_review")),
        "review_count": max(
            _int_value(peer_summary.get("review_count")),
            _int_value(peer_summary.get("review_round")),
            _int_value(loop_outcome.get("review_rounds_used")),
        ),
        "must_fix_remaining": must_fix_remaining,
        "reason": str(upstream.get("reason") or loop_outcome.get("reason") or "").strip().upper(),
    }


def _review_evidence_check(review_evidence_payload: dict[str, Any]) -> dict[str, str]:
    status = str(review_evidence_payload.get("status") or "").strip().upper()
    checks = dict(review_evidence_payload.get("checks") or {})
    self_reviews = int(checks.get("self_review_count") or 0)
    peer_reviews = int(checks.get("peer_review_count") or 0)
    require_self_reviews = _review_required_flag(checks, "required_self_review")
    require_peer_reviews = _review_required_flag(checks, "required_peer_review")
    blocking_reason = str(review_evidence_payload.get("blocking_reason") or "").strip()
    issues = [str(item) for item in list(review_evidence_payload.get("issues") or []) if str(item).strip()]
    details = blocking_reason or "; ".join(issues)
    if status != "PASS":
        return {"status": "FAIL", "details": details or "Review evidence missing."}
    if require_self_reviews and self_reviews <= 0:
        return {"status": "FAIL", "details": "Missing self-review evidence."}
    if require_peer_reviews and peer_reviews <= 0:
        return {"status": "FAIL", "details": "Missing peer-review evidence."}
    if issues:
        return {"status": "FAIL", "details": details}
    if not details:
        details = (
            "Dual-review evidence present."
            if require_self_reviews and require_peer_reviews
            else "Review evidence present."
        )
    return {"status": "PASS", "details": details}


def _needs_review_evidence_reconciliation(
    *,
    planning_dir: Path,
    review_evidence_payload: dict[str, Any],
    workflow_chain: dict[str, Any],
    review_payload: dict[str, Any],
) -> bool:
    checks = dict(review_evidence_payload.get("checks") or {})
    if not review_evidence_payload:
        return False
    evidence_status = str(review_evidence_payload.get("status") or "").strip().upper()
    if evidence_status == "PASS":
        return False
    current_review = _runtime_review_snapshot(workflow_chain)
    current_review_ready = (
        bool(current_review["approved"])
        and _int_value(current_review["review_count"]) > 0
        and _int_value(current_review["must_fix_remaining"]) == 0
        and str(current_review["reason"]) in {"PROCEED_TO_GATE", "PIPELINE_FINISH"}
    )
    if current_review_ready:
        return True
    if "required_self_review" in checks and "required_peer_review" in checks:
        return False
    execution_payload = _load_json_dict(planning_dir / ".execution_result.json")
    upstream = dict(workflow_chain.get("upstream") or {})
    execution_status = str(
        execution_payload.get("status")
        or review_payload.get("execution_status")
        or upstream.get("status")
        or ""
    ).strip().upper()
    return execution_status in {"BLOCKED", "FAIL", "ERROR"}


def _reconcile_review_evidence_payload(
    *,
    planning_dir: Path,
    review_evidence_payload: dict[str, Any],
    workflow_chain: dict[str, Any],
    review_payload: dict[str, Any],
) -> dict[str, Any]:
    checks = dict(review_evidence_payload.get("checks") or {})
    execution_payload = _load_json_dict(planning_dir / ".execution_result.json")
    upstream = dict(workflow_chain.get("upstream") or {})
    peer_runtime = dict(upstream.get("peer_review_runtime") or {})
    current_review = _runtime_review_snapshot(workflow_chain)
    self_reviews = _int_value(checks.get("self_review_count"))
    peer_reviews = max(
        _int_value(checks.get("peer_review_count")),
        _int_value(current_review["review_count"]),
    )
    must_fix_remaining = (
        _int_value(current_review["must_fix_remaining"])
        if _int_value(current_review["must_fix_remaining"], -1) >= 0
        else _int_value(checks.get("must_fix_remaining"))
    )
    execution_status = str(
        execution_payload.get("status")
        or review_payload.get("execution_status")
        or upstream.get("status")
        or ""
    ).strip().upper()
    loop_reason = str(upstream.get("reason") or "").strip().upper()
    execution_backend = str(
        execution_payload.get("backend") or review_payload.get("execution_backend") or ""
    ).strip().lower()
    descriptor = execution_backend_descriptor(execution_backend) if execution_backend else None
    review_stage_blocked = execution_status in {"BLOCKED", "FAIL", "ERROR"} or loop_reason not in {"", "PROCEED_TO_GATE", "PIPELINE_FINISH"}
    require_self_review = False if review_stage_blocked else bool(descriptor and descriptor.self_review_selectable)
    require_peer_review = False if review_stage_blocked else bool(
        peer_runtime.get("real_requested")
        or peer_runtime.get("real_required")
        or peer_reviews > 0
        or workflow_chain.get("peer_review_enabled")
        or upstream.get("peer_review_enabled")
    )

    issues: list[str] = []
    if require_self_review and self_reviews <= 0:
        issues.append("Missing self-review evidence.")
    if require_peer_review and peer_reviews <= 0:
        issues.append("Missing peer-review evidence.")
    if require_peer_review and peer_reviews > 0 and not bool(current_review["approved"]):
        issues.append("Peer review is not approved.")
    if must_fix_remaining > 0:
        issues.append("Must-fix items are still open.")

    normalized = dict(review_evidence_payload)
    normalized["status"] = "PASS" if not issues else "FAIL"
    normalized["blocking_reason"] = "" if not issues else issues[0]
    normalized["details"] = (
        "Review evidence contract reconciled against current execution/review runtime."
        if not issues
        else issues[0]
    )
    normalized["issues"] = issues
    normalized["checks"] = {
        **checks,
        "self_review_count": self_reviews,
        "peer_review_count": peer_reviews,
        "must_fix_remaining": must_fix_remaining,
        "required_self_review": require_self_review,
        "required_peer_review": require_peer_review,
        "execution_backend": execution_backend,
        "execution_status": execution_status,
    }
    normalized["evidence"] = [
        *list(normalized.get("evidence") or []),
        {
            "file": ".workflow_chain.json",
            "rule": "review_evidence.reconciled_peer_review",
            "hit": f"peer review approved={bool(current_review['approved'])}; count={peer_reviews}; must_fix_remaining={must_fix_remaining}",
            "confidence": 1.0,
        },
    ]
    normalized["contract_reconciled"] = True
    return (
        coerce_review_evidence_payload(
            normalized,
            source=f"reconciled:{str(review_evidence_payload.get('source') or '.review_evidence.json')}",
            explicit=bool(review_evidence_payload.get("explicit", True)),
        )
        or review_evidence_payload
    )


def _summary_review_evidence_payload(
    *,
    final_review: dict[str, Any],
    semantic_compact: dict[str, Any] | None,
    gate_status: str,
) -> dict[str, Any]:
    final_status = str(final_review.get("status") or "UNKNOWN").upper()
    must_fix_open_items = _must_fix_items(semantic_compact)
    passed = final_status == "PASS" and not must_fix_open_items and gate_status == "PASS"
    details = (
        "Dual-review evidence derived from review summary fallback."
        if passed
        else "Dual-review evidence artifact unavailable."
    )
    checks = {
        "self_review_count": 1 if passed else 0,
        "peer_review_count": 1 if passed else 0,
        "must_fix_remaining": len(must_fix_open_items),
    }
    issues = [] if passed else ["Dual-review evidence artifact unavailable."]
    return coerce_review_evidence_payload(
        {
            "status": "PASS" if passed else "FAIL",
            "blocking_reason": "" if passed else details,
            "issues": issues,
            "checks": checks,
            "evidence": [],
        },
        source="summary_fallback",
        explicit=False,
    ) or {
        "status": "FAIL",
        "blocking_reason": "Dual-review evidence artifact unavailable.",
        "issues": ["Dual-review evidence artifact unavailable."],
        "checks": checks,
        "evidence": [],
        "source": "summary_fallback",
        "explicit": False,
    }


def _review_evidence(
    *,
    planning_dir: Path | None,
    workflow_chain: dict[str, Any],
    semantic_compact: dict[str, Any] | None,
    gate_payload: dict[str, Any] | None,
    review_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    final_review = dict(workflow_chain.get("final_quality_review") or {})
    review_payload = review_payload or {}
    review_evidence_payload: dict[str, Any] = {}
    resolved_gate = (
        _resolve_gate_check(planning_dir=planning_dir, gate_payload=gate_payload)
        if planning_dir is not None
        else {"gate_status": str((gate_payload or {}).get("total_status") or "UNKNOWN").upper()}
    )
    if planning_dir is not None:
        review_evidence_payload = _load_review_evidence_artifact_payload(planning_dir) or {}
    if not review_evidence_payload and planning_dir is not None:
        review_evidence_payload = _load_legacy_review_evidence(
            planning_dir=planning_dir,
            review_payload=review_payload,
        ) or {}
    if (
        planning_dir is not None
        and review_evidence_payload
        and _needs_review_evidence_reconciliation(
            planning_dir=planning_dir,
            review_evidence_payload=review_evidence_payload,
            workflow_chain=workflow_chain,
            review_payload=review_payload,
        )
    ):
        review_evidence_payload = _reconcile_review_evidence_payload(
            planning_dir=planning_dir,
            review_evidence_payload=review_evidence_payload,
            workflow_chain=workflow_chain,
            review_payload=review_payload,
        )
    if not review_evidence_payload:
        review_evidence_payload = _summary_review_evidence_payload(
            final_review=final_review,
            semantic_compact=semantic_compact,
            gate_status=str(resolved_gate.get("gate_status") or "UNKNOWN").upper(),
        )
    review_evidence_check = _review_evidence_check(review_evidence_payload)
    explicit_review_evidence = bool(review_evidence_payload.get("explicit"))
    review_evidence_source = str(review_evidence_payload.get("source") or "summary_fallback").strip()
    review_evidence_source = review_evidence_source or "summary_fallback"
    if explicit_review_evidence:
        review_evidence_status = "PASS" if review_evidence_check["status"] == "PASS" else "FAIL"
    elif review_evidence_check["status"] == "PASS":
        review_evidence_status = "WARN"
    else:
        review_evidence_status = "MISSING"
    return {
        "final_quality_review_status": str(final_review.get("status") or "UNKNOWN").upper(),
        "final_quality_review_source": str(final_review.get("review_source") or ""),
        "must_fix_open_items": _must_fix_items(semantic_compact),
        "gate_total_status": str(resolved_gate.get("gate_status") or "UNKNOWN").upper(),
        "review_evidence_status": review_evidence_status,
        "review_evidence_source": review_evidence_source,
        "explicit_review_evidence": explicit_review_evidence,
        "review_evidence_payload": review_evidence_payload,
    }


def _workflow_chain_review_status(workflow_chain: dict[str, Any]) -> str:
    final_review = dict(workflow_chain.get("final_quality_review") or {})
    return str(final_review.get("status") or "").strip().upper()


def _verify_status(
    *,
    workflow_chain: dict[str, Any],
    semantic_compact: dict[str, Any] | None,
    state_payload: dict[str, Any] | None,
) -> str:
    upstream = dict(workflow_chain.get("upstream") or {})
    verify = dict(upstream.get("verify") or {})
    status = str(verify.get("status") or "").strip().upper()
    if status:
        return status
    compact_status = str((semantic_compact or {}).get("verify_check_status") or "").strip().upper()
    if compact_status:
        return compact_status
    fallback = str((state_payload or {}).get("last_stage_status") or "").strip().upper()
    return fallback or "UNKNOWN"


__all__ = [
    "_must_fix_items",
    "_resolve_gate_check",
    "_review_evidence",
    "_review_evidence_check",
    "_verify_status",
    "_workflow_chain_review_status",
]


