"""Implementation and verification helpers for the autopilot engine."""
from __future__ import annotations
import hashlib
import os
import re
import time
from pathlib import Path
from typing import Any, Iterable

class PolicyHashViolation(RuntimeError):
    """Raised when Codex modifies a protected policy file detected via content hash."""

from kodawari.autopilot.core.collaboration import (
    CollaborationAction,
    CollaborationRole,
    record_codex_self_review_result,
    record_opus_review,
    request_executor_recovery,
    update_round_record_outcome,
)
from kodawari.autopilot.execution.execution_artifacts import (
    EXECUTION_RESULT_FILENAME,
    build_execution_result,
    write_execution_result,
)
from kodawari.autopilot.engine.engine_support import (
    _LoopRuntime,
    check_protected_files,
    looks_like_setup_error,
    snapshot_dirty_files,
)
from kodawari.autopilot.engine.gate_round import run_rules_gate_round, run_verify_round
from kodawari.autopilot.core.permission_policy import find_blocked_writes
from kodawari.autopilot.execution.implementation_runtime import (
    apply_codex_success_transition,
    attach_runtime_instinct_hints,
    codex_stage_status,
    implementation_round_details,
    post_implement_success_details,
)
from kodawari.autopilot.core.prompt_profiles import model_family
from kodawari.autopilot.core.runtime_checks import build_verify_check
from kodawari.autopilot.core.phase_guard import (
    guard_pre_implement,
    normalize_contract_mode,
    scope_guard,
)
from kodawari.autopilot.verify.failure_analyzer import classify_failure, parse_pytest_failures
from kodawari.autopilot.review.review_bridge import (
    normalize_self_review_payload,
    run_codex_self_review,
    run_post_execution_qa,
)
from kodawari.autopilot.core.state import Stage, StopReason
from kodawari.gate.checkers import check_scope_drift

def _merge_changed_files(*groups: Iterable[Any]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for item in group or []:
            normalized = str(item or "").strip().replace("\\", "/")
            while normalized.startswith("./"):
                normalized = normalized[2:]
            if not normalized:
                continue
            key = normalized.lower()
            if key in seen:
                continue
            seen.add(key)
            merged.append(normalized)
    return merged

class EngineImplementationMixin:
    def _recover_verify_setup_error(self, error_message: str) -> dict[str, Any]:
        self.state.verify_setup_recovery_attempted += 1
        self.state.verify_setup_recovery_last_error = error_message

        recovered = self.state.verify_setup_recovery_attempted <= self.config.verify_setup_recovery_max_attempts
        if recovered:
            self.state.verify_setup_recovery_succeeded += 1
            self._maybe_wait_before_retry()

        return {
            "attempted": True,
            "recovered": recovered,
            "attempts_used": self.state.verify_setup_recovery_attempted,
            "max_attempts": self.config.verify_setup_recovery_max_attempts,
            "cleanup_strategy": self.config.verify_setup_cleanup_strategy,
            "fallback_strategy": self.config.verify_setup_recovery_fallback_strategy,
            "retry_interval_seconds": self.config.verify_setup_recovery_retry_interval_seconds,
        }

    def handle_verify_failure(self, task_id: str, error_message: str) -> dict[str, Any]:
        """Compatibility wrapper for legacy tests/callers."""
        del task_id
        recovery = self._recover_verify_setup_error(error_message)
        return {
            "is_setup_error": True,
            "should_retry": recovery["recovered"],
            "retry_count": recovery["attempts_used"],
            **recovery,
        }

    def _maybe_wait_before_retry(self) -> None:
        delay = max(0, int(self.config.verify_setup_recovery_retry_interval_seconds))
        if delay:
            time.sleep(0)

    def _check_protected_files(
        self,
        changed_files: list[str],
        *,
        task_label: str = "",
        task_scope: str = "",
    ) -> dict[str, Any]:
        return check_protected_files(
            changed_files,
            task_label=task_label,
            task_scope=task_scope,
            protected_files_check_enabled=self.config.protected_files_check_enabled,
            protected_files=self.config.protected_files,
            protected_files_critical=self.config.protected_files_critical,
            protected_files_warning=self.config.protected_files_warning,
        )

    def _looks_like_setup_error(self, message: str) -> bool:
        return looks_like_setup_error(message)

    def _contract_first_mode(self) -> str:
        return normalize_contract_mode(getattr(self.config, "contract_first_mode", "off"))

    def _phase_guard_pre_implement(self) -> dict[str, Any]:
        return guard_pre_implement(
            contract_first_mode=self._contract_first_mode(),
            phase_mode=str(getattr(self.config, "phase_mode", "implement")),
            task_card_path=getattr(self.config, "task_card_path", None),
            task_card_payload=self._task_card_payload,
        )

    def _build_implementation_request(
        self,
        runtime: _LoopRuntime,
        action: CollaborationAction,
    ) -> dict[str, Any]:
        self.state.current_stage = Stage.IMPLEMENT
        runtime.context.implementation_started = True
        self._refresh_recovery_base_workspace_for_runtime(runtime)
        impl_context = self._build_implementation_context(runtime.task_label, runtime.task_scope)
        impl_context["attempt"] = runtime.context.review_feedback.review_iteration + 1
        impl_context["review_round"] = runtime.context.review_feedback.review_iteration
        impl_context["requested_action"] = action.value
        impl_context["must_fix"] = list(runtime.context.review_feedback.must_fix)
        impl_context["architecture_decision_ids"] = [
            item.decision_id for item in runtime.context.architecture_decisions
        ]
        if runtime.pending_recovery_card:
            recovery_card = dict(runtime.pending_recovery_card)
            # When the deterministic no_write_stall recovery activated this
            # card, force the retry into action-only mode so the executor
            # cannot fall back into another read loop. Tool schemas are
            # rebuilt every iteration in execution_openai_tool_use, so
            # flipping the flag here causes read tools to drop from the
            # tool list on the very next chat turn. The flag is embedded
            # in task_card because build_execution_request preserves the
            # task_card dict verbatim while filtering most other context
            # fields. Stall recovery is the only path that emits this
            # detector_name, so codex/claude/gpt paths are unaffected.
            detector_name = str(
                (recovery_card.get("detector_evidence") or {}).get("detector_name")
                or recovery_card.get("detector_name")
                or ""
            ).strip()
            if detector_name == "no_write_stall":
                recovery_card["action_only_on_start"] = True
                recovery_card["action_only_reason"] = "no_write_stall_recovery_retry"
            impl_context["task_card"] = recovery_card
            impl_context["task_card_files"] = [
                str(item) for item in list(runtime.pending_recovery_card.get("files_to_change") or []) if str(item).strip()
            ]
            impl_context["recovery_card"] = dict(runtime.pending_recovery_card)
        attach_runtime_instinct_hints(impl_context, pre_compact_payload=runtime.pre_compact_payload)
        return impl_context

    def _run_codex_round(
        self,
        *,
        runtime: _LoopRuntime,
        action: CollaborationAction,
        round_record: dict[str, Any],
    ) -> dict[str, Any] | None:
        phase_guard = self._phase_guard_pre_implement()
        pre_details: dict[str, Any] = {"must_fix": list(runtime.context.review_feedback.must_fix)}
        if phase_guard.get("warnings"):
            pre_details["phase_guard_warnings"] = list(phase_guard.get("warnings") or [])
        if bool(phase_guard.get("blocked")):
            pre_details["phase_guard"] = dict(phase_guard)
            self._maybe_emit_hook(
                runtime.hook_events,
                event="pre_implement",
                task_id=runtime.task_id,
                task_label=runtime.task_label,
                action=action,
                task_scope=runtime.task_scope,
                details=pre_details,
            )
            return self._handle_phase_guard_block(
                runtime=runtime,
                action=action,
                round_record=round_record,
                phase_guard=phase_guard,
            )
        self._maybe_emit_hook(
            runtime.hook_events,
            event="pre_implement",
            task_id=runtime.task_id,
            task_label=runtime.task_label,
            action=action,
            task_scope=runtime.task_scope,
            details=pre_details,
        )
        pre_dirty = snapshot_dirty_files(Path(self.config.project_root), planning_dir=self._planning_dir)
        pre_policy_hashes = self._hash_protected_policy_files()
        recovery_block = self._maybe_prepare_executor_recovery(
            runtime=runtime,
            action=action,
            round_record=round_record,
        )
        if recovery_block is not None:
            return recovery_block
        impl_context = self._build_implementation_request(runtime, action)
        if self.config.rollback_on_failure:
            from kodawari.autopilot.execution.rollback import RollbackCheckpoint
            target_files = self._rollback_target_files(runtime)
            runtime.rollback_checkpoint = RollbackCheckpoint.capture(
                project_root=Path(self.config.project_root),
                target_files=target_files,
                cycle=self.state.cycle,
            )
        result = self.adapter.implement(task=runtime.task_label, context=impl_context)
        status = str(result.get("status") or "error").lower()
        changed_files = [str(item) for item in result.get("changes", [])]
        if status == "blocked":
            return self._handle_execution_backend_block(
                runtime=runtime,
                action=action,
                round_record=round_record,
                result=result,
                impl_context=impl_context,
            )
        if status != "done":
            return self._handle_codex_error(
                runtime=runtime,
                action=action,
                round_record=round_record,
                result=result,
            )
        # Layer 2: git-diff unreported changes — add newly-dirty files to changed_files
        post_dirty = snapshot_dirty_files(Path(self.config.project_root), planning_dir=self._planning_dir)
        unreported = post_dirty - pre_dirty
        if unreported:
            changed_files = sorted(set(changed_files) | unreported)
        # Layer 3: content-hash comparison catches pre-dirty policy files that
        # were modified again during implement (git diff misses these).
        post_policy_hashes = self._hash_protected_policy_files()
        policy_violations = self._detect_policy_hash_violations(pre_policy_hashes, post_policy_hashes)
        if policy_violations:
            # Hard block — bypass is_authorized_to_modify() entirely.
            # A hash mismatch on policy files is never an authorized change
            # regardless of what the task label/scope says.
            violated_paths = [v.split(" (")[0] for v in policy_violations]
            return self._handle_protected_file_block(
                runtime=runtime,
                action=action,
                round_record=round_record,
                protection={"blocked": True, "critical": violated_paths, "warning": []},
            )
        protection = self._check_protected_files(
            changed_files,
            task_label=runtime.task_label,
            task_scope=runtime.task_scope or "",
        )
        if protection["blocked"]:
            return self._handle_protected_file_block(
                runtime=runtime,
                action=action,
                round_record=round_record,
                protection=protection,
            )
        # Phase E (defense-in-depth): permission policy BLOCK-tier check on
        # changed_files. Isolation mode already filters via
        # execution_isolation.sync_isolated_workspace_to_project_root; this
        # covers the non-isolation path where the executor writes directly
        # into project_root. Planning validation also checks `files_to_change`
        # ahead of time, but execution may drift; this is the runtime gate.
        permission_blocks = find_blocked_writes(changed_files)
        if permission_blocks:
            return self._handle_protected_file_block(
                runtime=runtime,
                action=action,
                round_record=round_record,
                protection={
                    "blocked": True,
                    "critical": [entry["path"] for entry in permission_blocks],
                    "warning": [],
                    "permission_policy_reasons": permission_blocks,
                },
            )
        strict_scope_check = scope_guard(
            changed_files=changed_files,
            task_card=self._task_card_payload,
            strict_scope=bool(getattr(self.config, "strict_scope", False)),
            contract_mode=self._contract_first_mode(),
        ).to_dict()
        if bool(strict_scope_check.get("blocked")):
            return self._handle_phase_guard_block(
                runtime=runtime,
                action=action,
                round_record=round_record,
                phase_guard={
                    "blocked": True,
                    "status": "FAIL",
                    "reason": str(strict_scope_check.get("reason") or "scope drift blocked"),
                    "details": {"message": str(strict_scope_check.get("reason") or "scope drift blocked"), "scope_guard": strict_scope_check},
                    "warnings": list(strict_scope_check.get("warnings") or []),
                },
            )
        self._record_codex_success(
            runtime=runtime,
            action=action,
            round_record=round_record,
            result=result,
            changed_files=changed_files,
            protection=protection,
            impl_context=impl_context,
        )
        return None

    def _ingest_execution_backend_prompt_lessons(
        self,
        *,
        runtime: _LoopRuntime,
        result: dict[str, Any],
        backend_error_code: str,
    ) -> dict[str, Any]:
        normalized_code = str(backend_error_code or "").strip().upper()
        outcomes: list[dict[str, Any]] = []
        if normalized_code in {"VERIFY_FAILED", "VERIFY_FAILED_RETRYABLE"}:
            analysis = self._execution_verify_failure_analysis(result)
            if analysis:
                outcome = self._ingest_verify_failure_prompt_lessons(runtime=runtime, analysis=analysis)
                outcome["source"] = "execution_backend_verify_failure"
                outcomes.append(outcome)
        stall_report = self._execution_stall_report(result)
        if stall_report:
            outcome = self._ingest_executor_stall_prompt_lessons(
                runtime=runtime,
                stall_report=stall_report,
                decision=None,
            )
            outcome["source"] = "execution_backend_stall_report"
            outcomes.append(outcome)
        return self._combine_prompt_lesson_outcomes(outcomes)

    def _execution_verify_failure_analysis(self, result: dict[str, Any]) -> list[dict[str, Any]]:
        execution_result = result.get("execution_result")
        candidates: list[Any] = []
        if isinstance(execution_result, dict):
            candidates.extend(
                [
                    execution_result.get("verify_summary"),
                    execution_result.get("verify_check"),
                ]
            )
        candidates.extend([result.get("verify_summary"), result.get("verify_check")])
        stdout = ""
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            stdout = str(candidate.get("stdout_excerpt") or candidate.get("stdout") or "").strip()
            if stdout:
                break
        if not stdout:
            return []
        allowed_mutations = []
        if isinstance(self._task_card_payload, dict):
            allowed_mutations = [dict(item) for item in list(self._task_card_payload.get("allowed_test_mutations") or []) if isinstance(item, dict)]
        analysis: list[dict[str, Any]] = []
        for failure in parse_pytest_failures(stdout):
            row = classify_failure(failure, allowed_mutations).to_dict()
            row["failure"] = failure.to_dict()
            analysis.append(row)
        return analysis

    def _execution_stall_report(self, result: dict[str, Any]) -> dict[str, Any] | None:
        execution_result = result.get("execution_result")
        for candidate in (
            result.get("stall_report"),
            execution_result.get("stall_report") if isinstance(execution_result, dict) else None,
            self._load_executor_stall_report(),
        ):
            if isinstance(candidate, dict):
                return dict(candidate)
        return None

    def _ingest_verify_failure_prompt_lessons(self, *, runtime: _LoopRuntime, analysis: list[dict[str, Any]]) -> dict[str, Any]:
        try:
            from kodawari.instincts import ingest_verify_failure_prompt_lessons
        except Exception:
            return {"processed": 0, "promoted": 0, "reason": "prompt_lesson_module_unavailable"}
        try:
            return ingest_verify_failure_prompt_lessons(
                Path(self.config.project_root),
                analysis,
                executor_family=self._executor_prompt_lesson_family(),
                run_id=self._prompt_lesson_run_id(runtime),
            )
        except Exception:
            return {"processed": 0, "promoted": 0, "reason": "prompt_lesson_ingest_failed"}

    def _ingest_executor_stall_prompt_lessons(
        self,
        *,
        runtime: _LoopRuntime,
        stall_report: dict[str, Any] | None,
        decision: dict[str, Any] | None,
    ) -> dict[str, Any]:
        try:
            from kodawari.instincts import ingest_executor_stall_prompt_lessons
        except Exception:
            return {"processed": 0, "promoted": 0, "reason": "prompt_lesson_module_unavailable"}
        try:
            return ingest_executor_stall_prompt_lessons(
                Path(self.config.project_root),
                stall_report,
                executor_family=self._executor_prompt_lesson_family(),
                run_id=self._prompt_lesson_run_id(runtime),
                recovery_decision=decision,
            )
        except Exception:
            return {"processed": 0, "promoted": 0, "reason": "prompt_lesson_ingest_failed"}

    def _ingest_successful_deterministic_recovery_prompt_lessons(self, runtime: _LoopRuntime) -> dict[str, Any]:
        decisions = [
            dict(item)
            for item in list(runtime.recovery_decisions or [])
            if isinstance(item, dict) and str(item.get("role") or "") == "deterministic_recovery"
        ]
        if not decisions:
            return {}
        try:
            from kodawari.instincts import ingest_deterministic_recovery_prompt_lessons
        except Exception:
            return {"processed": 0, "promoted": 0, "reason": "prompt_lesson_module_unavailable"}
        try:
            return ingest_deterministic_recovery_prompt_lessons(
                Path(self.config.project_root),
                decisions,
                executor_family=self._executor_prompt_lesson_family(),
                run_id=self._prompt_lesson_run_id(runtime),
            )
        except Exception:
            return {"processed": 0, "promoted": 0, "reason": "prompt_lesson_ingest_failed"}

    def _executor_prompt_lesson_family(self) -> str:
        return model_family(
            model=str(getattr(self.config, "executor_model", "") or ""),
            driver=str(getattr(self.config, "executor_backend", "") or ""),
        )

    def _prompt_lesson_run_id(self, runtime: _LoopRuntime) -> str:
        state_run_id = str(getattr(self.state, "run_id", "") or "").strip()
        if state_run_id:
            return state_run_id
        return ":".join(
            part
            for part in (str(self.config.feature or "").strip(), str(runtime.task_id or "").strip())
            if part
        )

    @staticmethod
    def _combine_prompt_lesson_outcomes(outcomes: list[dict[str, Any]]) -> dict[str, Any]:
        useful = [dict(item) for item in outcomes if isinstance(item, dict) and item]
        if not useful:
            return {}
        return {
            "processed": sum(int(item.get("processed") or 0) for item in useful),
            "promoted": sum(int(item.get("promoted") or 0) for item in useful),
            "events": useful,
        }

    def _handle_execution_backend_block(
        self,
        *,
        runtime: _LoopRuntime,
        action: CollaborationAction,
        round_record: dict[str, Any],
        result: dict[str, Any],
        impl_context: dict[str, Any],
    ) -> dict[str, Any]:
        blocking_reason = str(result.get("blocking_reason") or result.get("reason") or "EXECUTION_BACKEND_BLOCKED")
        runtime.execution_result = (
            dict(result.get("execution_result") or {})
            if isinstance(result.get("execution_result"), dict)
            else None
        )
        runtime.execution_artifacts = (
            dict(result.get("execution_artifacts") or {})
            if isinstance(result.get("execution_artifacts"), dict)
            else None
        )
        backend_error_code = self._execution_backend_error_code(result)
        backend_name = str(result.get("backend") or "").strip()
        if self._maybe_accept_no_write_resume(
            runtime=runtime,
            action=action,
            round_record=round_record,
            result=result,
            backend_error_code=backend_error_code,
            impl_context=impl_context,
        ):
            return None
        prompt_lesson_learning = self._ingest_execution_backend_prompt_lessons(
            runtime=runtime,
            result=result,
            backend_error_code=backend_error_code,
        )
        if self._recoverable_executor_block(backend_error_code):
            # Internal recovery — route to FIX_ROUND without consuming reviewer round budget.
            # Using record_opus_review here would increment review_iteration (boundary.py:80) and
            # exhaust _round_limit_reached before the real external reviewer gets a second turn.
            request_executor_recovery(
                runtime.context,
                blocking_reason=blocking_reason,
                summary=f"Executor backend blocked with {backend_error_code}; synthesize a narrow recovery patch plan before retry.",
                blocking_items=[blocking_reason],
                source="executor_stall_recovery",
            )
            self.state.add_error(
                blocking_reason,
                phase=Stage.IMPLEMENT.value,
                action=action.value,
                category="recovery",
                error_code=backend_error_code,
                metadata={"backend": backend_name} if backend_name else None,
            )
            self.state.last_stage_status = "executor_recovery_requested"
            round_record["stage_status"] = "needs_recovery"
            round_record["last_error"] = blocking_reason
            round_record["details"] = {"execution_backend": dict(result), "recovery": {"requested": True, "error_code": backend_error_code}}
            if prompt_lesson_learning:
                round_record["details"]["prompt_lesson_learning"] = prompt_lesson_learning
            if runtime.execution_result:
                round_record["details"]["execution_result"] = dict(runtime.execution_result)
            if runtime.execution_artifacts:
                round_record["details"]["execution_artifacts"] = dict(runtime.execution_artifacts)
            runtime.round_records.append(update_round_record_outcome(round_record, runtime.context))
            return None
        self.state.add_error(
            blocking_reason,
            phase=Stage.IMPLEMENT.value,
            action=action.value,
            category="implement",
            error_code=backend_error_code,
            metadata={"backend": backend_name} if backend_name else None,
        )
        self.state.mark_completed(StopReason.HARD_ERROR, "BLOCKED")
        self.state.last_stage_status = "execution_backend_blocked"
        round_record["stage_status"] = "blocked"
        round_record["last_error"] = blocking_reason
        round_record["details"] = {"execution_backend": dict(result)}
        if runtime.execution_result:
            round_record["details"]["execution_result"] = dict(runtime.execution_result)
        if runtime.execution_artifacts:
            round_record["details"]["execution_artifacts"] = dict(runtime.execution_artifacts)
        runtime.round_records.append(update_round_record_outcome(round_record, runtime.context))
        return self._finish_loop(
            runtime,
            reason=str(result.get("reason") or "EXECUTION_BACKEND_BLOCKED"),
            last_error=blocking_reason,
            action=action,
        )

    def _maybe_accept_no_write_resume(
        self,
        *,
        runtime: _LoopRuntime,
        action: CollaborationAction,
        round_record: dict[str, Any],
        result: dict[str, Any],
        backend_error_code: str,
        impl_context: dict[str, Any],
    ) -> bool:
        if str(backend_error_code or "").strip().upper() != "EXECUTOR_STALLED_NO_WRITE_PROGRESS":
            return False
        if runtime.context.review_feedback.must_fix:
            return False
        changed_files = self._existing_resume_task_files()
        if not changed_files:
            return False
        protection = self._check_protected_files(
            changed_files,
            task_label=runtime.task_label,
            task_scope=runtime.task_scope or "",
        )
        if protection["blocked"] or find_blocked_writes(changed_files):
            return False
        strict_scope_check = scope_guard(
            changed_files=changed_files,
            task_card=self._task_card_payload,
            strict_scope=bool(getattr(self.config, "strict_scope", False)),
            contract_mode=self._contract_first_mode(),
        ).to_dict()
        if bool(strict_scope_check.get("blocked")):
            return False
        verify_check = build_verify_check(
            project_root=Path(self.config.project_root),
            feature=self.config.feature,
            task_label=runtime.task_label,
            verify_cmd=self.config.verify_cmd,
            changed_files=changed_files,
            qa_payload={},
            instinct_hints=self._runtime_instinct_hints(runtime),
        )
        if not bool(verify_check.get("passed")):
            return False
        planning_dir = self._planning_dir
        planning_dir.mkdir(parents=True, exist_ok=True)
        execution_result_path = planning_dir / EXECUTION_RESULT_FILENAME
        backend_name = str(
            result.get("backend")
            or result.get("execution_backend")
            or result.get("mode")
            or "openai_tool_use"
        )
        execution_result = build_execution_result(
            feature=self.config.feature,
            task=runtime.task_label,
            backend=backend_name,
            status="PASS",
            changed_files=list(changed_files),
            artifacts=list(changed_files),
            summary="accepted existing task-scope artifacts after no-write executor stall because scoped verify passed",
            implementer_note={
                "resume_verify_only": True,
                "backend_error_code": backend_error_code,
            },
        )
        write_execution_result(execution_result_path, execution_result)
        artifacts = {EXECUTION_RESULT_FILENAME: str(execution_result_path.resolve())}
        runtime.verify_check = verify_check
        resume_result = {
            "status": "done",
            "mode": "resume_verify_only",
            "backend": str(result.get("backend") or ""),
            "changes": list(changed_files),
            "execution_result": execution_result,
            "execution_artifacts": artifacts,
        }
        self._record_codex_success(
            runtime=runtime,
            action=action,
            round_record=round_record,
            result=resume_result,
            changed_files=changed_files,
            protection=protection,
            impl_context=impl_context,
        )
        details = round_record.setdefault("details", {})
        details["resume_verify_only"] = {
            "accepted": True,
            "source_error_code": backend_error_code,
            "verify_status": str(verify_check.get("status") or ""),
            "verify_cmd_resolved": str(verify_check.get("verify_cmd_resolved") or ""),
        }
        return True

    def _existing_resume_task_files(self) -> list[str]:
        if not isinstance(self._task_card_payload, dict):
            return []
        root = Path(self.config.project_root).resolve()
        declared = _merge_changed_files(
            self._task_card_payload.get("files_to_change"),
            self._task_card_payload.get("new_files"),
        )
        new_files = _merge_changed_files(self._task_card_payload.get("new_files"))
        if not declared or not new_files:
            return []
        existing: list[str] = []
        for rel_path in declared:
            try:
                candidate = (root / rel_path).resolve()
                candidate.relative_to(root)
            except ValueError:
                continue
            if candidate.is_file():
                existing.append(rel_path)
        existing_keys = {item.lower() for item in existing}
        if any(item.lower() not in existing_keys for item in new_files):
            return []
        return existing

    @staticmethod
    def _runtime_instinct_hints(runtime: _LoopRuntime) -> list[dict[str, Any]]:
        payload = runtime.pre_compact_payload if isinstance(runtime.pre_compact_payload, dict) else {}
        hints = payload.get("instinct_hints")
        if not isinstance(hints, list):
            return []
        return [dict(item) for item in hints if isinstance(item, dict)]

    @staticmethod
    def _execution_backend_error_code(result: dict[str, Any]) -> str:
        execution_result = result.get("execution_result")
        if isinstance(execution_result, dict):
            code = str(execution_result.get("error_code") or "").strip()
            if code:
                return code
        return str(result.get("error_code") or result.get("reason") or "").strip()

    @staticmethod
    def _recoverable_executor_block(error_code: str) -> bool:
        normalized = str(error_code or "").strip().upper()
        return normalized in {
            "EXECUTOR_STALLED_BUDGET_PRESSURE",
            "EXECUTOR_STALLED_CONTEXT_OVERFLOW",
            "EXECUTOR_STALLED_FRAGMENTED_READS",
            "EXECUTOR_STALLED_NO_WRITE_PROGRESS",
            "EXECUTOR_STALLED_PATCH_FAILURES",
            "EXECUTOR_STALLED_PATCH_PLAN_REQUIRED",
            "EXECUTOR_STALLED_REDUNDANT_READS",
            "EXECUTOR_STALLED_REPEATED_SEARCH",
            "MAX_TOOL_ITERATIONS",
            "MAX_SAME_TOOL_CALLS_PER_PATH",
            "MAX_TOOL_CALLS_PER_RESPONSE",
            "MAX_TOKEN_BUDGET",
            "NO_PROGRESS_ABORTED",
            "PATCH_OCCURRENCE_MISMATCH",
            "PATCH_PLAN_APPLY_FAILED",
            "PATCH_PLAN_PARTIAL_VERIFY_FAILED",
            "PATCH_PRECONDITION_MISMATCH",
            "PATCH_TARGET_MISSING",
            "VERIFY_FAILED",
            "VERIFY_FAILED_RETRYABLE",
            "WRITE_NEW_FILE_EXISTS",
        }

    def _handle_codex_error(
        self,
        *,
        runtime: _LoopRuntime,
        action: CollaborationAction,
        round_record: dict[str, Any],
        result: dict[str, Any],
    ) -> dict[str, Any] | None:
        error_message = str(result.get("error") or "implementation failed")
        setup_error = self._looks_like_setup_error(error_message)
        recovery: dict[str, Any] | None = None
        if setup_error:
            recovery = self._recover_verify_setup_error(error_message)
        backend_error_code = str(result.get("error_code") or "").strip()
        backend_name = str(result.get("backend") or "").strip()
        backend_returncode = result.get("returncode")
        error_metadata: dict[str, Any] = {}
        if backend_name:
            error_metadata["backend"] = backend_name
        if isinstance(backend_returncode, int):
            error_metadata["returncode"] = backend_returncode
        self.state.add_error(
            error_message,
            phase=Stage.IMPLEMENT.value,
            action=action.value,
            category="setup" if setup_error else "implement",
            recovery_attempted=bool(recovery),
            recovery_succeeded=bool(recovery and recovery.get("recovered")),
            error_code=backend_error_code,
            metadata=error_metadata or None,
        )
        self.state.last_stage_status = "error"
        round_record["stage_status"] = "error"
        round_record["last_error"] = error_message
        if recovery is not None:
            round_record["details"] = {"recovery": recovery}
            if recovery["recovered"]:
                # Internal recovery — same reason as the stall path above: don't burn
                # reviewer round budget on a setup-retry signal.
                request_executor_recovery(
                    runtime.context,
                    blocking_reason="Retry after setup recovery",
                    summary="Recover verify setup issue before retry",
                    source="setup_recovery",
                )
                runtime.round_records.append(update_round_record_outcome(round_record, runtime.context))
                self._maybe_emit_hook(
                    runtime.hook_events,
                    event="post_implement",
                    task_id=runtime.task_id,
                    task_label=runtime.task_label,
                    action=action,
                    task_scope=runtime.task_scope,
                    details={"recovery": recovery, "status": "recovered"},
                )
                return None
        runtime.round_records.append(update_round_record_outcome(round_record, runtime.context))
        self._maybe_emit_hook(
            runtime.hook_events,
            event="post_implement",
            task_id=runtime.task_id,
            task_label=runtime.task_label,
            action=action,
            task_scope=runtime.task_scope,
            details={"status": "error", "error": error_message},
        )
        return self._finish_loop(
            runtime,
            reason="IMPLEMENTATION_ERROR",
            last_error=error_message,
            action=action,
        )

    def _handle_protected_file_block(
        self,
        *,
        runtime: _LoopRuntime,
        action: CollaborationAction,
        round_record: dict[str, Any],
        protection: dict[str, Any],
    ) -> dict[str, Any]:
        # When the block came from the Phase E(a) permission-policy check,
        # surface the specific rule that matched (path_glob + reason) so the
        # human reviewing the blocking_reason knows which policy line to
        # inspect. For the plain protected_files case, fall back to the
        # legacy comma-joined list.
        permission_reasons = list(protection.get("permission_policy_reasons") or [])
        if permission_reasons:
            formatted = ", ".join(
                f"{entry.get('path','')} "
                f"(matches {entry.get('rule_path_glob','?')}: {entry.get('reason','?')})"
                for entry in permission_reasons
                if isinstance(entry, dict)
            )
            error_message = f"Blocked by permission policy: {formatted}"
        else:
            error_message = f"Blocked protected files: {', '.join(protection['critical'])}"
        self.state.add_error(
            error_message,
            phase=Stage.IMPLEMENT.value,
            action=action.value,
            category="implement",
        )
        self.state.last_stage_status = "blocked"
        round_record["stage_status"] = "blocked"
        round_record["last_error"] = error_message
        round_record["details"] = {"protection": protection}
        runtime.round_records.append(update_round_record_outcome(round_record, runtime.context))
        self._maybe_emit_hook(
            runtime.hook_events,
            event="post_implement",
            task_id=runtime.task_id,
            task_label=runtime.task_label,
            action=action,
            task_scope=runtime.task_scope,
            details={"status": "blocked", "protection": protection},
        )
        return self._finish_loop(
            runtime,
            reason="PROTECTED_FILE_BLOCK",
            last_error=error_message,
            action=action,
        )

    def _handle_phase_guard_block(
        self,
        *,
        runtime: _LoopRuntime,
        action: CollaborationAction,
        round_record: dict[str, Any],
        phase_guard: dict[str, Any],
    ) -> dict[str, Any]:
        error_message = str(phase_guard.get("details", {}).get("message") or phase_guard.get("reason") or "PHASE_GUARD_BLOCKED")
        self.state.add_error(
            error_message,
            phase=Stage.IMPLEMENT.value,
            action=action.value,
            category="implement",
        )
        self.state.last_stage_status = "blocked"
        round_record["stage_status"] = "blocked"
        round_record["last_error"] = error_message
        round_record["details"] = {"phase_guard": dict(phase_guard)}
        runtime.round_records.append(update_round_record_outcome(round_record, runtime.context))
        self._maybe_emit_hook(
            runtime.hook_events,
            event="post_implement",
            task_id=runtime.task_id,
            task_label=runtime.task_label,
            action=action,
            task_scope=runtime.task_scope,
            details={"status": "blocked", "phase_guard": dict(phase_guard)},
        )
        return self._finish_loop(
            runtime,
            reason="PHASE_GUARD_BLOCKED",
            last_error=error_message,
            action=action,
        )

    def _record_codex_success(
        self,
        *,
        runtime: _LoopRuntime,
        action: CollaborationAction,
        round_record: dict[str, Any],
        result: dict[str, Any],
        changed_files: list[str],
        protection: dict[str, Any],
        impl_context: dict[str, Any],
    ) -> None:
        self.state.changed_files.update(changed_files)
        self.state.tokens_used += int(result.get("tokens_used", 0) or 0)
        self.state.last_stage_status = codex_stage_status(action)
        runtime.last_changed_files = _merge_changed_files(runtime.last_changed_files, changed_files)
        allowed_files = self._task_card_files()
        scope_drift_payload: dict[str, Any] | None = None
        if allowed_files:
            scope_drift_payload = check_scope_drift(changed_files, allowed_files)
            scope_verdict = scope_guard(
                changed_files=changed_files,
                task_card=self._task_card_payload,
                strict_scope=bool(getattr(self.config, "strict_scope", False)),
                contract_mode=self._contract_first_mode(),
            ).to_dict()
            scope_drift_payload["guard"] = scope_verdict
            if bool(scope_verdict.get("blocked")):
                self.state.add_error(
                    str(scope_verdict.get("reason") or "scope drift blocked"),
                    phase=Stage.IMPLEMENT.value,
                    action=action.value,
                    category="implement",
                )
        apply_codex_success_transition(
            context=runtime.context,
            action=action,
            result=result,
        )
        if runtime.pending_recovery_card:
            result.setdefault("recovery_card_used", True)
            runtime.pending_recovery_card = None
        runtime.execution_result = dict(result.get("execution_result") or {}) if isinstance(result.get("execution_result"), dict) else None
        runtime.execution_artifacts = dict(result.get("execution_artifacts") or {}) if isinstance(result.get("execution_artifacts"), dict) else None
        if runtime.execution_result is None and changed_files:
            compat_execution = build_execution_result(
                feature=str(impl_context.get("feature") or self.config.feature),
                task=runtime.task_label,
                backend=str(result.get("mode") or "adapter_compat"),
                status="PASS",
                changed_files=list(changed_files),
                artifacts=list(changed_files),
                summary="compat execution result synthesized from adapter changed_files",
                implementer_note=impl_context.get("implementer_note"),
            )
            runtime.execution_result = compat_execution
            result["execution_result"] = dict(compat_execution)
            planning_dir = self._planning_dir
            planning_dir.mkdir(parents=True, exist_ok=True)
            execution_result_path = planning_dir / EXECUTION_RESULT_FILENAME
            write_execution_result(execution_result_path, compat_execution)
            artifacts = dict(runtime.execution_artifacts or {})
            artifacts.setdefault(EXECUTION_RESULT_FILENAME, str(execution_result_path.resolve()))
            runtime.execution_artifacts = artifacts
            result["execution_artifacts"] = dict(artifacts)
        round_record["stage_status"] = "ok"
        round_record["details"] = implementation_round_details(
            changed_files=changed_files,
            protection=protection,
            impl_context=impl_context,
        )
        if runtime.execution_result:
            round_record["details"]["execution_result"] = dict(runtime.execution_result)
        if runtime.execution_artifacts:
            round_record["details"]["execution_artifacts"] = dict(runtime.execution_artifacts)
        if scope_drift_payload is not None:
            round_record["details"]["scope_drift"] = scope_drift_payload
        round_record["review_round"] = runtime.context.review_feedback.review_iteration
        runtime.round_records.append(update_round_record_outcome(round_record, runtime.context))
        details = post_implement_success_details(changed_files=changed_files, impl_context=impl_context)
        self._maybe_emit_hook(
            runtime.hook_events,
            event="post_implement",
            task_id=runtime.task_id,
            task_label=runtime.task_label,
            action=action,
            task_scope=runtime.task_scope,
            details=details,
        )

    def _hash_protected_policy_files(self) -> dict[str, str]:
        """Hash content of .claude/workflow/*.yaml files for change detection."""
        hashes: dict[str, str] = {}
        pattern_dir = Path(self.config.project_root) / ".claude" / "workflow"
        if pattern_dir.exists():
            for yaml_file in sorted(pattern_dir.glob("*.yaml")):
                try:
                    content = yaml_file.read_bytes()
                    rel = str(yaml_file.relative_to(self.config.project_root))
                    hashes[rel] = hashlib.sha256(content).hexdigest()
                except OSError:
                    pass
        return hashes

    def _detect_policy_hash_violations(
        self,
        pre_hashes: dict[str, str],
        post_hashes: dict[str, str],
    ) -> list[str]:
        """Return list of policy file paths that were modified, deleted, or created."""
        violations: list[str] = []
        for path, pre_hash in pre_hashes.items():
            post_hash = post_hashes.get(path)
            if post_hash is None:
                violations.append(f"{path} (deleted)")
            elif post_hash != pre_hash:
                violations.append(f"{path} (modified)")
        for path in post_hashes:
            if path not in pre_hashes:
                violations.append(f"{path} (created)")
        return violations

    def _run_codex_self_review(
        self,
        *,
        runtime: _LoopRuntime,
        changed_files: list[str],
        impl_context: dict[str, Any],
    ) -> dict[str, Any]:
        callback = getattr(self.adapter, "self_review", None)
        if callable(callback):
            payload = callback(
                task=runtime.task_label,
                context=dict(impl_context),
                changed_files=list(changed_files),
                review_iteration=runtime.context.review_feedback.review_iteration,
            )
        else:
            payload = run_codex_self_review(
                self.config.feature,
                "\n".join(changed_files),
                reviewer=CollaborationRole.CODEX.value,
            )
        return normalize_self_review_payload(
            payload=payload,
            feature=self.config.feature,
            changed_files=changed_files,
            reviewer=CollaborationRole.CODEX.value,
        )

    def _run_rules_gate_round(
        self,
        *,
        runtime: _LoopRuntime,
        action: CollaborationAction,
        round_record: dict[str, Any],
    ) -> dict[str, Any] | None:
        return run_rules_gate_round(
            self,
            runtime=runtime,
            action=action,
            round_record=round_record,
        )

    def _run_verify_round(
        self,
        *,
        runtime: _LoopRuntime,
        action: CollaborationAction,
        round_record: dict[str, Any],
    ) -> dict[str, Any] | None:
        return run_verify_round(
            self,
            runtime=runtime,
            action=action,
            round_record=round_record,
        )

    def _run_codex_self_review_round(
        self,
        *,
        runtime: _LoopRuntime,
        action: CollaborationAction,
        round_record: dict[str, Any],
    ) -> dict[str, Any] | None:
        self.state.current_stage = Stage.PLAN_REVIEW
        impl_context = self._build_implementation_context(runtime.task_label, runtime.task_scope)
        attach_runtime_instinct_hints(impl_context, pre_compact_payload=runtime.pre_compact_payload)
        review_payload = self._run_codex_self_review(
            runtime=runtime,
            changed_files=list(runtime.last_changed_files),
            impl_context=impl_context,
        )
        if str(review_payload.get("status") or "").upper() == "BLOCKED":
            blocking_reason = str(review_payload.get("blocking_reason") or review_payload.get("summary") or "SELF_REVIEW_BLOCKED")
            self.state.add_error(
                blocking_reason,
                phase=Stage.PLAN_REVIEW.value,
                action=action.value,
                category="review",
            )
            self.state.mark_completed(StopReason.HARD_ERROR, "BLOCKED")
            self.state.last_stage_status = "self_review_blocked"
            round_record["stage_status"] = "blocked"
            round_record["last_error"] = blocking_reason
            round_record["details"] = {"codex_self_review": review_payload}
            runtime.round_records.append(update_round_record_outcome(round_record, runtime.context))
            return self._finish_loop(
                runtime,
                reason="SELF_REVIEW_BLOCKED",
                last_error=blocking_reason,
                action=action,
            )
        runtime.codex_self_reviews.append(dict(review_payload))
        record_codex_self_review_result(
            runtime.context,
            approved=bool(review_payload.get("approved", False)),
            summary=str(review_payload.get("summary") or ""),
        )
        self.state.last_stage_status = (
            "self_review_passed" if runtime.context.self_review_approved else "self_review_changes_requested"
        )
        round_record["stage_status"] = "pass" if runtime.context.self_review_approved else "changes_requested"
        round_record["details"] = {"codex_self_review": review_payload}
        runtime.round_records.append(update_round_record_outcome(round_record, runtime.context))
        return None

    def _resolve_post_execution_qa(self, runtime: _LoopRuntime) -> dict[str, Any]:
        callback = getattr(self.adapter, "post_execution_qa", None)
        if callable(callback):
            payload = callback(
                task=runtime.task_label,
                context=self._build_implementation_context(runtime.task_label, runtime.task_scope),
                artifacts=list(runtime.last_changed_files),
            )
        else:
            payload = run_post_execution_qa(
                self.config.feature,
                artifacts=list(runtime.last_changed_files),
                context=self._build_implementation_context(runtime.task_label, runtime.task_scope),
                rounds=len(runtime.round_records),
            )
        if not isinstance(payload, dict):
            payload = {}
        payload.setdefault("status", "FAIL")
        payload.setdefault("feature", self.config.feature)
        payload.setdefault("artifacts", list(runtime.last_changed_files))
        return payload

__all__ = ["EngineImplementationMixin"]

