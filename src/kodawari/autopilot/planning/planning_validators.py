"""Shared validators for planning pipeline file-existence checks."""

from __future__ import annotations

import os
from pathlib import Path
import re
from typing import Any

from kodawari.autopilot.planning.review_evidence_scout import (
    RESOLUTION_STATUSES,
    pending_evidence_requests,
    _tokens as _scout_tokens,
)


_CLOSED_STATUSES = {"finding_supported", "finding_refuted"}


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_clean(item) for item in value if _clean(item)]


def normalize_planning_path(value: Any) -> str:
    """Normalize planner-emitted repo paths for stable comparisons."""
    return _clean(value).replace("\\", "/")


def path_comparison_is_case_insensitive(project_root: Path | None = None) -> bool:
    """Return True when repo-path comparisons should ignore case."""
    if os.name == "nt":
        return True
    drive = str(getattr(project_root, "drive", "") or "")
    return len(drive) == 2 and drive[1] == ":"


def planning_path_key(
    value: Any,
    *,
    case_insensitive: bool = False,
) -> str:
    normalized = normalize_planning_path(value)
    return normalized.casefold() if case_insensitive else normalized


_VERIFY_PATH_RE = re.compile(r"(?P<path>[A-Za-z0-9_./\\-]+\.py)(?:::[A-Za-z0-9_./\\\[\]-]+)?")
_GENERIC_ROUTE_TOKENS = {
    "api",
    "app",
    "backend",
    "controller",
    "controllers",
    "endpoint",
    "endpoints",
    "handler",
    "handlers",
    "main",
    "route",
    "router",
    "routes",
    "test",
    "tests",
    "v1",
    "v2",
    "view",
    "views",
}


def _looks_like_test_path(path: str) -> bool:
    normalized = normalize_planning_path(path).lower()
    parts = [part for part in normalized.split("/") if part]
    if "tests" in parts or "test" in parts:
        return True
    name = parts[-1] if parts else normalized
    return name.startswith("test_") or name.endswith("_test.py") or name.endswith(".test.py")


def _looks_like_route_handler_path(path: str) -> bool:
    normalized = normalize_planning_path(path).lower()
    if not normalized or _looks_like_test_path(normalized):
        return False
    route_markers = (
        "/api/v",
        "/routes/",
        "/route/",
        "/controllers/",
        "/controller/",
        "/handlers/",
        "/handler/",
        "/endpoints/",
        "/endpoint/",
    )
    if any(marker in f"/{normalized}" for marker in route_markers):
        return True
    name = normalized.rsplit("/", 1)[-1]
    return any(
        marker in name
        for marker in (
            "_route",
            "_routes",
            "router",
            "_handler",
            "_handlers",
            "_controller",
            "_controllers",
            "_endpoint",
            "_endpoints",
        )
    )


def _path_subject_tokens(path: str) -> set[str]:
    normalized = normalize_planning_path(path).lower()
    tokens = {
        token
        for token in re.split(r"[^a-z0-9]+", normalized)
        if len(token) >= 3 and token not in _GENERIC_ROUTE_TOKENS
    }
    return tokens


def _extract_verify_test_paths(*texts: str) -> list[str]:
    seen: set[str] = set()
    paths: list[str] = []
    for text in texts:
        for match in _VERIFY_PATH_RE.finditer(_clean(text)):
            path = normalize_planning_path(match.group("path")).lstrip("./")
            if not _looks_like_test_path(path):
                continue
            key = path.casefold()
            if key in seen:
                continue
            seen.add(key)
            paths.append(path)
    return paths


def _existing_project_path(project_root: Path, rel_path: str) -> bool:
    normalized = normalize_planning_path(rel_path)
    if not normalized:
        return False
    try:
        candidate = (project_root.resolve() / normalized).resolve()
        return candidate.is_relative_to(project_root.resolve()) and candidate.is_file()
    except (OSError, ValueError):
        return False


def _test_matches_route_subject(test_path: str, route_files: list[str]) -> bool:
    test_tokens = _path_subject_tokens(test_path)
    if not test_tokens:
        return False
    for route_file in route_files:
        if test_tokens & _path_subject_tokens(route_file):
            return True
    return False


def check_route_handler_related_tests(
    tasks: list[dict[str, Any]],
    *,
    project_root: Path,
    case_insensitive: bool | None = None,
) -> list[str]:
    """Require route/handler plans to declare targeted existing tests in scope.

    The guard is intentionally narrow: it only fires for task-level verify
    commands that name concrete existing test files and share a subject token
    with a changed route/handler/controller file. Broad commands such as
    ``pytest tests/`` are left to review/precheck.
    """
    root = project_root.resolve()
    case_insensitive = (
        path_comparison_is_case_insensitive(root)
        if case_insensitive is None
        else bool(case_insensitive)
    )
    errors: list[str] = []
    for index, task in enumerate(tasks, start=1):
        if not isinstance(task, dict):
            continue
        label = f"tasks[{index}]"
        files_to_change = [
            normalize_planning_path(item)
            for item in list(task.get("files_to_change") or [])
            if _clean(item)
        ]
        route_files = [path for path in files_to_change if _looks_like_route_handler_path(path)]
        if not route_files:
            continue
        declared_scope = {
            planning_path_key(item, case_insensitive=case_insensitive)
            for item in (
                files_to_change
                + [
                    normalize_planning_path(item)
                    for item in list(task.get("related_existing_tests") or [])
                    if _clean(item)
                ]
            )
        }
        verify_tests = _extract_verify_test_paths(
            _clean(task.get("verify_cmd")),
            _clean(task.get("test_plan")),
        )
        missing_scope = [
            path
            for path in verify_tests
            if _existing_project_path(root, path)
            and planning_path_key(path, case_insensitive=case_insensitive) not in declared_scope
            and _test_matches_route_subject(path, route_files)
        ]
        if missing_scope:
            errors.append(
                f"{label}.related_existing_tests missing targeted existing tests for route/handler change: "
                f"{missing_scope}; add them to related_existing_tests or files_to_change before planning verify mutations"
            )
    return errors


def check_missing_source_files(
    files_to_change: list[str],
    *,
    task_new_files: set[str],
    upstream_new_files: set[str],
    project_root: Path,
    case_insensitive: bool | None = None,
) -> list[str]:
    """Return paths in files_to_change that are neither newly created nor on disk.

    A file is considered "available" if any of the following is true:
    - it is listed in the current task's new_files (the task creates it)
    - it is listed in upstream_new_files (a declared dependency creates it)
    - it already exists on disk at project_root / path

    Tasks that reference files created by other tasks MUST declare the creator
    in their depends_on list — the upstream_new_files set is derived from those
    declared dependencies. No cross-task inference is performed here; doing so
    would mask missing dependency declarations that the planner needs to fix.
    """
    root = project_root.resolve()
    case_insensitive = (
        path_comparison_is_case_insensitive(root)
        if case_insensitive is None
        else bool(case_insensitive)
    )
    all_planned_keys = {
        planning_path_key(item, case_insensitive=case_insensitive)
        for item in (task_new_files | upstream_new_files)
        if normalize_planning_path(item)
    }
    missing: list[str] = []
    for path in files_to_change:
        normalized = normalize_planning_path(path)
        if not normalized:
            continue
        if planning_path_key(normalized, case_insensitive=case_insensitive) in all_planned_keys:
            continue
        candidate = root / normalized
        try:
            if candidate.resolve().is_relative_to(root) and not candidate.exists():
                missing.append(normalized)
        except (OSError, ValueError):
            pass
    return missing


def _plan_delta_text(
    *,
    current_plan: dict[str, Any],
    previous_plan: dict[str, Any] | None,
) -> str:
    """Concatenate plan elements that changed between rounds, lowercased.

    Used by the Layer C plan-delta token-anchor check. We deliberately include
    only fields a planner would touch when responding to a finding (scope-
    bearing fields), not the entire plan JSON — otherwise narrative summary
    edits would always pass the anchor check.

    When ``previous_plan`` is None (round 1) we treat the entire current plan
    as the delta. The token-anchor caller still scopes enforcement to
    previously-emitted finding_ids, so first-round acceptances are not blocked.
    """
    parts: list[str] = []
    current_tasks_by_id: dict[str, dict[str, Any]] = {}
    for task in list(current_plan.get("tasks") or []):
        if isinstance(task, dict):
            current_tasks_by_id[_clean(task.get("task_id"))] = task
    previous_tasks_by_id: dict[str, dict[str, Any]] = {}
    for task in list((previous_plan or {}).get("tasks") or []):
        if isinstance(task, dict):
            previous_tasks_by_id[_clean(task.get("task_id"))] = task
    for task_id, task in current_tasks_by_id.items():
        prior = previous_tasks_by_id.get(task_id)
        if prior == task:
            continue
        for field in (
            "files_to_change",
            "new_files",
            "invariants",
            "related_existing_tests",
            "behavior_changes",
        ):
            parts.extend(_string_list(task.get(field)))
        parts.append(_clean(task.get("approach")))
        parts.append(_clean(task.get("test_plan")))
        parts.append(_clean(task.get("verify_cmd")))
    current_recipes = list(current_plan.get("verify_recipes") or [])
    previous_recipes = list((previous_plan or {}).get("verify_recipes") or [])
    if current_recipes != previous_recipes:
        for recipe in current_recipes:
            if isinstance(recipe, dict):
                parts.append(_clean(recipe.get("command")))
                parts.append(_clean(recipe.get("surface")))
    for entry in list(current_plan.get("change_log") or []):
        if isinstance(entry, dict):
            parts.append(_clean(entry.get("reason")))
            parts.extend(_string_list(entry.get("fields")))
            parts.append(_clean(entry.get("task_id")))
    return " ".join(part for part in parts if part).lower()


def _plan_delta_basenames(
    *,
    current_plan: dict[str, Any],
    previous_plan: dict[str, Any] | None,
) -> set[str]:
    """Return basename tokens of files that changed in the current plan.

    Token form: lowercase basename without the extension, split on
    non-alphanumerics. This is the Q1 (c) basename-overlap fallback —
    it lets a finding whose recommendation refers to e.g. `social_thread`
    match a plan that adds `social_thread_service.py` even when the
    free-text recommendation never mentioned the full filename.
    """
    previous_files: set[str] = set()
    for task in list((previous_plan or {}).get("tasks") or []):
        if isinstance(task, dict):
            previous_files.update(
                planning_path_key(item, case_insensitive=True)
                for item in _string_list(task.get("files_to_change"))
            )
    out: set[str] = set()
    for task in list(current_plan.get("tasks") or []):
        if not isinstance(task, dict):
            continue
        for path in _string_list(task.get("files_to_change")):
            key = planning_path_key(path, case_insensitive=True)
            if key in previous_files:
                continue
            basename = key.rsplit("/", 1)[-1]
            stem = basename.rsplit(".", 1)[0] if "." in basename else basename
            for piece in re.split(r"[^a-z0-9]+", stem):
                if len(piece) >= 3:
                    out.add(piece)
    return out


def _previously_emitted_finding_ids(
    evidence_packs: list[dict[str, Any]] | None,
    *,
    current_round_only: dict[str, Any] | None = None,
) -> set[str]:
    """Set of finding_ids the scout emitted in any prior pack.

    When ``current_round_only`` is the most recent pack, finding_ids
    introduced in that pack only (i.e. not seen in earlier rounds) are
    excluded — the planner gets a free pass to ``finding_supported`` /
    ``finding_refuted`` a fresh finding without proving plan-scope delta,
    because "you've never seen this complaint before" is a legitimate
    first-round closure case (avoids the docstring-flagged unclosable-loop
    footgun on first acceptance).
    """
    seen: set[str] = set()
    packs = list(evidence_packs or [])
    if current_round_only is not None and packs and packs[-1] is current_round_only:
        packs = packs[:-1]
    for pack in packs:
        if not isinstance(pack, dict):
            continue
        for request in list(pack.get("requests") or []):
            if not isinstance(request, dict):
                continue
            finding_id = _clean(request.get("finding_id"))
            if finding_id:
                seen.add(finding_id)
    return seen


def validate_evidence_resolutions(
    plan_payload: dict[str, Any],
    evidence_packs: list[dict[str, Any]] | None,
    *,
    previous_plan: dict[str, Any] | None = None,
) -> list[str]:
    """Validate planner responses to deterministic review evidence packs.

    Structural checks:

      * each pending request (status ``ambiguous`` and not yet resolved by
        an earlier round) must have an ``evidence_resolutions`` entry,
      * the entry's status must be one of ``finding_refuted`` /
        ``finding_supported`` / ``ambiguous``,
      * ``evidence_refs`` must be non-empty and only cite refs from the
        pack (prevents the planner from inventing refs).

    Layer C semantic-closure check (added when ``previous_plan`` is supplied):

      * for resolutions that close a previously-emitted finding (``status``
        is ``finding_supported`` or ``finding_refuted`` AND the same
        ``finding_id`` appeared in an earlier round's pack), the plan must
        have changed in the scope the finding pointed at. "In the scope"
        is measured by token overlap: subject tokens extracted from the
        finding's reviewer_claim/recommendation must intersect either the
        token bag of changed plan-delta text (tasks/files/invariants/
        verify_recipes/change_log) OR the basename tokens of newly added
        files_to_change paths.

    Footgun avoidance (per validator docstring history): the closure check
    only fires for previously-emitted finding_ids, so a planner that
    accepts a brand-new first-round finding by setting status=ambiguous
    or finding_supported is never blocked — they have a legal exit.

    The validator does not try to re-judge the finding semantically — the
    reviewer owns that. The earlier interlock blocks (``finding_supported``
    cannot be refuted, ``ambiguous`` must stay ambiguous, ``needs_human_decision``
    must remain ``needs_human_decision``) are removed because they made the
    request unclosable and routed the orchestrator into ``planning_evidence_blocked``
    even when the planner had revised the plan correctly.
    """
    raw_resolutions = plan_payload.get("evidence_resolutions")
    resolutions = (
        [item for item in raw_resolutions if isinstance(item, dict)]
        if isinstance(raw_resolutions, list)
        else []
    )
    requests = pending_evidence_requests(evidence_packs, prior_resolutions=resolutions)
    errors: list[str] = []
    if not isinstance(raw_resolutions, list) and requests:
        return [
            "evidence_resolutions missing: respond to each Review-Triggered Evidence Pack request "
            "with finding_id, status, evidence_refs, and rationale"
        ]
    by_id = {_clean(item.get("finding_id")): item for item in resolutions if _clean(item.get("finding_id"))}
    current_pack = list(evidence_packs or [])[-1] if evidence_packs else None
    prior_emitted = _previously_emitted_finding_ids(evidence_packs, current_round_only=current_pack)
    delta_text = _plan_delta_text(current_plan=plan_payload, previous_plan=previous_plan)
    delta_tokens = _scout_tokens(delta_text) if delta_text else set()
    delta_basenames = _plan_delta_basenames(
        current_plan=plan_payload, previous_plan=previous_plan
    )
    request_by_id: dict[str, dict[str, Any]] = {}
    for pack in list(evidence_packs or []):
        if not isinstance(pack, dict):
            continue
        for request in list(pack.get("requests") or []):
            if isinstance(request, dict):
                finding_id_text = _clean(request.get("finding_id"))
                if finding_id_text and finding_id_text not in request_by_id:
                    request_by_id[finding_id_text] = request
    # First: structural checks on still-pending requests (the original
    # contract — must reply, must use legal status, must cite real refs).
    for request in requests:
        finding_id = _clean(request.get("finding_id"))
        if not finding_id:
            continue
        resolution = by_id.get(finding_id)
        if resolution is None:
            errors.append(f"evidence_resolutions missing entry for {finding_id}")
            continue
        status = _clean(resolution.get("status"))
        if status not in RESOLUTION_STATUSES:
            errors.append(
                f"evidence_resolutions[{finding_id}].status must be one of "
                f"{sorted(RESOLUTION_STATUSES)}"
            )
            continue
        refs = _string_list(resolution.get("evidence_refs"))
        allowed_refs = {
            _clean(item.get("ref_id"))
            for item in list(request.get("evidence") or [])
            if isinstance(item, dict) and _clean(item.get("ref_id"))
        }
        unknown_refs = [ref for ref in refs if ref not in allowed_refs]
        if not refs:
            errors.append(f"evidence_resolutions[{finding_id}].evidence_refs must cite at least one evidence ref")
        elif unknown_refs:
            errors.append(f"evidence_resolutions[{finding_id}].evidence_refs unknown refs: {unknown_refs}")
    # Second: Layer C closure check. Iterate ALL resolutions (not just
    # still-pending requests), because a planner who self-closes a finding
    # with finding_supported / finding_refuted removes it from the
    # pending-request set — exactly the deadlock dance we need to catch.
    if previous_plan is None:
        return errors
    for resolution in resolutions:
        finding_id = _clean(resolution.get("finding_id"))
        if not finding_id or finding_id not in prior_emitted:
            continue
        status = _clean(resolution.get("status"))
        if status not in _CLOSED_STATUSES:
            continue
        request = request_by_id.get(finding_id)
        if request is None:
            continue
        claim_text = " ".join(
            part
            for part in (
                _clean(request.get("reviewer_claim")),
                _clean(request.get("instruction")),
            )
            if part
        )
        finding_tokens = _scout_tokens(claim_text)
        if not finding_tokens:
            continue
        token_overlap = bool(finding_tokens & delta_tokens)
        basename_overlap = bool(finding_tokens & delta_basenames)
        if not token_overlap and not basename_overlap:
            errors.append(
                f"evidence_resolutions[{finding_id}].status={status} but the plan did not change "
                "in the scope this finding pointed at — change tasks/files_to_change/"
                "invariants/verify_recipes touching the finding's subject, or set status=ambiguous"
            )
    return errors
