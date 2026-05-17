"""Session orchestration helpers for the autopilot engine."""
from __future__ import annotations
import json
import logging
from typing import Any, Iterable

from kodawari.autopilot.collaboration import (
    CollaborationAction,
    CollaborationContext,
    build_peer_review_policy,
    build_round_record,
    update_round_record_outcome,
)
from kodawari.autopilot.engine.engine_hooks import emit_hook_event
from kodawari.autopilot.engine.hook_lifecycle import (
    build_pre_compact_payload,
    materialize_runtime_compact,
)
from kodawari.autopilot.engine.loop_result_payload import build_loop_result_payload
from kodawari.autopilot.engine.loop_runner import run_peer_review_loop, run_single_pass_loop
from kodawari.autopilot.planning.prd_contract import prd_coverage_check
from kodawari.autopilot.core.semantic_compact import materialize_semantic_compact
from kodawari.autopilot.planning.task_graph import validate_task_graph
from kodawari.autopilot.engine.engine_support import _LoopRuntime
from kodawari.autopilot.review.bridge import summarize_peer_review
from kodawari.autopilot.core.state import StopReason
from kodawari.gate.checkers import (
    build_contract_compliance_report,
    discover_project_schema_files,
)
from kodawari.utils.glob_match import glob_match
from kodawari.autopilot.core.pipeline_config import resolve_pipeline

logger = logging.getLogger(__name__)

_HIGH_RISK_PATTERNS = [
    "**/auth_*", "**/credential_*", "**/password_*",
    "**/migration_sql/**", "**/migrations/**",
]
_MEDIUM_RISK_PATTERNS = ["**/services/*", "**/api/**"]


def _compute_risk_profile(changed_files: list[str]) -> str:
    """Classify overall risk level based on changed file paths."""
    for f in changed_files:
        for pattern in _HIGH_RISK_PATTERNS:
            if glob_match(f, pattern):
                return "high"
    for f in changed_files:
        for pattern in _MEDIUM_RISK_PATTERNS:
            if glob_match(f, pattern):
                return "medium"
    return "low"


def _dedupe_changed_files(values: Iterable[Any]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for item in values or []:
        normalized = str(item or "").strip().replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        if not normalized:
            continue
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(normalized)
    return deduped


class EngineSessionMixin:
    def _maybe_emit_hook(
        self,
        hook_events: list[dict[str, Any]],
        *,
        event: str,
        task_id: str,
        task_label: str,
        action: CollaborationAction | None = None,
        task_scope: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        emit_hook_event(
            hook_events,
            hook_events_enabled=self.config.hook_events_enabled,
            adapter=self.adapter,
            build_context=self._build_implementation_context,
            event=event,
            task_id=task_id,
            task_label=task_label,
            task_scope=task_scope,
            action_name=action.value if action is not None else None,
            role_name=self._action_role(action).value if action is not None else None,
            cycle=self.state.cycle,
            details=details,
        )

    def _build_loop_result(self, *, stopped: bool, reason: str, task_label: str, context: CollaborationContext, round_records: list[dict[str, Any]], hook_events: list[dict[str, Any]], peer_review_policy: dict[str, Any], peer_review_summary: dict[str, Any] | None = None, codex_self_reviews: list[dict[str, Any]] | None = None, post_execution_qa: dict[str, Any] | None = None, verify_check: dict[str, Any] | None = None, gate_check: dict[str, Any] | None = None, pre_compact: dict[str, Any] | None = None, last_error: str | None = None, execution_result: dict[str, Any] | None = None, execution_artifacts: dict[str, str] | None = None) -> dict[str, Any]:
        return build_loop_result_payload(
            feature=self.config.feature,
            unified_status=self.state.get_unified_status(),
            stopped=stopped,
            reason=reason,
            task_label=task_label,
            context=context,
            round_records=round_records,
            hook_events=hook_events,
            peer_review_policy=peer_review_policy,
            peer_review_summary=peer_review_summary,
            codex_self_reviews=codex_self_reviews,
            post_execution_qa=post_execution_qa,
            verify_check=verify_check,
            gate_check=gate_check,
            pre_compact=pre_compact,
            last_error=last_error,
            execution_result=execution_result,
            execution_artifacts=execution_artifacts,
            tokens_used=int(getattr(self.state, "tokens_used", 0) or 0),
            token_budget=int(getattr(self.config, "token_budget", 0) or 0),
        )

    def _semantic_compact_mode(self, *, reason: str) -> str:
        resolved = str(reason or "").strip().upper()
        if resolved in {
            "VERIFY_BLOCKED",
            "GATE_BLOCKED",
            "COLLABORATION_ROUND_LIMIT",
            "OPUS_REVIEW_BLOCKED",
        }:
            return "incremental"
        if self.state.is_stuck():
            return "incremental"
        return "full"

    def _semantic_trigger_event(self, *, reason: str) -> str:
        mapping = {
            "VERIFY_BLOCKED": "verify_blocked",
            "GATE_BLOCKED": "gate_blocked",
            "COLLABORATION_ROUND_LIMIT": "review_round_limit",
            "OPUS_REVIEW_BLOCKED": "review_blocked",
            "PROCEED_TO_GATE": "post_loop",
            "MAX_CYCLES_REACHED": "max_cycles",
            "PROTECTED_FILE_BLOCK": "protected_file_block",
            "IMPLEMENTATION_ERROR": "implementation_error",
        }
        key = str(reason or "").strip().upper()
        return mapping.get(key, "post_loop")

    def _refresh_semantic_compact(
        self,
        runtime: _LoopRuntime,
        *,
        reason: str,
        trigger_event: str,
        mode: str,
    ) -> dict[str, Any]:
        payload = materialize_semantic_compact(
            project_root=self.config.project_root,
            feature=self.config.feature,
            state=self.state,
            context=runtime.context,
            task_label=runtime.task_label,
            task_scope=runtime.task_scope or "",
            verify_check=runtime.verify_check,
            gate_check=runtime.gate_check,
            reason=reason,
            token_budget=self.config.token_budget,
            trigger_event=trigger_event,
            mode=mode,
        )
        runtime.semantic_compact_payload = dict(payload)
        runtime.pre_compact_payload["semantic_compact_runtime"] = dict(payload)
        semantic_payload = payload.get("payload")
        if isinstance(semantic_payload, dict):
            runtime.pre_compact_payload["semantic_compact"] = dict(semantic_payload)
        return payload

    def _create_loop_runtime(
        self,
        *,
        task_label: str,
        task_scope: str | None,
        max_rounds: int | None,
        enable_peer_review: bool = True,
    ) -> _LoopRuntime:
        rounds_limit = int(max_rounds or self.config.collaboration_max_rounds)
        context = self._build_or_get_context(task_label, task_scope)
        context.peer_review_enabled = bool(enable_peer_review)
        context.verify_passed = False
        context.rules_gate_passed = False
        context.self_review_completed = False
        context.self_review_approved = False
        runtime = _LoopRuntime(
            task_label=task_label,
            task_scope=task_scope,
            task_id=self._task_id_from_label(task_label),
            context=context,
            last_changed_files=_dedupe_changed_files(
                getattr(self.config, "initial_changed_files", []) or []
            ),
            peer_review_policy=build_peer_review_policy(max_rounds=rounds_limit),
            peer_review_enabled=bool(enable_peer_review),
            pre_compact_payload=build_pre_compact_payload(
                project_root=self.config.project_root,
                feature=self.config.feature,
            ),
        )
        runtime.peer_review_summary = summarize_peer_review(
            self.config.feature,
            runtime.peer_reviews,
            require_real_peer_review=bool(getattr(self.config, "require_real_peer_review", False)),
        )
        self._inject_user_redesign_must_fix(runtime)
        return runtime

    def _inject_user_redesign_must_fix(self, runtime: Any) -> None:
        """Bridge user-chosen refactor approach into the executor prompt.

        `kodawari decide` writes a sticky decision file
        `.user_redesign_decision.json` in the planning dir. We inject the
        chosen `must_fix` text into `runtime.context.review_feedback.must_fix`
        ONLY for the matching task_id, then immediately mark the decision
        as consumed so it does not leak into subsequent tasks.

        Critical: the decision is task-scoped. T2's user-chosen refactor
        target (`gpt_enrichment_pipeline.py`) is NOT in T3's writable files
        list — leaking the must_fix into T3 caused TASK_BLOCKED_BY_PRECONDITION
        on the first real T3 run.
        """
        import json
        from datetime import datetime, timezone
        from pathlib import Path as _P

        user_must_fix: list[str] = []
        chosen_title: str = ""
        decision_file: _P | None = None
        decision_data: dict[str, Any] = {}

        # Current task id
        current_task_id = ""
        try:
            current_task_id = str(getattr(runtime, "task_id", "") or "").strip()
        except AttributeError:
            current_task_id = ""
        if not current_task_id:
            task_card = getattr(self, "_task_card_payload", None)
            if isinstance(task_card, dict):
                current_task_id = str(task_card.get("task_id") or "").strip()

        # 1) Primary path: sticky decision file
        try:
            decision_file = _P(self._planning_dir) / ".user_redesign_decision.json"
            if decision_file.exists():
                decision_data = json.loads(decision_file.read_text(encoding="utf-8"))
                decision_task_id = str(decision_data.get("task_id") or "").strip()
                # Only inject if:
                #  - not yet consumed
                #  - decision targets the *current* task (no leakage to T3+)
                if (
                    not decision_data.get("consumed_at")
                    and decision_task_id
                    and current_task_id
                    and decision_task_id == current_task_id
                ):
                    must_fix = [
                        str(item).strip()
                        for item in list(decision_data.get("must_fix") or [])
                        if str(item).strip()
                    ]
                    if must_fix:
                        user_must_fix = must_fix
                        chosen_title = str(decision_data.get("chosen_title") or "")
        except (OSError, ValueError, AttributeError):
            decision_file = None
            decision_data = {}

        # 2) Fallback: in-card recovery block
        if not user_must_fix:
            task_card = getattr(self, "_task_card_payload", None)
            if isinstance(task_card, dict):
                recovery_block = task_card.get("recovery")
                if isinstance(recovery_block, dict):
                    source_action = str(recovery_block.get("source_action") or "").strip()
                    if source_action == "user_redesign_accepted":
                        user_must_fix = [
                            str(item).strip()
                            for item in list(recovery_block.get("must_fix") or [])
                            if str(item).strip()
                        ]
                        chosen_title = str(recovery_block.get("user_chosen_title") or "")

        if not user_must_fix:
            return

        try:
            runtime.context.review_feedback.must_fix = list(user_must_fix)
            # Mark task_card with a flag so tool_use_prompt's user_redesign
            # preamble detection works regardless of source_action
            task_card = getattr(self, "_task_card_payload", None)
            if isinstance(task_card, dict):
                recovery = task_card.setdefault("recovery", {})
                if isinstance(recovery, dict):
                    recovery["source_action"] = "user_redesign_accepted"
                    if chosen_title:
                        recovery["user_chosen_title"] = chosen_title
                    recovery["must_fix"] = list(user_must_fix)
        except AttributeError:
            return

        # Mark sticky decision as consumed — one-shot per task.
        # Even if the executor fails, the next round on T2 will re-load the
        # same task_card.recovery block (kept in memory) via the fallback
        # branch. The sticky file only matters at engine startup.
        if decision_file is not None and decision_data:
            try:
                decision_data["consumed_at"] = datetime.now(timezone.utc).isoformat()
                decision_data["consumed_for_task_id"] = current_task_id
                decision_file.write_text(
                    json.dumps(decision_data, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
            except (OSError, ValueError):
                pass

    def _initialize_compact_artifacts(self, runtime: _LoopRuntime) -> None:
        """Write both compact artifacts at loop start in a single call.

        Two distinct artifacts are written here on purpose — they serve
        different consumers and draw from different data sources:

        1. runtime compact (COMPACT_CONTEXT.md + compact_context.json):
           Instinct hints, log tail, absorption status.  Built from the
           pre_compact_payload accumulated before execution starts.

        2. semantic compact (semantic_compact.json + .md):
           Current loop state — must_fix, last_error, gate/verify status.
           Built from live self.state + runtime.context.

        Both calls MUST run together at session start; calling one without
        the other leaves the planning dir partially initialized.
        """
        runtime.pre_compact_payload["runtime"] = materialize_runtime_compact(
            project_root=self.config.project_root,
            feature=self.config.feature,
            payload=runtime.pre_compact_payload,
            trigger_event="pre_compact",
        )
        self._refresh_semantic_compact(
            runtime,
            reason="",
            trigger_event="pre_compact",
            mode="full",
        )

    def _start_loop_session(self, runtime: _LoopRuntime) -> None:
        self.state.active_task = runtime.task_label
        self._initialize_compact_artifacts(runtime)
        self._maybe_emit_hook(
            runtime.hook_events,
            event="session_start",
            task_id=runtime.task_id,
            task_label=runtime.task_label,
            task_scope=runtime.task_scope,
            details={"source": "kodawari", "contract": "ws114.v2"},
        )
        self._maybe_emit_hook(
            runtime.hook_events,
            event="pre_compact",
            task_id=runtime.task_id,
            task_label=runtime.task_label,
            task_scope=runtime.task_scope,
            details=runtime.pre_compact_payload,
        )

    def _finish_loop(
        self,
        runtime: _LoopRuntime,
        *,
        reason: str,
        last_error: str | None = None,
        action: CollaborationAction | None = None,
    ) -> dict[str, Any]:
        prompt_lesson_learning: dict[str, Any] = {}
        if reason == "PROCEED_TO_GATE":
            callback = getattr(self, "_ingest_successful_deterministic_recovery_prompt_lessons", None)
            if callable(callback):
                prompt_lesson_learning = callback(runtime)
        self._refresh_semantic_compact(
            runtime,
            reason=reason,
            trigger_event=self._semantic_trigger_event(reason=reason),
            mode=self._semantic_compact_mode(reason=reason),
        )
        self._maybe_emit_hook(
            runtime.hook_events,
            event="session_stop",
            task_id=runtime.task_id,
            task_label=runtime.task_label,
            action=action,
            task_scope=runtime.task_scope,
            details={"reason": reason, "last_error": last_error, "review_rounds_used": int(runtime.context.review_feedback.review_iteration), "must_fix_remaining": len(runtime.context.review_feedback.must_fix), "gate_recommendation": runtime.context.review_feedback.gate_recommendation},
        )
        loop_result = self._build_loop_result(
            stopped=True,
            reason=reason,
            task_label=runtime.task_label,
            context=runtime.context,
            round_records=runtime.round_records,
            hook_events=runtime.hook_events,
            peer_review_policy=runtime.peer_review_policy,
            peer_review_summary=runtime.peer_review_summary,
            codex_self_reviews=runtime.codex_self_reviews,
            post_execution_qa=runtime.post_execution_qa,
            verify_check=runtime.verify_check,
            gate_check=runtime.gate_check,
            pre_compact=runtime.pre_compact_payload,
            last_error=last_error,
            execution_result=runtime.execution_result,
            execution_artifacts=runtime.execution_artifacts,
        )
        if prompt_lesson_learning:
            loop_result["prompt_lesson_learning"] = dict(prompt_lesson_learning)
        loop_result["changed_files"] = list(runtime.last_changed_files)
        # FIX_ROUND / CODEX_FIX are also executor invocations (post-review retries),
        # not separate stages — counting only IMPLEMENT undercounted real executor work
        # by a factor of N every time reviewer feedback drove retries.
        loop_result["executor_attempts"] = sum(
            1
            for item in runtime.round_records
            if str(item.get("stage") or "").upper() in {"IMPLEMENT", "FIX_ROUND", "CODEX_FIX"}
        )
        loop_result["recovery_attempts"] = int(runtime.recovery_attempts)
        loop_result["recovery_decisions"] = [dict(item) for item in list(runtime.recovery_decisions or [])]
        compliance = self._contract_compliance_report(runtime=runtime, loop_reason=reason)
        if compliance is not None:
            loop_result["compliance_report"] = compliance
            self._write_compliance_report_artifacts(compliance)
        loop_result["risk_profile"] = _compute_risk_profile(sorted(self.state.changed_files))
        return loop_result

    def _contract_compliance_report(
        self,
        *,
        runtime: _LoopRuntime,
        loop_reason: str,
    ) -> dict[str, Any] | None:
        if self._contract_first_mode() == "off":
            return None
        task_graph = self._task_graph_payload if isinstance(self._task_graph_payload, dict) else {"tasks": []}
        if task_graph and isinstance(task_graph, dict):
            task_graph_errors = validate_task_graph(task_graph)
            if task_graph_errors:
                task_graph = {"tasks": []}
        prd_intake = self._resolve_prd_intake_for_compliance()
        # Reuse cached evidence from the last gate round rather than calling the
        # validator again — avoids a redundant second pass on the same runtime.
        review_evidence = (
            runtime.last_proceed_evidence
            if runtime.last_proceed_evidence is not None
            else self._validate_proceed_review_evidence(runtime)
        )
        report = build_contract_compliance_report(
            project_root=self.config.project_root,
            changed_files=list(runtime.last_changed_files),
            task_card=self._task_card_payload if isinstance(self._task_card_payload, dict) else None,
            task_graph=task_graph,
            prd_intake=prd_intake,
            review_evidence=review_evidence,
            schema_files=discover_project_schema_files(self.config.project_root),
        )
        report["loop_reason"] = str(loop_reason)
        report["prd_coverage"] = prd_coverage_check(
            tasks=list(task_graph.get("tasks") or []),
            prd_intake=prd_intake,
        )
        return report

    def _resolve_prd_intake_for_compliance(self) -> dict[str, Any]:
        if isinstance(self._prd_intake_payload, dict):
            return dict(self._prd_intake_payload)
        conversation = (
            dict(self._planning_conversation_payload)
            if isinstance(getattr(self, "_planning_conversation_payload", None), dict)
            else {}
        )
        if not conversation:
            return {}
        return {
            "business_outcome": str(conversation.get("business_outcome") or "").strip(),
            "source_of_truth": [str(item).strip() for item in list(conversation.get("source_of_truth") or []) if str(item).strip()],
            "source_of_truth_canonical": [
                str(item).strip() for item in list(conversation.get("source_of_truth_canonical") or []) if str(item).strip()
            ],
            "out_of_scope": [str(item).strip() for item in list(conversation.get("out_of_scope") or []) if str(item).strip()],
            "coverage_hints": [str(item).strip() for item in list(conversation.get("coverage_hints") or []) if str(item).strip()],
            "path_type": str(conversation.get("path_type") or "").strip(),
            "layers": [str(item).strip() for item in list(conversation.get("layers") or []) if str(item).strip()],
        }

    def _write_compliance_report_artifacts(self, report: dict[str, Any]) -> None:
        planning_dir = self._planning_dir
        planning_dir.mkdir(parents=True, exist_ok=True)
        json_path = planning_dir / "COMPLIANCE_REPORT.json"
        md_path = planning_dir / "COMPLIANCE_REPORT.md"
        json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        checks = list(report.get("checks") or [])
        lines = [
            "# Compliance Report",
            "",
            f"- status: {report.get('status', 'UNKNOWN')}",
            f"- loop_reason: {report.get('loop_reason', '')}",
            "",
            "## Checks",
        ]
        if checks:
            for item in checks:
                if not isinstance(item, dict):
                    continue
                lines.append(
                    f"- {item.get('check_name', '')}: {item.get('status', '')} ({item.get('details', '')})"
                )
        else:
            lines.append("- (none)")
        md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _new_round_record(
        self,
        runtime: _LoopRuntime,
        action: CollaborationAction,
    ) -> dict[str, Any]:
        return build_round_record(
            round_index=len(runtime.round_records) + 1,
            cycle=self.state.cycle,
            task_id=runtime.task_id,
            task_label=runtime.task_label,
            action=action,
            actor=self._action_role(action),
            context=runtime.context,
        )

    def _handle_max_cycles(
        self,
        runtime: _LoopRuntime,
        *,
        action: CollaborationAction,
        round_record: dict[str, Any],
    ) -> dict[str, Any] | None:
        if self.state.cycle <= self.config.max_cycles:
            return None
        if self._allow_executor_recovery_after_max_cycles(
            runtime=runtime,
            action=action,
            round_record=round_record,
        ):
            return None
        self.state.mark_completed(StopReason.MAX_CYCLES, "BLOCKED")
        round_record["stage_status"] = "max_cycles"
        round_record["last_error"] = "MAX_CYCLES_REACHED"
        runtime.round_records.append(update_round_record_outcome(round_record, runtime.context))
        return self._finish_loop(runtime, reason="MAX_CYCLES_REACHED", action=action)

    def _allow_executor_recovery_after_max_cycles(
        self,
        *,
        runtime: _LoopRuntime,
        action: CollaborationAction,
        round_record: dict[str, Any],
    ) -> bool:
        if action not in {CollaborationAction.FIX_ROUND, CollaborationAction.CODEX_FIX}:
            return False
        feedback = getattr(runtime.context, "review_feedback", None)
        must_fix = getattr(feedback, "must_fix", []) if feedback is not None else []
        if not any(str(item).strip() for item in list(must_fix or [])):
            return False
        execution_result = runtime.execution_result if isinstance(runtime.execution_result, dict) else {}
        error_code = str(execution_result.get("error_code") or execution_result.get("reason") or "").strip()
        recovery_card = getattr(runtime, "pending_recovery_card", None)
        has_recovery_card = isinstance(recovery_card, dict) and bool(recovery_card.get("recovery"))
        recoverable = False
        callback = getattr(self, "_recoverable_executor_block", None)
        if callable(callback) and error_code:
            recoverable = bool(callback(error_code))
        if not has_recovery_card and not recoverable:
            return False
        details = round_record.setdefault("details", {})
        if isinstance(details, dict):
            details["max_cycles"] = {
                "allowed_executor_recovery": True,
                "cycle": int(self.state.cycle),
                "max_cycles": int(self.config.max_cycles),
                "error_code": error_code,
                "has_recovery_card": has_recovery_card,
            }
        return True

    def _dispatch_round_action(
        self,
        runtime: _LoopRuntime,
        *,
        action: CollaborationAction,
        round_record: dict[str, Any],
    ) -> dict[str, Any] | None:
        handlers = {
            # Canonical values (new code emits these)
            CollaborationAction.DESIGN: self._run_opus_design_round,
            CollaborationAction.IMPLEMENT: self._run_codex_round,
            CollaborationAction.FIX_ROUND: self._run_codex_round,
            CollaborationAction.PEER_REVIEW: self._run_opus_review_round,
            CollaborationAction.SELF_REVIEW: self._run_codex_self_review_round,
            CollaborationAction.VERIFY: self._run_verify_round,
            CollaborationAction.RULES_GATE: self._run_rules_gate_round,
            CollaborationAction.PROCEED_TO_GATE: self._run_proceed_round,
            CollaborationAction.FINISH: self._run_finish_round,
            # Legacy aliases kept for existing state files and round records
            CollaborationAction.OPUS_DESIGN: self._run_opus_design_round,
            CollaborationAction.CODEX_IMPLEMENT: self._run_codex_round,
            CollaborationAction.CODEX_FIX: self._run_codex_round,
            CollaborationAction.OPUS_REVIEW: self._run_opus_review_round,
            CollaborationAction.CODEX_SELF_REVIEW: self._run_codex_self_review_round,
        }
        handler = handlers.get(action)
        if handler is None:
            round_record["stage_status"] = "unknown_action"
            runtime.round_records.append(update_round_record_outcome(round_record, runtime.context))
            return None
        result = handler(runtime=runtime, action=action, round_record=round_record)
        if result is not None:
            return result
        return self._enforce_token_budget_after_round(runtime=runtime, action=action)

    def _enforce_token_budget_after_round(
        self,
        *,
        runtime: _LoopRuntime,
        action: CollaborationAction,
    ) -> dict[str, Any] | None:
        if not self._is_budget_checkpoint_action(action):
            return None
        budget = int(getattr(self.config, "token_budget", 0) or 0)
        used = int(getattr(self.state, "tokens_used", 0) or 0)
        if budget <= 0 or used <= budget:
            return None
        message = f"Token budget exceeded: used={used}, budget={budget}"
        logger.warning(message)
        self.state.add_error(
            message,
            phase=self.state.current_stage.value,
            action=action.value,
            category="runtime",
            error_code="TOKEN_BUDGET_EXCEEDED",
            metadata={"used_tokens": int(used), "budget_tokens": int(budget)},
        )
        self.state.mark_completed(StopReason.TOKEN_BUDGET, "BLOCKED")
        self.state.last_stage_status = "token_budget_exhausted"
        return self._finish_loop(
            runtime,
            reason="TOKEN_BUDGET_EXCEEDED",
            last_error=message,
            action=action,
        )

    def _is_budget_checkpoint_action(self, action: CollaborationAction) -> bool:
        return action in {
            CollaborationAction.IMPLEMENT,
            CollaborationAction.FIX_ROUND,
            CollaborationAction.PEER_REVIEW,
            CollaborationAction.SELF_REVIEW,
            # Legacy aliases
            CollaborationAction.CODEX_IMPLEMENT,
            CollaborationAction.CODEX_FIX,
            CollaborationAction.OPUS_REVIEW,
            CollaborationAction.CODEX_SELF_REVIEW,
        }

    def run_collaboration_loop(
        self,
        task_label: str,
        task_scope: str | None = None,
        *,
        max_rounds: int | None = None,
        enable_peer_review: bool = True,
    ) -> dict[str, Any]:
        from kodawari.autopilot.collaboration import CollaborationAction as _CA

        # Fix 4: Explicit CLI flag takes priority over pipeline preset.
        if not enable_peer_review:
            return run_single_pass_loop(self, task_label=task_label, task_scope=task_scope, max_rounds=max_rounds)

        # Fix 3: Use task card's prospective files, not accumulated state.
        project_root = getattr(self.config, "project_root", None)
        task_files = self._pipeline_task_files()
        pipeline = resolve_pipeline(project_root, task_files) if project_root is not None and task_files else None

        if pipeline is not None:
            effective_max_rounds = pipeline.max_cycles or max_rounds
            if pipeline.preset == "skip_review":
                return run_single_pass_loop(
                    self,
                    task_label=task_label,
                    task_scope=task_scope,
                    max_rounds=effective_max_rounds,
                    actions_override=(
                        _CA.OPUS_DESIGN,
                        _CA.CODEX_IMPLEMENT,
                        _CA.FINISH,
                    ),
                )
            if pipeline.preset == "strict_review":
                original = _safe_override_review_config(
                    self.adapter,
                    real_peer_review=True,
                    require_real_peer_review=True,
                )
                try:
                    result = run_peer_review_loop(
                        self,
                        task_label=task_label,
                        task_scope=task_scope,
                        max_rounds=effective_max_rounds,
                        loop_config_override=pipeline,
                    )
                finally:
                    _safe_restore_review_config(self.adapter, original)
                return result

        # No pipeline config or default preset
        return run_peer_review_loop(self, task_label=task_label, task_scope=task_scope, max_rounds=max_rounds)

    def _pipeline_task_files(self) -> list[str]:
        """Extract prospective task files for pipeline resolution from task card."""
        card = getattr(self, "_task_card_payload", None)
        if isinstance(card, dict):
            files = [str(f) for f in card.get("files_to_change") or [] if str(f).strip()]
            if files:
                return files
            core = [str(f) for f in card.get("core_files") or [] if str(f).strip()]
            if core:
                return core
        return []

    def _run_finish_round(
        self,
        *,
        runtime: _LoopRuntime,
        action: CollaborationAction,
        round_record: dict[str, Any],
    ) -> dict[str, Any]:
        """Handle the FINISH action: terminate the loop cleanly (skip verify/gate)."""
        round_record["stage_status"] = "finish"
        from kodawari.autopilot.collaboration import update_round_record_outcome
        runtime.round_records.append(update_round_record_outcome(round_record, runtime.context))
        return self._finish_loop(runtime, reason="PIPELINE_FINISH", action=action)

    def execute_cycle(self, task_label: str) -> dict[str, Any]:
        """Compatibility cycle counter used by existing tests."""
        self.state.cycle += 1
        if self.state.cycle > self.config.max_cycles:
            return {"stopped": True, "reason": "MAX_CYCLES_REACHED", "task": task_label}
        return {"stopped": False, "reason": None, "task": task_label}


def _safe_override_review_config(
    adapter: Any,
    *,
    real_peer_review: bool,
    require_real_peer_review: bool,
) -> dict[str, bool]:
    """Call adapter.override_review_config() if available; return original values or {}."""
    callback = getattr(adapter, "override_review_config", None)
    if callable(callback):
        result = callback(
            real_peer_review=real_peer_review,
            require_real_peer_review=require_real_peer_review,
        )
        if isinstance(result, dict):
            return result
    return {}


def _safe_restore_review_config(adapter: Any, original: dict[str, bool]) -> None:
    """Call adapter.restore_review_config() if available; silently skip otherwise."""
    if not original:
        return
    callback = getattr(adapter, "restore_review_config", None)
    if callable(callback):
        callback(original)


__all__ = ["EngineSessionMixin"]

