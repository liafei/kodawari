"""Lane observation signal helpers for autopilot CLI runtime."""

from __future__ import annotations

from pathlib import Path
import subprocess
from typing import Any, Callable

from kodawari.autopilot.planning.lane_observation import ActualRunSignals


SubprocessRun = Callable[..., Any]
DiffEstimator = Callable[..., int]


def collect_actual_signals(
    *,
    payload: dict[str, Any],
    rounds: list[dict[str, Any]],
    reliable_changed_files: tuple[str, ...],
    project_root: Path | None = None,
    diff_loc_fn: DiffEstimator,
) -> ActualRunSignals:
    run_truth = payload.get("run_truth")
    final = payload.get("final_outcome") or {}
    interaction_state = str(payload.get("interaction_state") or "").upper()
    decision_kind = str(payload.get("decision_kind") or "").lower()
    blocking = 0
    for round_payload in rounds:
        if not isinstance(round_payload, dict):
            continue
        try:
            blocking += int(round_payload.get("blocking_findings_count") or 0)
        except (TypeError, ValueError):
            continue
    contract_or_schema = any(
        any(token in (path or "").lower() for token in (
            "/migration", ".sql", "/auth_", "/credential", "/permission",
            "/api/v1/routes/", "/schema/",
        ))
        for path in reliable_changed_files
    )
    release_decision = decision_kind == "release_approval" or interaction_state == "AWAITING_DECISION"
    escalated = decision_kind == "planning_escalation" or "ESCALAT" in interaction_state
    diff_loc = diff_loc_fn(project_root=project_root, changed_files=reliable_changed_files)
    if isinstance(run_truth, dict) and run_truth:
        return ActualRunSignals(
            diff_loc=diff_loc,
            files_changed=len(reliable_changed_files),
            rounds_used=_int(run_truth.get("runtime_rounds")) or len(rounds),
            planning_rounds=_int(run_truth.get("planning_rounds")),
            execution_rounds=_int(run_truth.get("execution_rounds")),
            review_rounds=_int(run_truth.get("review_rounds")),
            executor_attempts=_int(run_truth.get("executor_attempts")),
            deterministic_recovery_hits=_int(run_truth.get("deterministic_recovery_hits")),
            synthesizer_calls=_int(run_truth.get("synthesizer_calls")),
            recovery_pressure=_int(run_truth.get("recovery_pressure")),
            review_must_fix_max=_int(run_truth.get("review_must_fix_max")),
            blocking_findings=_int(run_truth.get("blocking_findings")),
            gate_status=str(run_truth.get("gate_status") or ""),
            verify_status=str(run_truth.get("verify_status") or ""),
            escalated=escalated,
            release_decision_required=release_decision,
            contract_or_schema_touched=contract_or_schema,
            precondition_blocked=str(run_truth.get("run_reason") or "").upper() == "BLOCKED_BY_PRECONDITION",
        )
    return ActualRunSignals(
        diff_loc=diff_loc,
        files_changed=len(reliable_changed_files),
        rounds_used=len(rounds),
        blocking_findings=blocking,
        gate_status=str(final.get("gate_status") or ""),
        verify_status=str(final.get("verify_status") or ""),
        escalated=escalated,
        release_decision_required=release_decision,
        contract_or_schema_touched=contract_or_schema,
    )


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def estimate_diff_loc(
    *,
    project_root: Path | None,
    changed_files: tuple[str, ...],
    subprocess_run: SubprocessRun,
) -> int:
    if project_root is None:
        return 0
    files = [str(path).strip() for path in changed_files if str(path).strip()]
    if not files:
        return 0
    root = Path(project_root).resolve()
    candidate_cmds = [
        ["git", "-C", str(root), "diff", "--numstat", "HEAD", "--", *files],
        ["git", "-C", str(root), "diff", "--numstat", "--", *files],
    ]
    stdout = ""
    for cmd in candidate_cmds:
        try:
            result = subprocess_run(
                cmd,
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
        except (FileNotFoundError, subprocess.CalledProcessError):
            continue
        stdout = str(result.stdout or "")
        break
    if not stdout:
        return 0
    total = 0
    for raw in stdout.splitlines():
        parts = raw.split("\t")
        if len(parts) < 3:
            continue
        added_text, deleted_text = parts[0].strip(), parts[1].strip()
        try:
            added = int(added_text) if added_text != "-" else 0
            deleted = int(deleted_text) if deleted_text != "-" else 0
        except ValueError:
            continue
        total += max(0, added) + max(0, deleted)
    return total
