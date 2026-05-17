"""Finding analysis, signature generation, and severity helpers.

Split out of planning_orchestrator to keep that module under the canonical
file-shape redline. Pure helpers + constants only; no orchestration logic.
All names preserve their original semantics.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from kodawari.autopilot.planning.review_evidence_scout import classify_review_finding

if TYPE_CHECKING:
    from kodawari.autopilot.planning.planning_orchestrator import PlanningRound

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_clean_text(item) for item in value if _clean_text(item)]


def _dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _severity(value: Any) -> str:
    text = _clean_text(value).lower()
    return text if text else "info"


DEFAULT_BLOCKING_SEVERITIES: frozenset[str] = frozenset({"blocking", "critical", "high"})
SOFT_GATE_ESCALATION_SEVERITIES: frozenset[str] = frozenset({"blocking", "critical"})
SOFT_EXECUTION_GUIDANCE_SEVERITIES: frozenset[str] = frozenset({"high", "medium"})
HIGH_HARD_STOP_CATEGORIES: frozenset[str] = frozenset(
    {
        "auth",
        "authentication",
        "authorization",
        "compliance",
        "credential",
        "credentials",
        "data_integrity",
        "data-loss",
        "data_loss",
        "destructive",
        "permission",
        "permissions",
        "privacy",
        "secret",
        "secrets",
        "security",
    }
)
HIGH_HARD_STOP_TERMS: frozenset[str] = frozenset(
    {
        "auth_forbidden",
        "credential",
        "credentials",
        "data loss",
        "data_loss",
        "delete user data",
        "destructive",
        "exfiltrate",
        "forbidden path",
        "forbidden_paths",
        "leak secret",
        "leaks secret",
        "path_out_of_scope",
        "scope drift",
        "scope_drift",
        "secret",
        "secrets",
        "security",
        "token leak",
        "unauthorized",
        "violates contract",
        "write outside",
    }
)
PLANNER_ENVIRONMENT_ERROR_KINDS: frozenset[str] = frozenset(
    {
        "api_error",
        "api_timeout",
        "auth_forbidden",
        "auth_missing",
        "executable_missing",
        "home_access_error",
        "max_turns",
        "nested_session",
        "planner_context_overflow",
        "planner_http_4xx",
        "planner_http_5xx",
        "planner_http_error",
        "planner_http_timeout",
        "planner_empty_output",
        "planner_output_truncated_empty",
        "planner_remote_closed",
        "planner_streaming_required",
        "planner_transport_timeout",
        "planner_tool_use_checkpoint_invalid_json",
        "tool_use_no_progress",
        "timeout",
    }
)
_CHAT_KIND_TO_PLANNER_ERROR_KIND: dict[str, str] = {
    "auth_forbidden": "auth_forbidden",
    "auth_invalid": "auth_forbidden",
    "auth_missing": "auth_missing",
    "context_overflow": "planner_context_overflow",
    "http_4xx": "planner_http_4xx",
    "http_5xx": "planner_http_5xx",
    "http_timeout": "planner_http_timeout",
    "redirect_blocked": "planner_http_error",
    "remote_closed": "planner_remote_closed",
    "streaming_required": "planner_streaming_required",
}

def _is_blocking_finding(
    item: dict[str, Any],
    threshold: frozenset[str] = DEFAULT_BLOCKING_SEVERITIES,
) -> bool:
    return _severity(item.get("severity")) in threshold or _high_finding_requires_hard_stop(item)


def _finding_text_for_policy(item: dict[str, Any]) -> str:
    return " ".join(
        part
        for part in (
            _clean_text(item.get("category")),
            _clean_text(item.get("description")),
            _clean_text(item.get("recommendation")),
        )
        if part
    ).lower()


def _high_finding_requires_hard_stop(item: dict[str, Any]) -> bool:
    if _severity(item.get("severity")) != "high":
        return False
    category = _clean_text(item.get("category")).lower().replace(" ", "_")
    if category in HIGH_HARD_STOP_CATEGORIES:
        return True
    text = _finding_text_for_policy(item)
    return any(term in text for term in HIGH_HARD_STOP_TERMS)


def _review_findings(review_payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    payload = dict(review_payload or {})
    return _dict_list(payload.get("findings"))


# ---------------------------------------------------------------------------
# Post-repair severity demotion
#
# deterministic_repair.apply_deterministic_repairs runs *before* the reviewer
# every round. The reviewer never sees the repair log: it gets the cleaned-up
# plan and may still flag the same field as missing/malformed because its
# context window was built from the planner's raw output. That manifested as
# a 4-round death loop where rounds 4-7 were entirely "evidence_resolutions
# missing", "change_log missing modified task fields", "layer_owner wrong" —
# all categories the orchestrator had already auto-fixed.
#
# We refuse to spend planning rounds re-litigating already-fixed structure.
# After the reviewer returns, any blocking finding that maps to a repair rule
# applied this round is demoted to ``severity=info`` with a marker so it
# remains visible in the artifact for audit but no longer counts toward
# blocking_findings_count or escalation.
# ---------------------------------------------------------------------------


_REPAIR_RULE_BUCKETS: dict[str, tuple[str, ...]] = {
    "truncate_invariants": ("invariants", "invariant"),
    "infer_missing_layer_owner": ("layer_owner", "layer owner"),
    "add_missing_task_change_log_entry": ("change_log", "change log", "changelog"),
    "change_log_known_task_ref": ("change_log", "change log", "changelog"),
    "dedupe_verify_recipes": ("verify_recipes", "verify recipe", "verify_recipe"),
    "filter_missing_verify_recipe_roots": ("verify_recipes", "verify recipe", "verify_recipe"),
    "promote_review_requested_write_path": (
        "files_to_change",
        "files to change",
        "read_only_files",
        "read only files",
        "scope conflict",
        "scope_conflict",
    ),
    "demote_verification_only_write_anchors": (
        "files_to_change",
        "files to change",
        "read_only_files",
        "read only files",
        "verification_only",
        "verification-only",
        "no-op",
        "noop",
        "scope conflict",
        "scope_conflict",
    ),
    "ensure_verification_only_task_constraints": (
        "execution_constraints",
        "verification_only_noop",
        "executor_must_not_edit",
        "task-level",
        "task level",
        "verification_only",
        "verification-only",
        "no-op",
        "noop",
    ),
    "normalize_workspace_relative_verify_commands": (
        "verify_cmd",
        "verify_recipes",
        "verify recipe",
        "cd ",
        "&&",
        "workspace-root",
        "workspace root",
        "project_root",
        "powershell",
    ),
    "remove_verification_only_unrequested_smoke_gates": (
        "workspace smoke",
        "minimal api smoke",
        "test_t001",
        "test_t002",
        "workspace_smoke",
        "approval_points",
        "acceptance",
        "verify_cmd",
        "extra smoke",
        "blocking gate",
    ),
    "normalize_verification_only_no_edit_contracts": (
        "verification_only",
        "verification-only",
        "no-op",
        "noop",
        "dirtiness",
        "git status",
        "git diff",
        "no-edit",
        "no_edit",
        "scratch",
        "tmp",
        "workflow",
    ),
    "add_verification_only_frontend_read_only_scope": (
        "frontend",
        "mobile",
        "page",
        "ui",
        "页面",
        "界面",
        "read_only_files",
        "source_of_truth",
        "scope",
    ),
    "add_verification_only_truth_docs_read_only_scope": (
        "docs",
        "documentation",
        "runbook",
        "source_of_truth",
        "source of truth",
        "read_only_files",
        "task plan",
        "任务计划",
        "启动交付",
    ),
    "add_verification_only_read_later_persistence_scope": (
        "read-later",
        "read_later",
        "read later",
        "persistence",
        "schema",
        "migration",
        "daily_read_state",
        "edition_assembly",
        "read_only_files",
        "module_boundaries",
    ),
    "demote_verification_only_implementation_canonical_truth": (
        "source_of_truth_canonical",
        "source_of_truth",
        "canonical truth",
        "implementation files",
        "docs/tests",
        "governing truth",
        "edition_assembly",
        "mobile/www",
    ),
    "promote_verification_only_tests_to_truth": (
        "source_of_truth_canonical",
        "canonical evidence",
        "verification tests",
        "required tests",
        "tests/test",
        "docs + tests",
        "authoritative",
    ),
    "sync_verification_only_source_docs_read_only_scope": (
        "docs",
        "source-of-truth",
        "source_of_truth",
        "read_only_files",
        "do_not_change",
        "evidence boundary",
        "prd_coverage_matrix",
        "开发交付现状",
    ),
    "ensure_verification_only_work_all_approval": (
        "workflow work-all",
        "work-all",
        "work all",
        "approval_points",
        "approval point",
        "acceptance",
        "gate",
    ),
    "ensure_verification_only_report_approval": (
        "final report",
        "report",
        "exit code",
        "pytest summary",
        "pytest output",
        "real command",
        "approval_points",
        "acceptance",
    ),
    "serialize_parallel_file_conflicts": (
        "depends_on",
        "depends on",
        "parallel",
        "task_graph",
        "task graph",
        "serialize",
        "serialise",
    ),
}

_LOCATION_TASK_INDEX_RE = re.compile(r"tasks\[(\d+)\]")


def _repair_log_task_id(entry: dict[str, Any], plan_payload: dict[str, Any] | None) -> str:
    """Resolve a repair log entry's task_id, falling back to ``location`` parsing.

    Most entries set ``task_id`` directly; older shapes only carry the location
    string (e.g. ``tasks[2].layer_owner``) which we map back through the plan's
    task list.
    """
    direct = _clean_text(entry.get("task_id"))
    if direct:
        return direct
    location = _clean_text(entry.get("location"))
    match = _LOCATION_TASK_INDEX_RE.search(location)
    if not match:
        return ""
    index = int(match.group(1)) - 1
    tasks = _dict_list(dict(plan_payload or {}).get("tasks"))
    if not 0 <= index < len(tasks):
        return ""
    return _clean_text(tasks[index].get("task_id"))


def _build_repaired_signatures(
    deterministic_repairs: list[dict[str, Any]] | None,
    plan_payload: dict[str, Any] | None,
) -> list[tuple[str, frozenset[str], frozenset[str]]]:
    """Return ``[(rule, keywords, task_ids), ...]`` for matching findings.

    Empty ``task_ids`` means the repair is plan-wide (verify_recipes,
    serialize_parallel) and should match findings without task-id constraint.
    """
    signatures: list[tuple[str, frozenset[str], frozenset[str]]] = []
    for entry in list(deterministic_repairs or []):
        if not isinstance(entry, dict):
            continue
        rule = _clean_text(entry.get("rule"))
        keywords = _REPAIR_RULE_BUCKETS.get(rule)
        if not keywords:
            continue
        task_id = _repair_log_task_id(entry, plan_payload)
        task_ids = frozenset({task_id}) if task_id else frozenset()
        signatures.append((rule, frozenset(keyword.lower() for keyword in keywords), task_ids))
    return signatures


def _finding_repair_match(
    finding: dict[str, Any],
    *,
    repaired_signatures: list[tuple[str, frozenset[str], frozenset[str]]],
    known_task_ids: list[str],
) -> tuple[str, frozenset[str]] | None:
    """Return ``(rule, matched_task_ids)`` if the finding is fully covered by a
    repair already applied this round; otherwise ``None``.

    Task-scoped repairs (e.g. ``infer_missing_layer_owner`` on T1) only match
    findings that mention T1 — a layer_owner concern about T2 is *not*
    auto-fixed by T1's repair. Plan-wide repairs (verify_recipes,
    serialize_parallel) match any finding hitting their keyword bucket.
    """
    if not repaired_signatures:
        return None
    text = _finding_text_for_policy(finding)
    if not text:
        return None
    for rule, keywords, task_ids in repaired_signatures:
        if not any(keyword in text for keyword in keywords):
            continue
        if not task_ids:
            return rule, frozenset()
        mentioned = _mentioned_task_ids(text, known_task_ids) if known_task_ids else set()
        intersected = task_ids & mentioned
        if intersected:
            return rule, frozenset(intersected)
    return None


def classify_findings_by_active_scope(
    findings: list[dict[str, Any]],
    *,
    scope_task_ids: set[str] | frozenset[str],
    known_task_ids: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Split findings into ``(in_scope, out_of_scope, unscoped)`` partitions.

    A finding is **out_of_scope** only when:
      * the plan has known task ids, AND
      * the finding mentions at least one task id, AND
      * no mentioned task id is in ``scope_task_ids``.

    A finding is **in_scope** when at least one mentioned task id is in
    ``scope_task_ids``, or when the finding text mentions no task id but
    ``scope_task_ids`` covers the entire plan.

    A finding is **unscoped** when scope cannot be decided (no task ids on
    the plan, or scope is empty). Caller should treat unscoped findings as
    blocking — *never* silently pass.
    """
    in_scope: list[dict[str, Any]] = []
    out_of_scope: list[dict[str, Any]] = []
    unscoped: list[dict[str, Any]] = []
    if not findings:
        return in_scope, out_of_scope, unscoped
    if not known_task_ids:
        return list(findings), [], []
    if not scope_task_ids:
        return [], [], list(findings)
    scope = set(scope_task_ids)
    for finding in findings:
        text = _finding_text_for_policy(finding)
        mentioned = _mentioned_task_ids(text, known_task_ids) if text else set()
        if not mentioned:
            in_scope.append(finding)
            continue
        if mentioned & scope:
            in_scope.append(finding)
        else:
            out_of_scope.append(finding)
    return in_scope, out_of_scope, unscoped


def derive_active_scope_review_view(
    final_review: dict[str, Any] | None,
    *,
    active_scope_outcome: dict[str, Any],
    known_task_ids: list[str],
    threshold: frozenset[str] = DEFAULT_BLOCKING_SEVERITIES,
    demoted_repaired_findings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return a non-authoritative active-scope view of reviewer findings.

    ``final_review`` remains the full-plan reviewer truth. This helper only
    projects that truth onto the active task scope so downstream artifacts can
    explain why an auto-skipped lite run proceeded even when the full epic plan
    still had future-task review debt.
    """
    if not isinstance(final_review, dict):
        return {}
    findings = _review_findings(final_review)
    scope_task_ids = set(active_scope_outcome.get("scope_task_ids") or [])
    in_scope, future_scope, unscoped = classify_findings_by_active_scope(
        findings,
        scope_task_ids=scope_task_ids,
        known_task_ids=known_task_ids,
    )
    active_blockers = [
        dict(item)
        for item in [*in_scope, *unscoped]
        if _is_blocking_finding(item, threshold)
    ]
    return {
        "approved": not active_blockers,
        "active_task_ids": list(active_scope_outcome.get("active_task_ids") or []),
        "scope_task_ids": list(active_scope_outcome.get("scope_task_ids") or []),
        "scope_source": str(active_scope_outcome.get("source") or ""),
        "findings_in_scope": [dict(item) for item in in_scope],
        "findings_future_scope": [dict(item) for item in future_scope],
        "findings_unscoped": [dict(item) for item in unscoped],
        "findings_demoted_by_repair": [dict(item) for item in list(demoted_repaired_findings or [])],
        "active_scope_blocker_count": len(active_blockers),
        "future_scope_blocker_count": len(list(active_scope_outcome.get("future_scope_blockers") or [])),
        "score": final_review.get("score"),
        "score_scope": "full_plan",
        "assessment": _clean_text(final_review.get("assessment")),
    }


def demote_findings_already_repaired(
    review_payload: dict[str, Any] | None,
    *,
    deterministic_repairs: list[dict[str, Any]] | None,
    plan_payload: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Return ``(payload', demoted)`` where ``payload'`` is a copy of the
    review payload with already-repaired findings demoted to ``severity=info``.

    The original finding fields are preserved verbatim; we only rewrite
    ``severity`` and append two markers (``severity_demoted`` and
    ``demoted_reason``) so the audit trail stays explicit. ``demoted`` is the
    list of findings (with markers) that were rewritten — useful for tests and
    progress reporting.

    No-ops when there are no repairs, no findings, or no matches. The reviewer
    payload's other fields (``approved``, ``score``, ``assessment`` …) are
    passed through untouched: this layer rewrites severity, not assessments.
    """
    if not isinstance(review_payload, dict):
        return review_payload, []
    findings = _review_findings(review_payload)
    if not findings:
        return review_payload, []
    repaired_signatures = _build_repaired_signatures(deterministic_repairs, plan_payload)
    if not repaired_signatures:
        return review_payload, []
    known_task_ids = [
        _clean_text(task.get("task_id"))
        for task in _dict_list(dict(plan_payload or {}).get("tasks"))
        if _clean_text(task.get("task_id"))
    ]
    rewritten: list[dict[str, Any]] = []
    demoted: list[dict[str, Any]] = []
    for finding in findings:
        if _severity(finding.get("severity")) == "info":
            rewritten.append(dict(finding))
            continue
        match = _finding_repair_match(
            finding,
            repaired_signatures=repaired_signatures,
            known_task_ids=known_task_ids,
        )
        if match is None:
            rewritten.append(dict(finding))
            continue
        rule, matched_task_ids = match
        rewritten_finding = dict(finding)
        rewritten_finding["severity"] = "info"
        rewritten_finding["severity_demoted"] = True
        rewritten_finding["demoted_reason"] = f"deterministic_repair_already_applied:{rule}"
        if matched_task_ids:
            rewritten_finding["demoted_task_ids"] = sorted(matched_task_ids)
        rewritten.append(rewritten_finding)
        demoted.append(rewritten_finding)
    if not demoted:
        return review_payload, []
    out = dict(review_payload)
    out["findings"] = rewritten
    return out, demoted


# ---------------------------------------------------------------------------
# Phase B — meta-blocker streak demotion
#
# When the reviewer recurses on plan-meta fields (evidence_resolutions[Rxfy]
# being asked to cite itself, "meta-structural claim" demands, etc.), no
# planner response can close the loop — the next round's reviewer files a
# new meta-claim about the response. A1's stateful-reviewer prompt cannot
# suppress these because each round's wording is genuinely new (different
# finding_id, different recursion-depth phrasing). _finding_signature also
# misses them because token_bag drifts every round.
#
# classify_review_finding now returns ``meta_blocker`` for these (Phase B
# step 1). When the orchestrator sees that EVERY blocking finding in a round
# falls into meta_blocker for ``META_BLOCKER_STREAK_LIMIT`` consecutive
# rounds, AND the plan otherwise meets strict guardrails (planner score
# >= 8.5, reviewer score >= 8.0, no real-blocker bucket co-existing), the
# orchestrator calls ``demote_meta_blocker_findings_to_info`` to rewrite
# those blockers to ``severity=info``. The demotion flips
# ``review_payload["approved"]`` to True so ``_evaluate_approval`` enters
# the strict auto_approve path via ``reviewer_approved_raw``.
# Hard-stop concerns (security/auth/credentials/data_loss/privacy) wearing
# meta wording are explicitly excluded by ``is_meta_blocker_finding`` so a
# real safety blocker can never be auto-demoted.
# ---------------------------------------------------------------------------


META_BLOCKER_CANONICAL_CATEGORY = "meta_blocker"
META_BLOCKER_STREAK_LIMIT = 3
META_BLOCKER_PLANNER_SCORE_FLOOR = 8.5
META_BLOCKER_REVIEWER_SCORE_FLOOR = 8.0

# Reason markers persisted on demoted findings + meta_blocker_demotion_log.
# Phase B fires on streak >= LIMIT; Phase C fires only at the final round
# (single-shot meta block after the planner addressed earlier concerns).
META_BLOCKER_STREAK_REASON = "meta_blocker_streak_demotion"
META_BLOCKER_LATE_ROUND_RECOVERY_REASON = "meta_blocker_late_round_recovery"


def is_meta_blocker_finding(finding: dict[str, Any]) -> bool:
    """True only when the finding classifies as meta_blocker AND is not a
    hard-stop concern (security / auth / data_loss / credentials / privacy).

    ``classify_review_finding`` returns ``meta_blocker`` as soon as a
    meta-field reference (``evidence_resolutions`` / ``change_log``) pairs
    with a recursive marker (``itself`` / ``meta-structural`` / ...). That
    would otherwise let a reviewer file ``severity=high, category=security,
    description="evidence_resolutions must reference itself"`` and have it
    silently demoted. We refuse to bucket those findings as meta_blocker so
    they (a) never feed the streak counter and (b) never get rewritten by
    ``demote_meta_blocker_findings_to_info``.
    """
    if classify_review_finding(finding) != META_BLOCKER_CANONICAL_CATEGORY:
        return False
    if _high_finding_requires_hard_stop(finding):
        return False
    category = _clean_text(finding.get("category")).lower().replace(" ", "_")
    if category in HIGH_HARD_STOP_CATEGORIES:
        return False
    return True


def demote_meta_blocker_findings_to_info(
    review_payload: dict[str, Any] | None,
    *,
    reason: str = META_BLOCKER_STREAK_REASON,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Rewrite meta_blocker bucket findings to ``severity=info``.

    Caller is responsible for guardrails (streak threshold or final-round
    precondition, score floors, no real-blocker coexistence). This helper
    only performs the rewrite and flips ``review_payload["approved"]`` to
    True with an audit marker (``approved_by_meta_blocker_demotion``). The
    True flag is what makes ``_evaluate_approval`` count the plan as
    reviewer-approved via ``reviewer_approved_raw``; the existing
    ``_all_findings_demoted_by_repair`` helper would also pass for fully-
    demoted payloads but it requires *every* finding to carry
    ``severity_demoted=True`` and is therefore not the load-bearing path
    when info findings without that marker coexist.

    Hard-stop findings are filtered out by ``is_meta_blocker_finding`` so a
    security/auth/data_loss concern wearing meta wording is never demoted.

    ``reason`` flows through to each demoted finding's ``demoted_reason``
    audit field so the orchestrator's two demotion paths (streak vs
    late-round recovery) remain distinguishable in the artifact.

    Returns ``(rewritten_payload, demoted_findings)``. When nothing
    qualifies for demotion, the input payload is returned unchanged and
    ``demoted_findings`` is empty.
    """
    if not isinstance(review_payload, dict):
        return review_payload, []
    findings = _review_findings(review_payload)
    if not findings:
        return review_payload, []
    rewritten: list[dict[str, Any]] = []
    demoted: list[dict[str, Any]] = []
    for finding in findings:
        if _severity(finding.get("severity")) == "info":
            rewritten.append(dict(finding))
            continue
        if not is_meta_blocker_finding(finding):
            rewritten.append(dict(finding))
            continue
        rewritten_finding = dict(finding)
        rewritten_finding["severity"] = "info"
        rewritten_finding["severity_demoted"] = True
        rewritten_finding["demoted_reason"] = reason
        rewritten.append(rewritten_finding)
        demoted.append(rewritten_finding)
    if not demoted:
        return review_payload, []
    out = dict(review_payload)
    out["findings"] = rewritten
    out["approved"] = True
    out["approved_by_meta_blocker_demotion"] = True
    return out, demoted


def _blocking_findings(
    *,
    review_payload: dict[str, Any] | None,
    structural_issues: list[str],
    threshold: frozenset[str] = DEFAULT_BLOCKING_SEVERITIES,
) -> list[dict[str, Any]]:
    blocked = [
        dict(item)
        for item in _all_findings(review_payload=review_payload, structural_issues=structural_issues)
        if _is_blocking_finding(item, threshold)
    ]
    return blocked


def _all_findings(
    *,
    review_payload: dict[str, Any] | None,
    structural_issues: list[str],
) -> list[dict[str, Any]]:
    findings = [dict(item) for item in _review_findings(review_payload)]
    findings.extend(
        {
            "severity": "blocking",
            "category": "structure",
            "description": issue,
            "recommendation": "Fix planner output structure before execution.",
        }
        for issue in structural_issues
    )
    return findings


def _dedupe_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for finding in findings:
        key = "|".join(
            (
                _severity(finding.get("severity")),
                _clean_text(finding.get("category")).lower(),
                _clean_text(finding.get("description")).lower(),
                _clean_text(finding.get("recommendation")).lower(),
            )
        )
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(dict(finding))
    return deduped


def _round_findings(
    round_item: PlanningRound,
    *,
    threshold: frozenset[str] | None = DEFAULT_BLOCKING_SEVERITIES,
) -> list[dict[str, Any]]:
    findings = _all_findings(
        review_payload=round_item.review_payload,
        structural_issues=round_item.structural_issues,
    )
    if threshold is None:
        return _dedupe_findings(findings)
    filtered = [dict(item) for item in findings if _is_blocking_finding(item, threshold)]
    return _dedupe_findings(filtered)


def _unresolved_findings(
    rounds: list[PlanningRound],
    *,
    threshold: frozenset[str] | None = DEFAULT_BLOCKING_SEVERITIES,
) -> list[dict[str, Any]]:
    if not rounds:
        return []
    return _round_findings(rounds[-1], threshold=threshold)


def _severity_counts(findings: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for finding in findings:
        key = _severity(finding.get("severity"))
        counts[key] = counts.get(key, 0) + 1
    return counts


def _soft_gate_requires_escalation(
    findings: list[dict[str, Any]],
    *,
    threshold: frozenset[str] = SOFT_GATE_ESCALATION_SEVERITIES,
) -> tuple[bool, str]:
    if any(_severity(item.get("severity")) in threshold for item in findings):
        return True, "critical_or_blocking_present"
    if any(_high_finding_requires_hard_stop(item) for item in findings):
        return True, "high_hard_stop_present"
    return False, ""


def _round_is_selectable_clean(
    round_item: PlanningRound,
    *,
    threshold: frozenset[str] = DEFAULT_BLOCKING_SEVERITIES,
) -> bool:
    if not round_item.plan_payload:
        return False
    if round_item.review_payload is None:
        return False
    if round_item.planner_error or round_item.review_error:
        return False
    if round_item.structural_issues:
        return False
    return not _round_findings(round_item, threshold=threshold)


def _round_quality_key(round_item: PlanningRound) -> tuple[int, int, int, int, int, int]:
    counts = _severity_counts(_round_findings(round_item, threshold=None))
    return (
        counts.get("critical", 0) + counts.get("blocking", 0),
        counts.get("high", 0),
        counts.get("medium", 0),
        counts.get("low", 0),
        sum(counts.values()),
        -int(round_item.round_number or 0),
    )


def _best_clean_round(
    rounds: list[PlanningRound],
    *,
    threshold: frozenset[str] = DEFAULT_BLOCKING_SEVERITIES,
) -> PlanningRound | None:
    best: PlanningRound | None = None
    for round_item in rounds:
        if not _round_is_selectable_clean(round_item, threshold=threshold):
            continue
        if best is None or _round_quality_key(round_item) < _round_quality_key(best):
            best = round_item
    return best


def _plan_tasks(plan_payload: dict[str, Any]) -> list[dict[str, Any]]:
    return _dict_list(plan_payload.get("tasks"))


def _finding_guidance_text(finding: dict[str, Any]) -> str:
    severity = _severity(finding.get("severity"))
    category = _clean_text(finding.get("category")).lower() or "review"
    description = _clean_text(finding.get("description"))
    recommendation = _clean_text(finding.get("recommendation"))
    parts = [f"Reviewer {severity} finding ({category})"]
    if description:
        parts.append(description)
    if recommendation:
        parts.append(f"Recommendation: {recommendation}")
    text = " — ".join(parts)
    return text[:600]


def _mentioned_task_ids(text: str, known_task_ids: list[str]) -> set[str]:
    hits: set[str] = set()
    for task_id in known_task_ids:
        pattern = rf"(?<![A-Za-z0-9_]){re.escape(task_id)}(?![A-Za-z0-9_])"
        if re.search(pattern, text, flags=re.IGNORECASE):
            hits.add(task_id)
    return hits


def _soft_execution_guidance_findings(
    review_payload: dict[str, Any] | None,
    *,
    threshold: frozenset[str],
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for finding in _review_findings(review_payload):
        if _severity(finding.get("severity")) not in SOFT_EXECUTION_GUIDANCE_SEVERITIES:
            continue
        if _is_blocking_finding(finding, threshold):
            continue
        findings.append(dict(finding))
    return findings


def _attach_soft_findings_to_plan_tasks(
    plan_payload: dict[str, Any],
    review_payload: dict[str, Any] | None,
    *,
    threshold: frozenset[str],
) -> dict[str, Any]:
    findings = _soft_execution_guidance_findings(review_payload, threshold=threshold)
    tasks = [dict(item) for item in list(plan_payload.get("tasks") or []) if isinstance(item, dict)]
    if not findings or not tasks:
        return plan_payload
    known_task_ids = [_clean_text(task.get("task_id")) for task in tasks if _clean_text(task.get("task_id"))]
    if not known_task_ids:
        return plan_payload
    guidance_by_task: dict[str, list[str]] = {task_id: [] for task_id in known_task_ids}
    for finding in findings:
        text = _finding_text_for_policy(finding)
        targets = _mentioned_task_ids(text, known_task_ids) or set(known_task_ids)
        guidance = _finding_guidance_text(finding)
        if not guidance:
            continue
        for task_id in targets:
            guidance_by_task.setdefault(task_id, []).append(guidance)
    changed = False
    for task in tasks:
        task_id = _clean_text(task.get("task_id"))
        guidance = guidance_by_task.get(task_id, [])
        if not guidance:
            continue
        existing = _string_list(task.get("review_focus"))
        merged = list(dict.fromkeys([*existing, *guidance]))
        if merged != existing:
            task["review_focus"] = merged
            changed = True
    if not changed:
        return plan_payload
    updated = dict(plan_payload)
    updated["tasks"] = tasks
    soft_log = _dict_list(updated.get("soft_review_guidance_log"))
    soft_log.append(
        {
            "rule": "attach_soft_review_findings_to_task_review_focus",
            "finding_count": len(findings),
            "targeted_tasks": sorted(task_id for task_id, values in guidance_by_task.items() if values),
        }
    )
    updated["soft_review_guidance_log"] = soft_log
    return updated

_FINDING_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*")

# 顽固检测要识别"reviewer 反复说同一件事"。LLM 改措辞（动词替换、
# 连接词替换）会让原文哈希变化，所以做激进归一化：剥离动词/介词/
# 连接词/助动词/常见副词，只留主题词（名词、技术术语、专有名词）。
_FINDING_STOPWORDS: frozenset[str] = frozenset({
    # 冠词 / 代词 / 限定词
    "the", "this", "that", "these", "those", "its", "their", "them",
    "they", "our", "your", "some", "any", "all", "none", "every", "each",
    "many", "much", "few", "several", "both", "either", "neither",
    # 介词
    "for", "with", "from", "into", "onto", "upon", "over", "under",
    "after", "before", "between", "during", "while", "without",
    "within", "across", "through", "via", "about", "around", "among",
    "above", "below", "beyond", "against", "per", "out",
    # 连接词
    "and", "but", "nor", "yet", "because", "since", "though",
    "although", "whereas", "than", "then",
    # 系动词 / 助动词 / 情态
    "are", "was", "were", "been", "being", "have", "has", "had",
    "having", "does", "did", "done", "doing", "can", "could", "may",
    "might", "must", "shall", "should", "will", "would", "ought",
    # 否定 / 副词
    "not", "very", "such", "just", "only", "also", "too", "more",
    "most", "less", "least", "even", "still", "ever", "never", "now",
    "here", "there", "when", "where", "how", "why", "who", "what",
    # 通用动词（措辞类）
    "use", "used", "using", "uses", "make", "made", "making", "makes",
    "take", "took", "taken", "taking", "takes", "give", "gave", "given",
    "giving", "gives", "got", "gotten", "getting", "gets", "set", "sets",
    "setting", "put", "puts", "putting", "add", "added", "adding", "adds",
    "remove", "removed", "removing", "removes", "delete", "deleted",
    "deleting", "deletes", "change", "changed", "changing", "changes",
    "update", "updated", "updating", "updates", "replace", "replaced",
    "replacing", "replaces", "switch", "switched", "switching",
    "switches", "move", "moved", "moving", "moves", "adopt", "adopted",
    "adopting", "adopts", "apply", "applied", "applying", "applies",
    "migrate", "migrated", "migrating", "migrates", "manage", "managed",
    "managing", "manages", "handle", "handled", "handling", "handles",
    "support", "supported", "supporting", "supports", "provide",
    "provided", "providing", "provides", "enable", "enabled",
    "enabling", "enables", "disable", "disabled", "disabling",
    "disables", "allow", "allowed", "allowing", "allows", "avoid",
    "avoided", "avoiding", "avoids", "prevent", "prevented",
    "preventing", "prevents", "ensure", "ensured", "ensuring",
    "ensures", "implement", "implemented", "implementing", "implements",
    "refactor", "refactored", "refactoring", "refactors", "fix",
    "fixed", "fixing", "fixes", "convert", "converted", "converting",
    "converts", "transform", "transformed", "transforming", "transforms",
    "look", "looking", "looked", "see", "seen", "seeing", "find",
    "found", "finding", "finds", "show", "showing", "shown", "need",
    "needs", "needed", "needing", "want", "wants", "wanted", "call",
    "calls", "called", "calling", "run", "runs", "running", "ran",
    "let", "lets", "letting", "require", "required", "requiring",
    "requires", "include", "included", "including", "includes",
    "contain", "contained", "containing", "contains", "follow",
    "followed", "following", "follows", "leave", "left", "leaving",
    "leaves", "keep", "kept", "keeping", "keeps",
})


def _finding_token_bag(text: str) -> frozenset[str]:
    """从 finding 文本里提取主题词 bag。

    顽固检测要识别 reviewer 反复说同一件事。LLM 改措辞（动词替换、
    连接词替换）会让原文哈希变化，所以做激进归一化：剥离所有
    动词/介词/连接词/助动词/常见副词，只留主题词（名词、技术术语、
    专有名词）。
    """
    if not text:
        return frozenset()
    out: set[str] = set()
    for match in _FINDING_TOKEN_RE.finditer(text):
        token = match.group(0).lower()
        if len(token) < 3:
            continue
        if token in _FINDING_STOPWORDS:
            continue
        out.add(token)
    return frozenset(out)


def _canonical_finding_category(item: dict[str, Any]) -> str:
    """把 reviewer 自由文本的 category 归一到一个稳定 bucket。

    Reviewer 在不同轮里把同一个语义抱怨写成 ``scope correctness`` /
    ``structural_validity`` / ``consistency`` 是常见现象（LLM 改包装），如果
    deadlock 签名直接吃 reviewer 原文 category，category 一漂签名就不等，
    repeated_blocker_rounds 会被重置 — 哪怕底层主题完全一样。

    优先复用 review_evidence_scout 的 classify_review_finding（它已经把
    factual 类目归到 4 个桶：canonical_task_anchor / owner_surface /
    product_semantics / test_coverage）。如果 scout 分类器不认识，落到原始
    category 串小写；如果连原始 category 都没有，落到固定的 ``"other"``。

    形状不变：仍然是单个字符串，所以 _signature_payload 的 artifact 契约
    （severity/category/tokens 三键）不破。
    """
    canonical = classify_review_finding(item)
    if canonical:
        return canonical
    raw = _clean_text(item.get("category")).lower()
    return raw or "other"


def _finding_signature(
    findings: list[dict[str, Any]],
) -> tuple[tuple[str, str, frozenset[str]], ...]:
    """为 findings 生成内容签名，用于顽固轮次检测。

    设计原则：避开自然语言原文（LLM 改个措辞就绕过）。三层信号：
      1. severity — reviewer 标注的严重等级
      2. category — 经 _canonical_finding_category 归一后的稳定 bucket
         （不是 reviewer 原文 category — 否则换包装就绕过签名）
      3. 主题词 token bag — 剥离动词/连接词后的名词/术语集合
    """
    normalized: list[tuple[str, str, frozenset[str]]] = []
    for item in findings:
        severity = _severity(item.get("severity"))
        category = _canonical_finding_category(item)
        bag = _finding_token_bag(
            _clean_text(item.get("description"))
            + " "
            + _clean_text(item.get("recommendation"))
        )
        normalized.append((severity, category, bag))
    return tuple(
        sorted(normalized, key=lambda triple: (triple[0], triple[1], tuple(sorted(triple[2]))))
    )


def _task_feedback_signature(plan_payload: dict[str, Any]) -> tuple[tuple[str, tuple[str, ...], tuple[str, ...], str], ...]:
    tasks: list[tuple[str, tuple[str, ...], tuple[str, ...], str]] = []
    for index, task in enumerate(_plan_tasks(plan_payload), start=1):
        task_id = _clean_text(task.get("task_id")) or f"task-{index}"
        files_to_change = tuple(_string_list(task.get("files_to_change")))
        invariants = tuple(_string_list(task.get("invariants")))
        test_plan = _clean_text(task.get("test_plan") or task.get("verify_cmd"))
        tasks.append((task_id, files_to_change, invariants, test_plan))
    return tuple(sorted(tasks))


def _module_boundary_signature(plan_payload: dict[str, Any]) -> tuple[tuple[str, str, tuple[str, ...], tuple[str, ...]], ...]:
    boundaries: list[tuple[str, str, tuple[str, ...], tuple[str, ...]]] = []
    for boundary in _dict_list(plan_payload.get("module_boundaries")):
        boundaries.append(
            (
                _clean_text(boundary.get("name")).lower(),
                _clean_text(boundary.get("surface")).lower(),
                tuple(sorted(_string_list(boundary.get("roots")))),
                tuple(sorted(item.lower() for item in _string_list(boundary.get("layers")))),
            )
        )
    return tuple(sorted(boundaries))


def _verify_recipe_signature(plan_payload: dict[str, Any]) -> tuple[tuple[str, str, str, tuple[str, ...]], ...]:
    recipes: list[tuple[str, str, str, tuple[str, ...]]] = []
    for recipe in _dict_list(plan_payload.get("verify_recipes")):
        required = recipe.get("required")
        if isinstance(required, bool):
            required_signature = "true" if required else "false"
        else:
            required_signature = ""
        recipes.append(
            (
                _clean_text(recipe.get("surface")).lower(),
                _clean_text(recipe.get("command")),
                required_signature,
                tuple(sorted(_string_list(recipe.get("roots")))),
            )
        )
    return tuple(sorted(recipes))


def _plan_feedback_signature(plan_payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "tasks": _task_feedback_signature(plan_payload),
        "path_type": _clean_text(plan_payload.get("path_type")).lower(),
        "layers": tuple(item.lower() for item in _string_list(plan_payload.get("layers"))),
        "module_boundaries": _module_boundary_signature(plan_payload),
        "verify_recipes": _verify_recipe_signature(plan_payload),
    }
