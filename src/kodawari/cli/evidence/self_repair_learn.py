"""Phase 4: post-success learning from self-repair attempts.

Only Level-2-validated self-repair attempts produce prompt_lessons:

  Level 0 — execution skipped/blocked or SDK autopilot run failed.
            ``learning_action="telemetry_only"``; no lesson is emitted.
  Level 1 — SDK autopilot reached PROCEED_TO_GATE on its own tests but
            the original target run was not re-verified. Insufficient
            for learning — a passing SDK test does not prove the fix
            actually unblocks the upstream feature.
            ``learning_action="telemetry_only"``.
  Level 2 — SDK autopilot succeeded AND the original target run, when
            re-executed, advanced past its prior stop_reason. This is
            the only case that emits a ``prompt_lesson`` event.
            ``learning_action="lesson_emitted"``.

The plan is explicit that failed self-repairs must NOT be learned from
(they would teach the system to repeat broken patterns). The journal
entry is always written so operators can audit attempts regardless of
outcome.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from kodawari.cli.evidence.self_repair_execute import (
    SELF_REPAIR_EXECUTION_FILENAME,
    SELF_REPAIR_EXECUTION_SCHEMA_VERSION,
)
from kodawari.infra.io_atomic import atomic_write_canonical_json
from kodawari.instincts.prompt_lessons import ingest_prompt_lesson_event


SELF_REPAIR_JOURNAL_RELATIVE_PATH = Path(".workflow") / "self_repair_journal.jsonl"
SELF_REPAIR_JOURNAL_SCHEMA_VERSION = "workflow.self_repair.journal.v1"

# Stop reasons that count as "advanced past" the original failure when the
# target run is re-executed. Anything else (BLOCKED, STUCK, TIMEOUT) means
# the fix did not actually unblock the upstream feature.
_TARGET_SUCCESS_REASONS: frozenset[str] = frozenset(
    {"PROCEED_TO_GATE", "PROCEED_TO_RELEASE", "PIPELINE_FINISH", "OK", "PASS"}
)

_ROOT_CAUSE_TO_TEMPLATE: dict[str, str] = {
    "executor_fragmented_read_loop": "self_repair.executor_fix_validated",
    "executor_budget_no_write_loop": "self_repair.executor_fix_validated",
    "executor_patch_plan_required": "self_repair.executor_fix_validated",
    "executor_stall_unhandled": "self_repair.executor_fix_validated",
    "executor_fix_round_unproductive": "executor.fix_round_unproductive",
    "recovery_synthesizer_timeout": "self_repair.recovery_fix_validated",
    "planning_deterministic_contradiction": "self_repair.planner_fix_validated",
    "planner_transport_or_output_failure": "self_repair.planner_fix_validated",
    "semantic_closure_failure": "self_repair.planner_fix_validated",
}


@dataclass
class LearningOutcome:
    level: int
    action: str
    template_id: str = ""
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "action": self.action,
            "template_id": self.template_id,
            "reason": self.reason,
        }


def learn_from_self_repair(
    *,
    execution_record_path: Path,
    target_after_planning_dir: Path | None = None,
    sdk_root: Path | None = None,
    project_root_for_lesson: Path | None = None,
) -> dict[str, Any]:
    """Inspect a self-repair execution record and (when warranted) emit
    a prompt_lesson + journal entry. Always returns a structured outcome.

    ``target_after_planning_dir``: the planning directory of the original
    target run AFTER the SDK fix landed and the run was re-executed. When
    omitted, the function caps learning at Level 1.

    ``sdk_root`` / ``project_root_for_lesson``: where to write the journal
    + prompt lesson respectively. Both default to the SDK root inferred
    from ``WORKFLOW_SDK_SELF_REPAIR_ROOT`` or this module's location.
    """

    execution_record_path = Path(execution_record_path).resolve()
    record = _load_execution_record(execution_record_path)
    if record is None:
        return _fail_outcome("execution_record_unreadable", execution_record_path)

    sdk_root_resolved = (sdk_root or _infer_sdk_root(record)).resolve()
    project_root_for_lesson = (project_root_for_lesson or sdk_root_resolved).resolve()
    bug_hash = _bug_signature_hash(record)
    root_cause_code = str(record.get("proposal_root_cause", {}).get("code") or "")
    original_stop_reason = _original_stop_reason(record)

    outcome = _decide_learning_outcome(
        record=record,
        target_after_planning_dir=target_after_planning_dir,
        original_stop_reason=original_stop_reason,
        root_cause_code=root_cause_code,
    )

    journal_entry = _journal_entry(
        record=record,
        outcome=outcome,
        bug_hash=bug_hash,
        root_cause_code=root_cause_code,
        original_stop_reason=original_stop_reason,
        target_after_planning_dir=target_after_planning_dir,
    )
    journal_path = _append_journal(sdk_root_resolved, journal_entry)

    lesson_record: dict[str, Any] = {}
    if outcome.action == "lesson_emitted":
        lesson_record = _emit_lesson(
            project_root=project_root_for_lesson,
            template_id=outcome.template_id,
            bug_hash=bug_hash,
            root_cause_code=root_cause_code,
            record=record,
        )

    return {
        "schema_version": "workflow.self_repair.learn.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "execution_record_path": str(execution_record_path),
        "bug_signature_hash": bug_hash,
        "root_cause_code": root_cause_code,
        "original_stop_reason": original_stop_reason,
        "outcome": outcome.to_dict(),
        "journal_path": str(journal_path),
        "lesson": lesson_record,
    }


# --- Decision logic -------------------------------------------------------


def _decide_learning_outcome(
    *,
    record: dict[str, Any],
    target_after_planning_dir: Path | None,
    original_stop_reason: str,
    root_cause_code: str,
) -> LearningOutcome:
    sdk_failure = _check_sdk_run_succeeded(record)
    if sdk_failure is not None:
        return sdk_failure
    target_check = _load_target_after_state(target_after_planning_dir)
    if isinstance(target_check, LearningOutcome):
        return target_check
    return _evaluate_target_advancement(
        target_state=target_check,
        original_stop_reason=original_stop_reason,
        root_cause_code=root_cause_code,
    )


def _check_sdk_run_succeeded(record: dict[str, Any]) -> LearningOutcome | None:
    """Return a Level-0 outcome if the SDK self-repair run did not pass;
    otherwise None to indicate the SDK side is good and we should look
    at the target rerun next."""

    if str(record.get("status") or "") != "executed":
        return LearningOutcome(level=0, action="telemetry_only", reason="execution_not_completed")
    spawn = record.get("spawn") if isinstance(record.get("spawn"), dict) else {}
    if str(spawn.get("status") or "") != "ok":
        return LearningOutcome(level=0, action="telemetry_only", reason="sdk_spawn_failed")
    raw_exit = spawn.get("exit_code")
    exit_code = int(raw_exit) if raw_exit is not None else 1
    if exit_code != 0:
        return LearningOutcome(level=0, action="telemetry_only", reason="sdk_run_non_zero_exit")
    return None


def _load_target_after_state(
    target_after_planning_dir: Path | None,
) -> dict[str, Any] | LearningOutcome:
    """Either return the parsed target rerun state, or a Level-1
    LearningOutcome explaining why we cannot evaluate it."""

    if target_after_planning_dir is None:
        return LearningOutcome(level=1, action="telemetry_only", reason="target_after_run_not_provided")
    target_state = _load_target_state(Path(target_after_planning_dir))
    if target_state is None:
        return LearningOutcome(level=1, action="telemetry_only", reason="target_after_state_unreadable")
    return target_state


def _evaluate_target_advancement(
    *,
    target_state: dict[str, Any],
    original_stop_reason: str,
    root_cause_code: str,
) -> LearningOutcome:
    new_reason = _normalize_stop_reason(target_state)
    template_id = _ROOT_CAUSE_TO_TEMPLATE.get(root_cause_code, "")
    advanced = new_reason in _TARGET_SUCCESS_REASONS or (
        bool(original_stop_reason) and bool(new_reason) and new_reason != original_stop_reason.upper()
    )
    if advanced and template_id:
        return LearningOutcome(level=2, action="lesson_emitted", template_id=template_id)
    if advanced:
        return LearningOutcome(
            level=1,
            action="telemetry_only",
            reason=f"no_template_for_root_cause:{root_cause_code or 'unknown'}",
        )
    return LearningOutcome(
        level=1,
        action="telemetry_only",
        reason=f"target_did_not_advance (was {original_stop_reason!r}, still {new_reason!r})",
    )


def _normalize_stop_reason(state: dict[str, Any]) -> str:
    for key in ("run_reason", "blocking_reason", "stop_reason", "final_status"):
        value = str(state.get(key) or "").strip().upper()
        if value:
            return value
    return ""


# --- Helpers --------------------------------------------------------------


def _load_execution_record(path: Path) -> dict[str, Any] | None:
    if path.is_dir():
        path = path / SELF_REPAIR_EXECUTION_FILENAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if str(payload.get("schema_version") or "") != SELF_REPAIR_EXECUTION_SCHEMA_VERSION:
        return None
    return payload


def _load_target_state(planning_dir: Path) -> dict[str, Any] | None:
    for name in (".run_truth.json", ".autopilot_state.json"):
        candidate = planning_dir / name
        if not candidate.exists():
            continue
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _infer_sdk_root(record: dict[str, Any]) -> Path:
    spawn = record.get("spawn") if isinstance(record.get("spawn"), dict) else {}
    declared = str(spawn.get("sdk_root") or "").strip()
    if declared:
        return Path(declared)
    env_root = str(os.environ.get("WORKFLOW_SDK_SELF_REPAIR_ROOT", "")).strip()
    if env_root:
        return Path(env_root)
    return Path(__file__).resolve().parents[4]


def _bug_signature_hash(record: dict[str, Any]) -> str:
    """Stable hash for journaling so repeated attempts on the same root
    cause can be tracked. Mirrors the bug_report extractor's hash shape
    so consumers can join across the two artifacts."""

    proposal_status = str(record.get("proposal_status") or "")
    root_cause = record.get("proposal_root_cause") if isinstance(record.get("proposal_root_cause"), dict) else {}
    code = str(root_cause.get("code") or "")
    pieces = [proposal_status, code, str(root_cause.get("summary") or "")]
    return hashlib.sha256("|".join(pieces).encode("utf-8")).hexdigest()[:12]


def _original_stop_reason(record: dict[str, Any]) -> str:
    """Pull the target run's original stop_reason from the execution record."""

    proposal_root_cause = record.get("proposal_root_cause") if isinstance(record.get("proposal_root_cause"), dict) else {}
    summary = str(proposal_root_cause.get("summary") or "")
    return summary


def _journal_entry(
    *,
    record: dict[str, Any],
    outcome: LearningOutcome,
    bug_hash: str,
    root_cause_code: str,
    original_stop_reason: str,
    target_after_planning_dir: Path | None,
) -> dict[str, Any]:
    spawn = record.get("spawn") if isinstance(record.get("spawn"), dict) else {}
    return {
        "schema_version": SELF_REPAIR_JOURNAL_SCHEMA_VERSION,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "bug_signature_hash": bug_hash,
        "root_cause_code": root_cause_code,
        "original_stop_reason": original_stop_reason,
        "execution_status": str(record.get("status") or ""),
        "spawn_feature": str(spawn.get("feature") or ""),
        "spawn_exit_code": spawn.get("exit_code"),
        "target_after_planning_dir": str(target_after_planning_dir) if target_after_planning_dir else "",
        "outcome": outcome.to_dict(),
    }


def _append_journal(sdk_root: Path, entry: dict[str, Any]) -> Path:
    path = sdk_root / SELF_REPAIR_JOURNAL_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)
    return path


def _emit_lesson(
    *,
    project_root: Path,
    template_id: str,
    bug_hash: str,
    root_cause_code: str,
    record: dict[str, Any],
) -> dict[str, Any]:
    spawn = record.get("spawn") if isinstance(record.get("spawn"), dict) else {}
    event = {
        "template_id": template_id,
        "family": root_cause_code or "unknown",
        "run_id": f"self-repair-{bug_hash}",
        "variables": {
            "root_cause": root_cause_code,
            "spawn_feature": str(spawn.get("feature") or ""),
        },
        "metadata": {
            "source": "self_repair_phase_4",
            "bug_signature_hash": bug_hash,
        },
    }
    result = ingest_prompt_lesson_event(project_root, event)
    return {
        "template_id": template_id,
        "event": event,
        "ingest_result": result,
        "store_path": str(project_root / Path(".workflow") / "prompt_lessons.json"),
    }


def _fail_outcome(reason: str, path: Path) -> dict[str, Any]:
    return {
        "schema_version": "workflow.self_repair.learn.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "execution_record_path": str(path),
        "outcome": {"level": 0, "action": "telemetry_only", "reason": reason},
        "journal_path": "",
        "lesson": {},
    }


def _atomic_journal_init(sdk_root: Path) -> Path:
    """Convenience for tests/operators: create the journal file empty if
    absent. Not used by ``learn_from_self_repair`` which appends directly.
    """
    path = sdk_root / SELF_REPAIR_JOURNAL_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        atomic_write_canonical_json(path.parent / "_journal_placeholder.json", {})
    return path


__all__ = [
    "LearningOutcome",
    "SELF_REPAIR_JOURNAL_RELATIVE_PATH",
    "SELF_REPAIR_JOURNAL_SCHEMA_VERSION",
    "learn_from_self_repair",
]
