"""Build kodawari self-repair proposals from run truth artifacts.

The proposal is intentionally a task artifact, not an auto patch. Runtime
failures often happen while kodawari is driving another project, so the
safe autonomous step is to convert evidence into a precise SDK repair task
that a following workflow run can execute and review.

Implementation status (see WORKFLOW_SELF_REPAIR_PHASES below):
    Phase 1 — failure classifier:                 partial (this module)
    Phase 2 — proposal artifact + markdown:       implemented (this module)
    Phase 3 — env-gated auto-execution of repair: implemented but off by default
              (kodawari.cli.evidence.self_repair_execute, opt-in via
              WORKFLOW_SELF_REPAIR_AUTO_EXECUTE=1 +
              WORKFLOW_SELF_REPAIR=1 + WORKFLOW_SDK_SELF_REPAIR_ROOT)
    Phase 4 — prompt-lesson learning on success:  implemented
              (kodawari.cli.evidence.self_repair_learn, only emits
              prompt_lessons on Level-2 validation: SDK fix + target rerun
              advanced past original stop_reason)

``safety.auto_apply_allowed`` stays False on the proposal payload — Phase 3
does not silent-patch. It spawns a fresh kodawari autopilot run that
goes through planner / review / verify / gate; any code change still
requires the spawned run to pass review.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any

from kodawari.cli.evidence.artifact_truth import load_run_truth
from kodawari.infra.io_atomic import atomic_write_canonical_json, atomic_write_text

SELF_REPAIR_FILENAME = ".workflow_self_repair.json"
SELF_REPAIR_MARKDOWN_FILENAME = "SELF_REPAIR.md"
SELF_REPAIR_SCHEMA_VERSION = "workflow.self_repair.v1"

WORKFLOW_SELF_REPAIR_PHASES: dict[str, str] = {
    "phase_1_classifier": "partial",
    "phase_2_proposal_artifact": "implemented",
    "phase_3_auto_execute_gate": "implemented_opt_in",
    "phase_4_post_success_learning": "implemented_opt_in",
}


def build_self_repair_proposal(
    *,
    project_root: Path,
    planning_dir: Path,
    run_truth: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a self-repair proposal for workflow-runtime failures.

    Status semantics:
      ``ready``           — failure has a known classifier, repair_task is
                            populated, target_files have been validated for
                            SDK-root containment.
      ``triage_required`` — failure has a code but no specialized
                            classifier; needs human review.
      ``not_applicable``  — run succeeded, or failure is environment-class
                            (turn limit, timeout, auth, missing executable,
                            OOM, network) and routes to doctor/config
                            diagnosis instead of SDK code repair.
    """

    root = Path(project_root).resolve()
    planning = Path(planning_dir).resolve()
    sdk_root = _find_kodawari_root()
    truth = dict(run_truth or load_run_truth(planning) or {})
    artifacts = _load_artifacts(planning)
    truth_fresh = _truth_is_fresh(truth, artifacts)
    payload = _base_payload(
        project_root=root,
        planning_dir=planning,
        run_truth=truth,
        sdk_root=sdk_root,
        truth_fresh=truth_fresh,
    )
    payload["evidence"] = _evidence_index(truth=truth, artifacts=artifacts)
    status, classification = _classify_failure(truth=truth, artifacts=artifacts, truth_fresh=truth_fresh)
    if status == "not_applicable":
        return _finalize_not_applicable(payload, classification, sdk_root=sdk_root)
    if status == "triage_required":
        return _finalize_triage_required(payload, classification, sdk_root=sdk_root)
    return _finalize_ready(payload, classification, sdk_root=sdk_root, truth_fresh=truth_fresh)


def _finalize_not_applicable(
    payload: dict[str, Any],
    classification: dict[str, Any],
    *,
    sdk_root: Path,
) -> dict[str, Any]:
    payload.update(
        {
            "status": "not_applicable",
            "reason": str(classification.get("reason") or ""),
            "root_cause": {},
            "repair_task": {},
            "safety": _safety_payload(
                auto_apply_allowed=False, sdk_root=sdk_root, rejected_target_files=[]
            ),
        }
    )
    if "environment_error_code" in classification:
        payload["environment_error_code"] = classification["environment_error_code"]
    return payload


def _finalize_triage_required(
    payload: dict[str, Any],
    classification: dict[str, Any],
    *,
    sdk_root: Path,
) -> dict[str, Any]:
    unhandled = str(classification.get("unhandled_code") or "")
    payload.update(
        {
            "status": "triage_required",
            "reason": str(classification.get("reason") or "unsupported_workflow_failure"),
            "unhandled_code": unhandled,
            "root_cause": {
                "code": "unsupported_workflow_failure",
                "confidence": 0.0,
                "summary": (
                    f"Failure code {unhandled or 'unknown'} has no specialized "
                    "self-repair classifier. Routed to manual triage."
                ),
            },
            "repair_task": {},
            "safety": _safety_payload(
                auto_apply_allowed=False, sdk_root=sdk_root, rejected_target_files=[]
            ),
        }
    )
    return payload


def _finalize_ready(
    payload: dict[str, Any],
    classification: dict[str, Any],
    *,
    sdk_root: Path,
    truth_fresh: bool,
) -> dict[str, Any]:
    """Apply target_files containment and lower confidence if truth was
    stale (we may have classified off transient evidence)."""

    repair_task = dict(classification.get("repair_task") or {})
    raw_targets = list(repair_task.get("target_files") or [])
    safe_targets, rejected_targets = _filter_safe_target_files(raw_targets, sdk_root=sdk_root)
    repair_task["target_files"] = safe_targets
    root_cause = dict(classification.get("root_cause") or {})
    if not truth_fresh and root_cause:
        root_cause["confidence"] = round(max(0.0, float(root_cause.get("confidence") or 0.0) - 0.2), 3)
        root_cause["confidence_adjustment_reason"] = "run_truth_stale"
    payload.update(
        {
            "status": "ready",
            "root_cause": root_cause,
            "repair_task": repair_task,
            "evidence": payload["evidence"] + list(classification.get("evidence") or []),
            "safety": _safety_payload(
                auto_apply_allowed=False,
                sdk_root=sdk_root,
                rejected_target_files=rejected_targets,
            ),
        }
    )
    return payload


def write_self_repair_proposal(planning_dir: Path, payload: dict[str, Any]) -> Path:
    path = Path(planning_dir).resolve() / SELF_REPAIR_FILENAME
    atomic_write_canonical_json(path, payload)
    return path


def render_self_repair_markdown(payload: dict[str, Any]) -> str:
    status = str(payload.get("status") or "")
    if status == "triage_required":
        return _render_triage_markdown(payload)
    if status != "ready":
        return "# Workflow Self-Repair\n\nNo actionable kodawari self-repair proposal was generated.\n"
    root_cause = payload.get("root_cause") if isinstance(payload.get("root_cause"), dict) else {}
    task = payload.get("repair_task") if isinstance(payload.get("repair_task"), dict) else {}
    safety = payload.get("safety") if isinstance(payload.get("safety"), dict) else {}
    lines = ["# Workflow Self-Repair", ""]
    lines.extend(_render_root_cause_section(root_cause))
    lines.extend(_render_repair_task_section(task))
    lines.extend(_render_target_files_section(task, safety))
    lines.extend(_render_list_section("Suggested Tests", task.get("suggested_tests"), code_format=True))
    lines.extend(_render_list_section("Acceptance", task.get("acceptance")))
    lines.extend(_render_evidence_section(payload.get("evidence")))
    return "\n".join(lines).rstrip() + "\n"


def _render_root_cause_section(root_cause: dict[str, Any]) -> list[str]:
    return [
        "## Root Cause",
        "",
        f"- code: {root_cause.get('code') or 'unknown'}",
        f"- confidence: {root_cause.get('confidence') or 0}",
        f"- summary: {root_cause.get('summary') or ''}",
        "",
    ]


def _render_repair_task_section(task: dict[str, Any]) -> list[str]:
    return [
        "## Repair Task",
        "",
        str(task.get("title") or "").strip(),
        "",
        "## Task Direction",
        "",
        str(task.get("task_direction") or "").strip(),
        "",
    ]


def _render_target_files_section(task: dict[str, Any], safety: dict[str, Any]) -> list[str]:
    lines: list[str] = ["## Target Files", ""]
    for item in list(task.get("target_files") or []):
        lines.append(f"- {item}")
    rejected = list(safety.get("rejected_target_files") or [])
    if not rejected:
        return lines
    lines.extend(["", "## Rejected Target Files (path containment / denylist)", ""])
    for item in rejected:
        if isinstance(item, dict):
            lines.append(f"- {item.get('path') or item}: {item.get('reason') or ''}")
        else:
            lines.append(f"- {item}")
    return lines


def _render_list_section(title: str, items: Any, *, code_format: bool = False) -> list[str]:
    out = ["", f"## {title}", ""]
    for item in list(items or []):
        if code_format:
            out.append(f"- `{item}`")
        else:
            out.append(f"- {item}")
    return out


def _render_evidence_section(evidence: Any) -> list[str]:
    out = ["", "## Evidence", ""]
    for item in list(evidence or []):
        if not isinstance(item, dict):
            continue
        source = item.get("source") or item.get("artifact") or "evidence"
        summary = item.get("summary") or item.get("value") or ""
        out.append(f"- {source}: {summary}")
    return out


def write_self_repair_markdown(planning_dir: Path, payload: dict[str, Any]) -> Path:
    path = Path(planning_dir).resolve() / SELF_REPAIR_MARKDOWN_FILENAME
    atomic_write_text(path, render_self_repair_markdown(payload))
    return path


# --- Helpers: SDK root + path containment ---------------------------------


def _find_kodawari_root() -> Path:
    """Locate the kodawari repo root for path containment checks.

    Default: derive from this module's __file__ (we live at
    ``src/kodawari/cli/evidence/self_repair.py`` — go up 4 parents).
    Override via ``WORKFLOW_SDK_SELF_REPAIR_ROOT`` for cases where the SDK
    is imported from an unusual location (e.g. a vendored copy).
    """
    raw = os.environ.get("WORKFLOW_SDK_SELF_REPAIR_ROOT", "").strip()
    if raw:
        return Path(raw).resolve()
    return Path(__file__).resolve().parents[4]


_TARGET_FILES_DENYLIST: tuple[str, ...] = (
    # Test infrastructure that affects all tests; mistakes here cascade.
    "tests/conftest.py",
    # Build / packaging / repo-root metadata.
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "MANIFEST.in",
    # Operator scripts and ratchet baselines — high blast radius, low
    # likelihood that an executor stall is a baseline issue.
    "scripts/",
    "_baseline/",
    # Local runtime / venv.
    ".workflow_runtime/",
    ".venv/",
    # The CLI runtime that's actually executing the self-repair invocation.
    # Editing it mid-run is the main "modify the code that's running" risk.
    "src/kodawari/cli/runtime/",
    # Git / CI metadata.
    ".git/",
    ".github/",
)


def _is_relative_to(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _filter_safe_target_files(
    target_files: list[str],
    *,
    sdk_root: Path,
) -> tuple[list[str], list[dict[str, str]]]:
    """Validate target_files for SDK-root containment and denylist.

    Returns ``(safe, rejected)``. ``rejected`` items carry ``{path, reason}``
    so consumers (and the rendered markdown) can show why a path was
    dropped. Rejection reasons:
      ``absolute_path``    — path is absolute (must be SDK-relative)
      ``outside_sdk_root`` — path resolves outside SDK repo (``..`` etc.)
      ``denylisted``       — path is in a high-blast-radius location
    """
    safe: list[str] = []
    rejected: list[dict[str, str]] = []
    sdk_root_resolved = sdk_root.resolve()
    for raw in target_files:
        rel = str(raw or "").strip().replace("\\", "/")
        if not rel:
            continue
        if Path(rel).is_absolute():
            rejected.append({"path": rel, "reason": "absolute_path"})
            continue
        candidate = (sdk_root_resolved / rel).resolve()
        if not _is_relative_to(candidate, sdk_root_resolved):
            rejected.append({"path": rel, "reason": "outside_sdk_root"})
            continue
        normalized = candidate.relative_to(sdk_root_resolved).as_posix()
        if any(
            normalized == prefix.rstrip("/") or normalized.startswith(prefix)
            for prefix in _TARGET_FILES_DENYLIST
        ):
            rejected.append({"path": rel, "reason": "denylisted"})
            continue
        safe.append(normalized)
    return safe, rejected


# --- Helpers: run_truth freshness -----------------------------------------


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _truth_is_fresh(truth: dict[str, Any], artifacts: dict[str, dict[str, Any]]) -> bool:
    """Return True iff run_truth has the fields required to classify and
    is no older than the most recent on-disk artifact.

    Stale truth (missing fields, or generated_at older than the artifact
    files it claims to summarize) is itself a known failure mode. We do
    not block classification on it — the build proceeds with reduced
    confidence (see build_self_repair_proposal). But callers consuming
    the proposal should respect the ``truth_freshness`` flag in the
    payload.
    """
    if not truth:
        return False
    final_status = _clean(truth.get("final_status"))
    run_reason = _clean(truth.get("run_reason"))
    blocking_reason = _clean(truth.get("blocking_reason"))
    if not final_status or not (run_reason or blocking_reason):
        return False
    truth_ts = _parse_iso(truth.get("generated_at"))
    if truth_ts is None:
        return True
    artifact_timestamps: list[datetime] = []
    for name in (
        ".execution_failure_snapshot.json",
        ".execution_stall_report.json",
        ".execution_result.json",
        ".task_run_result.json",
    ):
        payload = artifacts.get(name) or {}
        ts = _parse_iso(payload.get("generated_at"))
        if ts is not None:
            artifact_timestamps.append(ts)
    if not artifact_timestamps:
        return True
    return truth_ts >= max(artifact_timestamps)


# --- Helpers: env vs workflow error classification ------------------------


_WORKFLOW_INTERNAL_PATTERNS: tuple[str, ...] = (
    # Recovery synthesizer timeout is a workflow design issue, not env.
    "RECOVERY_SYNTHESIZER",
    # Executor stalls are runtime behavior we can fix in code.
    "EXECUTOR_STALLED",
    "EXECUTOR_FIX_ROUND",
    # Planner tool-use checkpoint failures are workflow-control issues, not
    # remote model environment problems.
    "PLANNER_TOOL_USE_CHECKPOINT",
    "PLANNER_OUTPUT_TRUNCATED_EMPTY",
    "PLANNER_EMPTY_OUTPUT",
    "PLANNER_TRANSPORT",
    # Planning escalation / max rounds are planning logic issues.
    "PLANNING_ESCALATION",
    "PLANNING_MAX_ROUNDS",
    # Readiness blocks are workflow contract issues.
    "READINESS_BLOCKED",
    "BLOCKED_BY_PRECONDITION",
)


_ENVIRONMENT_ERROR_PATTERNS: tuple[str, ...] = (
    # Subprocess turn / wall-clock budgets exhausted.
    "MAX_TURNS",
    "MAXIMUM_TURNS",
    "PLANNER_TIMEOUT",
    "REVIEWER_TIMEOUT",
    "WALL_CLOCK_TIMEOUT",
    "WALLCLOCK_TIMEOUT",
    # Auth / permission problems with the model gateway or local CLI.
    "AUTH_FORBIDDEN",
    "UNAUTHORIZED",
    "FORBIDDEN",
    "API_KEY_MISSING",
    # Missing or wrong CLI executable.
    "EXECUTABLE_MISSING",
    "EXECUTABLE_NOT_FOUND",
    "COMMAND_NOT_FOUND",
    # Filesystem permission / capacity errors that masquerade as exec failures.
    "HOME_ACCESS",
    "EACCES",
    "EPERM",
    "ENOSPC",
    "DISK_FULL",
    # Resource exhaustion.
    "OOM",
    "OUT_OF_MEMORY",
    "RATE_LIMIT",
    "QUOTA_EXCEEDED",
    "RATE_LIMITED",
    # Codex-specific session lifecycle issues.
    "NESTED_SESSION",
    "SESSION_EXPIRED",
)


def _is_environment_error(code: str) -> bool:
    """Environment-class errors (turn budget, timeout, auth, missing
    executable, disk/memory, rate limit, session lifecycle) are NOT
    eligible for SDK code self-repair — they route to doctor/config
    diagnosis instead.

    Workflow-internal patterns (e.g. ``RECOVERY_SYNTHESIZER_TIMEOUT``)
    must take precedence over env patterns: the substring ``TIMEOUT``
    appears in both ``RECOVERY_SYNTHESIZER_TIMEOUT`` (workflow-class —
    the synthesizer was a poor choice) and ``PLANNER_TIMEOUT`` (env-class
    — bump the env var). The order of the two checks here is load-bearing.
    """
    if not code:
        return False
    upper = code.upper()
    if any(pattern in upper for pattern in _WORKFLOW_INTERNAL_PATTERNS):
        return False
    return any(pattern in upper for pattern in _ENVIRONMENT_ERROR_PATTERNS)


# --- Helpers: classification ----------------------------------------------


def _dispatch_classifier(code: str, artifacts: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """Allowlist dispatch: only known codes get a specialized classifier.

    Falls through to ``_generic_executor_stall_proposal`` for unknown
    EXECUTOR_STALLED_* codes (a controlled extension point). Any other
    unknown code returns None → triage_required upstream.

    The lookup table is built lazily (function references resolve at call
    time, not module load time) so it can reference proposals defined
    later in this file.
    """
    table: dict[str, Any] = {
        "EXECUTOR_STALLED_FRAGMENTED_READS": _fragmented_read_proposal,
        "EXECUTOR_STALLED_BUDGET_PRESSURE": _budget_no_write_proposal,
        "EXECUTOR_STALLED_PATCH_PLAN_REQUIRED": _patch_plan_required_proposal,
        "EXECUTOR_FIX_ROUND_UNPRODUCTIVE": _unproductive_fix_round_proposal,
        "REVIEWER_DRIFT_DETECTED": _reviewer_drift_proposal,
        "PLANNING_ESCALATION_REQUIRED": _planning_contradiction_proposal,
        "PLANNING_MAX_ROUNDS_EXHAUSTED": _planning_contradiction_proposal,
        "PLANNER_REVIEWER_DEADLOCK": _planner_reviewer_deadlock_proposal,
        "STUBBORN_ROUND_LIMIT": _planner_reviewer_deadlock_proposal,
        "TASK_INPUT_INFEASIBLE_SURFACE": _task_input_infeasible_proposal,
        "PLANNER_ENVIRONMENT_ERROR:PLANNER_TRANSPORT_TIMEOUT": _planner_transport_or_output_proposal,
        "PLANNER_ENVIRONMENT_ERROR:PLANNER_OUTPUT_TRUNCATED_EMPTY": _planner_transport_or_output_proposal,
        "PLANNER_ENVIRONMENT_ERROR:PLANNER_EMPTY_OUTPUT": _planner_transport_or_output_proposal,
        "PLANNER_TRANSPORT_TIMEOUT": _planner_transport_or_output_proposal,
        "PLANNER_OUTPUT_TRUNCATED_EMPTY": _planner_transport_or_output_proposal,
        "PLANNER_EMPTY_OUTPUT": _planner_transport_or_output_proposal,
    }
    # Layer C fix: PLANNING_ESCALATION_REQUIRED / PLANNING_MAX_ROUNDS_EXHAUSTED
    # are the live escalation codes that surface when the planner-reviewer loop
    # bottoms out on semantic closure. Default-routing them to
    # _planning_contradiction_proposal makes _semantic_closure_proposal dead
    # code on the most common path. Prefer semantic_closure when its strong
    # markers fire; otherwise fall through to contradiction.
    if code in {"PLANNING_ESCALATION_REQUIRED", "PLANNING_MAX_ROUNDS_EXHAUSTED"}:
        if _looks_like_semantic_closure_failure(artifacts):
            return _semantic_closure_proposal(artifacts)
        return _planning_contradiction_proposal(artifacts)
    specialized = table.get(code)
    if specialized is not None:
        return specialized(artifacts)
    if code.startswith("EXECUTOR_STALLED_"):
        return _generic_executor_stall_proposal(code, artifacts)
    if _looks_like_planner_transport_or_output_failure(artifacts):
        return _planner_transport_or_output_proposal(artifacts)
    if _looks_like_semantic_closure_failure(artifacts):
        return _semantic_closure_proposal(artifacts)
    if _looks_like_planning_contradiction(artifacts):
        return _planning_contradiction_proposal(artifacts)
    return None


def _classify_failure(
    *,
    truth: dict[str, Any],
    artifacts: dict[str, dict[str, Any]],
    truth_fresh: bool,
) -> tuple[str, dict[str, Any]]:
    """Return ``(status, classification_payload)``. See
    build_self_repair_proposal for status semantics."""

    if _run_succeeded(truth):
        return "not_applicable", {"reason": "run_succeeded"}
    # RECOVERY_SYNTHESIZER_TIMEOUT must be checked before _is_environment_error
    # — the substring "TIMEOUT" would otherwise route it to env-class.
    if _has_recovery_timeout(truth=truth, artifacts=artifacts):
        return "ready", _recovery_timeout_proposal(artifacts)
    code = _primary_failure_code(truth=truth, artifacts=artifacts, truth_fresh=truth_fresh)
    if _has_planner_checkpoint_invalid_json(truth=truth, artifacts=artifacts, code=code):
        return "ready", _planner_checkpoint_invalid_json_proposal(artifacts)
    if not code:
        return "not_applicable", {"reason": "no_workflow_runtime_failure_code"}
    if _is_environment_error(code):
        return "not_applicable", {"reason": "environment_error", "environment_error_code": code}
    proposal = _dispatch_classifier(code, artifacts)
    if proposal is not None:
        return "ready", proposal
    return "triage_required", {"reason": "unsupported_workflow_failure", "unhandled_code": code}


def _primary_failure_code(
    *,
    truth: dict[str, Any],
    artifacts: dict[str, dict[str, Any]],
    truth_fresh: bool,
) -> str:
    """Pick the most informative failure code across artifacts.

    Source order is failure-cause-first, status-last:

      1. ``.execution_failure_snapshot.json``    — terminal failure cause
         (engine writes this when finalizing a failure with a structured
         decision). Most authoritative for ``why did the run fail``.
      2. ``.execution_stall_report.json``        — most recent stall before
         terminal failure. Terminal cause when no synthesizer round ran.
      3. ``.execution_result.json``              — execution outcome with
         explicit error_code, when the executor wrote one.
      4. ``.task_run_result.json``               — task wrapper outcome.
      5. ``.planning_failure.json``              — planning-side failures.
      6. ``run_truth.run_reason / blocking_reason`` (only when truth is
         fresh). This is intentionally last: ``run_reason`` is often a
         summary status (e.g. ``EXECUTION_BACKEND_BLOCKED``) rather than
         a specific failure code, and using it first would obscure the
         underlying cause that lives in the artifacts above.

    The plan asks for ``run_truth 终态`` first — ``_run_succeeded`` already
    consults run_truth.final_status for the gate decision (run vs no-run).
    For the *code-level* classification this lower position is correct.
    """
    snapshot = artifacts.get(".execution_failure_snapshot.json", {})
    stall = artifacts.get(".execution_stall_report.json", {})
    execution = artifacts.get(".execution_result.json", {})
    task_result = artifacts.get(".task_run_result.json", {})
    planning_failure = artifacts.get(".planning_failure.json", {})
    candidates: list[Any] = [
        snapshot.get("error_code"),
        snapshot.get("reason"),
        stall.get("error_code"),
        stall.get("reason"),
        execution.get("error_code"),
        execution.get("blocking_reason"),
        task_result.get("error_code"),
        task_result.get("reason"),
        planning_failure.get("error_code"),
        planning_failure.get("reason"),
    ]
    if truth_fresh:
        candidates.extend([truth.get("blocking_reason"), truth.get("run_reason")])
    for value in candidates:
        code = _clean(value).upper()
        if code:
            return code
    return ""


def _run_succeeded(truth: dict[str, Any]) -> bool:
    status = _clean(truth.get("final_status")).upper()
    reason = _clean(truth.get("run_reason")).upper()
    return status in {"OK", "PASS", "PROCEED_TO_GATE", "PROCEED_TO_RELEASE"} or reason in {
        "PROCEED_TO_GATE",
        "PROCEED_TO_RELEASE",
        "PIPELINE_FINISH",
    }


def _has_recovery_timeout(*, truth: dict[str, Any], artifacts: dict[str, dict[str, Any]]) -> bool:
    snapshot = artifacts.get(".execution_failure_snapshot.json", {})
    candidates = (
        truth.get("run_reason"),
        truth.get("blocking_reason"),
        snapshot.get("reason"),
        snapshot.get("error_code"),
    )
    return any(_clean(item).upper() == "RECOVERY_SYNTHESIZER_TIMEOUT" for item in candidates)


def _is_planner_checkpoint_invalid_json_code(value: Any) -> bool:
    text = _clean(value).lower()
    return "planner_tool_use_checkpoint_invalid_json" in text


def _has_planner_checkpoint_invalid_json(
    *,
    truth: dict[str, Any],
    artifacts: dict[str, dict[str, Any]],
    code: str = "",
) -> bool:
    planning_failure = artifacts.get(".planning_failure.json", {})
    escalation = planning_failure.get("escalation") if isinstance(planning_failure.get("escalation"), dict) else {}
    candidates = (
        code,
        truth.get("run_reason"),
        truth.get("blocking_reason"),
        planning_failure.get("error_code"),
        planning_failure.get("reason"),
        escalation.get("termination_reason") if isinstance(escalation, dict) else "",
        escalation.get("environment_error_kind") if isinstance(escalation, dict) else "",
    )
    if any(_is_planner_checkpoint_invalid_json_code(item) for item in candidates):
        return True
    trace = artifacts.get(".planner_tool_use_trace.jsonl", {})
    if not isinstance(trace, dict):
        return False
    return bool(trace.get("final_decision_checkpoint_parse_failed"))


# --- Helpers: classifier proposals ----------------------------------------


def _fragmented_read_proposal(artifacts: dict[str, dict[str, Any]]) -> dict[str, Any]:
    stall = artifacts.get(".execution_stall_report.json", {})
    path = _fragmented_read_path(stall)
    return {
        "root_cause": {
            "code": "executor_fragmented_read_loop",
            "confidence": 0.95,
            "summary": "Executor kept slicing the same file into many partial reads without writing.",
        },
        "repair_task": {
            "title": "Harden openai_tool_use fragmented-read discipline",
            "task_direction": (
                "Fix kodawari executor behavior for fragmented read loops. Use the stall report to ensure "
                "read_file_partial windows on the same path stop earning progress after the path is saturated, "
                "raise EXECUTOR_STALLED_FRAGMENTED_READS early, route the failure into deterministic recovery, "
                "and add regression coverage for the observed path."
            ),
            "target_files": [
                "src/kodawari/autopilot/execution/tool_use_stall.py",
                "src/kodawari/autopilot/execution/execution_openai_tool_use.py",
                "src/kodawari/autopilot/engine/engine_recovery_mixin.py",
                "tests/test_read_discipline.py",
                "tests/test_read_discipline_integration.py",
            ],
            "suggested_tests": [
                "pytest -q tests/test_read_discipline.py tests/test_read_discipline_integration.py",
                "pytest -q tests/test_stall_recovery.py",
            ],
            "acceptance": [
                "A 9KB file read in many 300-byte windows blocks with EXECUTOR_STALLED_FRAGMENTED_READS before budget pressure.",
                "The stall report records the offending path and window count.",
                "The recovery layer sees this as a workflow-runtime repair signal, not a product-code failure.",
            ],
        },
        "evidence": [
            {"source": ".execution_stall_report.json", "summary": f"fragmented path={path or 'unknown'}"},
        ],
    }


def _budget_no_write_proposal(artifacts: dict[str, dict[str, Any]]) -> dict[str, Any]:
    stall = artifacts.get(".execution_stall_report.json", {})
    counters = stall.get("counters") if isinstance(stall.get("counters"), dict) else {}
    return {
        "root_cause": {
            "code": "executor_budget_no_write_loop",
            "confidence": 0.9,
            "summary": "Executor spent the token budget while still making no write or patch progress.",
        },
        "repair_task": {
            "title": "Route budget-pressure no-write stalls into deterministic recovery",
            "task_direction": (
                "Fix kodawari recovery handling for EXECUTOR_STALLED_BUDGET_PRESSURE. Classify the stall from "
                ".execution_stall_report.json, avoid another broad reread cycle, and either produce a narrow "
                "recovery card or yield to the synthesizer with a compact failure snapshot."
            ),
            "target_files": [
                "src/kodawari/autopilot/execution/tool_use_stall.py",
                "src/kodawari/autopilot/recovery/registry.py",
                "src/kodawari/autopilot/engine/engine_recovery_mixin.py",
                "tests/test_stall_recovery.py",
            ],
            "suggested_tests": ["pytest -q tests/test_stall_recovery.py tests/test_execution_openai_tool_use.py"],
            "acceptance": [
                "Budget pressure with no writes emits a structured detector match.",
                "Repeated deterministic attempts do not loop forever.",
                "The failure snapshot is small enough for recovery synthesis.",
            ],
        },
        "evidence": [
            {
                "source": ".execution_stall_report.json",
                "summary": f"no_write_iterations={counters.get('no_write_iterations', 0)}",
            }
        ],
    }


def _patch_plan_required_proposal(artifacts: dict[str, dict[str, Any]]) -> dict[str, Any]:
    stall = artifacts.get(".execution_stall_report.json", {})
    counters = stall.get("counters") if isinstance(stall.get("counters"), dict) else {}
    return {
        "root_cause": {
            "code": "executor_patch_plan_required",
            "confidence": 0.9,
            "summary": (
                "Executor entered patch-plan-required mode and kept reading without producing a patch — "
                "deterministic recovery should turn this into a write-first recovery card."
            ),
        },
        "repair_task": {
            "title": "Route EXECUTOR_STALLED_PATCH_PLAN_REQUIRED into deterministic write-first recovery",
            "task_direction": (
                "Fix kodawari recovery handling for EXECUTOR_STALLED_PATCH_PLAN_REQUIRED. The stall report "
                "shows the executor exhausted reads without writes after the engine asked for a patch plan. "
                "Confirm the no_write_stall detector accepts this code, the recovery card is produced, and the "
                "executor cannot loop on additional reads after the recovery card is in scope."
            ),
            "target_files": [
                "src/kodawari/autopilot/recovery/registry.py",
                "src/kodawari/autopilot/recovery/stall_recovery.py",
                "src/kodawari/autopilot/execution/tool_use_runtime.py",
                "tests/test_stall_recovery.py",
            ],
            "suggested_tests": ["pytest -q tests/test_stall_recovery.py"],
            "acceptance": [
                "EXECUTOR_STALLED_PATCH_PLAN_REQUIRED matches the no_write_stall deterministic detector.",
                "Recovery card is produced within max_recovery_attempts.",
                "Tests cover the patch-plan-required path explicitly.",
            ],
        },
        "evidence": [
            {
                "source": ".execution_stall_report.json",
                "summary": f"no_write_iterations={counters.get('no_write_iterations', 0)} patch_plan_required",
            }
        ],
    }


def _recovery_timeout_proposal(artifacts: dict[str, dict[str, Any]]) -> dict[str, Any]:
    snapshot = artifacts.get(".execution_failure_snapshot.json", {})
    return {
        "root_cause": {
            "code": "recovery_synthesizer_timeout",
            "confidence": 0.9,
            "summary": "Recovery synthesizer hit its timeout instead of returning a bounded fallback decision.",
        },
        "repair_task": {
            "title": "Bound recovery synthesizer work and preserve compact snapshots",
            "task_direction": (
                "Fix kodawari recovery synthesis timeout behavior. Keep the hard timeout, write a compact "
                ".execution_failure_snapshot.json, and ensure timeout does not consume another recovery attempt "
                "or hide the original detector evidence."
            ),
            "target_files": [
                "src/kodawari/autopilot/engine/engine_recovery_mixin.py",
                "src/kodawari/autopilot/execution/local_adapter_recovery.py",
                "tests/test_stall_recovery.py",
            ],
            "suggested_tests": ["pytest -q tests/test_stall_recovery.py"],
            "acceptance": [
                "Synthesizer timeout returns BLOCKED with RECOVERY_SYNTHESIZER_TIMEOUT.",
                "The failure snapshot contains the original stall or review evidence.",
                "The timeout path does not erase deterministic recovery telemetry.",
            ],
        },
        "evidence": [
            {
                "source": ".execution_failure_snapshot.json",
                "summary": _clean(snapshot.get("reason") or snapshot.get("error_code") or "snapshot present"),
            }
        ],
    }


def _unproductive_fix_round_proposal(artifacts: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "root_cause": {
            "code": "executor_fix_round_unproductive",
            "confidence": 0.88,
            "summary": "Peer-review fix rounds repeated without adding any new changed files.",
        },
        "repair_task": {
            "title": "Terminate unproductive peer-review fix rounds deterministically",
            "task_direction": (
                "Fix kodawari peer-review loop convergence for EXECUTOR_FIX_ROUND_UNPRODUCTIVE. "
                "Use cumulative changed-file deltas around FIX_ROUND/CODEX_FIX dispatch, terminate after "
                "a bounded no-new-write streak, and keep self-repair plus prompt lesson routing connected."
            ),
            "target_files": [
                "src/kodawari/autopilot/engine/loop_runner.py",
                "src/kodawari/autopilot/engine/engine_recovery_mixin.py",
                "src/kodawari/cli/evidence/self_repair.py",
                "tests/test_loop_runner.py",
                "tests/test_self_repair.py",
            ],
            "suggested_tests": ["pytest -q tests/test_loop_runner.py tests/test_self_repair.py"],
            "acceptance": [
                "Two consecutive fix rounds with no new changed files finish as EXECUTOR_FIX_ROUND_UNPRODUCTIVE.",
                "Any fix round that adds a new changed file resets the unproductive streak.",
                "Self-repair classifies the terminal reason as executor_fix_round_unproductive.",
            ],
        },
        "evidence": [
            {"source": ".execution_failure_snapshot.json", "summary": "EXECUTOR_FIX_ROUND_UNPRODUCTIVE"},
        ],
    }


def _reviewer_drift_proposal(artifacts: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "root_cause": {
            "code": "reviewer_drift_detected",
            "confidence": 0.85,
            "summary": (
                "Peer reviewer raised a different must_fix topic each round "
                "(token-bag Jaccard < threshold) — moving goalposts rather than "
                "the executor failing to converge."
            ),
        },
        "repair_task": {
            "title": "Stabilize peer-review must_fix surfaces or tighten reviewer prompt",
            "task_direction": (
                "Fix kodawari peer-review behavior for REVIEWER_DRIFT_DETECTED. The drift "
                "detector terminated because consecutive rounds raised distinct must_fix items "
                "with low overlap. Inspect the reviewer prompt and bundle: either the reviewer "
                "is exploring orthogonal concerns each round (clamp the prompt scope), or the "
                "executor's fixes are introducing new violations (verify scope guard correctness "
                "and tighten test surface). Tune ``_drift_similarity_threshold`` if the threshold "
                "is too sensitive for the workload."
            ),
            "target_files": [
                "src/kodawari/autopilot/engine/loop_runner.py",
                "src/kodawari/autopilot/review/opus_gateway.py",
                "src/kodawari/autopilot/execution/local_adapter.py",
                "tests/test_loop_runner.py",
            ],
            "suggested_tests": ["pytest -q tests/test_loop_runner.py"],
            "acceptance": [
                "Reviewer must_fix bag overlap stays >= drift threshold within a single feature run.",
                "Drift detector still fires on synthetic test of three distinct-topic rounds.",
                "Self-repair classifies the terminal reason as reviewer_drift_detected.",
            ],
        },
        "evidence": [
            {"source": ".execution_failure_snapshot.json", "summary": "REVIEWER_DRIFT_DETECTED"},
        ],
    }


def _planner_checkpoint_invalid_json_proposal(artifacts: dict[str, dict[str, Any]]) -> dict[str, Any]:
    trace = artifacts.get(".planner_tool_use_trace.jsonl", {})
    parse_error = _clean(trace.get("parse_error") or "invalid_json") if isinstance(trace, dict) else "invalid_json"
    content_chars = trace.get("content_chars") if isinstance(trace, dict) else ""
    return {
        "root_cause": {
            "code": "planner_tool_use_checkpoint_invalid_json",
            "confidence": 0.9,
            "summary": (
                "Planner tool-use reached a decision checkpoint, but the checkpoint response was not valid JSON. "
                "The SDK should force a bounded JSON-only repair turn and classify any remaining failure precisely."
            ),
        },
        "repair_task": {
            "title": "Harden planner tool-use checkpoint JSON recovery",
            "task_direction": (
                "Fix kodawari planner tool-use checkpoint handling so an invalid checkpoint response gets one "
                "tools-disabled JSON repair turn. If it still fails, preserve the "
                "planner_tool_use_checkpoint_invalid_json code in planning failure and self-repair artifacts. "
                "Keep blocker JSON as a valid non-plan outcome instead of forcing fabricated tasks."
            ),
            "target_files": [
                "src/kodawari/autopilot/planning/planning_agent.py",
                "src/kodawari/autopilot/planning/planning_findings.py",
                "src/kodawari/cli/evidence/self_repair.py",
                "tests/test_planning_agent.py",
                "tests/test_planning_orchestrator.py",
                "tests/test_self_repair.py",
            ],
            "suggested_tests": [
                "pytest -q tests/test_planning_agent.py tests/test_planning_orchestrator.py tests/test_self_repair.py"
            ],
            "acceptance": [
                "Decision checkpoint invalid JSON gets one no-tools repair turn.",
                "Blocker JSON remains a valid checkpoint outcome and does not fabricate executable tasks.",
                "Remaining checkpoint parse failures classify as planner_tool_use_checkpoint_invalid_json, not planning contradiction.",
            ],
        },
        "evidence": [
            {
                "source": ".planner_tool_use_trace.jsonl",
                "summary": f"decision checkpoint parse failed: {parse_error}; content_chars={content_chars or 'unknown'}",
            }
        ],
    }


def _planner_transport_or_output_proposal(artifacts: dict[str, dict[str, Any]]) -> dict[str, Any]:
    planning_failure = artifacts.get(".planning_failure.json", {})
    trace = artifacts.get(".planner_tool_use_trace.jsonl", {})
    error_code = _clean(planning_failure.get("error_code") or planning_failure.get("reason"))
    if not error_code and isinstance(trace, dict):
        error_code = _clean(trace.get("planner_error_kind") or trace.get("error_code") or trace.get("parse_error"))
    return {
        "root_cause": {
            "code": "planner_transport_or_output_failure",
            "confidence": 0.9,
            "summary": (
                "Planner HTTP tool-use failed at the transport/output-control layer instead of producing a "
                "usable plan or a precise blocker."
            ),
        },
        "repair_task": {
            "title": "Harden planner HTTP tool-use timeout, empty-output handling, and chat fallback",
            "task_direction": (
                "Fix kodawari planner HTTP tool-use behavior for transport timeouts and empty length outputs. "
                "Ensure tool_use HTTP calls have a real hard timeout, classify finish_reason=length with empty "
                "content/tool_calls as planner_output_truncated_empty, skip JSON repair when there is no content, "
                "and allow exactly one compact no-tools chat fallback for eligible tool-use transport/output failures."
            ),
            "target_files": [
                "src/kodawari/autopilot/execution/tool_use_transport.py",
                "src/kodawari/autopilot/planning/planning_agent.py",
                "src/kodawari/autopilot/planning/planning_orchestrator.py",
                "src/kodawari/cli/evidence/self_repair.py",
                "tests/test_planning_agent.py",
                "tests/test_planning_orchestrator.py",
                "tests/test_self_repair.py",
            ],
            "suggested_tests": [
                "pytest -q tests/test_planning_agent.py tests/test_planning_orchestrator.py tests/test_self_repair.py"
            ],
            "acceptance": [
                "HTTP tool-use transport returns within the configured hard timeout.",
                "Empty length output is classified as planner_output_truncated_empty and does not trigger JSON repair.",
                "Eligible tool-use transport/output failures get one compact chat fallback with telemetry.",
            ],
        },
        "evidence": [
            {
                "source": ".planning_failure.json",
                "summary": error_code or "planner transport/output failure",
            }
        ],
    }


def _semantic_closure_proposal(artifacts: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "root_cause": {
            "code": "semantic_closure_failure",
            "confidence": 0.82,
            "summary": (
                "Planner produced a structurally valid plan, but reviewer evidence shows the chosen goal, "
                "files_to_change, and verification path did not close semantically."
            ),
        },
        "repair_task": {
            "title": "Require planner evidence-backed closure after reviewer scope blockers",
            "task_direction": (
                "Fix kodawari planning convergence for reviewer scope/closure blockers. When reviewer findings "
                "question owner surface, route/handler/service wiring, files_to_change, or test closure, the next "
                "plan must cite review evidence and either change the scoped files/tests or mark the evidence as "
                "needing a human decision. Add validator coverage so unsupported verbal refutes stay blocking."
            ),
            "target_files": [
                "src/kodawari/autopilot/planning/planning_validators.py",
                "src/kodawari/autopilot/planning/review_evidence_scout.py",
                "src/kodawari/autopilot/planning/planning_orchestrator.py",
                "tests/test_planning_orchestrator.py",
            ],
            "suggested_tests": ["pytest -q tests/test_planning_orchestrator.py"],
            "acceptance": [
                "Reviewer scope/closure blockers force evidence_resolutions on the next plan.",
                "Supported or ambiguous closure findings cannot be dismissed without scoped plan changes or human-decision status.",
                "The self-repair classifier does not mislabel semantic closure failures as deterministic contradictions.",
            ],
        },
        "evidence": [{"source": "PLANNING_CONVERSATION.json", "summary": "reviewer scope/closure blocker detected"}],
    }


def _planning_contradiction_proposal(artifacts: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "root_cause": {
            "code": "planning_deterministic_contradiction",
            "confidence": 0.8,
            "summary": "Planning reached escalation on a structural contradiction the SDK should repair deterministically.",
        },
        "repair_task": {
            "title": "Move machine-fixable planning contradictions into deterministic repair",
            "task_direction": (
                "Fix kodawari planning so machine-fixable contradictions are repaired before another model "
                "planning round. Focus on task file scope conflicts, verify-only test demotion, duplicate "
                "verify recipes, and change_log references."
            ),
            "target_files": [
                "src/kodawari/autopilot/planning/deterministic_repair.py",
                "src/kodawari/autopilot/planning/planning_orchestrator.py",
                "tests/test_deterministic_repair.py",
                "tests/test_planning_orchestrator.py",
            ],
            "suggested_tests": ["pytest -q tests/test_deterministic_repair.py tests/test_planning_orchestrator.py"],
            "acceptance": [
                "Machine-fixable structural findings are logged as deterministic repairs.",
                "Critical semantic or security findings still block execution.",
                "The planner does not spend repeated rounds on the same repairable contradiction.",
            ],
        },
        "evidence": [{"source": "PLANNING_CONVERSATION.json", "summary": "planning escalation or contradiction detected"}],
    }


def _planner_reviewer_deadlock_proposal(artifacts: dict[str, dict[str, Any]]) -> dict[str, Any]:
    planning_failure = artifacts.get(".planning_failure.json", {})
    escalation = planning_failure.get("escalation") if isinstance(planning_failure.get("escalation"), dict) else {}
    repeated = escalation.get("repeated_blocker_rounds") if isinstance(escalation, dict) else ""
    return {
        "root_cause": {
            "code": "planner_reviewer_deadlock",
            "confidence": 0.85,
            "summary": "Planner and plan reviewer repeated the same blocking theme without converging to an executable plan.",
        },
        "repair_task": {
            "title": "Finalize and repair repeated planning-review deadlocks",
            "task_direction": (
                "Fix kodawari planning convergence handling for repeated reviewer blockers. Preserve per-round "
                "blocking finding details in progress/failure artifacts, terminate repeated blocker themes as "
                "planner_reviewer_deadlock instead of spinning, and keep the planning self-repair classifier routed "
                "to a bounded SDK repair task."
            ),
            "target_files": [
                "src/kodawari/autopilot/planning/planning_orchestrator.py",
                "src/kodawari/autopilot/planning/planning_findings.py",
                "src/kodawari/cli/evidence/self_repair.py",
                "tests/test_planning_orchestrator.py",
                "tests/test_self_repair.py",
            ],
            "suggested_tests": ["pytest -q tests/test_planning_orchestrator.py tests/test_self_repair.py"],
            "acceptance": [
                "Repeated reviewer blocker signatures terminate as planner_reviewer_deadlock.",
                "The final progress and failure artifacts include compact blocking finding details.",
                "Planning escalation can produce a self-repair proposal without entering execution.",
            ],
        },
        "evidence": [
            {
                "source": ".planning_failure.json",
                "summary": f"planner_reviewer_deadlock repeated_rounds={repeated or 'unknown'}",
            }
        ],
    }


def _task_input_infeasible_proposal(artifacts: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Proposal for a Layer-D early-exit: the task as posed cannot close.

    Unlike other root-cause codes, this one does NOT recommend an SDK code
    repair — the SDK behaved correctly by detecting the unfeasible task
    shape. The right action is a human decision: implement the missing
    surface, re-anchor to an existing surface, or change the task scope.
    The proposal carries the precheck's missing_surfaces so the operator
    sees the exact routes that were not findable.
    """
    planning_failure = artifacts.get(".planning_failure.json", {})
    escalation = (
        planning_failure.get("escalation")
        if isinstance(planning_failure.get("escalation"), dict)
        else {}
    )
    missing_surfaces = list(escalation.get("missing_surfaces") or []) if isinstance(escalation, dict) else []
    return {
        "root_cause": {
            "code": "task_input_infeasible_surface",
            "confidence": 0.9,
            "summary": (
                "Task input names a route surface absent from the repo and is "
                "test-only in intent — there is no legal closure path for a "
                "test-only plan against a non-existent surface. This requires "
                "a human decision, not an SDK code repair."
            ),
        },
        "repair_task": {
            "title": "Decide task scope for missing route surface (human decision)",
            "task_direction": (
                "The task input feasibility precheck identified one or more "
                f"route surfaces not present in the repo: {missing_surfaces}. "
                "Choose one path: (a) widen the task to implement the route "
                "before adding tests, (b) re-anchor the task to an existing "
                "surface, or (c) confirm the route should be created and update "
                "the task direction to 'implement + test' before re-planning. "
                "No SDK code change is recommended."
            ),
            "target_files": [],
            "suggested_tests": [],
            "acceptance": [
                "Operator records which path was chosen.",
                "Re-planning proceeds with a task direction whose route surface(s) exist in the repo.",
            ],
        },
        "evidence": [
            {
                "source": ".planning_failure.json",
                "summary": (
                    f"task_input_infeasible_surface missing={missing_surfaces}"
                    if missing_surfaces
                    else "task_input_infeasible_surface"
                ),
            }
        ],
    }


def _generic_executor_stall_proposal(code: str, artifacts: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "root_cause": {
            "code": "executor_stall_unhandled",
            "confidence": 0.7,
            "summary": f"Executor emitted {code}, but no specialized self-repair classifier handled it.",
        },
        "repair_task": {
            "title": f"Add deterministic recovery handling for {code}",
            "task_direction": (
                f"Add or refine kodawari recovery handling for {code}. Use the stall report as the truth source, "
                "add a detector registry match if missing, and include telemetry plus regression tests."
            ),
            "target_files": [
                "src/kodawari/autopilot/recovery/registry.py",
                "src/kodawari/autopilot/engine/engine_recovery_mixin.py",
                "tests/test_stall_recovery.py",
            ],
            "suggested_tests": ["pytest -q tests/test_stall_recovery.py"],
            "acceptance": [
                f"{code} is classified from structured artifacts, not log wording.",
                "The chosen recovery path is bounded and visible in recovery_decisions.",
            ],
        },
        "evidence": [{"source": ".execution_stall_report.json", "summary": code}],
    }


# --- Helpers: artifact loading + evidence rendering -----------------------


def _load_artifacts(planning_dir: Path) -> dict[str, dict[str, Any]]:
    names = (
        ".execution_stall_report.json",
        ".execution_failure_snapshot.json",
        ".execution_result.json",
        ".task_run_result.json",
        ".planning_failure.json",
        "PLANNING_CONVERSATION.json",
    )
    artifacts = {name: _load_json_dict(planning_dir / name) for name in names}
    artifacts[".planner_tool_use_trace.jsonl"] = _load_planner_tool_trace_summary(
        planning_dir / ".planner_tool_use_trace.jsonl"
    )
    return artifacts


def _load_json_dict(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _load_planner_tool_trace_summary(path: Path) -> dict[str, Any]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    if not lines:
        return {}
    decision_checkpoint_seen = False
    progress_reason = ""
    final_parse: dict[str, Any] = {}
    scanned = 0
    for line in lines[-200:]:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        scanned += 1
        name = _clean(event.get("event"))
        if name == "progress_guard_triggered":
            decision_checkpoint_seen = True
            progress_reason = _clean(event.get("reason")) or progress_reason
        elif name == "http_request_start" and _clean(event.get("phase")) == "decision_checkpoint":
            decision_checkpoint_seen = True
        elif name == "final_parse_result":
            final_parse = dict(event)
            if bool(event.get("decision_checkpoint")):
                decision_checkpoint_seen = True
    if not (decision_checkpoint_seen or final_parse):
        return {}
    final_decision_checkpoint_parse_failed = (
        bool(final_parse.get("decision_checkpoint"))
        and not bool(final_parse.get("ok"))
        and not bool(final_parse.get("blocked"))
        and bool(final_parse.get("parse_error"))
    )
    return {
        "events_scanned": scanned,
        "decision_checkpoint_seen": decision_checkpoint_seen,
        "progress_guard_reason": progress_reason,
        "final_decision_checkpoint_parse_failed": final_decision_checkpoint_parse_failed,
        "parse_error": _clean(final_parse.get("parse_error")),
        "content_chars": _int_value(final_parse.get("content_chars")),
        "tool_calls_used": _int_value(final_parse.get("tool_calls_used")),
        "final_ok": bool(final_parse.get("ok")),
        "final_blocked": bool(final_parse.get("blocked")),
        "json_repair_attempt": bool(final_parse.get("json_repair_attempt")),
    }


def _int_value(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _fragmented_read_path(stall_report: dict[str, Any]) -> str:
    fragmented = stall_report.get("fragmented_read_paths")
    if isinstance(fragmented, dict) and fragmented:
        return max(((str(path), int(count or 0)) for path, count in fragmented.items()), key=lambda item: item[1])[0]
    recent = stall_report.get("recent_tool_calls")
    counts: dict[str, int] = {}
    if isinstance(recent, list):
        for item in recent:
            if not isinstance(item, dict):
                continue
            args = item.get("arguments")
            if not isinstance(args, dict):
                continue
            path = _clean(args.get("path")).replace("\\", "/")
            if path:
                counts[path] = counts.get(path, 0) + 1
    if counts:
        return max(counts.items(), key=lambda item: item[1])[0]
    return ""


def _looks_like_planner_transport_or_output_failure(artifacts: dict[str, dict[str, Any]]) -> bool:
    planning_failure = artifacts.get(".planning_failure.json", {})
    trace = artifacts.get(".planner_tool_use_trace.jsonl", {})
    text = " ".join(
        _clean(item).lower()
        for item in (
            planning_failure.get("error_code"),
            planning_failure.get("reason"),
            json.dumps(planning_failure.get("escalation") or {}, ensure_ascii=False),
            json.dumps(trace, ensure_ascii=False) if isinstance(trace, dict) else "",
        )
    )
    markers = (
        "planner_transport_timeout",
        "planner_output_truncated_empty",
        "planner_empty_output",
        "finish_reason=length",
        "empty output",
    )
    return any(marker in text for marker in markers)


def _looks_like_semantic_closure_failure(artifacts: dict[str, dict[str, Any]]) -> bool:
    """Detect reviewer scope/closure blockers that semantic_closure_failure owns.

    Two-tier marker set: semantic_closure must require a STRONG marker so
    every-day deterministic-contradiction artifacts (which routinely mention
    ``files_to_change`` / ``handler`` / ``service`` in plan payload narration)
    do NOT mis-route here. Weak markers stay informational only — they used to
    be the sole gate, which made the heuristic over-fire.
    """
    planning = artifacts.get("PLANNING_CONVERSATION.json", {})
    failure = artifacts.get(".planning_failure.json", {})
    text = " ".join(
        item
        for item in (
            json.dumps(planning, ensure_ascii=False).lower(),
            json.dumps(failure, ensure_ascii=False).lower(),
        )
        if item
    )
    if "reviewer" not in text and "blocking_findings" not in text:
        return False
    strong_markers = (
        "owner_surface",
        "semantic closure",
        "does not close",
        "not close",
        "scope correctness",
        "scope_correctness",
        "structural_validity",
        "call chain",
    )
    return any(marker in text for marker in strong_markers)


def _looks_like_planning_contradiction(artifacts: dict[str, dict[str, Any]]) -> bool:
    planning = artifacts.get("PLANNING_CONVERSATION.json", {})
    text = json.dumps(planning, ensure_ascii=False).lower()
    if not text:
        return False
    signatures = (
        "read_only_files",
        "files_to_change",
        "verify-only",
        "verify only",
        "change_log",
        "invariants",
        "parallel",
    )
    return "escalation" in text and any(signature in text for signature in signatures)


def _evidence_index(*, truth: dict[str, Any], artifacts: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if truth:
        rows.append(
            {
                "artifact": ".run_truth.json",
                "summary": f"{_clean(truth.get('final_status')) or 'unknown'} / {_clean(truth.get('run_reason')) or 'unknown'}",
            }
        )
    for name, payload in artifacts.items():
        if payload:
            rows.append({"artifact": name, "summary": _artifact_summary(name, payload)})
    return rows


_ARTIFACT_SUMMARY_FIELDS: dict[str, tuple[tuple[str, ...], str]] = {
    ".execution_stall_report.json": (("error_code", "reason"), "stall_report"),
    ".execution_result.json": (("status", "error_code", "reason"), "execution_result"),
    ".planning_failure.json": (("error_code", "reason"), "planning_failure"),
    ".planner_tool_use_trace.jsonl": (("parse_error", "progress_guard_reason"), "planner_tool_use_trace"),
}


def _artifact_summary(name: str, payload: dict[str, Any]) -> str:
    config = _ARTIFACT_SUMMARY_FIELDS.get(name)
    if config is None:
        return "present"
    fields, default = config
    return _clean(_first_present(payload, fields) or default)


def _first_present(payload: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = payload.get(key)
        if value:
            return value
    return None


# --- Helpers: payload assembly --------------------------------------------


def _base_payload(
    *,
    project_root: Path,
    planning_dir: Path,
    run_truth: dict[str, Any],
    sdk_root: Path,
    truth_fresh: bool,
) -> dict[str, Any]:
    return {
        "schema_version": SELF_REPAIR_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "project_root": str(project_root),
        "planning_dir": str(planning_dir),
        "kodawari_root": str(sdk_root.resolve()),
        "phase_implementation_status": dict(WORKFLOW_SELF_REPAIR_PHASES),
        "truth_freshness": "fresh" if truth_fresh else "stale",
        "source_run": {
            "feature": _clean(run_truth.get("feature") or planning_dir.name),
            "final_status": _clean(run_truth.get("final_status")),
            "run_reason": _clean(run_truth.get("run_reason")),
            "blocking_reason": _clean(run_truth.get("blocking_reason")),
        },
    }


def _safety_payload(
    *,
    auto_apply_allowed: bool,
    sdk_root: Path,
    rejected_target_files: list[dict[str, str]],
) -> dict[str, Any]:
    return {
        "auto_apply_allowed": bool(auto_apply_allowed),
        "requires_review": True,
        "target_repo": "kodawari",
        "kodawari_root": str(sdk_root.resolve()),
        "rejected_target_files": list(rejected_target_files),
        "rationale": (
            "Self-repair proposals may edit the SDK that is driving another project. By default "
            "the runtime only writes diagnostic artifacts. Phase 3 auto-execution requires the "
            "extra WORKFLOW_SELF_REPAIR_AUTO_EXECUTE=1 opt-in, then spawns a fresh kodawari "
            "autopilot run that goes through the normal "
            "planner/review/verify/gate pipeline — there is no silent-patch path. target_files "
            "are validated against the SDK root and a denylist of high-blast-radius paths "
            "(test fixtures, runtime, baselines, build files). ``auto_apply_allowed`` stays "
            "False on the proposal payload because Phase 3 does not silent-apply: any code "
            "change still requires the spawned run to pass review."
        ),
    }


def _render_triage_markdown(payload: dict[str, Any]) -> str:
    unhandled = str(payload.get("unhandled_code") or "")
    return (
        "# Workflow Self-Repair — Triage Required\n"
        "\n"
        f"Failure code `{unhandled or 'unknown'}` has no specialized self-repair "
        "classifier. The proposal could not generate a precise SDK repair task; "
        "manual triage is required.\n"
        "\n"
        "## Evidence\n"
        "\n"
        + "\n".join(
            f"- {item.get('source') or item.get('artifact') or 'evidence'}: "
            f"{item.get('summary') or item.get('value') or ''}"
            for item in list(payload.get("evidence") or [])
            if isinstance(item, dict)
        )
        + "\n"
    )


def _clean(value: Any) -> str:
    return str(value or "").strip()


__all__ = [
    "SELF_REPAIR_FILENAME",
    "SELF_REPAIR_MARKDOWN_FILENAME",
    "SELF_REPAIR_SCHEMA_VERSION",
    "WORKFLOW_SELF_REPAIR_PHASES",
    "build_self_repair_proposal",
    "render_self_repair_markdown",
    "write_self_repair_markdown",
    "write_self_repair_proposal",
]
