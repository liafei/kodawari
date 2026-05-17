"""Executor recovery routing helpers for the autopilot engine.

Split out of engine_implementation_mixin to keep that mixin under the
canonical file-shape redline (lines + complexity). All methods here
preserve the original semantics; only the location changes.
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import threading
from pathlib import Path
from typing import Any

from kodawari.autopilot.core.collaboration import (
    CollaborationAction,
    update_round_record_outcome,
)
from kodawari.autopilot.core.secret_redactor import redact_jsonable
from kodawari.autopilot.core.state import Stage, StopReason
from kodawari.autopilot.engine.engine_support import _LoopRuntime
from kodawari.autopilot.recovery.executor_recovery import (
    build_recovery_card,
    build_scope_expansion_recovery_card,
    write_recovery_artifacts,
)
from kodawari.autopilot.recovery.escalation_handler import (
    escalation_count_from_context,
    is_gate_complexity_exhausted,
    write_redesign_request,
)
from kodawari.autopilot.recovery.failure_event import FailureEvent, build_failure_event
from kodawari.autopilot.recovery.registry import RecoveryContext, route_deterministic_recovery
from kodawari.infra.io_atomic import atomic_write_canonical_json, load_jsonl_rows


_SUCCESSFUL_RECOVERY_BASE_WRITE_TOOLS = {"apply_patch_plan_item", "delete_file", "str_replace", "write_new_file"}


def _runtime_verify_passed(runtime: Any) -> bool:
    verify_check = runtime.verify_check if isinstance(getattr(runtime, "verify_check", None), dict) else {}
    return bool(verify_check.get("passed")) or str(verify_check.get("status") or "").upper() == "PASS"


def _recovery_synthesizer_enabled() -> bool:
    """Gate the LLM recovery synthesizer behind an explicit opt-in.

    The synthesizer routinely times out at 60s on stalled executor sessions
    and lands the run in STUCK / RECOVERY_SYNTHESIZER_TIMEOUT with no useful
    artifacts. Pure deterministic recovery + ``recovery_attempts_for_signature``
    exhaustion produces the same final outcome (BLOCKED) without burning a
    model round, so the synthesizer is OFF by default. Set
    ``WORKFLOW_AUTOPILOT_RECOVERY_SYNTHESIZER`` to ``1``/``true``/``yes``/``on``
    to re-enable it.
    """

    raw = str(os.getenv("WORKFLOW_AUTOPILOT_RECOVERY_SYNTHESIZER", "")).strip().lower()
    return raw in {"1", "true", "yes", "on"}


class EngineRecoveryMixin:
    def _maybe_prepare_executor_recovery(
        self,
        *,
        runtime: _LoopRuntime,
        action: CollaborationAction,
        round_record: dict[str, Any],
    ) -> dict[str, Any] | None:
        if action not in {CollaborationAction.FIX_ROUND, CollaborationAction.CODEX_FIX}:
            return None
        must_fix = [str(item) for item in list(runtime.context.review_feedback.must_fix or []) if str(item).strip()]
        if not must_fix:
            return None
        self._refresh_recovery_base_workspace_for_runtime(runtime)
        recovery_base_card = dict(runtime.pending_recovery_card or self._task_card_payload or {})
        recovery_base_files = [
            str(item) for item in list(recovery_base_card.get("files_to_change") or []) if str(item).strip()
        ]
        stall_report = self._load_executor_stall_report()
        failure_event = build_failure_event(
            stall_report=stall_report,
            execution_result=dict(runtime.execution_result or {}) if isinstance(runtime.execution_result, dict) else None,
            verify_check=dict(runtime.verify_check or {}) if isinstance(runtime.verify_check, dict) else None,
            gate_check=dict(runtime.gate_check or {}) if isinstance(runtime.gate_check, dict) else None,
            must_fix=must_fix,
            verify_passed=_runtime_verify_passed(runtime),
        )
        deterministic_match = route_deterministic_recovery(
            RecoveryContext(
                project_root=Path(self.config.project_root),
                original_card=recovery_base_card,
                task_id=runtime.task_id,
                must_fix=must_fix,
                event=failure_event,
            )
        )
        failure_mode_tag = (
            deterministic_match.name
            if deterministic_match is not None
            else failure_event.detector_hint or failure_event.error_code or "peer_review_fix"
        )
        max_attempts = self._executor_recovery_attempt_limit()
        signature = self._executor_recovery_attempt_signature(
            runtime=runtime,
            must_fix=must_fix,
            error_code=failure_event.error_code,
            failure_mode_tag=failure_mode_tag,
            affected_paths=failure_event.affected_paths,
        )
        if runtime.recovery_attempt_signature != signature:
            runtime.recovery_attempt_signature = signature
            runtime.recovery_attempts_for_signature = 0
        total_cap = self._executor_recovery_total_attempt_cap()
        if runtime.recovery_attempts >= total_cap:
            message = (
                "Executor recovery total attempts exhausted "
                f"({runtime.recovery_attempts}/{total_cap}) "
                "across failure modes — task cannot be salvaged by recovery alone"
            )
            self.state.add_error(message, phase=Stage.IMPLEMENT.value, action=action.value, category="recovery")
            self.state.mark_completed(StopReason.STUCK, "BLOCKED")
            self.state.last_stage_status = "executor_recovery_total_exhausted"
            round_record["stage_status"] = "blocked"
            round_record["last_error"] = message
            round_record["details"] = {
                "recovery": {
                    "status": "blocked",
                    "reason": "RECOVERY_TOTAL_ATTEMPTS_EXHAUSTED",
                    "signature": signature,
                    "failure_event": failure_event.to_dict(),
                    "attempts_for_signature": runtime.recovery_attempts_for_signature,
                    "total_attempts": runtime.recovery_attempts,
                    "total_cap": total_cap,
                }
            }
            runtime.round_records.append(update_round_record_outcome(round_record, runtime.context))
            return self._finish_loop(runtime, reason="RECOVERY_TOTAL_ATTEMPTS_EXHAUSTED", last_error=message, action=action)
        if runtime.recovery_attempts_for_signature >= max_attempts:
            # Try the unified escalation system: classify failure, write
            # .{phase}_decision_request.json, pause the loop. Falls through
            # to legacy BLOCKED only when failure is not escalatable OR
            # escalation_count has reached the per-phase cap.
            from kodawari.autopilot.escalation import maybe_escalate
            completed = list(self.state.completed_task_ids) if hasattr(self.state, "completed_task_ids") else []
            escalated, kind = maybe_escalate(
                planning_dir=self._planning_dir,
                phase="executor",
                failure_event=failure_event,
                feature=self.config.feature,
                task_id=runtime.task_id,
                failure_summary=failure_event.evidence or failure_event.error_code,
                completed_task_ids=completed,
                extra_context={
                    "signature": signature,
                    "attempts_for_signature": runtime.recovery_attempts_for_signature,
                    "total_attempts": runtime.recovery_attempts,
                    "detector_hint": failure_event.detector_hint,
                    "failure_code": failure_event.error_code,
                },
            )
            if escalated:
                # Auto-decide: invoke the HTTP planner inline to design a
                # concrete refactor approach and write the recovery card. Lets
                # autopilot stay fully autonomous on gate_complexity failures.
                # The next autopilot start consumes the response via
                # apply_pending_resume. Controlled by WORKFLOW_AUTO_DECIDE
                # (default ON). Failure here is non-fatal — we still pause the
                # loop and let the operator run ``kodawari decide`` manually.
                auto_applied = False
                try:
                    from kodawari.cli.runtime.decide_cmd import auto_decide_pending

                    project_root = getattr(self.config, "project_root", None)
                    auto_applied = bool(
                        auto_decide_pending(self._planning_dir, project_root=project_root)
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("auto_decide_pending raised: %s", exc)

                message = (
                    f"Recovery exhausted for signature {signature}; "
                    f"{'auto-decided' if auto_applied else 'escalating'} "
                    f"({kind.value if kind else 'unknown'})"
                )
                self.state.last_stage_status = "executor_recovery_escalated"
                round_record["stage_status"] = "paused"
                round_record["last_error"] = message
                round_record["details"] = {
                    "recovery": {
                        "status": "auto_decided" if auto_applied else "escalated",
                        "reason": "RECOVERY_EXHAUSTED_ESCALATE",
                        "kind": kind.value if kind else None,
                        "signature": signature,
                        "failure_event": failure_event.to_dict(),
                        "attempts_for_signature": runtime.recovery_attempts_for_signature,
                        "total_attempts": runtime.recovery_attempts,
                        "auto_decide_applied": auto_applied,
                    }
                }
                runtime.round_records.append(update_round_record_outcome(round_record, runtime.context))
                # Pause the engine loop. When auto_decide wrote the recovery
                # card, the next autopilot start will apply it via
                # apply_pending_resume and re-run the task with the planner's
                # specific approach injected as recovery.must_fix.
                finish_reason = "AUTO_DECIDED_RETRY_PENDING" if auto_applied else "ESCALATED_TO_DECIDE"
                return self._finish_loop(runtime, reason=finish_reason, last_error=message, action=action)
            else:
                # No escalation path (or cap reached) — final BLOCKED
                message = (
                    "Executor recovery attempts exhausted "
                    f"({runtime.recovery_attempts_for_signature}/{max_attempts}) "
                    f"for failure signature {signature}"
                )
                self.state.add_error(message, phase=Stage.IMPLEMENT.value, action=action.value, category="recovery")
                self.state.mark_completed(StopReason.STUCK, "BLOCKED")
                self.state.last_stage_status = "executor_recovery_exhausted"
                round_record["stage_status"] = "blocked"
                round_record["last_error"] = message
                round_record["details"] = {
                    "recovery": {
                        "status": "blocked",
                        "reason": "RECOVERY_ATTEMPTS_EXHAUSTED",
                        "signature": signature,
                        "failure_event": failure_event.to_dict(),
                        "attempts_for_signature": runtime.recovery_attempts_for_signature,
                        "total_attempts": runtime.recovery_attempts,
                    }
                }
                runtime.round_records.append(update_round_record_outcome(round_record, runtime.context))
                return self._finish_loop(runtime, reason="RECOVERY_ATTEMPTS_EXHAUSTED", last_error=message, action=action)
        yielded_deterministic_detector = ""
        if deterministic_match is not None and self._should_yield_deterministic_recovery_to_synthesizer(
            deterministic_match=deterministic_match,
            runtime=runtime,
        ):
            yielded_deterministic_detector = deterministic_match.name
            deterministic_match = None
        if deterministic_match is not None:
            decision = dict(deterministic_match.decision)
            card = dict(deterministic_match.card)
            decision["role"] = "deterministic_recovery"
            decision["backend"] = "kodawari"
            decision["model"] = ""
            decision["detector_name"] = deterministic_match.name
            decision["detector_priority"] = deterministic_match.priority
            decision["detector_evidence"] = dict(deterministic_match.evidence)
            # Propagate detector identity into the card itself so downstream
            # consumers (specifically the action_only_mode flip in
            # engine_implementation_mixin) can recognise which detector
            # produced this card. Without this, the card is anonymous and
            # detector-specific handling at consume-time is dead code.
            card["detector_name"] = deterministic_match.name
            card["detector_evidence"] = dict(deterministic_match.evidence)
            runtime.recovery_attempts += 1
            runtime.recovery_attempts_for_signature += 1
            runtime.recovery_decisions.append(dict(decision))
            # Detectors that produce no recovery card (action ==
            # task_blocked_by_precondition) signal "no retry possible — stop
            # the loop and surface the structured signal to the planner". Do
            # not enter the implementation round again; finish_loop now so
            # the next plan pass can insert the missing prerequisite work.
            if not card or str(decision.get("action") or "") == "task_blocked_by_precondition":
                write_recovery_artifacts(self._planning_dir, decision=decision, card=None)
                self.state.last_stage_status = "executor_recovery_task_blocked_by_precondition"
                round_record["stage_status"] = "blocked"
                round_record["last_error"] = str(decision.get("reason") or "task blocked by precondition")
                round_record["details"] = {
                    "recovery": dict(decision),
                    "missing_preconditions": list(decision.get("missing_preconditions") or []),
                }
                self.state.add_error(
                    str(decision.get("reason") or "task blocked by precondition"),
                    phase=Stage.IMPLEMENT.value,
                    action=action.value,
                    category="recovery",
                    error_code="TASK_BLOCKED_BY_PRECONDITION",
                )
                self.state.mark_completed(StopReason.STUCK, "BLOCKED")
                runtime.round_records.append(update_round_record_outcome(round_record, runtime.context))
                return self._finish_loop(
                    runtime,
                    reason="TASK_BLOCKED_BY_PRECONDITION",
                    last_error=str(decision.get("reason") or "task blocked by precondition"),
                    action=action,
                )
            self._attach_recovery_base_workspace(card, runtime)
            runtime.pending_recovery_card = card
            write_recovery_artifacts(self._planning_dir, decision=decision, card=card)
            self.state.last_stage_status = "executor_recovery_deterministic"
            self._maybe_emit_hook(
                runtime.hook_events,
                event="executor_recovery_deterministic",
                task_id=runtime.task_id,
                task_label=runtime.task_label,
                action=action,
                task_scope=runtime.task_scope,
                details={
                    "attempt": runtime.recovery_attempts,
                    "attempt_for_signature": runtime.recovery_attempts_for_signature,
                    "signature": signature,
                    "detector_name": deterministic_match.name,
                    "decision": decision,
                    "files_to_change": card.get("files_to_change"),
                },
            )
            return None
        if not self._should_synthesize_executor_recovery(runtime):
            return None
        synthesizer = getattr(self.adapter, "synthesize_executor_recovery", None)
        if not callable(synthesizer):
            return None
        runtime.recovery_attempts += 1
        runtime.recovery_attempts_for_signature += 1
        recovery_context = {
            "task_id": runtime.task_id,
            "task_label": runtime.task_label,
            "feature": self.config.feature,
            "project_root": str(self.config.project_root),
            "recovery_source_root": str(self._executor_recovery_source_root(runtime)),
            "planning_dir": str(self._planning_dir),
            "task_card": recovery_base_card,
            "task_card_files": recovery_base_files,
            "requested_action": action.value,
            "previous_recovery_decisions": list(runtime.recovery_decisions),
            "previous_execution_result": dict(runtime.execution_result or {}),
        }
        if yielded_deterministic_detector:
            recovery_context["yielded_deterministic_detector"] = yielded_deterministic_detector
        synthesis = self._call_recovery_synthesizer_with_timeout(
            synthesizer,
            failure_event=failure_event,
            task=runtime.task_label,
            context=recovery_context,
            must_fix=must_fix,
            stall_report=stall_report,
        )
        if isinstance(synthesis, dict) and str(synthesis.get("status") or "").lower() == "timeout":
            runtime.recovery_attempts = max(0, runtime.recovery_attempts - 1)
            runtime.recovery_attempts_for_signature = max(0, runtime.recovery_attempts_for_signature - 1)
            message = str(synthesis.get("error") or "recovery synthesizer timed out")
            decision = {
                "schema_version": "execution.recovery_decision.v1",
                "action": "escalate_to_human",
                "reason": "RECOVERY_SYNTHESIZER_TIMEOUT",
                "diagnosis": message,
                "role": "recovery_synthesizer",
                "source": "kodawari.recovery_synthesizer_timeout",
                "error_code": "RECOVERY_SYNTHESIZER_TIMEOUT",
            }
            write_recovery_artifacts(self._planning_dir, decision=decision, card=None)
            self._write_recovery_failure_snapshot(
                failure_event=failure_event,
                decision=decision,
                stall_report=stall_report,
                execution_result=dict(runtime.execution_result or {}) if isinstance(runtime.execution_result, dict) else {},
            )
            self.state.add_error(
                message,
                phase=Stage.IMPLEMENT.value,
                action=action.value,
                category="recovery",
                error_code="RECOVERY_SYNTHESIZER_TIMEOUT",
            )
            self.state.mark_completed(StopReason.STUCK, "BLOCKED")
            self.state.last_stage_status = "executor_recovery_synthesizer_timeout"
            round_record["stage_status"] = "blocked"
            round_record["last_error"] = message
            round_record["details"] = {"recovery": decision, "failure_event": failure_event.to_dict()}
            runtime.round_records.append(update_round_record_outcome(round_record, runtime.context))
            return self._finish_loop(runtime, reason="RECOVERY_SYNTHESIZER_TIMEOUT", last_error=message, action=action)
        if isinstance(synthesis, dict) and str(synthesis.get("status") or "").lower() == "unavailable":
            runtime.recovery_attempts = max(0, runtime.recovery_attempts - 1)
            runtime.recovery_attempts_for_signature = max(0, runtime.recovery_attempts_for_signature - 1)
            return None
        decision = dict(synthesis.get("decision") or {}) if isinstance(synthesis, dict) else {}
        if isinstance(synthesis, dict):
            decision["role"] = str(synthesis.get("role") or "recovery_synthesizer")
            decision["source"] = str(synthesis.get("source") or "kodawari.recovery_synthesizer")
            decision["backend"] = str(synthesis.get("backend") or "")
            decision["model"] = str(synthesis.get("model") or "")
        if (
            str(decision.get("action") or "") == "escalate_to_human"
            and not str(decision.get("backend") or "").strip()
            and "not configured" in str(decision.get("diagnosis") or "").lower()
        ):
            runtime.recovery_attempts = max(0, runtime.recovery_attempts - 1)
            runtime.recovery_attempts_for_signature = max(0, runtime.recovery_attempts_for_signature - 1)
            return None
        runtime.recovery_decisions.append(dict(decision))
        if str(decision.get("action") or "") == "expand_scope_request":
            card = build_scope_expansion_recovery_card(
                original_card=recovery_base_card,
                decision=decision,
                task_id=runtime.task_id,
                must_fix=must_fix,
                project_root=Path(self.config.project_root),
            )
            if card is not None:
                self._attach_recovery_base_workspace(card, runtime)
                runtime.pending_recovery_card = card
                write_recovery_artifacts(self._planning_dir, decision=decision, card=card)
                self.state.last_stage_status = "executor_recovery_scope_expanded"
                self._maybe_emit_hook(
                    runtime.hook_events,
                    event="executor_recovery_scope_expanded",
                    task_id=runtime.task_id,
                    task_label=runtime.task_label,
                    action=action,
                    task_scope=runtime.task_scope,
                    details={
                        "attempt": runtime.recovery_attempts,
                        "attempt_for_signature": runtime.recovery_attempts_for_signature,
                        "signature": signature,
                        "decision": decision,
                        "files_to_change": card.get("files_to_change"),
                    },
                )
                return None
        if str(decision.get("action") or "") != "narrow_patch_plan":
            message = str(decision.get("diagnosis") or decision.get("reason") or "executor recovery did not produce a narrow patch plan")
            prompt_lesson_learning = self._ingest_executor_stall_prompt_lessons(
                runtime=runtime,
                stall_report=stall_report,
                decision=decision,
            )
            write_recovery_artifacts(self._planning_dir, decision=decision, card=None)
            self.state.add_error(message, phase=Stage.IMPLEMENT.value, action=action.value, category="recovery")
            self.state.mark_completed(StopReason.STUCK, "BLOCKED")
            self.state.last_stage_status = "executor_recovery_blocked"
            round_record["stage_status"] = "blocked"
            round_record["last_error"] = message
            round_record["details"] = {"recovery": decision}
            if prompt_lesson_learning:
                round_record["details"]["prompt_lesson_learning"] = prompt_lesson_learning
            runtime.round_records.append(update_round_record_outcome(round_record, runtime.context))
            return self._finish_loop(runtime, reason="EXECUTOR_RECOVERY_BLOCKED", last_error=message, action=action)
        card = build_recovery_card(
            original_card=recovery_base_card,
            decision=decision,
            task_id=runtime.task_id,
            must_fix=must_fix,
        )
        self._attach_recovery_base_workspace(card, runtime)
        runtime.pending_recovery_card = card
        write_recovery_artifacts(self._planning_dir, decision=decision, card=card)
        self._maybe_emit_hook(
            runtime.hook_events,
            event="executor_recovery_synthesized",
            task_id=runtime.task_id,
            task_label=runtime.task_label,
            action=action,
            task_scope=runtime.task_scope,
            details={
                "attempt": runtime.recovery_attempts,
                "attempt_for_signature": runtime.recovery_attempts_for_signature,
                "signature": signature,
                "decision": decision,
            },
        )
        return None

    def _call_recovery_synthesizer_with_timeout(
        self,
        synthesizer,
        *,
        failure_event: FailureEvent,
        **kwargs: Any,
    ) -> dict[str, Any]:
        timeout_seconds = self._recovery_synthesizer_timeout_seconds()
        result_queue: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)

        def run() -> None:
            try:
                result_queue.put(("ok", synthesizer(**kwargs)))
            except BaseException as exc:  # pragma: no cover - re-raised in caller thread
                result_queue.put(("error", exc))

        worker = threading.Thread(target=run, name="workflow-recovery-synthesizer", daemon=True)
        worker.start()
        try:
            kind, payload = result_queue.get(timeout=timeout_seconds)
        except queue.Empty:
            return {
                "status": "timeout",
                "error_code": "RECOVERY_SYNTHESIZER_TIMEOUT",
                "error": f"recovery synthesizer timed out after {timeout_seconds}s",
                "failure_event": failure_event.to_dict(),
            }
        if kind == "error":
            raise payload
        return dict(payload or {}) if isinstance(payload, dict) else {"status": "blocked", "error": str(payload)}

    def _recovery_synthesizer_timeout_seconds(self) -> int:
        for key in ("WORKFLOW_RECOVERY_SYNTHESIZER_TIMEOUT_SECONDS", "WORKFLOW_RECOVERY_TIMEOUT"):
            raw = os.getenv(key)
            if raw:
                try:
                    return max(1, int(raw))
                except ValueError:
                    continue
        adapter_config = getattr(self.adapter, "config", None)
        configured = getattr(adapter_config, "recovery_timeout_seconds", None)
        try:
            return max(1, int(configured or 60))
        except (TypeError, ValueError):
            return 60

    def _should_yield_deterministic_recovery_to_synthesizer(
        self,
        *,
        deterministic_match: Any,
        runtime: _LoopRuntime,
    ) -> bool:
        """Use the LLM recovery synthesizer after an instruction-only retry repeats.

        The deterministic no-write card is useful once: it narrows the next
        executor attempt to write-first behavior without spending a model round
        on recovery synthesis. If the same signature stalls again, repeating the
        same card just replays the failure. At that point the useful artifact is
        a concrete narrow_patch_plan, so yield to the synthesizer when available.

        Disabled by default (2026-05-05): the LLM synthesizer routinely times
        out at 60s and lands the run in STUCK with no useful artifacts. Pure
        deterministic recovery + ``recovery_attempts_for_signature`` exhaustion
        gives the same final outcome (BLOCKED) without burning a model round.
        Set ``WORKFLOW_AUTOPILOT_RECOVERY_SYNTHESIZER=1`` to re-enable.
        """
        if not _recovery_synthesizer_enabled():
            return False
        if str(getattr(deterministic_match, "name", "") or "") != "no_write_stall":
            return False
        has_previous_no_write = any(
            str(item.get("role") or "") == "deterministic_recovery"
            and str(item.get("detector_name") or "") == "no_write_stall"
            for item in list(getattr(runtime, "recovery_decisions", []) or [])
            if isinstance(item, dict)
        )
        if not has_previous_no_write and int(getattr(runtime, "recovery_attempts_for_signature", 0) or 0) <= 0:
            return False
        return callable(getattr(self.adapter, "synthesize_executor_recovery", None))

    def _write_recovery_failure_snapshot(
        self,
        *,
        failure_event: FailureEvent,
        decision: dict[str, Any],
        stall_report: dict[str, Any] | None,
        execution_result: dict[str, Any],
    ) -> None:
        payload = {
            "schema_version": "execution.failure_snapshot.v1",
            "failure_event": failure_event.to_dict(),
            "decision": dict(decision),
            "stall_report": dict(stall_report or {}) if isinstance(stall_report, dict) else {},
            "execution_result": dict(execution_result),
        }
        self._planning_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_canonical_json(
            self._planning_dir / ".execution_failure_snapshot.json",
            redact_jsonable(payload),
        )

    def _should_synthesize_executor_recovery(self, runtime: _LoopRuntime) -> bool:
        if not _recovery_synthesizer_enabled():
            return False
        execution_result = runtime.execution_result if isinstance(runtime.execution_result, dict) else {}
        error_code = str(execution_result.get("error_code") or execution_result.get("reason") or "").strip().upper()
        if self._recoverable_executor_block(error_code):
            return True
        gate_check = runtime.gate_check if isinstance(runtime.gate_check, dict) else {}
        return _runtime_verify_passed(runtime) and str(gate_check.get("total_status") or "").upper() == "BLOCKED"

    def _attach_recovery_base_workspace(self, card: dict[str, Any], runtime: _LoopRuntime) -> None:
        source_root = self._executor_recovery_source_root(runtime).resolve()
        project_root = Path(self.config.project_root).resolve()
        recovery = card.setdefault("recovery", {})
        if source_root == project_root or not source_root.exists() or not source_root.is_dir():
            if isinstance(recovery, dict):
                recovery.pop("base_workspace_path", None)
            return
        if isinstance(recovery, dict):
            recovery["base_workspace_path"] = str(source_root)

    def _refresh_recovery_base_workspace_for_runtime(self, runtime: _LoopRuntime) -> None:
        if isinstance(self._task_card_payload, dict) and isinstance(self._task_card_payload.get("recovery"), dict):
            self._attach_recovery_base_workspace(self._task_card_payload, runtime)
        pending_card = getattr(runtime, "pending_recovery_card", None)
        if isinstance(pending_card, dict):
            self._attach_recovery_base_workspace(pending_card, runtime)

    @staticmethod
    def _executor_recovery_attempt_signature(
        *,
        runtime: _LoopRuntime,
        must_fix: list[str],
        error_code: str = "",
        failure_mode_tag: str = "",
        affected_paths: list[str] | None = None,
    ) -> str:
        execution_result = runtime.execution_result if isinstance(runtime.execution_result, dict) else {}
        code = str(error_code or execution_result.get("error_code") or "").strip().upper()
        if not code:
            code = "PEER_REVIEW_FIX"
        mode = str(failure_mode_tag or code).strip().lower() or "unknown"
        text_parts = EngineRecoveryMixin._normalized_must_fix_bucket(must_fix)
        for key in ("blocking_reason", "reason", "summary", "error"):
            value = execution_result.get(key)
            if value:
                text_parts.append(str(value))
        joined = "\n".join(text_parts)
        if code in {"VERIFY_FAILED", "VERIFY_FAILED_RETRYABLE"}:
            nodes = sorted(set(re.findall(r"tests[\\/][^\s,)\]]+\.py::[^\s,)\]]+", joined)))
            basis = "\n".join(nodes) if nodes else joined[:4000]
        elif code.startswith("EXECUTOR_STALLED_") or code in {"MAX_TOOL_ITERATIONS", "NO_PROGRESS_ABORTED"}:
            basis = "\n".join(
                [
                    joined[:1000],
                    json.dumps(
                        EngineRecoveryMixin._executor_recovery_scope_signature_payload(runtime),
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                ]
            )
        else:
            basis = joined[:2000]
        basis_payload = {
            "mode": mode,
            "basis": basis,
            "affected_paths": sorted(
                str(item or "").strip().replace("\\", "/")
                for item in list(affected_paths or [])
                if str(item or "").strip()
            ),
        }
        digest = hashlib.sha256(
            json.dumps(basis_payload, ensure_ascii=False, sort_keys=True).encode("utf-8", errors="replace")
        ).hexdigest()[:12]
        task_id = str(runtime.task_id or "").strip().upper() or "UNKNOWN_TASK"
        return f"{task_id}:{code}:{mode}:{digest}"

    @staticmethod
    def _normalized_must_fix_bucket(must_fix: list[str]) -> list[str]:
        bucket: list[str] = []
        seen: set[str] = set()
        for raw in list(must_fix or []):
            text = re.sub(r"\s+", " ", str(raw or "").strip().lower())
            text = re.sub(r"[.!?;:]+$", "", text)
            if not text:
                continue
            key = text[:500]
            if key in seen:
                continue
            seen.add(key)
            bucket.append(key)
        return bucket[:8]

    @staticmethod
    def _executor_recovery_scope_signature_payload(runtime: _LoopRuntime) -> dict[str, Any]:
        card = runtime.pending_recovery_card if isinstance(runtime.pending_recovery_card, dict) else {}
        if not card:
            return {"files_to_change": []}
        recovery = card.get("recovery") if isinstance(card.get("recovery"), dict) else {}
        return {
            "files_to_change": sorted(str(item) for item in list(card.get("files_to_change") or []) if str(item).strip()),
            "recovery_source_action": str(recovery.get("source_action") or ""),
            "requested_files": sorted(str(item) for item in list(recovery.get("requested_files") or []) if str(item).strip()),
            "approved_scope_files": sorted(str(item) for item in list(recovery.get("approved_scope_files") or []) if str(item).strip()),
        }

    def _executor_recovery_source_root(self, runtime: _LoopRuntime) -> Path:
        project_root = Path(self.config.project_root).resolve()
        write_workspace = self._latest_successful_executor_write_workspace()
        if write_workspace is not None:
            if self._project_root_has_newer_recovery_scope(runtime, write_workspace):
                return project_root
            return write_workspace
        execution_result = runtime.execution_result if isinstance(runtime.execution_result, dict) else {}
        scratch_root = str(execution_result.get("scratch_root") or "").strip()
        if scratch_root:
            workspace = (Path(scratch_root) / "workspace").resolve()
            if workspace.exists() and workspace.is_dir():
                if self._project_root_has_newer_recovery_scope(runtime, workspace):
                    return project_root
                return workspace
        return project_root

    def _project_root_has_newer_recovery_scope(self, runtime: _LoopRuntime, workspace: Path) -> bool:
        project_root = Path(self.config.project_root).resolve()
        try:
            workspace_root = workspace.resolve()
        except OSError:
            return False
        for rel in self._recovery_scope_files(runtime):
            root_path = (project_root / rel).resolve()
            workspace_path = (workspace_root / rel).resolve()
            if not self._path_is_under(root_path, project_root) or not self._path_is_under(workspace_path, workspace_root):
                continue
            root_state = self._file_state(root_path)
            workspace_state = self._file_state(workspace_path)
            if root_state[0] is None or root_state[0] == workspace_state[0]:
                continue
            if workspace_state[0] is None or root_state[1] > workspace_state[1]:
                return True
        return False

    def _recovery_scope_files(self, runtime: _LoopRuntime) -> list[str]:
        cards = [
            getattr(runtime, "pending_recovery_card", None),
            self._task_card_payload,
        ]
        out: list[str] = []
        for card in cards:
            if not isinstance(card, dict):
                continue
            for key in ("files_to_change", "new_files"):
                for raw in list(card.get(key) or []):
                    text = str(raw or "").strip().replace("\\", "/")
                    if text and text not in out:
                        out.append(text)
        return out

    @staticmethod
    def _path_is_under(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    @staticmethod
    def _file_state(path: Path) -> tuple[str | None, int]:
        try:
            if not path.exists() or not path.is_file():
                return None, 0
            data = path.read_bytes()
            return hashlib.sha256(data).hexdigest(), path.stat().st_mtime_ns
        except OSError:
            return None, 0

    def _latest_successful_executor_write_workspace(self) -> Path | None:
        tool_log_path = self._planning_dir / ".execution_tool_calls.jsonl"
        try:
            rows, _bad_lines, _quarantine = load_jsonl_rows(tool_log_path)
            scratch_root = (self.config.project_root / ".workflow" / ".executor_scratch").resolve()
        except Exception:
            return None
        for row in reversed(rows):
            if not isinstance(row, dict):
                continue
            tool = str(row.get("tool") or "").strip()
            if tool not in _SUCCESSFUL_RECOVERY_BASE_WRITE_TOOLS:
                continue
            if str(row.get("error_code") or "").strip():
                continue
            result = row.get("result")
            if not isinstance(result, dict) or not bool(result.get("ok")):
                continue
            run_id = str(row.get("run_id") or "").strip()
            if not run_id:
                continue
            try:
                workspace = (scratch_root / run_id / "workspace").resolve()
                workspace.relative_to(scratch_root)
            except (OSError, ValueError):
                continue
            if workspace.name == "workspace" and workspace.exists() and workspace.is_dir():
                return workspace
        return None

    def _rollback_target_files(self, runtime: _LoopRuntime) -> list[str]:
        """Collect the files that should be snapshotted before implement."""
        targets: set[str] = set()
        card = self._task_card_payload
        if isinstance(card, dict):
            targets.update(str(f) for f in card.get("files_to_change") or [])
            targets.update(str(f) for f in card.get("core_files") or [])
        if runtime.last_changed_files:
            targets.update(runtime.last_changed_files)
        return sorted(targets)

    def _executor_recovery_total_attempt_cap(self) -> int:
        raw = os.getenv("WORKFLOW_RECOVERY_MAX_TOTAL_ATTEMPTS")
        if raw:
            try:
                return max(1, int(raw))
            except ValueError:
                pass
        card = self._task_card_payload if isinstance(self._task_card_payload, dict) else {}
        card_caps = card.get("runtime_caps") if isinstance(card.get("runtime_caps"), dict) else {}
        if isinstance(card_caps, dict) and "max_total_recovery_attempts" in card_caps:
            try:
                return max(1, int(card_caps.get("max_total_recovery_attempts") or 8))
            except (TypeError, ValueError):
                pass
        return 8

    def _executor_recovery_attempt_limit(self) -> int:
        card = self._task_card_payload if isinstance(self._task_card_payload, dict) else {}
        cap_sources: list[dict[str, Any]] = []
        card_caps = card.get("runtime_caps") if isinstance(card.get("runtime_caps"), dict) else {}
        if isinstance(card_caps, dict):
            cap_sources.append(card_caps)
        adapter_config = getattr(self.adapter, "config", None)
        adapter_caps = getattr(adapter_config, "executor_runtime_caps", None)
        if isinstance(adapter_caps, dict):
            cap_sources.append(adapter_caps)
        for runtime_caps in cap_sources:
            try:
                if "max_recovery_attempts" in runtime_caps:
                    return max(1, int(runtime_caps.get("max_recovery_attempts") or 2))
            except (TypeError, ValueError):
                return 2
        return 2

    def _load_executor_stall_report(self) -> dict[str, Any] | None:
        path = self._planning_dir / ".execution_stall_report.json"
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return dict(payload) if isinstance(payload, dict) else None


__all__ = ["EngineRecoveryMixin"]
