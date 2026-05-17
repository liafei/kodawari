"""Context and request-building helpers for the autopilot engine."""
from __future__ import annotations
import json
import logging
import os
from pathlib import Path
from typing import Any

from kodawari.autopilot.core.collaboration import (
    ArchitectureDecision,
    CollaborationAction,
    CollaborationContext,
    CollaborationRole,
    build_collaboration_context,
)
from kodawari.autopilot.execution.execution_artifacts import is_test_environment
from kodawari.autopilot.execution.execution_backend import execution_backend_descriptor, resolve_execution_backend
from kodawari.autopilot.planning.effort_scoring import score_effort_profile
from kodawari.autopilot.engine.engine_support import (
    ExecutionPhase,
    ExecutionPlan,
    build_default_adapter,
    build_default_pattern_registry,
    pattern_suggestions,
    resolve_requirements_text,
    serialize_decision,
    task_id_from_label,
    to_pattern_hint,
)
from kodawari.gate.checker_import_rules import relevant_ownership_context
from kodawari.autopilot.core.phase_guard import normalize_contract_mode

logger = logging.getLogger(__name__)
_DEFAULT_VERIFY_CMD = "pytest -q"
_FORCE_GLOBAL_VERIFY_ENVS = (
    "WORKFLOW_FORCE_GLOBAL_VERIFY",
    "WORKFLOW_IMPLEMENTATION_FORCE_GLOBAL_VERIFY",
)


def _env_truthy(name: str) -> bool:
    return str(os.environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _force_global_verify_enabled() -> bool:
    return any(_env_truthy(name) for name in _FORCE_GLOBAL_VERIFY_ENVS)


class EngineContextMixin:
    def _load_contract_json(self, path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _contract_mode(self) -> str:
        return normalize_contract_mode(getattr(self.config, "contract_first_mode", "off"))

    def _contract_strict(self) -> bool:
        return self._contract_mode() == "strict"

    def _task_card_files(self) -> list[str]:
        payload = self._task_card_payload if isinstance(self._task_card_payload, dict) else {}
        values = payload.get("files_to_change")
        if not isinstance(values, list):
            return []
        return [str(item).strip() for item in values if str(item).strip()]

    def _implementation_verify_cmd(self) -> str:
        configured = str(getattr(self.config, "verify_cmd", "") or "").strip()
        payload = self._task_card_payload if isinstance(self._task_card_payload, dict) else {}
        card_verify = str(payload.get("verify_cmd") or "").strip()
        if _force_global_verify_enabled():
            return configured or card_verify
        if card_verify:
            return card_verify
        return card_verify or configured

    def _project_model_context(self, *, task_id: str) -> dict[str, Any]:
        conversation_raw = getattr(self, "_planning_conversation_payload", None)
        conversation = dict(conversation_raw) if isinstance(conversation_raw, dict) else None
        architecture_plan = (
            conversation
            if conversation is not None
            else self._load_contract_json(self._planning_dir / "ARCHITECTURE_PLAN.json") or {}
        )
        repo_inventory = self._load_contract_json(self._planning_dir / "REPO_INVENTORY.json") or {}
        task_graph = self._load_contract_json(self._planning_dir / "TASK_GRAPH.json") or {}
        return {
            "archetype": str(architecture_plan.get("archetype") or repo_inventory.get("archetype") or "").strip(),
            "capabilities": [
                str(item) for item in list(architecture_plan.get("capabilities") or repo_inventory.get("capabilities") or []) if str(item).strip()
            ],
            "surface": self._task_surface(task_id=task_id, task_graph=task_graph),
        }

    def _task_surface(self, *, task_id: str, task_graph: dict[str, Any]) -> str:
        tasks = [dict(item) for item in list(task_graph.get("tasks") or []) if isinstance(item, dict)]
        for item in tasks:
            if str(item.get("task_id") or "").strip() != task_id:
                continue
            return str(item.get("surface") or "").strip()
        return ""

    def _resolve_requirements_text(
        self,
        *,
        requirements_text: str | None,
        requirements_file: Any,
    ) -> str:
        return resolve_requirements_text(
            requirements_text=requirements_text,
            requirements_file=requirements_file,
        )

    def _build_default_pattern_registry(self) -> Any:
        return build_default_pattern_registry()

    def _build_default_adapter(self) -> Any:
        return build_default_adapter(self.config)

    def generate_execution_plan(self) -> ExecutionPlan:
        return ExecutionPlan(
            stages=[
                {
                    "name": ExecutionPhase.PLAN_REVIEW.value,
                    "description": "Run Opus/Codex collaboration review loop",
                },
                {
                    "name": ExecutionPhase.IMPLEMENT.value,
                    "description": "Codex implementation and fix passes",
                },
                {
                    "name": ExecutionPhase.VERIFY.value,
                    "description": "Scoped verify and setup-recovery handling",
                },
                {
                    "name": ExecutionPhase.GATE.value,
                    "description": "Proceed-to-gate handoff (no gate execution here)",
                },
            ],
            estimated_cycles=4,
            estimated_tokens=20000,
        )

    def _task_id_from_label(self, task_label: str) -> str:
        return task_id_from_label(task_label)

    def _serialize_decision(self, item: Any) -> dict[str, Any]:
        return serialize_decision(item)

    def _build_implementation_context(
        self, task_label: str, task_scope: str | None = None
    ) -> dict[str, Any]:
        task_id = self._task_id_from_label(task_label)
        learned_hints = self._load_learned_instinct_hints(limit=5, min_confidence=0.6)
        project_model = self._project_model_context(task_id=task_id)
        context = {
            "task_id": task_id,
            "task_label": task_label,
            "task_scope": task_scope or "",
            "requirements": self.requirements_text,
            "feature": self.config.feature,
            "project_root": str(self.config.project_root),
            "planning_dir": str(self._planning_dir),
            "verify_cmd": self._implementation_verify_cmd(),
            "executor_backend": str(getattr(self.config, "executor_backend", "") or ""),
            "executor_command": str(getattr(self.config, "executor_command", "") or ""),
            "task_card": dict(self._task_card_payload or {}),
            "task_card_files": self._task_card_files(),
            "task_invariants": [str(item) for item in list((self._task_card_payload or {}).get("invariants") or []) if str(item).strip()],
            "current_stage": self.state.current_stage.value,
            "architecture_decisions": [
                self._serialize_decision(item) for item in self.state.architecture_decisions
            ],
            "learned_instinct_hints": learned_hints,
            "learned_instinct_hints_count": len(learned_hints),
        }
        context.update(project_model)
        effort_profile = score_effort_profile(
            task_label=task_label,
            task_scope=task_scope or "",
            requirements=self.requirements_text,
            task_card=dict(self._task_card_payload or {}),
            changed_files=context["task_card_files"],
            prior_failures=len(list(getattr(self.state, "error_events", []) or [])),
            project_model=project_model,
        )
        context["effort_profile"] = effort_profile
        context["reasoning_tier"] = str(effort_profile.get("tier") or "economy")
        suggestions = self._pattern_suggestions(
            task_id=task_id,
            task_label=task_label,
            task_scope=task_scope,
        )
        pattern_hints = [self._to_pattern_hint(item) for item in suggestions]
        pattern_hints.extend(self._learned_instinct_pattern_hints(learned_hints))
        context["pattern_hints"] = pattern_hints
        context["scope_risk_warnings"] = self._scope_risk_warnings(
            task_label=task_label,
            task_scope=task_scope or "",
            learned_hints=learned_hints,
        )
        ownership_context = relevant_ownership_context(
            project_root=self.config.project_root,
            changed_files=context["task_card_files"],
        )
        if ownership_context:
            context["ownership_context"] = ownership_context
            context["ownership_hints"] = [
                (
                    f"{item['module']} is canonical for {', '.join(item['canonical_for']) or 'declared module scope'}; "
                    f"reuse public API {', '.join(item['public_api']) or '(none declared)'}; "
                    f"avoid forbidden imports {', '.join(item['forbidden_imports']) or '(none declared)'}."
                )
                for item in ownership_context
            ]
        return context

    def _load_learned_instinct_hints(
        self,
        *,
        limit: int,
        min_confidence: float,
    ) -> list[dict[str, Any]]:
        try:
            from kodawari.instincts import select_instinct_hints
        except Exception:
            logger.warning("instinct selector unavailable while building implementation context", exc_info=True)
            return []
        try:
            payload = select_instinct_hints(
                self.config.project_root,
                limit=max(0, int(limit)),
                min_confidence=float(min_confidence),
            )
        except Exception:
            logger.warning("instinct hint load failed while building implementation context", exc_info=True)
            return []
        return [dict(item) for item in payload if isinstance(item, dict)]

    def _learned_instinct_pattern_hints(self, hints: list[dict[str, Any]]) -> list[dict[str, Any]]:
        pattern_hints: list[dict[str, Any]] = []
        for index, hint in enumerate(hints, start=1):
            pattern = str(hint.get("pattern") or "").strip()
            if not pattern:
                continue
            confidence = float(hint.get("confidence", 0.0) or 0.0)
            source = str(hint.get("source") or "instincts")
            explanation = str(hint.get("explanation") or "").strip()
            rationale = f"Learned instinct suggested pattern '{pattern}' (source={source}, confidence={confidence:.2f})."
            if explanation:
                rationale = f"{rationale} {explanation}"
            pattern_hints.append(
                {
                    "pattern_id": f"learned-instinct-{index}",
                    "title": "Learned Instinct Pattern",
                    "rationale": rationale,
                    "confidence": confidence,
                    "source": source,
                    "pattern": pattern,
                }
            )
        return pattern_hints

    def _scope_risk_warnings(
        self,
        *,
        task_label: str,
        task_scope: str,
        learned_hints: list[dict[str, Any]],
    ) -> list[str]:
        text = f"{task_label}\n{task_scope}".lower()
        warnings: list[str] = []
        seen: set[str] = set()
        for item in self._planning_risk_warnings(limit=4):
            normalized = str(item).strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            warnings.append(normalized)
        for hint in learned_hints:
            pattern = str(hint.get("pattern") or "").strip()
            if not pattern:
                continue
            confidence = float(hint.get("confidence", 0.0) or 0.0)
            if confidence < 0.75:
                continue
            lowered = pattern.lower()
            if lowered in text:
                continue
            warnings.append(
                f"High-confidence learned instinct '{pattern}' is not explicitly covered by current scope."
            )
            if len(warnings) >= 6:
                break
        return warnings

    def _planning_risk_warnings(self, *, limit: int) -> list[str]:
        conversation_raw = getattr(self, "_planning_conversation_payload", None)
        conversation = dict(conversation_raw) if isinstance(conversation_raw, dict) else {}
        escalation = dict(conversation.get("escalation") or {})
        unresolved = [
            dict(item)
            for item in list(escalation.get("unresolved_findings") or [])
            if isinstance(item, dict)
        ]
        warnings: list[str] = []
        for finding in unresolved:
            description = str(finding.get("description") or "").strip()
            recommendation = str(finding.get("recommendation") or "").strip()
            severity = str(finding.get("severity") or "").strip().lower()
            category = str(finding.get("category") or "").strip().lower()
            if not description:
                continue
            label = "Planning reviewer warning"
            tags = "/".join(part for part in (severity, category) if part)
            if tags:
                label = f"{label} ({tags})"
            warning = f"{label}: {description}"
            if recommendation:
                warning = f"{warning} Fix focus: {recommendation}"
            warnings.append(warning)
            if len(warnings) >= max(0, int(limit)):
                break
        return warnings

    def _pattern_suggestions(
        self,
        *,
        task_id: str,
        task_label: str,
        task_scope: str | None,
    ) -> list[Any]:
        return pattern_suggestions(
            self.pattern_registry,
            task_id=task_id,
            task_label=task_label,
            task_scope=task_scope,
            requirements=self.requirements_text,
        )

    def _to_pattern_hint(self, item: Any) -> dict[str, Any]:
        return to_pattern_hint(item)

    def build_execution_context(self, task_label: str, task_scope: str | None = None) -> dict[str, Any]:
        """Compatibility wrapper used by previous test naming."""
        return self._build_implementation_context(task_label, task_scope)

    def _build_or_get_context(self, task_label: str, task_scope: str | None = None) -> CollaborationContext:
        task_id = self._task_id_from_label(task_label)
        context = self._contexts.get(task_id)
        if context is not None:
            return context

        context = build_collaboration_context(
            task_id,
            task_label,
            task_scope=task_scope,
            architecture_decisions=[
                item if isinstance(item, ArchitectureDecision) else ArchitectureDecision.from_dict(self._serialize_decision(item))
                for item in self.state.architecture_decisions
            ],
            self_review_required=self._self_review_required(),
        )
        self._contexts[task_id] = context
        return context

    def _self_review_required(self) -> bool:
        configured_backend = str(self.config.self_review_backend or "").strip()
        backend_name = resolve_execution_backend(
            configured_backend or self.config.executor_backend,
            test_environment=is_test_environment(),
        )
        return bool(execution_backend_descriptor(backend_name).self_review_selectable)

    def _generate_design_decision(self, context: CollaborationContext) -> ArchitectureDecision:
        decision_id = f"ADR-{context.task_id or 'TASK'}-{len(context.architecture_decisions) + 1:02d}"
        decision = ArchitectureDecision(
            decision_id=decision_id,
            decision=f"Define minimal implementation boundary for {context.task_id}",
            rationale="Keep Codex execution scoped and testable before gate.",
            constraints=["Do not modify protected critical files without authorization"],
            test_strategy=["Add or update scoped tests for changed files"],
        )
        return decision

    def _action_role(self, action: CollaborationAction) -> CollaborationRole:
        if action in {CollaborationAction.DESIGN, CollaborationAction.PEER_REVIEW,
                       CollaborationAction.OPUS_DESIGN, CollaborationAction.OPUS_REVIEW}:
            return CollaborationRole.OPUS
        if action in {CollaborationAction.VERIFY, CollaborationAction.RULES_GATE, CollaborationAction.PROCEED_TO_GATE}:
            return CollaborationRole.SYSTEM
        return CollaborationRole.CODEX



__all__ = ["EngineContextMixin"]

