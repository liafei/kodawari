"""Extract a structured kodawari bug report from a failed autopilot run.

Reads artifacts under a target project's ``planning/<feature>/`` directory and
emits a JSON report describing:

- the trigger run (target project, feature, task_direction, stop_reason)
- the failure signature (error_code, phase, stall_kind, recovery path)
- suspected kodawari components (heuristic mapping by error_code)
- evidence paths (artifact files relevant to the failure)
- a reproduction command + env

The report is meant to be consumed by a downstream meta-autopilot run on
kodawari itself: read it and turn it into a task_direction. This script
does **no** LLM call — it is mechanical artifact aggregation.

Usage:

    python scripts/extract_workflow_bug_report.py \
        --planning-dir /e/wf-test/newsapp/planning/<feature> \
        [--output .meta/bug_reports/<name>.json]

If ``--output`` is omitted, prints to stdout. Exit code 0 when a report is
written even if the run is partially-successful; exit code 2 if the planning
dir does not look like an autopilot run (e.g. ``.autopilot_state.json`` is
missing).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "kodawari.bug_report.v1"

ARTIFACT_FILENAMES = (
    ".autopilot_state.json",
    ".execution_failure_snapshot.json",
    ".execution_recovery_decision.json",
    ".execution_stall_report.json",
    ".execution_request.json",
    ".execution_result.json",
    ".execution_readiness.json",
    ".execution_tool_calls.jsonl",
    ".autopilot_rounds.jsonl",
    "TASK_CARD_ACTIVE.json",
    "TASK_GRAPH.json",
    "PLANNING_CONVERSATION.json",
)

# error_code -> list of suspected kodawari source files (relative paths).
# Heuristic mapping: when an autopilot run fails with the given code, these
# are the modules most likely to need a code change to address the gap.
SUSPECT_MAP: dict[str, list[str]] = {
    "RECOVERY_SYNTHESIZER_TIMEOUT": [
        "src/kodawari/autopilot/engine/engine_recovery_mixin.py",
        "src/kodawari/autopilot/execution/local_adapter_recovery.py",
        "src/kodawari/autopilot/recovery/executor_recovery.py",
    ],
    "EXECUTOR_STALLED_NO_WRITE_PROGRESS": [
        "src/kodawari/autopilot/execution/tool_use_stall.py",
        "src/kodawari/autopilot/recovery/stall_recovery.py",
        "src/kodawari/autopilot/recovery/registry.py",
    ],
    "EXECUTOR_STALLED_FRAGMENTED_READS": [
        "src/kodawari/autopilot/execution/tool_use_stall.py",
        "src/kodawari/autopilot/recovery/stall_recovery.py",
        "src/kodawari/autopilot/recovery/registry.py",
    ],
    "EXECUTOR_STALLED_PATCH_PLAN_REQUIRED": [
        "src/kodawari/autopilot/execution/tool_use_runtime.py",
        "src/kodawari/autopilot/recovery/stall_recovery.py",
    ],
    "PATH_OUT_OF_SCOPE": [
        "src/kodawari/autopilot/planning/planning_context.py",
        "src/kodawari/autopilot/execution/tool_use_runtime.py",
        "src/kodawari/autopilot/execution/tool_use_result.py",
    ],
    "MAX_SAME_TOOL_CALLS_PER_PATH": [
        "src/kodawari/autopilot/execution/tool_use_stall.py",
        "src/kodawari/autopilot/recovery/stall_recovery.py",
    ],
    "TASK_BLOCKED_BY_PRECONDITION": [
        "src/kodawari/autopilot/planning/execution_readiness.py",
        "src/kodawari/autopilot/planning/planning_context.py",
        "src/kodawari/autopilot/planning/planning_orchestrator.py",
    ],
    "RECOVERY_ATTEMPTS_EXHAUSTED": [
        "src/kodawari/autopilot/engine/engine_recovery_mixin.py",
        "src/kodawari/autopilot/recovery/registry.py",
    ],
    "RECOVERY_TOTAL_ATTEMPTS_EXHAUSTED": [
        "src/kodawari/autopilot/engine/engine_recovery_mixin.py",
    ],
    "VERIFY_FAILED": [
        "src/kodawari/autopilot/recovery/pytest_recovery.py",
        "src/kodawari/autopilot/engine/engine_recovery_mixin.py",
    ],
    "VERIFY_FAILED_RETRYABLE": [
        "src/kodawari/autopilot/recovery/pytest_recovery.py",
    ],
}

# Stage status -> suspected components, for failures that don't surface a
# clean error_code (e.g. ``round_limit`` after peer review demanded changes).
STAGE_STATUS_SUSPECT_MAP: dict[str, list[str]] = {
    "round_limit": [
        "src/kodawari/autopilot/engine/engine_review_mixin.py",
        "src/kodawari/autopilot/engine/loop_runner.py",
    ],
    "execution_backend_blocked": [
        "src/kodawari/autopilot/execution/local_adapter.py",
        "src/kodawari/autopilot/engine/engine_implementation_mixin.py",
    ],
    "executor_recovery_synthesizer_timeout": [
        "src/kodawari/autopilot/engine/engine_recovery_mixin.py",
    ],
    "executor_recovery_blocked": [
        "src/kodawari/autopilot/engine/engine_recovery_mixin.py",
        "src/kodawari/autopilot/recovery/registry.py",
    ],
}


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _read_jsonl_tail(path: Path, *, limit: int) -> list[dict[str, Any]]:
    if not path.exists() or limit <= 0:
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for raw in lines[-limit:]:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            out.append(payload)
    return out


def _state_summary(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "feature": state.get("feature") or "",
        "project_root": state.get("project_root") or "",
        "current_stage": state.get("current_stage") or "",
        "final_status": state.get("final_status") or "",
        "stop_reason": state.get("stop_reason") or "",
        "stop_action": state.get("stop_action") or "",
        "last_error": state.get("last_error") or "",
        "last_stage_status": state.get("last_stage_status") or "",
        "changed_files": list(state.get("changed_files") or []),
    }


def _failure_signature(
    state: dict[str, Any],
    failure_snapshot: dict[str, Any] | None,
    recovery_decision: dict[str, Any] | None,
    stall_report: dict[str, Any] | None,
    rounds_tail: list[dict[str, Any]],
) -> dict[str, Any]:
    error_events = list(state.get("error_events") or [])
    primary_error_code = ""
    primary_phase = ""
    # Walk events newest-first and pick the first one with a non-empty
    # error_code. The terminating event sometimes carries only a message
    # (e.g. ``Reached review round limit``) with error_code=null; skipping
    # those gives us the most informative real code.
    for event in reversed(error_events):
        code = str(event.get("error_code") or "").strip()
        if code:
            primary_error_code = code
            primary_phase = str(event.get("phase") or "")
            break
    if not primary_error_code and isinstance(recovery_decision, dict):
        code = str(recovery_decision.get("error_code") or "").strip()
        if code:
            primary_error_code = code
    if not primary_error_code and isinstance(stall_report, dict):
        code = str(stall_report.get("error_code") or "").strip()
        if code:
            primary_error_code = code
    # Synthesize a code from ``last_stage_status`` when no real code was
    # raised — typical for terminal states like ``round_limit`` where the
    # loop simply hit a configured cap.
    last_stage_status = str(state.get("last_stage_status") or "").strip()
    if not primary_error_code and last_stage_status:
        primary_error_code = f"STAGE_STATUS:{last_stage_status.upper()}"
        if not primary_phase and error_events:
            primary_phase = str(error_events[-1].get("phase") or "")

    deterministic_hits = 0
    synthesizer_invoked = False
    synthesizer_outcome = ""
    for round_record in rounds_tail:
        details = round_record.get("details") or {}
        if not isinstance(details, dict):
            continue
        recovery = details.get("recovery") or {}
        if isinstance(recovery, dict):
            role = str(recovery.get("role") or "")
            if role == "deterministic_recovery":
                deterministic_hits += 1
            elif role == "recovery_synthesizer":
                synthesizer_invoked = True
                synthesizer_outcome = str(recovery.get("status") or recovery.get("error_code") or "")

    # The terminal signal is what actually stopped the loop. It may differ
    # from ``error_code`` when an earlier failure was recovered (e.g. stall
    # recovered, then review round limit hit). Consumers should focus on
    # ``terminal_signal`` for "where did the run stop" and on ``error_code``
    # for "what was the most recent real failure code".
    terminal_signal = ""
    if last_stage_status:
        terminal_signal = f"STAGE_STATUS:{last_stage_status.upper()}"
    elif state.get("stop_reason"):
        terminal_signal = f"STOP_REASON:{str(state.get('stop_reason')).upper()}"

    return {
        "error_code": primary_error_code,
        "phase": primary_phase,
        "stall_kind": str(stall_report.get("error_code") or "") if isinstance(stall_report, dict) else "",
        "stop_reason": str(state.get("stop_reason") or ""),
        "stop_action": str(state.get("stop_action") or ""),
        "last_stage_status": last_stage_status,
        "terminal_signal": terminal_signal,
        "deterministic_recovery_hits": deterministic_hits,
        "synthesizer_invoked": synthesizer_invoked,
        "synthesizer_outcome": synthesizer_outcome,
        "rounds_executed": len(rounds_tail),
    }


def _suspected_components(signature: dict[str, Any]) -> list[str]:
    suspects: list[str] = []
    seen: set[str] = set()
    for key in (
        signature.get("error_code") or "",
        signature.get("stall_kind") or "",
    ):
        for path in SUSPECT_MAP.get(str(key).strip(), []):
            if path not in seen:
                suspects.append(path)
                seen.add(path)
    stage_status = str(signature.get("last_stage_status") or "").strip()
    for path in STAGE_STATUS_SUSPECT_MAP.get(stage_status, []):
        if path not in seen:
            suspects.append(path)
            seen.add(path)
    return suspects


def _bug_signature_hash(signature: dict[str, Any]) -> str:
    pieces = [
        str(signature.get("error_code") or ""),
        str(signature.get("phase") or ""),
        str(signature.get("stall_kind") or ""),
        str(signature.get("last_stage_status") or ""),
        "synth=" + ("1" if signature.get("synthesizer_invoked") else "0"),
        "deterministic=" + str(signature.get("deterministic_recovery_hits") or 0),
    ]
    digest = hashlib.sha256("|".join(pieces).encode("utf-8")).hexdigest()
    return digest[:12]


def _existing_evidence_paths(planning_dir: Path) -> list[str]:
    out: list[str] = []
    for name in ARTIFACT_FILENAMES:
        if (planning_dir / name).exists():
            out.append(name)
    return out


def _reproduction(state: dict[str, Any], planning_conversation: dict[str, Any] | None) -> dict[str, Any]:
    project_root = str(state.get("project_root") or "").strip()
    feature = str(state.get("feature") or "").strip()
    task_direction = ""
    if isinstance(planning_conversation, dict):
        task_direction = str(planning_conversation.get("task_direction") or "")
    if not task_direction:
        task_direction = "(reuse PLANNING_CONVERSATION.json from the failed run)"
    command = (
        f"kodawari autopilot --project-root {project_root} --feature {feature} "
        f'--task "{task_direction[:200]}{"..." if len(task_direction) > 200 else ""}" '
        "--planner-route model --executor-backend openai_tool_use --gate-profile blocking"
    )
    return {
        "command": command,
        "env": {
            "WORKFLOW_AUTOPILOT_AUTO_REPLAN_ON_PRECONDITION": "1",
        },
        "feature": feature,
        "project_root": project_root,
    }


def build_bug_report(planning_dir: Path) -> dict[str, Any]:
    state = _read_json(planning_dir / ".autopilot_state.json")
    if state is None:
        raise FileNotFoundError(f".autopilot_state.json missing under {planning_dir}")

    failure_snapshot = _read_json(planning_dir / ".execution_failure_snapshot.json")
    recovery_decision = _read_json(planning_dir / ".execution_recovery_decision.json")
    stall_report = _read_json(planning_dir / ".execution_stall_report.json")
    planning_conversation = _read_json(planning_dir / "PLANNING_CONVERSATION.json")
    rounds_tail = _read_jsonl_tail(planning_dir / ".autopilot_rounds.jsonl", limit=20)

    signature = _failure_signature(state, failure_snapshot, recovery_decision, stall_report, rounds_tail)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "trigger_run": _state_summary(state),
        "failure_signature": signature,
        "bug_signature_hash": _bug_signature_hash(signature),
        "suspected_components": _suspected_components(signature),
        "evidence_paths": _existing_evidence_paths(planning_dir),
        "planning_dir": str(planning_dir.resolve()),
        "reproduction": _reproduction(state, planning_conversation),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--planning-dir", required=True, type=Path)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)

    planning_dir: Path = args.planning_dir
    if not planning_dir.exists() or not planning_dir.is_dir():
        print(f"planning dir not found: {planning_dir}", file=sys.stderr)
        return 2

    try:
        report = build_bug_report(planning_dir)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    text = json.dumps(report, indent=2, ensure_ascii=False)
    if args.output is None:
        print(text)
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(text + "\n", encoding="utf-8")
    print(f"wrote {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
