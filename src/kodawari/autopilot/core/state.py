"""Autopilot state models and legacy compatibility manager."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import logging
from pathlib import Path
from typing import Any

from kodawari.autopilot.core.state_models import (
    LegacySubtaskCheckpoint,
    LegacySubtaskStatus,
    StateManager,
    TaskState,
    TaskStatus,
)
from kodawari.autopilot.core.secret_redactor import redact_jsonable, redact_secret_text
from kodawari.infra.artifact_versions import (
    AUTOPILOT_STATE_SCHEMA_VERSION,
    migrate_payload_for_path,
)
from kodawari.infra.io_atomic import atomic_write_json, load_json_dict, path_lock

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_text(value: Any, *, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def _to_string_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    return [str(item) for item in values]


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_string_float_dict(values: Any) -> dict[str, float]:
    source = dict(values or {})
    return {str(key): _to_float(value) for key, value in source.items()}


def _to_string_int_dict(values: Any) -> dict[str, int]:
    source = dict(values or {})
    return {str(key): _to_int(value) for key, value in source.items()}


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return bool(value)


try:
    from kodawari.autopilot.core.collaboration import ArchitectureDecision as _ArchitectureDecision
except Exception:
    logger.warning("collaboration decision model unavailable; using fallback architecture decision", exc_info=True)
    @dataclass
    class _ArchitectureDecision:
        """Fallback decision model used when collaboration module is unavailable."""

        decision_id: str
        decision: str
        rationale: str
        constraints: list[str] = field(default_factory=list)
        api_contracts: list[str] = field(default_factory=list)
        test_strategy: list[str] = field(default_factory=list)
        owner: str = "opus"
        created_at: str | None = None

        def to_dict(self) -> dict[str, Any]:
            return {
                "id": self.decision_id,
                "decision": self.decision,
                "rationale": self.rationale,
                "constraints": list(self.constraints),
                "api_contracts": list(self.api_contracts),
                "test_strategy": list(self.test_strategy),
                "owner": self.owner,
                "created_at": self.created_at,
            }

        @classmethod
        def from_dict(cls, data: dict[str, Any]) -> "_ArchitectureDecision":
            return cls(
                decision_id=_clean_text(data.get("id"), default=_clean_text(data.get("decision_id"))),
                decision=_clean_text(data.get("decision")),
                rationale=_clean_text(data.get("rationale")),
                constraints=_to_string_list(data.get("constraints")),
                api_contracts=_to_string_list(data.get("api_contracts")),
                test_strategy=_to_string_list(data.get("test_strategy")),
                owner=_clean_text(data.get("owner"), default="opus"),
                created_at=data.get("created_at"),
            )


ArchitectureDecision = _ArchitectureDecision


class Stage(str, Enum):
    INIT = "INIT"
    PLAN_REVIEW = "PLAN_REVIEW"
    IMPLEMENT = "IMPLEMENT"
    VERIFY = "VERIFY"
    GATE = "GATE"
    COMPLETED = "COMPLETED"


class StopReason(str, Enum):
    PASS = "PASS"
    MAX_CYCLES = "MAX_CYCLES"
    TOKEN_BUDGET = "TOKEN_BUDGET"
    HARD_ERROR = "HARD_ERROR"
    STUCK = "STUCK"
    NO_PROGRESS = "NO_PROGRESS"
    USER_INTERRUPT = "USER_INTERRUPT"


class StopAction(str, Enum):
    """Machine-readable next-step recommendation per StopReason.

    The English `_completed_next_action` mapping in this module is for
    operator UI; this enum is for downstream callers (release flow,
    schedulers) that need to branch on the recommended action without
    parsing prose.
    """

    PROCEED_NEXT_TASK = "PROCEED_NEXT_TASK"
    RETRY_WITH_HIGHER_BUDGET = "RETRY_WITH_HIGHER_BUDGET"
    ESCALATE_TO_PLAN = "ESCALATE_TO_PLAN"
    HARD_STOP = "HARD_STOP"
    AWAIT_USER = "AWAIT_USER"


# Keyed by the StopReason *string value* (not the enum member) so a module
# reload that re-creates the enum class does not leave the dict referencing
# stale class identities. importlib.reload(state) is exercised by the
# observability logging tests, and the dict survives that round-trip.
_STOP_REASON_NEXT_ACTION: dict[str, StopAction] = {
    StopReason.PASS.value: StopAction.PROCEED_NEXT_TASK,
    StopReason.MAX_CYCLES.value: StopAction.RETRY_WITH_HIGHER_BUDGET,
    StopReason.TOKEN_BUDGET.value: StopAction.RETRY_WITH_HIGHER_BUDGET,
    StopReason.NO_PROGRESS.value: StopAction.ESCALATE_TO_PLAN,
    StopReason.STUCK.value: StopAction.ESCALATE_TO_PLAN,
    StopReason.HARD_ERROR.value: StopAction.HARD_STOP,
    StopReason.USER_INTERRUPT.value: StopAction.AWAIT_USER,
}


def derived_final_status(reason: StopReason | str | None) -> str:
    """Return the canonical final_status string for a StopReason.

    PASS only when the run completed cleanly; every other reason maps to
    BLOCKED. Strings are accepted to keep this safe for state payloads that
    have round-tripped through JSON.
    """

    if reason is None:
        return "BLOCKED"
    # Identity-tolerant: see comment on stop_reason_next_action — after
    # importlib.reload(state) the test may pass an old-class StopReason that
    # would fail an `is` / `isinstance` check.
    raw = getattr(reason, "name", None) or getattr(reason, "value", None) or str(reason)
    return "PASS" if str(raw).strip().upper() == "PASS" else "BLOCKED"


def stop_reason_next_action(reason: StopReason | str | None) -> StopAction:
    """Return the structured next-step action for a StopReason.

    Strings are accepted to make this safe to call against serialized state
    payloads where the value may have round-tripped through JSON. Unknown
    values fall back to HARD_STOP so callers do not silently auto-resume.
    """

    if reason is None:
        return StopAction.HARD_STOP
    # Tolerate identity drift across importlib.reload(state) round-trips:
    # after a reload, `reason` may be from the pre-reload StopReason class
    # while this function's module-level StopReason is the post-reload class.
    # Compare on the underlying string instead of `isinstance`.
    raw = getattr(reason, "name", None) or getattr(reason, "value", None) or str(reason)
    key = str(raw).strip().upper()
    if not key:
        return StopAction.HARD_STOP
    # Inline switch instead of a module-level dict so this function survives
    # an importlib.reload(state) round-trip even if the dict somehow got
    # nuked (the observability logging tests block imports during reload).
    if key == "PASS":
        return StopAction.PROCEED_NEXT_TASK
    if key in {"MAX_CYCLES", "TOKEN_BUDGET"}:
        return StopAction.RETRY_WITH_HIGHER_BUDGET
    if key in {"STUCK", "NO_PROGRESS"}:
        return StopAction.ESCALATE_TO_PLAN
    if key == "USER_INTERRUPT":
        return StopAction.AWAIT_USER
    return StopAction.HARD_STOP


class SubtaskStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"


_ERROR_CATEGORY_VALUES: tuple[str, ...] = (
    "setup",
    "implement",
    "review",
    "verify",
    "gate",
    "runtime",
    "external_gateway",
)

# Categories whose semantic meaning makes every error worth feeding to the
# instinct learning queue. The phase/action context is enough on its own to
# disambiguate the signature.
_ALWAYS_LEARNABLE_CATEGORIES: frozenset[str] = frozenset({
    "setup",
    "implement",
    "review",
    "verify",
    "gate",
})

# Categories whose surface area is wide enough that we only learn from events
# that the executor / gateway layer has already attached a stable
# ``error_code`` to. Without that gate, generic "runtime" noise (logger
# breadcrumbs, transient retries) would otherwise pollute the learning store.
_GATED_LEARNABLE_CATEGORIES: frozenset[str] = frozenset({
    "runtime",
    "external_gateway",
})


def _event_is_learnable(event: "ErrorEvent") -> bool:
    """Return True when ``event`` belongs in the instinct learning queue.

    See PR3: this replaces the old "category in {...}" filter that blocked
    every runtime / external_gateway event from learning. The new rule keeps
    those categories out by default but admits them when the producer has
    supplied a structured ``error_code`` — that's the signal we need to do
    keyless learning instead of fragile message substring scans.
    """
    if event.category in _ALWAYS_LEARNABLE_CATEGORIES:
        return True
    if event.category in _GATED_LEARNABLE_CATEGORIES:
        return bool(str(event.error_code or "").strip())
    return False


def _normalize_error_phase(phase: Any) -> str:
    text = _clean_text(phase, default="RUNTIME")
    return text.upper() if text else "RUNTIME"


def _normalize_error_category(category: Any, *, phase: Any) -> str:
    resolved = _clean_text(category).lower()
    if resolved in _ERROR_CATEGORY_VALUES:
        return resolved
    phase_key = _normalize_error_phase(phase)
    phase_mapping = {
        Stage.PLAN_REVIEW.value: "review",
        Stage.IMPLEMENT.value: "implement",
        Stage.VERIFY.value: "verify",
        Stage.GATE.value: "gate",
        Stage.INIT.value: "runtime",
        Stage.COMPLETED.value: "runtime",
    }
    return phase_mapping.get(phase_key, "runtime")


class RevisionConflictError(RuntimeError):
    """Raised when autopilot state changes underneath the current writer."""


@dataclass
class ErrorEvent:
    timestamp: str
    phase: str
    action: str
    category: str
    message: str
    recovery_attempted: bool = False
    recovery_succeeded: bool = False
    # Stable run-scoped identifier. Populated by ``add_error()`` from
    # ``CollaborationState.run_id`` when present, so downstream learning can
    # tell apart "10 events in one bad run" from "10 events across 10 runs".
    run_id: str = ""
    # Structured error code emitted by the executor / backend layer (e.g.
    # ``CODEX_CLI_TIMEOUT``, ``CLAUDE_CODE_MISSING``). Lets the learning layer
    # match on a stable key instead of doing fragile substring scans on
    # ``message``.
    error_code: str = ""
    # Free-form structured context (e.g. ``{"backend": "codex_cli",
    # "returncode": 137}``). Kept JSON-serialisable; the learning store will
    # merge per-event metadata into the candidate over time.
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "timestamp": self.timestamp,
            "phase": self.phase,
            "action": self.action,
            "category": self.category,
            "message": self.message,
            "recovery_attempted": self.recovery_attempted,
            "recovery_succeeded": self.recovery_succeeded,
        }
        if self.run_id:
            payload["run_id"] = self.run_id
        if self.error_code:
            payload["error_code"] = self.error_code
        if self.metadata:
            payload["metadata"] = dict(self.metadata)
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ErrorEvent":
        phase = _normalize_error_phase(data.get("phase"))
        raw_metadata = data.get("metadata")
        metadata = dict(raw_metadata) if isinstance(raw_metadata, dict) else {}
        return cls(
            timestamp=_clean_text(data.get("timestamp"), default=_utc_now_iso()),
            phase=phase,
            action=_clean_text(data.get("action"), default=""),
            category=_normalize_error_category(data.get("category"), phase=phase),
            message=_clean_text(data.get("message")),
            recovery_attempted=_to_bool(data.get("recovery_attempted")),
            recovery_succeeded=_to_bool(data.get("recovery_succeeded")),
            run_id=_clean_text(data.get("run_id"), default=""),
            error_code=_clean_text(data.get("error_code"), default=""),
            metadata=metadata,
        )


@dataclass
class SubtaskCheckpoint:
    subtask_id: str
    title: str
    parent_task_id: str | None = None
    status: SubtaskStatus = SubtaskStatus.PENDING
    depends_on: list[str] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    tokens_used: int = 0
    duration_seconds: float = 0.0
    verify_cmd: str | None = None
    verify_status: str | None = None
    verify_output: str | None = None
    error: str | None = None
    attempt: int = 0
    started_at: str | None = None
    completed_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "subtask_id": self.subtask_id,
            "title": self.title,
            "parent_task_id": self.parent_task_id,
            "status": self.status.value,
            "depends_on": list(self.depends_on),
            "changed_files": list(self.changed_files),
            "tokens_used": self.tokens_used,
            "duration_seconds": self.duration_seconds,
            "verify_cmd": self.verify_cmd,
            "verify_status": self.verify_status,
            "verify_output": self.verify_output,
            "error": self.error,
            "attempt": self.attempt,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SubtaskCheckpoint":
        return cls(
            subtask_id=data["subtask_id"],
            title=data.get("title", ""),
            parent_task_id=data.get("parent_task_id"),
            status=SubtaskStatus(data.get("status", SubtaskStatus.PENDING.value)),
            depends_on=_to_string_list(data.get("depends_on")),
            changed_files=_to_string_list(data.get("changed_files")),
            tokens_used=_to_int(data.get("tokens_used", 0), 0),
            duration_seconds=_to_float(data.get("duration_seconds", 0.0), 0.0),
            verify_cmd=data.get("verify_cmd"),
            verify_status=data.get("verify_status"),
            verify_output=data.get("verify_output"),
            error=data.get("error"),
            attempt=_to_int(data.get("attempt", 0), 0),
            started_at=data.get("started_at"),
            completed_at=data.get("completed_at"),
        )


def _deserialize_subtasks(payload: Any) -> dict[str, SubtaskCheckpoint]:
    source = dict(payload or {})
    parsed: dict[str, SubtaskCheckpoint] = {}
    for key, value in source.items():
        if isinstance(value, dict):
            parsed[str(key)] = SubtaskCheckpoint.from_dict(value)
    return parsed


def _deserialize_decisions(payload: Any) -> list[ArchitectureDecision]:
    values = payload if isinstance(payload, list) else []
    return [_deserialize_architecture_decision(item) for item in values if isinstance(item, dict)]


def _deserialize_error_events(payload: Any, *, fallback_history: list[str]) -> list[ErrorEvent]:
    values = payload if isinstance(payload, list) else []
    parsed = [ErrorEvent.from_dict(item) for item in values if isinstance(item, dict)]
    if parsed:
        return parsed
    events: list[ErrorEvent] = []
    for message in fallback_history:
        clean_message = _clean_text(message)
        if not clean_message:
            continue
        events.append(
            ErrorEvent(
                timestamp=_utc_now_iso(),
                phase="RUNTIME",
                action="legacy_error_history",
                category="runtime",
                message=clean_message,
            )
        )
    return events


def _serialize_architecture_decision(item: Any) -> dict[str, Any]:
    if hasattr(item, "to_dict"):
        serialized = item.to_dict()
        if isinstance(serialized, dict):
            return serialized
    if isinstance(item, dict):
        return dict(item)
    return {
        "id": str(getattr(item, "decision_id", "")),
        "decision": str(getattr(item, "decision", "")),
        "rationale": str(getattr(item, "rationale", "")),
        "constraints": list(getattr(item, "constraints", [])),
        "api_contracts": list(getattr(item, "api_contracts", [])),
        "test_strategy": list(getattr(item, "test_strategy", [])),
        "owner": str(getattr(item, "owner", "opus")),
        "created_at": getattr(item, "created_at", None),
    }


def _deserialize_architecture_decision(item: dict[str, Any]) -> ArchitectureDecision:
    if hasattr(ArchitectureDecision, "from_dict"):
        return ArchitectureDecision.from_dict(item)
    return ArchitectureDecision(
        decision_id=str(item.get("id") or item.get("decision_id") or ""),
        decision=str(item.get("decision") or ""),
        rationale=str(item.get("rationale") or ""),
    )


@dataclass
class AutopilotState:
    feature: str
    project_root: Path
    schema_version: str = AUTOPILOT_STATE_SCHEMA_VERSION
    revision: int = 0
    current_stage: Stage = Stage.INIT
    cycle: int = 0
    tokens_used: int = 0
    error_history: list[str] = field(default_factory=list)
    error_events: list[ErrorEvent] = field(default_factory=list)
    last_error: str | None = None
    changed_files: set[str] = field(default_factory=set)
    completed_tasks: list[str] = field(default_factory=list)
    task_timings: dict[str, float] = field(default_factory=dict)
    active_task: str | None = None
    active_pid: int | None = None
    active_attempt: int | None = None
    stage_started_at: str | None = None
    heartbeat_at: str | None = None
    last_stage_status: str | None = None
    warning_noise_events: int = 0
    warning_noise_degraded_events: int = 0
    warning_noise_by_task: dict[str, int] = field(default_factory=dict)
    verify_setup_recovery_attempted: int = 0
    verify_setup_recovery_succeeded: int = 0
    verify_setup_recovery_last_error: str | None = None
    subtasks: dict[str, SubtaskCheckpoint] = field(default_factory=dict)
    active_subtask: str | None = None
    parallel_runtime: dict[str, Any] = field(default_factory=dict)
    architecture_decisions: list[ArchitectureDecision] = field(default_factory=list)
    started_at: str | None = None
    updated_at: str | None = None
    stop_reason: StopReason | None = None
    final_status: str | None = None
    # run_id ties the session-scoped fields (error_history, error_events,
    # last_error, verify_setup_recovery_*, warning_noise_*) to a specific
    # task-run invocation. When a new run sees a state file with a different
    # run_id, those session fields are reset before the new run writes its
    # terminal outcome — so a 2-week-old timeout error does not show up next
    # to a fresh PASS in `kodawari status`. Empty string means "legacy
    # state that pre-dates the run_id field".
    run_id: str = ""
    task_claim: dict[str, Any] = field(default_factory=dict)
    _loaded_revision: int = field(default=0, init=False, repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "revision": self.revision,
            "feature": self.feature,
            "project_root": str(self.project_root),
            "current_stage": self.current_stage.value,
            "cycle": self.cycle,
            "tokens_used": self.tokens_used,
            "error_history": list(self.error_history),
            "error_events": [item.to_dict() for item in self.error_events],
            "last_error": self.last_error,
            "changed_files": sorted(self.changed_files),
            "completed_tasks": list(self.completed_tasks),
            "task_timings": dict(self.task_timings),
            "active_task": self.active_task,
            "active_pid": self.active_pid,
            "active_attempt": self.active_attempt,
            "stage_started_at": self.stage_started_at,
            "heartbeat_at": self.heartbeat_at,
            "last_stage_status": self.last_stage_status,
            "warning_noise_events": self.warning_noise_events,
            "warning_noise_degraded_events": self.warning_noise_degraded_events,
            "warning_noise_by_task": dict(self.warning_noise_by_task),
            "verify_setup_recovery_attempted": self.verify_setup_recovery_attempted,
            "verify_setup_recovery_succeeded": self.verify_setup_recovery_succeeded,
            "verify_setup_recovery_last_error": self.verify_setup_recovery_last_error,
            "subtasks": {key: value.to_dict() for key, value in self.subtasks.items()},
            "active_subtask": self.active_subtask,
            "parallel_runtime": dict(self.parallel_runtime),
            "architecture_decisions": [
                _serialize_architecture_decision(item) for item in self.architecture_decisions
            ],
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "stop_reason": self.stop_reason.value if self.stop_reason else None,
            "final_status": self.final_status,
            "run_id": self.run_id,
            "task_claim": dict(self.task_claim),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AutopilotState":
        error_history = _to_string_list(data.get("error_history"))
        instance = cls(
            feature=data["feature"],
            project_root=Path(data["project_root"]),
            schema_version=_clean_text(data.get("schema_version"), default=AUTOPILOT_STATE_SCHEMA_VERSION),
            revision=_to_int(data.get("revision", 0), 0),
            current_stage=Stage(data.get("current_stage", Stage.INIT.value)),
            cycle=_to_int(data.get("cycle", 0), 0),
            tokens_used=_to_int(data.get("tokens_used", 0), 0),
            error_history=error_history,
            error_events=_deserialize_error_events(
                data.get("error_events"),
                fallback_history=error_history,
            ),
            last_error=data.get("last_error"),
            changed_files=set(_to_string_list(data.get("changed_files"))),
            completed_tasks=_to_string_list(data.get("completed_tasks")),
            task_timings=_to_string_float_dict(data.get("task_timings")),
            active_task=data.get("active_task"),
            active_pid=data.get("active_pid"),
            active_attempt=data.get("active_attempt"),
            stage_started_at=data.get("stage_started_at"),
            heartbeat_at=data.get("heartbeat_at"),
            last_stage_status=data.get("last_stage_status"),
            warning_noise_events=_to_int(data.get("warning_noise_events", 0), 0),
            warning_noise_degraded_events=_to_int(data.get("warning_noise_degraded_events", 0), 0),
            warning_noise_by_task=_to_string_int_dict(data.get("warning_noise_by_task")),
            verify_setup_recovery_attempted=_to_int(data.get("verify_setup_recovery_attempted", 0), 0),
            verify_setup_recovery_succeeded=_to_int(data.get("verify_setup_recovery_succeeded", 0), 0),
            verify_setup_recovery_last_error=data.get("verify_setup_recovery_last_error"),
            subtasks=_deserialize_subtasks(data.get("subtasks")),
            active_subtask=data.get("active_subtask"),
            parallel_runtime=dict(data.get("parallel_runtime") or {}),
            architecture_decisions=_deserialize_decisions(data.get("architecture_decisions")),
            started_at=data.get("started_at"),
            updated_at=data.get("updated_at"),
            stop_reason=StopReason(data["stop_reason"]) if data.get("stop_reason") else None,
            final_status=data.get("final_status"),
            run_id=_clean_text(data.get("run_id"), default=""),
            task_claim=dict(data.get("task_claim") or {}),
        )
        instance._loaded_revision = instance.revision
        return instance

    def save(self, path: Path, *, expected_revision: int | None = None) -> None:
        self.updated_at = _utc_now_iso()
        path.parent.mkdir(parents=True, exist_ok=True)
        resolved_expected = self._loaded_revision if expected_revision is None else _to_int(expected_revision, 0)
        with path_lock(path):
            current_revision = 0
            if path.exists():
                current_payload = load_json_dict(path, required=True)
                if current_payload is not None:
                    current_revision = _to_int(current_payload.get("revision", 0), 0)
            if path.exists() and current_revision != resolved_expected:
                raise RevisionConflictError(
                    f"autopilot state revision conflict at {path}: expected {resolved_expected}, found {current_revision}"
                )
            self.revision = current_revision + 1
            self.schema_version = AUTOPILOT_STATE_SCHEMA_VERSION
            atomic_write_json(path, self.to_dict(), use_lock=False)
            self._loaded_revision = self.revision

    @classmethod
    def load(cls, path: Path, *, allow_legacy: bool = True) -> "AutopilotState":
        payload = load_json_dict(path, required=True, quarantine_on_error=True)
        if payload is None:
            raise ValueError(f"required file not found: {path}")
        if allow_legacy:
            migration = migrate_payload_for_path(path, payload)
            if migration is not None:
                payload = migration.payload
        return cls.from_dict(payload)

    def add_error(
        self,
        error: str,
        *,
        phase: str | None = None,
        action: str | None = None,
        category: str | None = None,
        recovery_attempted: bool = False,
        recovery_succeeded: bool = False,
        timestamp: str | None = None,
        run_id: str | None = None,
        error_code: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ErrorEvent:
        message = redact_secret_text(_clean_text(error))
        self.last_error = message
        self.error_history.append(message)
        resolved_phase = _normalize_error_phase(phase or self.current_stage.value)
        # Default run_id to the state's own run_id so callers do not have to
        # thread it through every add_error() site.
        resolved_run_id = _clean_text(
            run_id if run_id is not None else self.run_id,
            default="",
        )
        event = ErrorEvent(
            timestamp=_clean_text(timestamp, default=_utc_now_iso()),
            phase=resolved_phase,
            action=_clean_text(action),
            category=_normalize_error_category(category, phase=resolved_phase),
            message=message,
            recovery_attempted=bool(recovery_attempted),
            recovery_succeeded=bool(recovery_succeeded),
            run_id=resolved_run_id,
            error_code=_clean_text(error_code, default=""),
            metadata=redact_jsonable(dict(metadata)) if isinstance(metadata, dict) else {},
        )
        self.error_events.append(event)
        self._ingest_error_learning(event)
        return event

    def _ingest_error_learning(self, event: ErrorEvent) -> None:
        if not _event_is_learnable(event):
            return
        try:
            from kodawari.instincts import ingest_error_event
        except Exception:
            logger.warning("instinct error-ingestion module unavailable", exc_info=True)
            return
        try:
            ingest_error_event(self.project_root, event.to_dict())
        except Exception:
            logger.warning("instinct error-ingestion failed", exc_info=True)
            return

    def recent_error_events(self, limit: int = 5) -> list[ErrorEvent]:
        if limit <= 0:
            return []
        return list(self.error_events[-limit:])

    def is_stuck(self) -> bool:
        return len(self.error_history) >= 3 and len(set(self.error_history[-3:])) == 1

    def has_progress(self, prev_changed_files: set[str]) -> bool:
        return self.changed_files != prev_changed_files

    def clear_runtime_state(self) -> None:
        self.active_task = None
        self.active_pid = None
        self.active_attempt = None
        self.active_subtask = None
        self.task_claim = {}

    def mark_completed(self, reason: StopReason, status: str | None = None) -> None:
        """Authoritative completion stamp.

        ``stop_reason`` is the canonical signal — ``final_status`` is derived
        from it (``"PASS"`` iff ``reason is StopReason.PASS``, otherwise
        ``"BLOCKED"``). The legacy ``status`` argument is accepted for
        backward compatibility but is normalized so the two fields can never
        disagree. ``last_stage_status`` is a debug-only mirror.
        """
        self.stop_reason = reason
        derived_status = derived_final_status(reason)
        self.final_status = derived_status
        self.current_stage = Stage.COMPLETED
        self.clear_runtime_state()
        # Preserve the caller-provided text for last_stage_status when given
        # so existing operator-facing strings stay readable, but never let it
        # diverge from final_status when no override is provided.
        self.last_stage_status = status if status else derived_status

    def add_subtask(self, subtask: SubtaskCheckpoint) -> None:
        if not str(subtask.parent_task_id or "").strip():
            subtask.parent_task_id = str(subtask.subtask_id).split(".", 1)[0].upper()
        self.subtasks[subtask.subtask_id] = subtask

    def add_architecture_decision(self, decision: ArchitectureDecision) -> None:
        normalized_id = str(getattr(decision, "decision_id", "")).strip()
        for index, existing in enumerate(self.architecture_decisions):
            existing_id = str(getattr(existing, "decision_id", "")).strip()
            if normalized_id and existing_id == normalized_id:
                self.architecture_decisions[index] = decision
                return
        self.architecture_decisions.append(decision)

    def get_subtask(self, subtask_id: str) -> SubtaskCheckpoint | None:
        return self.subtasks.get(subtask_id)

    def _subtasks_for_task(self, task_id: str | None = None) -> list[SubtaskCheckpoint]:
        if task_id is None:
            return list(self.subtasks.values())
        normalized = str(task_id).strip().upper()
        scoped: list[SubtaskCheckpoint] = []
        for subtask in self.subtasks.values():
            parent = str(subtask.parent_task_id or str(subtask.subtask_id).split(".", 1)[0]).upper()
            if parent == normalized:
                scoped.append(subtask)
        return scoped

    def get_pending_subtasks(self, task_id: str | None = None) -> list[SubtaskCheckpoint]:
        lookup = {key.lower(): value for key, value in self.subtasks.items()}
        pending: list[SubtaskCheckpoint] = []
        for subtask in self._subtasks_for_task(task_id):
            if subtask.status != SubtaskStatus.PENDING:
                continue
            if self._deps_satisfied(subtask, lookup):
                pending.append(subtask)
        return pending

    def _deps_satisfied(
        self,
        subtask: SubtaskCheckpoint,
        lookup: dict[str, SubtaskCheckpoint],
    ) -> bool:
        for dep in subtask.depends_on:
            dep_subtask = lookup.get(str(dep).lower())
            if dep_subtask is None or dep_subtask.status != SubtaskStatus.DONE:
                return False
        return True

    def get_completed_subtasks(self, task_id: str | None = None) -> list[SubtaskCheckpoint]:
        return [item for item in self._subtasks_for_task(task_id) if item.status == SubtaskStatus.DONE]

    def get_failed_subtasks(self, task_id: str | None = None) -> list[SubtaskCheckpoint]:
        return [item for item in self._subtasks_for_task(task_id) if item.status == SubtaskStatus.FAILED]

    def has_subtasks(self, task_id: str | None = None) -> bool:
        return bool(self._subtasks_for_task(task_id))

    def _infer_task_id(self) -> str | None:
        raw = str(self.active_task or "").strip()
        if raw:
            return raw.split(":", 1)[0].strip().upper()
        if self.active_subtask:
            subtask = self.get_subtask(self.active_subtask)
            if subtask and subtask.parent_task_id:
                return str(subtask.parent_task_id).strip().upper()
        return None

    def _derive_blocking_reason(self, failed_subtasks: list[SubtaskCheckpoint]) -> str | None:
        if failed_subtasks:
            return self._blocking_reason_from_subtask(failed_subtasks[0])
        reason = self._blocking_reason_from_state()
        if reason:
            return reason
        if self._is_error_stage_status():
            return self.last_stage_status
        return None

    def _blocking_reason_from_subtask(self, failed: SubtaskCheckpoint) -> str:
        detail = failed.error or failed.verify_output or failed.title
        return f"Subtask {failed.subtask_id} failed: {detail}"

    def _blocking_reason_from_state(self) -> str | None:
        if self.last_error:
            return self.last_error
        if self.stop_reason and self.stop_reason != StopReason.PASS:
            return self.stop_reason.value
        return None

    def _is_error_stage_status(self) -> bool:
        return _clean_text(self.last_stage_status).lower() in {
            "blocked",
            "error",
            "failed",
            "setup_error",
        }

    def _derive_next_action(self, blocking_reason: str | None, failed_subtasks: list[SubtaskCheckpoint]) -> str:
        if self.current_stage == Stage.COMPLETED:
            return self._completed_next_action()
        if failed_subtasks:
            return "Repair the failed subtask and rerun scoped verify"
        if self.current_stage == Stage.INIT:
            return "Run plan review to validate the planning artifacts"
        stage_handlers = {
            Stage.PLAN_REVIEW: self._plan_review_next_action,
            Stage.IMPLEMENT: self._implement_next_action,
            Stage.VERIFY: self._verify_next_action,
            Stage.GATE: self._gate_next_action,
        }
        handler = stage_handlers.get(self.current_stage)
        if handler:
            return handler(blocking_reason)
        return "Inspect state and continue from the latest successful checkpoint"

    def _completed_next_action(self) -> str:
        mapping = {
            StopReason.PASS: "Start the next queued task or close the automation run",
            StopReason.MAX_CYCLES: "Resume with a higher cycle budget or split the remaining work",
            StopReason.TOKEN_BUDGET: "Resume with a higher token budget or narrow the task scope",
            StopReason.STUCK: "Escalate to Opus for redesign or split the task further",
        }
        if self.stop_reason in mapping:
            return mapping[self.stop_reason]
        return "Inspect the last blocking error and apply a targeted fix before resuming"

    def _plan_review_next_action(self, blocking_reason: str | None) -> str:
        if blocking_reason:
            return "Fix planning artifacts and rerun plan review"
        return "Advance to implementation"

    def _implement_next_action(self, blocking_reason: str | None) -> str:
        del blocking_reason
        if self.active_task:
            return "Continue implementation for the active task"
        return "Pick the next executable task"

    def _verify_next_action(self, blocking_reason: str | None) -> str:
        if blocking_reason:
            return "Repair the verify environment and rerun verification"
        return "Fix verification failures and rerun verify"

    def _gate_next_action(self, blocking_reason: str | None) -> str:
        if blocking_reason:
            return "Address gate findings before moving to the next task"
        return "Proceed after gate approval"

    def get_unified_status(self) -> dict[str, Any]:
        task_id = self._infer_task_id()
        pending = self.get_pending_subtasks(task_id)
        completed = self.get_completed_subtasks(task_id)
        failed = self.get_failed_subtasks(task_id)
        blocking_reason = self._derive_blocking_reason(failed)
        stage_status = self._stage_status_value()
        is_terminal = self.current_stage == Stage.COMPLETED
        is_blocked = blocking_reason is not None and self.stop_reason != StopReason.PASS
        parallel_runtime = dict(self.parallel_runtime or {})
        worker_statuses = list(parallel_runtime.get("worker_statuses") or [])
        return {
            "feature": self.feature,
            "current_phase": self.current_stage.value,
            "stage_status": stage_status,
            "final_status": self.final_status,
            "stop_reason": self.stop_reason.value if self.stop_reason else None,
            "blocking_reason": blocking_reason,
            "is_blocked": is_blocked,
            "is_terminal": is_terminal,
            "current_task": self.active_task,
            "current_task_id": task_id,
            "current_subtask": self.active_subtask,
            "completed_tasks_total": len(self.completed_tasks),
            "pending_subtasks": [item.subtask_id for item in pending],
            "completed_subtasks": [item.subtask_id for item in completed],
            "failed_subtasks": [item.subtask_id for item in failed],
            "architecture_decisions_total": len(self.architecture_decisions),
            "parallel_merge_status": str(parallel_runtime.get("merge_status") or ""),
            "worker_statuses": worker_statuses,
            "parallel_runtime": parallel_runtime,
            "next_action": self._derive_next_action(blocking_reason, failed),
        }

    def _stage_status_value(self) -> str | None:
        if self.last_stage_status:
            return self.last_stage_status
        if self.current_stage == Stage.COMPLETED:
            return self.final_status
        return None


@dataclass
class AutopilotResult:
    feature: str
    cycles_completed: int
    tokens_used: int
    final_status: str
    stop_reason: StopReason
    last_error: str | None = None
    completed_tasks: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "feature": self.feature,
            "cycles_completed": self.cycles_completed,
            "tokens_used": self.tokens_used,
            "final_status": self.final_status,
            "stop_reason": self.stop_reason.value,
            "last_error": self.last_error,
            "completed_tasks": list(self.completed_tasks),
            "metadata": dict(self.metadata),
        }


__all__ = [
    "ArchitectureDecision",
    "AutopilotResult",
    "AutopilotState",
    "ErrorEvent",
    "LegacySubtaskCheckpoint",
    "LegacySubtaskStatus",
    "Stage",
    "StateManager",
    "StopReason",
    "SubtaskCheckpoint",
    "SubtaskStatus",
    "TaskState",
    "TaskStatus",
]

