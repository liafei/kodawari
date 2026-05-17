"""Deterministic post-execution review precheck helpers."""

from __future__ import annotations

import json
from pathlib import Path
import shlex
from typing import Any

from kodawari.autopilot.core.task_modes import is_verification_only_task


REVIEW_PRECHECK_SCHEMA_VERSION = "review.precheck.v1"

_SAFE_PYTEST_FLAGS = {
    "-q",
    "-v",
    "-vv",
    "-vvv",
    "-s",
    "-x",
    "--quiet",
    "--verbose",
    "--exitfirst",
    "--disable-warnings",
}
_SAFE_PYTEST_PREFIX_FLAGS = ("--maxfail=", "--tb=")
_BLOCKED_PYTEST_FLAGS = {
    "--collect-only",
    "--co",
    "--fixtures",
    "--funcargs",
    "--fixtures-per-test",
    "--setup-plan",
    "--setup-only",
    "--help",
    "-h",
    "--version",
    "--markers",
}
_SHELL_TOKENS = ("\n", "\r", ";", "|", "<", ">", "&&", "||", "$(", "`")


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _normalize_paths(values: list[str]) -> list[str]:
    normalized: list[str] = []
    for item in values:
        text = str(item).strip().replace("\\", "/")
        if text and text not in normalized:
            normalized.append(text)
    return normalized


def _strip_outer_quotes(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in {"'", '"'}:
        return token[1:-1]
    return token


def _has_shell_structure(command: str) -> bool:
    return any(token in command for token in _SHELL_TOKENS)


def _tokenize_verify_command(command: str) -> list[str]:
    text = str(command or "").strip()
    if not text or _has_shell_structure(text):
        return []
    try:
        tokens = shlex.split(text, posix=False)
    except ValueError:
        return []
    return [_strip_outer_quotes(token) for token in tokens if str(token).strip()]


def _is_python_token(token: str) -> bool:
    stem = Path(str(token or "")).stem.lower()
    return stem == "python" or stem.startswith("python")


def _pytest_arg_tokens(command: str) -> list[str]:
    tokens = _tokenize_verify_command(command)
    if not tokens:
        return []
    first = Path(tokens[0]).stem.lower()
    if first == "pytest":
        return tokens[1:]
    if len(tokens) >= 3 and _is_python_token(tokens[0]) and tokens[1] == "-m" and tokens[2] == "pytest":
        return tokens[3:]
    return []


def _normalize_candidate_path(token: str) -> str:
    text = str(token or "").strip()
    if "::" in text:
        text = text.split("::", 1)[0]
    if not text or any(char in text for char in "*?["):
        return ""
    text = text.replace("\\", "/")
    if text.startswith("../") or text == "..":
        return ""
    if text.startswith("./"):
        text = text[2:]
    return text


def _safe_pytest_positional_paths(command: str) -> list[str]:
    args = _pytest_arg_tokens(command)
    if not args:
        return []
    paths: list[str] = []
    positional_mode = False
    for token in args:
        text = str(token or "").strip()
        if not text:
            continue
        if not positional_mode and text == "--":
            positional_mode = True
            continue
        if not positional_mode and text.startswith("-"):
            if text in _BLOCKED_PYTEST_FLAGS:
                return []
            if text in _SAFE_PYTEST_FLAGS or any(text.startswith(prefix) for prefix in _SAFE_PYTEST_PREFIX_FLAGS):
                continue
            return []
        normalized = _normalize_candidate_path(text)
        if normalized:
            paths.append(normalized)
    return _normalize_paths(paths)


def _repo_file_path(project_root: Path | None, path: str) -> Path | None:
    if project_root is None:
        return None
    try:
        root = Path(project_root).resolve()
        candidate = (root / path).resolve()
        candidate.relative_to(root)
    except (OSError, ValueError):
        return None
    return candidate if candidate.is_file() else None


def _verified_runtime_check(runtime_verify_check: dict[str, Any] | None) -> dict[str, Any]:
    payload = dict(runtime_verify_check or {})
    if str(payload.get("status") or "").strip().upper() != "PASS":
        return {}
    if payload.get("passed") is not True:
        return {}
    if payload.get("command_executed") is not True:
        return {}
    if payload.get("returncode") != 0:
        return {}
    return payload


def _verify_evidence_passed(
    runtime_verify_check: dict[str, Any] | None,
    planning_dir: Path,
) -> bool:
    """Return True if any verify evidence source reports a passing result.

    Intentionally permissive (does not require command_executed) so both the
    compat-placeholder form and the real-execution form are accepted.
    """
    def _passed(payload: Any) -> bool:
        if not isinstance(payload, dict):
            return False
        if payload.get("passed") is False:
            return False
        status = str(payload.get("status") or "").strip().upper()
        if status and status != "PASS":
            return False
        return payload.get("passed") is True or status == "PASS"

    if _passed(runtime_verify_check):
        return True
    exec_payload = _load_json(planning_dir / ".execution_result.json")
    return _passed(exec_payload.get("verify_summary"))


def resolve_verified_test_evidence(
    *,
    project_root: Path | None,
    task_card_files: list[str],
    runtime_verify_check: dict[str, Any] | None,
) -> list[str]:
    """Return scoped test files proven by a conservative runtime verify command.

    The resolver is intentionally narrow: only executed pytest commands with
    simple positional test-file targets are accepted. Unsupported but valid
    pytest options produce no evidence rather than risking a false proof.
    """

    payload = _verified_runtime_check(runtime_verify_check)
    if not payload:
        return []
    command = str(payload.get("verify_cmd_resolved") or "").strip()
    command_paths = _safe_pytest_positional_paths(command)
    if not command_paths:
        return []
    target_paths = _normalize_paths(
        [
            _normalize_candidate_path(str(item))
            for item in list(payload.get("verify_targets") or [])
            if str(item).strip()
        ]
    )
    target_set = {item.lower() for item in target_paths}
    scope_set = {item.lower() for item in _normalize_paths(task_card_files)}
    evidence: list[str] = []
    for path in command_paths:
        lowered = path.lower()
        if target_set and lowered not in target_set:
            continue
        if not is_test_file(path):
            continue
        if scope_set and lowered not in scope_set:
            continue
        if _repo_file_path(project_root, path) is None:
            continue
        if path not in evidence:
            evidence.append(path)
    return evidence


def is_test_file(path: str) -> bool:
    """Path-structure-based test file detection.

    Matches against normalized path segments and well-known suffixes so
    files like ``latest_results.py`` or ``tests_helper.py`` are NOT
    misclassified (substring-``test`` matching is unsafe).
    """
    lowered = str(path or "").strip().replace("\\", "/").lower()
    if not lowered:
        return False
    name = Path(lowered).name
    if "/tests/" in lowered or lowered.startswith("tests/") or "/__tests__/" in lowered:
        return True
    if name.startswith("test_") or name.endswith("_test.py"):
        return True
    if name.endswith(".spec.ts") or name.endswith(".spec.tsx") or name.endswith(".test.ts") or name.endswith(".test.tsx"):
        return True
    if name.endswith(".spec.js") or name.endswith(".spec.jsx") or name.endswith(".test.js") or name.endswith(".test.jsx"):
        return True
    return False


# Backwards-compat alias for internal helpers that referenced the private name.
_is_test_file = is_test_file


# Strict rules for detecting docs-only paths (no testable behavior).
# Tightened on sub-agent review to avoid false positives:
#   * src/docs_helper.py  — substring "docs" elsewhere in path
#   * requirements.txt    — flat .txt file, not docs
#   * tests/fixtures/x.md — fixtures inside tests/ tree
# Match policy: path starts with ``docs/`` OR file extension is .md/.rst/.adoc,
# unless the path is excluded by the bait rules below.
_DOCS_ONLY_EXTENSIONS: tuple[str, ...] = (".md", ".rst", ".adoc")


def is_docs_only_path(path: str) -> bool:
    """Return True iff ``path`` is a docs/markdown artifact with no
    testable behavior. Used by the deterministic review guard to allow
    docs-first task splits (e.g. update plan markdown, then implement)
    to pass the ``test_scope_unavailable_files`` gate.
    """
    lowered = str(path or "").strip().replace("\\", "/").lower()
    if not lowered:
        return False
    if _is_test_file(lowered):
        return False
    name = Path(lowered).name
    # Bait exclusions — these can shape-match docs but are not docs.
    if name.startswith("requirements") and name.endswith(".txt"):
        return False
    if "/fixtures/" in lowered:
        return False
    # Strict positive match.
    if lowered.startswith("docs/"):
        return True
    if any(name.endswith(suffix) for suffix in _DOCS_ONLY_EXTENSIONS):
        return True
    return False


def _module_boundaries(architecture_plan: dict[str, Any]) -> list[dict[str, Any]]:
    boundaries: list[dict[str, Any]] = []
    for raw in list(architecture_plan.get("module_boundaries") or []):
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        if not name:
            continue
        roots = _normalize_paths([str(item) for item in list(raw.get("roots") or []) if str(item).strip()])
        if not roots:
            continue
        boundaries.append({"name": name, "roots": roots})
    return boundaries


def _matched_modules(path: str, boundaries: list[dict[str, Any]]) -> list[str]:
    matches: list[str] = []
    for item in boundaries:
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        for root in list(item.get("roots") or []):
            normalized_root = str(root).strip().rstrip("/")
            if not normalized_root:
                continue
            if path == normalized_root or path.startswith(normalized_root + "/"):
                if name not in matches:
                    matches.append(name)
                break
    return matches


def _has_related_test(source_path: str, test_files: list[str]) -> bool:
    source = Path(source_path)
    stem = source.stem.lower()
    parent = source.parent.name.lower()
    for candidate in test_files:
        lowered = candidate.lower()
        name = Path(lowered).name
        if stem and stem in name:
            return True
        if parent and parent in lowered:
            return True
    return False


def _test_scope_available(task_card_files: list[str]) -> bool:
    normalized = _normalize_paths(task_card_files)
    if not normalized:
        return True
    return any(_is_test_file(path) for path in normalized)


def _required_verify_surfaces(architecture_plan: dict[str, Any]) -> list[str]:
    surfaces: list[str] = []
    for raw in list(architecture_plan.get("verify_recipes") or []):
        if not isinstance(raw, dict):
            continue
        if not bool(raw.get("required")):
            continue
        surface = str(raw.get("surface") or "").strip()
        if surface and surface not in surfaces:
            surfaces.append(surface)
    return surfaces


def _planning_architecture_payload(planning_dir: Path) -> dict[str, Any]:
    conversation = _load_json(planning_dir / "PLANNING_CONVERSATION.json")
    if conversation:
        return {
            "module_boundaries": list(conversation.get("module_boundaries") or []),
            "verify_recipes": list(conversation.get("verify_recipes") or []),
        }
    return _load_json(planning_dir / "ARCHITECTURE_PLAN.json")


def compute_deterministic_findings(
    *,
    planning_dir: Path,
    changed_files: list[str],
    task_card_files: list[str],
    invariants: list[str],
    project_root: Path | None = None,
    runtime_verify_check: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compute machine-checkable findings before peer review."""

    normalized_changed = _normalize_paths(changed_files)
    normalized_task_scope = _normalize_paths(task_card_files)
    normalized_invariants = [str(item).strip() for item in invariants if str(item).strip()]
    architecture_plan = _planning_architecture_payload(planning_dir)
    task_card_payload = _load_json(planning_dir / "TASK_CARD_ACTIVE.json")
    _is_verify_only = is_verification_only_task(task_card_payload)
    boundaries = _module_boundaries(architecture_plan)
    changed_test_files = [path for path in normalized_changed if _is_test_file(path)]
    verified_test_files = resolve_verified_test_evidence(
        project_root=project_root,
        task_card_files=normalized_task_scope,
        runtime_verify_check=runtime_verify_check,
    )
    test_files = _normalize_paths(changed_test_files + verified_test_files)
    source_files = [path for path in normalized_changed if not _is_test_file(path)]

    out_of_scope_files: list[str] = []
    if normalized_task_scope:
        allowed = set(normalized_task_scope)
        out_of_scope_files = [path for path in normalized_changed if path not in allowed]

    # If no test files changed, deterministic review can safely require scoped
    # test coverage. If at least one test changed, do not hard-block purely on
    # path-name heuristics: many repos use task-numbered smoke files
    # (for example tests/test_t002_*.py) whose names cannot be related to the
    # source file without inspecting semantic content. The model reviewer still
    # receives the changed test snippets and can reject inadequate coverage.
    missing_test_candidates = [] if test_files else [path for path in source_files if not _has_related_test(path, test_files)]
    test_scope_available = _test_scope_available(normalized_task_scope)
    missing_test_files = missing_test_candidates if test_scope_available else []
    test_scope_unavailable_files = missing_test_candidates if not test_scope_available else []

    cross_boundary_files: list[dict[str, Any]] = []
    for path in normalized_changed:
        modules = _matched_modules(path, boundaries)
        if len(modules) > 1:
            cross_boundary_files.append({"file": path, "modules": modules})

    verify_surface_gaps: list[str] = []
    required_surfaces = _required_verify_surfaces(architecture_plan)
    if required_surfaces and not test_files:
        if not (_is_verify_only and _verify_evidence_passed(runtime_verify_check, planning_dir)):
            verify_surface_gaps = list(required_surfaces)

    invariant_conflicts: list[str] = []
    if normalized_invariants:
        lowered = " ".join(normalized_invariants).lower()
        if "single source of truth" in lowered and len(source_files) > 1:
            invariant_conflicts.append("single_source_of_truth_risk")

    # docs_only_changes: every actual changed file is a docs/markdown
    # artifact. Used by the guard to allow docs-first task splits to
    # bypass the scoped-tests-required gate. Requires non-empty
    # changed_files — a zero-change run is NOT docs-only (it is
    # ``review_no_changes`` territory and should keep its own treatment).
    docs_only_changes = bool(normalized_changed) and all(
        is_docs_only_path(path) for path in normalized_changed
    )

    return {
        "schema_version": REVIEW_PRECHECK_SCHEMA_VERSION,
        "changed_files": normalized_changed,
        "task_scope_files": normalized_task_scope,
        "invariants": normalized_invariants,
        "changed_test_files": changed_test_files,
        "verified_test_files": verified_test_files,
        "test_evidence_files": test_files,
        "out_of_scope_files": out_of_scope_files,
        "missing_test_files": missing_test_files,
        "test_scope_unavailable_files": test_scope_unavailable_files,
        "cross_boundary_files": cross_boundary_files,
        "verify_surface_gaps": verify_surface_gaps,
        "invariant_conflicts": invariant_conflicts,
        "docs_only_changes": docs_only_changes,
        "is_verification_only_task": _is_verify_only,
    }


def _clean_string_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


def _merge_unique(target: list[str], additions: list[str]) -> list[str]:
    existing = {item.lower() for item in target if item}
    for item in additions:
        normalized = str(item).strip()
        if not normalized:
            continue
        lowered = normalized.lower()
        if lowered in existing:
            continue
        target.append(normalized)
        existing.add(lowered)
    return target


def apply_deterministic_review_guard(
    review: dict[str, Any],
    *,
    deterministic_findings: dict[str, Any],
) -> dict[str, Any]:
    guarded = dict(review)
    out_of_scope_files = _clean_string_list(deterministic_findings.get("out_of_scope_files"))
    missing_test_files = _clean_string_list(deterministic_findings.get("missing_test_files"))
    test_scope_unavailable_files = _clean_string_list(deterministic_findings.get("test_scope_unavailable_files"))
    cross_boundary_files = [
        dict(item)
        for item in list(deterministic_findings.get("cross_boundary_files") or [])
        if isinstance(item, dict)
    ]
    verify_surface_gaps = _clean_string_list(deterministic_findings.get("verify_surface_gaps"))
    if bool(deterministic_findings.get("is_verification_only_task")):
        verify_surface_gaps = []
    docs_only_changes = bool(deterministic_findings.get("docs_only_changes"))
    # For docs-only tasks, missing_test_files and verify_surface_gaps are advisory:
    # docs surfaces have no associated test suite; the verify_cmd is the proof.
    if docs_only_changes:
        missing_test_files = []
        verify_surface_gaps = []
    invariant_conflicts = _clean_string_list(deterministic_findings.get("invariant_conflicts"))

    must_fix = _clean_string_list(guarded.get("must_fix"))
    blocking_items = _clean_string_list(guarded.get("blocking_items"))
    should_fix = _clean_string_list(guarded.get("should_fix"))
    deterministic_reasons: list[str] = []

    if out_of_scope_files:
        deterministic_reasons.append(
            "scope violation: changed files outside task scope: " + ", ".join(out_of_scope_files)
        )
    if missing_test_files:
        deterministic_reasons.append(
            "missing scoped tests for: " + ", ".join(missing_test_files)
        )
    if verify_surface_gaps:
        deterministic_reasons.append(
            "missing required verify surface coverage: " + ", ".join(verify_surface_gaps)
        )
    if invariant_conflicts:
        deterministic_reasons.append(
            "deterministic invariant conflicts: " + ", ".join(invariant_conflicts)
        )
    if cross_boundary_files:
        should_fix = _merge_unique(
            should_fix,
            [
                "explain architectural impact for cross-boundary changes: "
                + ", ".join(
                    f"{str(item.get('file') or '').strip()} ({', '.join(_clean_string_list(item.get('modules')))} )".replace(" )", ")")
                    for item in cross_boundary_files
                    if str(item.get("file") or "").strip()
                )
            ],
        )

    # Apply REVIEW_FIX_REQUIRED first so SCOPE_CONFLICT can take precedence below
    # when tests are structurally impossible to add within the current task scope.
    if deterministic_reasons:
        guarded["approved"] = False
        guarded["gate_recommendation"] = "REVIEW_FIX_REQUIRED"
        guarded["severity"] = "high"
        must_fix = _merge_unique(must_fix, deterministic_reasons)
        blocking_items = _merge_unique(blocking_items, deterministic_reasons)

    if test_scope_unavailable_files:
        # Docs-first task split short-circuit:
        #   * every changed file must be a docs/markdown artifact
        #     (``docs_only_changes=True`` from compute_deterministic_findings)
        #   * AND no other deterministic blocker exists (out_of_scope,
        #     missing_test, verify_surface_gaps, invariant_conflicts)
        # When both hold, the test-scope-unavailable signal is treated as
        # advisory: the deterministic-injected reason strings are stripped
        # from must_fix/blocking_items so collaboration_core's
        # ``_resolve_review_approved`` does not flip approved back to False.
        # The reviewer's own must_fix items (added before this guard runs)
        # are preserved verbatim — we never silently clear them.
        docs_only_changes = bool(deterministic_findings.get("docs_only_changes"))
        no_other_blockers = not (
            out_of_scope_files
            or missing_test_files
            or verify_surface_gaps
            or invariant_conflicts
        )
        if docs_only_changes and no_other_blockers:
            advisory_msg = (
                "Docs-only task: scoped-test requirement deferred to a downstream code "
                "task in the same TASK_GRAPH. The follow-up task must add the test "
                "coverage; this task's review is advisory on that ground alone."
            )
            # Strip ONLY the deterministic-injected reasons we added above
            # (line 425-440) — leave reviewer's own findings intact.
            deterministic_reason_set = set(deterministic_reasons)
            must_fix = [item for item in must_fix if item not in deterministic_reason_set]
            blocking_items = [item for item in blocking_items if item not in deterministic_reason_set]
            should_fix = _merge_unique(should_fix, [advisory_msg])
            guarded["approved"] = True
            guarded["gate_recommendation"] = "PROCEED_TO_GATE"
            guarded["severity"] = "info"
            guarded["docs_only_proceed"] = True
            guarded.pop("blocking_reason", None)
        else:
            scope_conflict_reason = (
                "scoped tests are required for "
                + ", ".join(test_scope_unavailable_files)
                + " but current task scope does not include any test files; widen files_to_change or add a follow-up test task"
            )
            guarded["approved"] = False
            guarded["gate_recommendation"] = "REVIEW_SCOPE_CONFLICT"
            guarded["severity"] = "high"
            guarded["blocking_reason"] = scope_conflict_reason
            blocking_items = _merge_unique(blocking_items, [scope_conflict_reason])
            should_fix = _merge_unique(
                should_fix,
                ["Replan or widen the task scope before requiring scoped test updates."],
            )

    guarded["must_fix"] = must_fix
    guarded["blocking_items"] = blocking_items
    guarded["should_fix"] = should_fix
    return guarded


__all__ = [
    "REVIEW_PRECHECK_SCHEMA_VERSION",
    "apply_deterministic_review_guard",
    "compute_deterministic_findings",
    "is_test_file",
    "resolve_verified_test_evidence",
]
