"""Auto-accept clean PLANNING_APPROVAL_REQUIRED escalations.

Scope (deliberately narrow): when the planner converged to a clean plan
(blocking_findings drops monotonically to zero, planner+reviewer both gave
strong scores, all structural checks pass) but the workflow still routes
the decision through ``PLANNING_APPROVAL_REQUIRED`` for a human nod, this
helper writes the accept response inline so the autopilot can proceed
without operator intervention.

Out of scope:
- Implementation-stage decisions (GATE_REFACTOR_NEEDED, executor recovery
  exhaustion, etc.) stay manual. This helper ONLY handles planning-phase
  approval gates.
- ``approval.decision != "auto_approve"`` plans stay manual.
- Plans that hit any safety predicate (low score, score gap, non-converged
  history, etc.) stay manual.

Integration point: ``cli/contract/model_bootstrap._bootstrap_from_fresh_plan``
calls this AFTER ``_write_fresh_conversation`` and BEFORE
``_validate_fresh_planning_result``. Caller mutates ``planning_result`` on
accept and rewrites PLANNING_CONVERSATION.json with the audit-tagged
``approved`` status; this helper only computes the decision + persists the
``.planning_decision_response.json``.

See: discussion thread that produced GPT v6 of this plan (two sub-agents
both signed off on the safety predicates listed below).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kodawari.autopilot.escalation.handler import (
    DecisionResponse,
    write_decision_response,
)
from kodawari.autopilot.planning.planning_orchestrator import STRUCTURAL_CHECK_NAMES

logger = logging.getLogger(__name__)


_PLANNING_REQUEST_FILENAME = ".planning_decision_request.json"
_MIN_PLANNER_SCORE = 8.0
_MIN_REVIEWER_SCORE = 8.0
# A3: relaxed floor when approval.reason ends with "_relaxed_score" — orchestrator
# already verified the plan is structurally clean (no blocking, all 17 checks,
# reviewer effective-approved). The lower score is acceptable because the
# orchestrator's relaxed path requires a STRICT SUPERSET of guardrails.
_MIN_PLANNER_SCORE_RELAXED = 7.5
_MIN_REVIEWER_SCORE_RELAXED = 7.5
_RELAXED_REASON_SUFFIX = "_relaxed_score"
_MAX_ROUND_COUNT = 5
_MAX_FILES_PER_TASK = 3


@dataclass
class AutoAcceptResult:
    """Outcome of an auto-accept attempt.

    ``applied is True`` when the helper wrote ``.planning_decision_response.json``
    and the caller MUST update ``planning_result`` (status="approved",
    final_plan=selected_plan) and rewrite the conversation file. ``applied is
    False`` is the default — caller leaves things alone and lets the
    existing manual approval path run.
    """

    applied: bool
    reason: str
    selected_plan: dict[str, Any] | None = None
    selected_round_number: int | None = None
    task_count: int = 0
    audit: dict[str, Any] = field(default_factory=dict)
    response_path: Path | None = None

    @classmethod
    def noop(cls, reason: str, **audit: Any) -> AutoAcceptResult:
        return cls(applied=False, reason=reason, audit=dict(audit))


def try_auto_accept_planning_approval(
    *,
    project_root: Path,
    planning_dir: Path,
    conversation_payload: dict[str, Any] | None = None,
) -> AutoAcceptResult:
    """Attempt to auto-accept a clean planning approval gate.

    Parameters
    ----------
    project_root:
        Absolute project root. NEVER guessed. Must be supplied by the caller
        so the helper can't accidentally write a response into a sibling
        feature's planning directory.
    planning_dir:
        Absolute planning directory (typically ``<project_root>/planning/<feature>``).
        Must contain (or imminently contain) ``PLANNING_CONVERSATION.json``
        and may contain ``.planning_decision_request.json``.
    conversation_payload:
        Optional. The just-rendered conversation payload before it's written
        to disk. When supplied, the helper trusts this over any stale
        ``PLANNING_CONVERSATION.json`` left from an earlier run.

    Returns
    -------
    AutoAcceptResult
        ``applied=True`` means the caller should mutate planning_result and
        rewrite the conversation file. ``applied=False`` means leave things
        alone and let the manual gate fire.
    """
    if not _enabled():
        return AutoAcceptResult.noop("disabled_via_env")
    if not isinstance(project_root, Path) or not isinstance(planning_dir, Path):
        return AutoAcceptResult.noop("invalid_arguments")

    payload = conversation_payload
    if not isinstance(payload, dict):
        payload = _load_conversation_from_disk(planning_dir)
    if not isinstance(payload, dict):
        return AutoAcceptResult.noop("conversation_payload_missing")

    safety = _check_safety(payload)
    if not safety["ok"]:
        return AutoAcceptResult.noop(safety["reason"], **safety.get("audit", {}))

    selected = _select_clean_plan(payload)
    if selected is None:
        return AutoAcceptResult.noop("no_clean_plan_in_rounds")
    plan_payload, round_number = selected

    plan_safety = _check_plan_safety(plan_payload)
    if not plan_safety["ok"]:
        return AutoAcceptResult.noop(plan_safety["reason"], **plan_safety.get("audit", {}))

    tasks = list(plan_payload.get("tasks") or [])
    audit_now = datetime.now(timezone.utc).isoformat()
    audit = {
        "auto_accepted_at": audit_now,
        "selected_round_number": round_number,
        "task_count": len(tasks),
        "planner_score": _safe_float(payload.get("approval", {}).get("checks", {}).get("planner_score")),
        "reviewer_score": _safe_float(payload.get("approval", {}).get("checks", {}).get("reviewer_score")),
        "blocking_findings_history": list(
            payload.get("escalation", {}).get("blocking_findings_history") or []
        ),
        "applied_inline_via": "model_bootstrap",
    }

    response = DecisionResponse(
        phase="planning",
        escalation_kind="PLANNING_APPROVAL_REQUIRED",
        action="accept",
        option_index=0,
        option={
            "title": "Auto-accept clean planning gate",
            "description": (
                f"Plan converged with blocking history "
                f"{audit['blocking_findings_history']!r}; "
                f"planner score {audit['planner_score']}, "
                f"reviewer score {audit['reviewer_score']}; "
                f"selected clean plan from round #{round_number} "
                f"({len(tasks)} task(s))."
            ),
        },
        description="planning_auto_accept",
        consumed_at=audit_now,
    )
    try:
        write_decision_response(planning_dir, "planning", response)
    except Exception as exc:  # noqa: BLE001
        logger.warning("planning_auto_accept: write_decision_response failed: %s", exc)
        return AutoAcceptResult.noop("write_response_failed", error=str(exc)[:200])

    response_path = planning_dir / ".planning_decision_response.json"
    _tag_response_with_inline_audit(response_path, audit)

    request_path = planning_dir / _PLANNING_REQUEST_FILENAME
    if request_path.exists():
        try:
            request_path.unlink()
        except OSError as exc:
            logger.warning("planning_auto_accept: cannot remove pending request: %s", exc)

    logger.info(
        "planning_auto_accept: accepted clean plan round=%s tasks=%s planner=%.2f reviewer=%.2f",
        round_number,
        len(tasks),
        audit["planner_score"] or 0.0,
        audit["reviewer_score"] or 0.0,
    )
    return AutoAcceptResult(
        applied=True,
        reason="auto_accepted",
        selected_plan=dict(plan_payload),
        selected_round_number=round_number,
        task_count=len(tasks),
        audit=audit,
        response_path=response_path,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _enabled() -> bool:
    """Default ON. Operators can opt out with WORKFLOW_PLANNING_AUTO_ACCEPT=0."""
    raw = str(os.environ.get("WORKFLOW_PLANNING_AUTO_ACCEPT", "1")).strip().lower()
    return raw not in {"0", "false", "off", "no"}


def _load_conversation_from_disk(planning_dir: Path) -> dict[str, Any] | None:
    import json

    path = planning_dir / "PLANNING_CONVERSATION.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _safe_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _check_safety(payload: dict[str, Any]) -> dict[str, Any]:
    """Apply the conversation-level safety predicates."""
    status = str(payload.get("status") or "").strip().lower()
    if status != "escalation_required":
        return {"ok": False, "reason": "status_not_escalation_required", "audit": {"status": status}}

    escalation = payload.get("escalation") or {}
    gate_reason = str(escalation.get("gate_reason") or "").strip()
    if gate_reason != "approval_required":
        return {"ok": False, "reason": "gate_reason_not_approval_required", "audit": {"gate_reason": gate_reason}}

    approval = payload.get("approval") or {}
    decision = str(approval.get("decision") or "").strip()
    if decision != "auto_approve":
        return {"ok": False, "reason": "approval_decision_not_auto_approve", "audit": {"decision": decision}}

    approval_reason = str(approval.get("reason") or "").strip()
    relaxed = approval_reason.endswith(_RELAXED_REASON_SUFFIX)
    planner_floor = _MIN_PLANNER_SCORE_RELAXED if relaxed else _MIN_PLANNER_SCORE
    reviewer_floor = _MIN_REVIEWER_SCORE_RELAXED if relaxed else _MIN_REVIEWER_SCORE

    checks = approval.get("checks") or {}
    planner_score = _safe_float(checks.get("planner_score"))
    reviewer_score = _safe_float(checks.get("reviewer_score"))
    if planner_score is None or planner_score < planner_floor:
        return {
            "ok": False,
            "reason": "planner_score_below_threshold",
            "audit": {"planner_score": planner_score, "threshold": planner_floor, "relaxed": relaxed},
        }
    if reviewer_score is None or reviewer_score < reviewer_floor:
        return {
            "ok": False,
            "reason": "reviewer_score_below_threshold",
            "audit": {"reviewer_score": reviewer_score, "threshold": reviewer_floor, "relaxed": relaxed},
        }
    if not bool(checks.get("score_gap_ok")):
        return {"ok": False, "reason": "score_gap_too_large"}

    # Shared structural whitelist with _evaluate_approval.
    failing = [name for name in STRUCTURAL_CHECK_NAMES if not bool(checks.get(name))]
    if failing:
        return {
            "ok": False,
            "reason": "structural_checks_failed",
            "audit": {"failing_checks": failing},
        }

    history_raw = escalation.get("blocking_findings_history")
    if not isinstance(history_raw, list) or not history_raw:
        return {"ok": False, "reason": "history_empty_or_missing"}
    try:
        history = [int(item) for item in history_raw]
    except (TypeError, ValueError):
        return {"ok": False, "reason": "history_not_numeric", "audit": {"history": list(history_raw)}}
    if history[-1] != 0:
        return {"ok": False, "reason": "history_last_not_zero", "audit": {"history": history}}
    for prev, curr in zip(history, history[1:]):
        if curr > prev:
            return {"ok": False, "reason": "history_not_monotonic_non_increasing", "audit": {"history": history}}

    round_count_raw = escalation.get("round_count")
    try:
        round_count = int(round_count_raw) if round_count_raw is not None else len(history)
    except (TypeError, ValueError):
        round_count = len(history)
    if round_count > _MAX_ROUND_COUNT:
        return {
            "ok": False,
            "reason": "round_count_above_threshold",
            "audit": {"round_count": round_count, "threshold": _MAX_ROUND_COUNT},
        }

    return {"ok": True, "reason": "safety_predicates_pass"}


def _select_clean_plan(payload: dict[str, Any]) -> tuple[dict[str, Any], int] | None:
    """Walk conversation.rounds in REVERSE order, return the first round with
    a clean plan payload (blocking_findings_count == 0 and plan tasks present).
    """
    rounds = payload.get("rounds")
    if not isinstance(rounds, list):
        return None
    for round_entry in reversed(rounds):
        if not isinstance(round_entry, dict):
            continue
        blocking = round_entry.get("blocking_findings_count")
        if blocking is None or int(blocking or 0) != 0:
            continue
        plan_payload = (
            round_entry.get("plan_payload")
            or round_entry.get("plan")
            or round_entry.get("planner_output")
        )
        if not isinstance(plan_payload, dict):
            continue
        if not isinstance(plan_payload.get("tasks"), list) or not plan_payload.get("tasks"):
            continue
        round_number = int(round_entry.get("round_number") or 0)
        return plan_payload, round_number
    # Fallback: payload['final_plan'] when rounds don't carry plan_payload.
    final_plan = payload.get("final_plan")
    if isinstance(final_plan, dict) and isinstance(final_plan.get("tasks"), list) and final_plan["tasks"]:
        last_round = rounds[-1] if rounds else {}
        round_number = int(last_round.get("round_number") or 0) if isinstance(last_round, dict) else 0
        return dict(final_plan), round_number
    return None


def _check_plan_safety(plan_payload: dict[str, Any]) -> dict[str, Any]:
    """Apply per-task safety predicates against the selected clean plan."""
    tasks = plan_payload.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        return {"ok": False, "reason": "selected_plan_tasks_empty"}
    for task in tasks:
        if not isinstance(task, dict):
            return {"ok": False, "reason": "task_not_dict"}
        task_id = str(task.get("task_id") or "")
        files = task.get("files_to_change")
        if not isinstance(files, list):
            return {"ok": False, "reason": "files_to_change_not_list", "audit": {"task_id": task_id}}
        if len(files) > _MAX_FILES_PER_TASK:
            return {
                "ok": False,
                "reason": "files_to_change_above_threshold",
                "audit": {"task_id": task_id, "files_count": len(files), "threshold": _MAX_FILES_PER_TASK},
            }
        # Write tasks must carry invariants. Read-only / planning-meta tasks
        # may legitimately have no invariants — they don't change code.
        path_type = str(task.get("path_type") or "").lower()
        is_write_task = path_type in {"", "write", "both"}  # default to write if unspecified
        invariants = task.get("invariants")
        if is_write_task:
            if not isinstance(invariants, list) or not invariants:
                return {
                    "ok": False,
                    "reason": "write_task_missing_invariants",
                    "audit": {"task_id": task_id, "path_type": path_type},
                }
        forbidden = task.get("forbidden_changes")
        if forbidden is not None and not isinstance(forbidden, list):
            return {
                "ok": False,
                "reason": "forbidden_changes_not_list",
                "audit": {"task_id": task_id, "type": type(forbidden).__name__},
            }
    return {"ok": True, "reason": "plan_safety_pass"}


def _tag_response_with_inline_audit(response_path: Path, audit: dict[str, Any]) -> None:
    """Append ``applied_at`` and ``applied_inline_via`` to the response so
    ``detect_pending_resume`` doesn't try to reprocess it on the next start.
    """
    import json

    try:
        data = json.loads(response_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    data["applied_at"] = audit["auto_accepted_at"]
    data["applied_inline_via"] = audit["applied_inline_via"]
    data["auto_accept_audit"] = dict(audit)
    try:
        response_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.warning("planning_auto_accept: cannot tag response: %s", exc)


__all__ = [
    "AutoAcceptResult",
    "try_auto_accept_planning_approval",
]
