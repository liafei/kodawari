"""Deterministic evidence scout for repeated planner-reviewer disputes."""

from __future__ import annotations

import json
import re
from typing import Any

from kodawari.autopilot.core.secret_redactor import redact_secret_text

MAX_REQUESTS = 4
MAX_EVIDENCE_ITEMS = 5
MAX_EXCERPT_CHARS = 360
MAX_PACK_CHARS = 10_000

FACTUAL_CATEGORIES = {
    "canonical_task_anchor",
    "owner_surface",
    "product_semantics",
    "test_coverage",
}
# Only ``ambiguous`` keeps a request in the pending set: a planner that
# accepts the finding (``finding_supported``) or refutes it with a valid ref
# (``finding_refuted``) closes the request immediately. Earlier we also kept
# ``finding_supported`` and ``needs_human_decision`` as pending, which made
# the loop unsolvable because no planner answer could close them.
# ``needs_human_decision`` is removed from the resolution surface entirely:
# product/canonical decisions are now reviewer-side findings that revisit
# the plan in a normal subsequent round, not an orchestrator escape hatch.
PENDING_STATUSES = {"ambiguous"}
RESOLUTION_STATUSES = {"finding_supported", "finding_refuted", "ambiguous"}

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]{3,}")


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_clean(item) for item in value if _clean(item)]


def _finding_text(finding: dict[str, Any]) -> str:
    return " ".join(
        part
        for part in (
            _clean(finding.get("category")),
            _clean(finding.get("description")),
            _clean(finding.get("recommendation")),
        )
        if part
    )


def classify_review_finding(finding: dict[str, Any]) -> str:
    """Return a narrow factual category, or ``""`` when a finding is semantic only.

    ``meta_blocker`` is a 5th bucket that catches reviewer findings recursing
    on the planner's plan-meta fields (``evidence_resolutions`` entries asked
    to cite themselves, change_log entries asked to log their own creation,
    "meta-structural" / "recursive evidence" demands). These findings have no
    bottom — every planner response generates a new meta-claim about the
    response itself — so the orchestrator demotes them to ``info`` once a
    streak threshold is hit (Phase B). Keywords are intentionally tight: a
    bare ``evidence_resolutions`` mention is often a legitimate first-round
    structural ask; we only bucket as meta when reviewer pairs the meta-field
    with a recursive/self-referential marker.
    """
    text = _finding_text(finding).lower()
    if not text:
        return ""
    has_meta_field_ref = any(
        term in text
        for term in (
            "evidence_resolutions",
            "evidence resolutions",
            "change_log",
            "change log",
        )
    )
    has_recursive_marker = any(
        term in text
        for term in (
            "itself",
            "meta-structural",
            "meta structural",
            "meta claim",
            "meta-claim",
            "recursive evidence",
            "recursive structural",
            "evidence about evidence",
            "evidence_ref about evidence",
            "evidence ref about evidence",
            "circular evidence",
            "self-referential",
            "self referential",
            # Calibrated 2026-05-16 against the real 7-round external_trends_v1
            # artifact. Round 7 reviewer asked the planner's
            # evidence_resolutions entry for R5F1 to cite a *Round 7 finding* —
            # i.e. the past response would need to anticipate a future
            # complaint. Real reviewer wording avoids the explicit ``itself``
            # / ``meta-structural`` markers but the structural ask is the same
            # recursive pattern.
            "cite the round",
            "explicitly cite the round",
            "directly cite the round",
            "address the reviewer's claim",
            "addresses the reviewer's claim",
            "address the reviewer claim",
            "evidence_ref that directly addresses",
            "evidence ref that directly addresses",
            "must cite at least one evidence ref that directly",
        )
    )
    if has_meta_field_ref and has_recursive_marker:
        return "meta_blocker"
    if any(
        term in text
        for term in (
            "meta claim about",
            "meta-claim about",
            "evidence about evidence",
            "recursive evidence requirement",
            "circular evidence requirement",
        )
    ):
        return "meta_blocker"
    if any(term in text for term in ("canonical", "real task", "真实任务", "task graph", "任务计划", "prd")):
        return "canonical_task_anchor"
    if any(term in text for term in ("owner", "surface", "files_to_change", "call chain", "route path", "handler")):
        return "owner_surface"
    if any(term in text for term in ("event vs", "post vs", "item vs", "semantics", "ranking atom", "产品语义")):
        return "product_semantics"
    if any(term in text for term in ("test", "coverage", "verify", "related_existing_tests", "regression")):
        return "test_coverage"
    return ""


def _tokens(text: str) -> set[str]:
    stop = {
        "the",
        "and",
        "for",
        "with",
        "that",
        "this",
        "plan",
        "task",
        "must",
        "should",
        "review",
        "finding",
        "需要",
    }
    return {item.lower() for item in _TOKEN_RE.findall(text) if item.lower() not in stop}


def _plan_text(plan_payload: dict[str, Any]) -> str:
    try:
        return json.dumps(plan_payload or {}, ensure_ascii=False, sort_keys=True)
    except TypeError:
        return str(plan_payload or "")


def _plan_files(plan_payload: dict[str, Any]) -> list[str]:
    files: list[str] = []
    for task in list(dict(plan_payload or {}).get("tasks") or []):
        if not isinstance(task, dict):
            continue
        for field in ("files_to_change", "new_files", "related_existing_tests", "read_only_files", "do_not_change"):
            files.extend(_string_list(task.get(field)))
    seen: set[str] = set()
    out: list[str] = []
    for path in files:
        key = path.replace("\\", "/").casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(path.replace("\\", "/"))
    return out


def _context_sources(context: dict[str, Any]) -> list[tuple[str, str]]:
    sources: list[tuple[str, str]] = []
    for key, label in (
        ("task_plans", "Task Plans"),
        ("dev_status", "Dev Status"),
        ("prd_coverage", "PRD Coverage Matrix"),
        ("prd_excerpt", "PRD Excerpt"),
        ("claude_md", "CLAUDE.md"),
        ("repo_manifest", "Repo Manifest"),
        ("candidate_snippets", "Candidate Snippets"),
    ):
        raw = context.get(key)
        if key == "repo_manifest":
            raw = "\n".join(_string_list(dict(raw or {}).get("files")))
        elif key == "candidate_snippets":
            parts = []
            for item in list(raw or []):
                if not isinstance(item, dict):
                    continue
                parts.append(f"[{_clean(item.get('path'))}]\n{_clean(item.get('snippet'))}")
            raw = "\n\n".join(parts)
        text = _clean(raw)
        if text:
            sources.append((label, text))
    return sources


def _excerpt_around(text: str, tokens: set[str]) -> str:
    lowered = text.lower()
    positions = [lowered.find(token) for token in tokens if token and lowered.find(token) >= 0]
    start = min(positions) if positions else 0
    start = max(0, start - 80)
    excerpt = text[start : start + MAX_EXCERPT_CHARS]
    return redact_secret_text(excerpt).strip()


def _evidence_items(
    *,
    category: str,
    finding: dict[str, Any],
    plan_payload: dict[str, Any],
    context: dict[str, Any],
) -> list[dict[str, Any]]:
    text = _finding_text(finding)
    tokens = _tokens(text)
    items: list[dict[str, Any]] = []
    plan_files = _plan_files(plan_payload)
    plan_summary = {
        "summary": _clean(plan_payload.get("summary")),
        "files": plan_files[:12],
        "task_ids": [
            _clean(task.get("task_id"))
            for task in list(plan_payload.get("tasks") or [])
            if isinstance(task, dict) and _clean(task.get("task_id"))
        ],
    }
    items.append(
        {
            "ref_id": "plan:summary",
            "source": "current_plan",
            "excerpt": redact_secret_text(json.dumps(plan_summary, ensure_ascii=False))[:MAX_EXCERPT_CHARS],
        }
    )
    for label, source_text in _context_sources(context):
        haystack = source_text.lower()
        if tokens and not any(token in haystack for token in tokens):
            continue
        items.append(
            {
                "ref_id": f"context:{len(items)}",
                "source": label,
                "excerpt": _excerpt_around(source_text, tokens),
            }
        )
        if len(items) >= MAX_EVIDENCE_ITEMS:
            break
    if category in {"owner_surface", "test_coverage"}:
        manifest_files = _string_list(dict(context.get("repo_manifest") or {}).get("files"))
        matching = [
            path
            for path in manifest_files
            if any(token in path.lower() for token in tokens)
        ][:20]
        if matching and len(items) < MAX_EVIDENCE_ITEMS:
            items.append(
                {
                    "ref_id": "repo_manifest:matches",
                    "source": "Repo Manifest",
                    "excerpt": redact_secret_text("\n".join(matching))[:MAX_EXCERPT_CHARS],
                }
            )
    return items[:MAX_EVIDENCE_ITEMS]


def _status_for_category(
    *,
    category: str,
    finding: dict[str, Any],
    plan_payload: dict[str, Any],
    evidence: list[dict[str, Any]],
) -> str:
    """Always ``ambiguous`` — scout no longer pre-judges findings.

    Earlier versions tried to stamp ``finding_supported`` or
    ``needs_human_decision`` at scout time based on shallow heuristics over
    the plan text. That meant the request status the validator pinned the
    planner to was decided before the planner could even respond, and the
    validator forbade refuting a ``finding_supported`` request — which made
    the request unclosable. Now the scout records every factual finding as
    ``ambiguous`` and lets the planner settle the resolution. Persistent
    ``ambiguous`` resolutions are caught by the orchestrator's streak
    detector.
    """
    return "ambiguous"


def _request_id(round_number: int, index: int) -> str:
    return f"R{int(round_number)}F{int(index)}"


def _trim_pack(pack: dict[str, Any]) -> dict[str, Any]:
    text = json.dumps(pack, ensure_ascii=False)
    if len(text) <= MAX_PACK_CHARS:
        return pack
    trimmed = dict(pack)
    requests = []
    for request in list(trimmed.get("requests") or []):
        if not isinstance(request, dict):
            continue
        item = dict(request)
        item["evidence"] = [
            {
                **dict(evidence),
                "excerpt": _clean(dict(evidence).get("excerpt"))[:200],
            }
            for evidence in list(item.get("evidence") or [])[:3]
            if isinstance(evidence, dict)
        ]
        requests.append(item)
    trimmed["requests"] = requests[:3]
    return trimmed


def build_review_evidence_pack(
    *,
    round_number: int,
    plan_payload: dict[str, Any],
    findings: list[dict[str, Any]],
    context: dict[str, Any],
) -> dict[str, Any]:
    requests: list[dict[str, Any]] = []
    for index, finding in enumerate([item for item in findings if isinstance(item, dict)], start=1):
        category = classify_review_finding(finding)
        if category not in FACTUAL_CATEGORIES:
            continue
        evidence = _evidence_items(
            category=category,
            finding=finding,
            plan_payload=plan_payload,
            context=context,
        )
        status = _status_for_category(
            category=category,
            finding=finding,
            plan_payload=plan_payload,
            evidence=evidence,
        )
        requests.append(
            {
                "finding_id": _request_id(round_number, index),
                "category": category,
                "status": status,
                "reviewer_claim": redact_secret_text(_finding_text(finding))[:800],
                "instruction": (
                    "In the next plan, add an evidence_resolutions entry for this finding_id. "
                    "Use status finding_refuted (cite refs from this pack to disprove the claim), "
                    "finding_supported (accept and revise the plan accordingly), or ambiguous "
                    "(only when evidence is genuinely inconclusive). Cite evidence_refs from this pack."
                ),
                "evidence": evidence,
            }
        )
        if len(requests) >= MAX_REQUESTS:
            break
    if not requests:
        return {}
    return _trim_pack(
        {
            "schema_version": "planning.review_evidence.v1",
            "round_number": int(round_number),
            "source": "review_evidence_scout",
            "requests": requests,
        }
    )


def pending_evidence_requests(
    packs: list[dict[str, Any]] | None,
    *,
    prior_resolutions: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Return pack requests that still need a planner response.

    A request is pending when its status is in ``PENDING_STATUSES`` (now
    just ``ambiguous``) AND the planner has not yet supplied a closing
    resolution (``finding_supported`` or ``finding_refuted`` with at least
    one ``evidence_refs`` entry). The ``prior_resolutions`` filter prevents
    a previously-closed finding from re-surfacing in later rounds — without
    it the same finding_id would re-appear every time the planner-reviewer
    loop produced a new plan.
    """
    resolved_ids: set[str] = set()
    for item in list(prior_resolutions or []):
        if not isinstance(item, dict):
            continue
        finding_id = _clean(item.get("finding_id"))
        if not finding_id:
            continue
        status = _clean(item.get("status"))
        refs = _string_list(item.get("evidence_refs"))
        if status in {"finding_supported", "finding_refuted"} and refs:
            resolved_ids.add(finding_id)
    requests: list[dict[str, Any]] = []
    for pack in list(packs or []):
        if not isinstance(pack, dict):
            continue
        for request in list(pack.get("requests") or []):
            if not isinstance(request, dict):
                continue
            if _clean(request.get("status")) not in PENDING_STATUSES:
                continue
            if _clean(request.get("finding_id")) in resolved_ids:
                continue
            requests.append(dict(request))
    return requests


__all__ = [
    "FACTUAL_CATEGORIES",
    "PENDING_STATUSES",
    "RESOLUTION_STATUSES",
    "build_review_evidence_pack",
    "classify_review_finding",
    "pending_evidence_requests",
]
