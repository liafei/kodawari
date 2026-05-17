"""Targeted tests for WS-104 autopilot engine recovery."""

from dataclasses import dataclass
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from kodawari.autopilot.core.collaboration import (
    CollaborationAction,
    build_collaboration_context,
    merge_loop_result_optionals,
)
from kodawari.autopilot.engine.engine import AutopilotConfig, AutopilotEngine, ExecutionPhase
from kodawari.autopilot.core.state import AutopilotState, Stage
from kodawari.autopilot.lane_config import LITE_LANE
from kodawari.cli.runtime.autopilot_workflow_runtime import _apply_task_cycle_verify_cmd
from kodawari.instincts import render_prompt_lessons_for_prompt


@dataclass
class _DummySuggestion:
    pattern_id: str

    def to_dict(self) -> dict[str, str]:
        return {
            "pattern_id": self.pattern_id,
            "title": "Dummy",
            "rationale": "Dummy rationale",
            "confidence": 1.0,
        }


class _DummyPatternRegistry:
    def analyze(
        self,
        *,
        task_id: str,
        task_label: str,
        task_scope: str | None = None,
        requirements: str | None = None,
    ) -> list[_DummySuggestion]:
        del task_id, task_label, task_scope, requirements
        return [_DummySuggestion(pattern_id="ranking-rules")]


class _AlwaysFailSetupAdapter:
    def __init__(self) -> None:
        self.calls = 0

    def implement(self, task: str, context: dict[str, object]) -> dict[str, object]:
        del task, context
        self.calls += 1
        return {
            "status": "error",
            "error": "fixture 'db_session' not found",
            "changes": [],
        }


class _ProtectedFileBlockAdapter:
    def implement(self, task: str, context: dict[str, object]) -> dict[str, object]:
        del task, context
        return {
            "status": "done",
            "changes": ["tests/conftest.py"],
        }


class _PermissionPolicyBlockAdapter:
    """Executor that tries to write a BLOCK-tier secret file (non-isolation
    path). Phase E post-execution gate must catch this.
    """

    def implement(self, task: str, context: dict[str, object]) -> dict[str, object]:
        del task, context
        return {
            "status": "done",
            "changes": [
                "backend/main.py",      # fine
                "config/.env.production",  # BLOCK via permission.default.yaml
            ],
        }


class _RuntimeGateRetryAdapter:
    def __init__(self) -> None:
        self.calls = 0

    def implement(self, task: str, context: dict[str, object]) -> dict[str, object]:
        del task
        self.calls += 1
        if self.calls == 1:
            return {
                "status": "done",
                "changes": ["src/bad_module.py", "tests/test_good_module.py"],
            }
        project_root = Path(str(context["project_root"]))
        (project_root / "src" / "bad_module.py").write_text(
            "def bad_branch(x):\n    return x\n",
            encoding="utf-8",
        )
        return {
            "status": "done",
            "changes": ["src/bad_module.py", "src/good_module.py", "tests/test_good_module.py"],
        }


class _HighTokenAdapter:
    def implement(self, task: str, context: dict[str, object]) -> dict[str, object]:
        del task, context
        return {
            "status": "done",
            "changes": ["src/token_heavy.py", "tests/test_token_heavy.py"],
            "tokens_used": 25,
        }


class _SimpleSuccessAdapter:
    def implement(self, task: str, context: dict[str, object]) -> dict[str, object]:
        del task, context
        return {
            "status": "done",
            "changes": ["src/good_module.py", "tests/test_good_module.py"],
        }


class _ReviewFailThenPassAdapter:
    def __init__(self) -> None:
        self.implement_calls = 0
        self.review_calls = 0

    def implement(self, task: str, context: dict[str, object]) -> dict[str, object]:
        del task
        self.implement_calls += 1
        project_root = Path(str(context["project_root"]))
        (project_root / "tests").mkdir(parents=True, exist_ok=True)
        (project_root / "tests" / "test_good_module.py").write_text(
            "def test_good_module():\n    assert True\n",
            encoding="utf-8",
        )
        return {
            "status": "done",
            "changes": ["tests/test_good_module.py"],
        }

    def review(self, **kwargs: object) -> dict[str, object]:
        del kwargs
        self.review_calls += 1
        if self.review_calls == 1:
            return {
                "approved": False,
                "summary": "The test does not assert the exact payload shape.",
                "must_fix": ["Assert the exact allowed key set in the happy-path test."],
                "should_fix": [],
                "blocking_items": ["Exact key-set assertion is missing."],
                "severity": "medium",
                "score": 88,
                "target_score": 95,
                "min_dimension_score": 90,
                "gate_recommendation": "REVIEW_FIX_REQUIRED",
                "reviewer": "opus",
            }
        return {
            "approved": True,
            "summary": "The fix round addressed the task-local review item.",
            "must_fix": [],
            "should_fix": [],
            "blocking_items": [],
            "severity": "low",
            "score": 96,
            "target_score": 95,
            "min_dimension_score": 90,
            "gate_recommendation": "PROCEED_TO_GATE",
            "reviewer": "opus",
        }


class _ExecutorStallThenRecoveryAdapter:
    def __init__(self) -> None:
        self.calls = 0
        self.contexts: list[dict[str, object]] = []
        self.recovery_calls = 0

    def implement(self, task: str, context: dict[str, object]) -> dict[str, object]:
        del task
        self.calls += 1
        self.contexts.append(dict(context))
        if self.calls == 1:
            return {
                "status": "blocked",
                "backend": "openai_tool_use",
                "error_code": "EXECUTOR_STALLED_NO_WRITE_PROGRESS",
                "blocking_reason": "executor made no write progress",
                "execution_result": {"error_code": "EXECUTOR_STALLED_NO_WRITE_PROGRESS"},
            }
        return {
            "status": "done",
            "changes": ["src/good_module.py", "tests/test_good_module.py"],
        }

    def synthesize_executor_recovery(
        self,
        *,
        task: str,
        context: dict[str, object],
        must_fix: list[str],
        stall_report: dict[str, object] | None = None,
    ) -> dict[str, object]:
        del task, context, stall_report
        self.recovery_calls += 1
        return {
            "status": "ok",
            "role": "recovery_synthesizer",
            "source": "test",
            "backend": "configurable_backend",
            "model": "swap-any-model",
            "decision": {
                "schema_version": "execution.recovery_decision.v1",
                "action": "narrow_patch_plan",
                "reason": "turn stall into a narrow retry card",
                "patch_plan": [
                    {
                        "id": "p1",
                        "operation": "write_new_file",
                        "path": "src/good_module.py",
                        "content": "def good_branch(x):\n    return x\n",
                    }
                ],
                "must_fix": list(must_fix),
            },
        }

    def review(self, **kwargs: object) -> dict[str, object]:
        del kwargs
        return {
            "approved": True,
            "summary": "Recovered implementation is acceptable.",
            "must_fix": [],
            "should_fix": [],
            "blocking_items": [],
            "severity": "low",
            "score": 95,
            "target_score": 90,
            "min_dimension_score": 80,
            "gate_recommendation": "PROCEED_TO_GATE",
        }


class _NoWriteResumeAdapter:
    def __init__(self) -> None:
        self.calls = 0
        self.recovery_calls = 0

    def implement(self, task: str, context: dict[str, object]) -> dict[str, object]:
        del task, context
        self.calls += 1
        return {
            "status": "blocked",
            "backend": "openai_tool_use",
            "error_code": "EXECUTOR_STALLED_NO_WRITE_PROGRESS",
            "blocking_reason": "executor made no write progress",
            "execution_result": {"error_code": "EXECUTOR_STALLED_NO_WRITE_PROGRESS"},
        }

    def synthesize_executor_recovery(
        self,
        *,
        task: str,
        context: dict[str, object],
        must_fix: list[str],
        stall_report: dict[str, object] | None = None,
    ) -> dict[str, object]:
        del task, context, must_fix, stall_report
        self.recovery_calls += 1
        return {"status": "unavailable"}

    def review(self, **kwargs: object) -> dict[str, object]:
        del kwargs
        return {
            "approved": True,
            "summary": "Existing task-scope artifacts pass scoped verify.",
            "must_fix": [],
            "should_fix": [],
            "blocking_items": [],
            "severity": "low",
            "score": 95,
            "target_score": 90,
            "min_dimension_score": 80,
            "gate_recommendation": "PROCEED_TO_GATE",
        }


class _ExecutorStallThenScopeExpansionAdapter(_ExecutorStallThenRecoveryAdapter):
    def synthesize_executor_recovery(
        self,
        *,
        task: str,
        context: dict[str, object],
        must_fix: list[str],
        stall_report: dict[str, object] | None = None,
    ) -> dict[str, object]:
        del task, context, stall_report
        self.recovery_calls += 1
        return {
            "status": "ok",
            "role": "recovery_synthesizer",
            "source": "test",
            "backend": "configurable_backend",
            "model": "swap-any-model",
            "decision": {
                "schema_version": "execution.recovery_decision.v1",
                "action": "expand_scope_request",
                "reason": "router contract owns the verifier failure",
                "requested_files": ["src/router.py"],
                "must_fix": list(must_fix),
            },
        }


class _ExecutorScopeExpansionThenPatchPlanAdapter(_ExecutorStallThenRecoveryAdapter):
    def implement(self, task: str, context: dict[str, object]) -> dict[str, object]:
        del task
        self.calls += 1
        self.contexts.append(dict(context))
        if self.calls <= 2:
            return {
                "status": "blocked",
                "backend": "openai_tool_use",
                "error_code": "EXECUTOR_STALLED_NO_WRITE_PROGRESS",
                "blocking_reason": "executor made no write progress",
                "execution_result": {
                    "error_code": "EXECUTOR_STALLED_NO_WRITE_PROGRESS",
                    "scratch_root": str(Path(context["project_root"]) / ".workflow" / ".executor_scratch" / f"run-{self.calls}"),
                },
            }
        return {
            "status": "done",
            "changes": ["src/good_module.py", "tests/test_good_module.py"],
        }

    def synthesize_executor_recovery(
        self,
        *,
        task: str,
        context: dict[str, object],
        must_fix: list[str],
        stall_report: dict[str, object] | None = None,
    ) -> dict[str, object]:
        del task, stall_report
        self.recovery_calls += 1
        if self.recovery_calls == 1:
            return {
                "status": "ok",
                "role": "recovery_synthesizer",
                "source": "test",
                "backend": "configurable_backend",
                "model": "swap-any-model",
                "decision": {
                    "schema_version": "execution.recovery_decision.v1",
                    "action": "expand_scope_request",
                    "reason": "router contract owns the verifier failure",
                    "requested_files": ["src/router.py"],
                    "must_fix": list(must_fix),
                },
            }
        assert "src/router.py" in list(context.get("task_card_files") or [])
        return {
            "status": "ok",
            "role": "recovery_synthesizer",
            "source": "test",
            "backend": "configurable_backend",
            "model": "swap-any-model",
            "decision": {
                "schema_version": "execution.recovery_decision.v1",
                "action": "narrow_patch_plan",
                "reason": "expanded context is enough for a patch",
                "patch_plan": [
                    {
                        "id": "p2",
                        "operation": "str_replace",
                        "path": "src/router.py",
                        "old_text": "old",
                        "new_text": "new",
                    }
                ],
                "must_fix": list(must_fix),
            },
        }


class _ExecutorDifferentFailuresThenSuccessAdapter(_ExecutorStallThenRecoveryAdapter):
    def implement(self, task: str, context: dict[str, object]) -> dict[str, object]:
        del task
        self.calls += 1
        self.contexts.append(dict(context))
        if self.calls == 1:
            return {
                "status": "blocked",
                "backend": "openai_tool_use",
                "error_code": "EXECUTOR_STALLED_NO_WRITE_PROGRESS",
                "blocking_reason": "executor made no write progress",
                "execution_result": {"error_code": "EXECUTOR_STALLED_NO_WRITE_PROGRESS"},
            }
        if self.calls == 2:
            return {
                "status": "blocked",
                "backend": "openai_tool_use",
                "error_code": "MAX_TOOL_CALLS_PER_RESPONSE",
                "blocking_reason": "model emitted too many tool calls",
                "execution_result": {"error_code": "MAX_TOOL_CALLS_PER_RESPONSE"},
            }
        if self.calls == 3:
            return {
                "status": "blocked",
                "backend": "openai_tool_use",
                "error_code": "VERIFY_FAILED_RETRYABLE",
                "blocking_reason": (
                    "verify failed: tests/test_good_module.py::test_ok expected PASS after applying "
                    "the recovery patch"
                ),
                "execution_result": {
                    "error_code": "VERIFY_FAILED_RETRYABLE",
                    "blocking_reason": "tests/test_good_module.py::test_ok failed",
                },
            }
        return {
            "status": "done",
            "changes": ["src/good_module.py", "tests/test_good_module.py"],
        }

    def synthesize_executor_recovery(
        self,
        *,
        task: str,
        context: dict[str, object],
        must_fix: list[str],
        stall_report: dict[str, object] | None = None,
    ) -> dict[str, object]:
        del task, context, stall_report
        self.recovery_calls += 1
        return {
            "status": "ok",
            "role": "recovery_synthesizer",
            "source": "test",
            "backend": "configurable_backend",
            "model": "swap-any-model",
            "decision": {
                "schema_version": "execution.recovery_decision.v1",
                "action": "narrow_patch_plan",
                "reason": f"recover distinct failure {self.recovery_calls}",
                "patch_plan": [
                    {
                        "id": f"p{self.recovery_calls}",
                        "operation": "write_new_file",
                        "path": "src/good_module.py",
                        "content": "def good_branch(x):\n    return x\n",
                    }
                ],
                "must_fix": list(must_fix),
            },
        }


class _RealReviewFallbackAdapter:
    def implement(self, task: str, context: dict[str, object]) -> dict[str, object]:
        del task, context
        return {
            "status": "done",
            "changes": ["src/good_module.py", "tests/test_good_module.py"],
        }

    def review(self, **kwargs: object) -> dict[str, object]:
        del kwargs
        return {
            "approved": True,
            "summary": "Real reviewer timeout fallback accepted.",
            "must_fix": [],
            "should_fix": [],
            "blocking_items": [],
            "severity": "low",
            "score": 99,
            "target_score": 95,
            "min_dimension_score": 80,
            "gate_recommendation": "PROCEED_TO_GATE",
            "reviewer": "opus",
            "source": "kodawari",
            "review_runtime": {
                "mode": "simulate_local",
                "real_requested": True,
                "real_required": False,
                "fallback_used": True,
                "error": {"message": "reviewer timeout"},
            },
        }


def _write_runtime_gate_fixture_files(tmp_path: Path) -> None:
    bad_file = tmp_path / "src" / "bad_module.py"
    bad_file.parent.mkdir(parents=True, exist_ok=True)
    bad_file.write_text(
        "\n".join(
            [
                "def complex_branch(x):",
                "    score = 0",
                "    if x > 0:",
                "        score += 1",
                "    if x > 1:",
                "        score += 1",
                "    if x > 2:",
                "        score += 1",
                "    if x > 3:",
                "        score += 1",
                "    if x > 4:",
                "        score += 1",
                "    if x > 5:",
                "        score += 1",
                "    if x > 6:",
                "        score += 1",
                "    if x > 7:",
                "        score += 1",
                "    if x > 8:",
                "        score += 1",
                "    if x > 9:",
                "        score += 1",
                "    if x > 10:",
                "        score += 1",
                "    return score",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "src" / "good_module.py").write_text(
        "def good_branch(x):\n    return x\n",
        encoding="utf-8",
    )
    test_file = tmp_path / "tests" / "test_good_module.py"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text("def test_ok():\n    assert True\n", encoding="utf-8")


@pytest.fixture
def engine(tmp_path: Path) -> AutopilotEngine:
    config = AutopilotConfig(
        project_root=tmp_path,
        feature="newsapp",
        verify_setup_recovery_max_attempts=2,
        verify_setup_cleanup_strategy="aggressive",
        verify_setup_recovery_retry_interval_seconds=5,
        verify_setup_recovery_fallback_strategy=False,
    )
    return AutopilotEngine(
        config,
        requirements_text="ranking logic with score normalization",
        pattern_registry=_DummyPatternRegistry(),
    )


def _assert_loop_runtime_semantics(result: dict[str, object]) -> None:
    assert result["merged_absorption_status"] == result["pre_compact"]["merged_absorption_status"]
    loop_outcome = result["loop_outcome"]
    assert loop_outcome["reason"] == "PROCEED_TO_GATE"
    assert loop_outcome["stop_reason"] == "PASS"
    assert loop_outcome["blocked"] is False
    assert loop_outcome["round_outcome"] == "ready_for_gate"
    assert loop_outcome["exit_category"] == "pass"
    assert loop_outcome["blocking_reason"] == ""
    assert loop_outcome["must_fix_remaining"] == 0

    runtime_semantics = result["runtime_semantics"]
    assert runtime_semantics["peer_review"]["approved"] is True
    assert runtime_semantics["peer_review"]["review_count"] >= 1
    assert runtime_semantics["peer_review"]["mode"] == "simulate_local"
    assert runtime_semantics["peer_review"]["source"] == "kodawari"
    assert runtime_semantics["peer_review"]["real_requested"] is False
    assert runtime_semantics["peer_review"]["fallback_used"] is False
    assert runtime_semantics["self_review"]["count"] == len(result["codex_self_reviews"])
    assert runtime_semantics["self_review"]["count"] == result["self_review_summary"]["review_count"]
    assert runtime_semantics["self_review"]["approved_count"] == result["self_review_summary"]["approved_count"]
    assert runtime_semantics["self_review"]["reviewers"] == result["self_review_summary"]["reviewers"]
    assert runtime_semantics["verify"]["status"] == "PASS"
    assert runtime_semantics["verify"]["passed"] is True
    assert runtime_semantics["gate"]["status"] == "PASS"
    assert runtime_semantics["gate"]["passed"] is True
    assert runtime_semantics["gate"]["profile"] == "blocking"
    assert runtime_semantics["compact_runtime"]["status"] == "partial"
    assert runtime_semantics["compact_runtime"]["mode"] == "compat"
    assert runtime_semantics["compact_runtime"]["instincts_loaded"] is False
    assert runtime_semantics["compact_runtime"]["merged_absorption_status"] == result["merged_absorption_status"]


def test_loop_runtime_pass_clears_stale_blocking_state() -> None:
    payload = {
        "reason": "PROCEED_TO_GATE",
        "unified_status": {
            "stop_reason": "",
            "is_blocked": True,
            "blocking_reason": "stale patch failure",
        },
        "must_fix_open_items": [],
    }

    result = merge_loop_result_optionals(payload, last_error="stale patch failure")

    assert result["loop_outcome"]["stop_reason"] == "PASS"
    assert result["loop_outcome"]["blocked"] is False
    assert result["loop_outcome"]["blocking_reason"] == ""


def test_generate_execution_plan_contains_main_stages(engine: AutopilotEngine) -> None:
    plan = engine.generate_execution_plan()

    assert [stage["name"] for stage in plan.stages] == [
        ExecutionPhase.PLAN_REVIEW.value,
        ExecutionPhase.IMPLEMENT.value,
        ExecutionPhase.VERIFY.value,
        ExecutionPhase.GATE.value,
    ]
    assert plan.estimated_cycles > 0
    assert plan.estimated_tokens > 0


def test_build_implementation_context_includes_pattern_hints_and_decisions(
    engine: AutopilotEngine,
) -> None:
    engine.state.current_stage = Stage.IMPLEMENT
    (engine.config.project_root / "module_ownership.yaml").write_text(
        json.dumps(
            {
                "modules": {
                    "scoring_service": {
                        "owner": "backend",
                        "path": "app/scoring_service.py",
                        "public_api": ["calculate_rank"],
                        "description": "Ranking rules",
                        "forbidden_imports": ["app.routes.*"],
                        "canonical_for": ["ranking rules"],
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    engine._task_card_payload = {"files_to_change": ["app/scoring_service.py"], "invariants": ["single source of truth"]}

    context = engine._build_implementation_context(
        "T010: Implement ranking rules",
        "ranking logic for recommendation sort order",
    )

    assert context["task_id"] == "T010"
    assert context["current_stage"] == Stage.IMPLEMENT.value
    assert context["pattern_hints"][0]["pattern_id"] == "ranking-rules"
    assert context["reasoning_tier"] in {"economy", "standard", "deep_reasoning"}
    assert context["effort_profile"]["schema_version"] == "effort.scoring.v1"
    assert isinstance(context["effort_profile"]["score"], int)
    assert context["ownership_context"][0]["module"] == "scoring_service"
    assert "calculate_rank" in context["ownership_hints"][0]


def test_build_implementation_context_includes_planning_reviewer_warnings(
    engine: AutopilotEngine,
) -> None:
    engine.state.current_stage = Stage.IMPLEMENT
    engine._task_card_payload = {"files_to_change": ["app/scoring_service.py"], "invariants": []}
    engine._planning_conversation_payload = {
        "status": "auto_skipped",
        "escalation": {
            "unresolved_findings": [
                {
                    "severity": "high",
                    "category": "scope",
                    "description": "missing route coverage",
                    "recommendation": "add route tests before touching service code",
                }
            ]
        },
    }

    context = engine._build_implementation_context(
        "T011: Apply planning warning",
        "touch scoring service implementation only",
    )

    assert any("missing route coverage" in item for item in context["scope_risk_warnings"])


def test_build_implementation_context_prefers_task_card_verify_over_default(
    engine: AutopilotEngine,
) -> None:
    engine._task_card_payload = {
        "files_to_change": ["app/scoring_service.py"],
        "invariants": [],
        "verify_cmd": "python -m pytest tests/test_scoring_service.py -q",
    }

    context = engine._build_implementation_context("T012: Verify scoring", "scoped verification")

    assert context["verify_cmd"] == "python -m pytest tests/test_scoring_service.py -q"


def test_build_implementation_context_task_card_verify_overrides_global_default(
    engine: AutopilotEngine,
) -> None:
    engine.config.verify_cmd = "python -m pytest tests/test_override.py -q"
    engine._task_card_payload = {
        "files_to_change": ["app/scoring_service.py"],
        "invariants": [],
        "verify_cmd": "python -m pytest tests/test_scoring_service.py -q",
    }

    context = engine._build_implementation_context("T013: Verify override", "explicit verification")

    assert context["verify_cmd"] == "python -m pytest tests/test_scoring_service.py -q"


def test_build_implementation_context_can_force_global_verify(
    engine: AutopilotEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WORKFLOW_FORCE_GLOBAL_VERIFY", "1")
    engine.config.verify_cmd = "python -m pytest tests/test_override.py -q"
    engine._task_card_payload = {
        "files_to_change": ["app/scoring_service.py"],
        "invariants": [],
        "verify_cmd": "python -m pytest tests/test_scoring_service.py -q",
    }

    context = engine._build_implementation_context("T013: Verify override", "explicit verification")

    assert context["verify_cmd"] == "python -m pytest tests/test_override.py -q"


def test_task_cycle_verify_prefers_active_task_card_command() -> None:
    engine = SimpleNamespace(config=SimpleNamespace(verify_cmd="python -m pytest tests/broad.py -q"))
    task_card = {"verify_cmd": "python -m pytest tests/test_scoring_service.py -q"}

    _apply_task_cycle_verify_cmd(engine, task_card)

    assert engine.config.verify_cmd == "python -m pytest tests/test_scoring_service.py -q"


def test_review_context_uses_runtime_execution_allowed_files(engine: AutopilotEngine) -> None:
    review_context = {
        "task_card_files": ["src/service.py"],
        "task_card": {"files_to_change": ["src/service.py"]},
    }
    execution_result = {
        "tool_manifest": {
            "allowed_files": [
                "src/service.py",
                "src/router.py",
                "tests/test_contract.py",
            ]
        }
    }

    engine._apply_runtime_execution_scope_to_review_context(review_context, execution_result)

    assert review_context["task_card_files"] == [
        "src/service.py",
        "src/router.py",
        "tests/test_contract.py",
    ]
    assert review_context["task_card"]["files_to_change"] == review_context["task_card_files"]
    assert review_context["task_card"]["recovery"]["scope_source"] == "execution_tool_manifest.allowed_files"


def test_review_context_counts_runtime_read_only_tests_as_review_scope(engine: AutopilotEngine) -> None:
    review_context = {
        "task_card_files": ["src/service.py", "tests/test_service.py"],
        "task_card": {
            "files_to_change": ["src/service.py", "tests/test_service.py"],
            "related_existing_tests": ["tests/test_service.py"],
        },
    }
    execution_result = {
        "tool_manifest": {
            "allowed_files": ["src/service.py"],
            "read_only_files": ["tests/test_service.py", "docs/service.md"],
        }
    }

    engine._apply_runtime_execution_scope_to_review_context(review_context, execution_result)

    assert review_context["runtime_execution_scope_files"] == ["src/service.py"]
    assert review_context["runtime_review_scope_files"] == ["src/service.py", "tests/test_service.py"]
    assert review_context["task_card_files"] == ["src/service.py", "tests/test_service.py"]
    assert review_context["task_card"]["files_to_change"] == ["src/service.py", "tests/test_service.py"]
    assert review_context["task_card"]["related_existing_tests"] == ["tests/test_service.py"]


def test_review_context_keeps_original_scope_when_recovery_execution_is_narrower(engine: AutopilotEngine) -> None:
    engine._task_card_payload = {"files_to_change": ["src/service.py", "tests/test_service.py"]}
    review_context = {
        "task_card_files": ["tests/test_service.py"],
        "task_card": {
            "files_to_change": ["tests/test_service.py"],
            "recovery": {"source_action": "gate_complexity_refactor"},
        },
    }
    execution_result = {"tool_manifest": {"allowed_files": ["tests/test_service.py"], "read_only_files": []}}

    engine._apply_runtime_execution_scope_to_review_context(review_context, execution_result)

    assert review_context["runtime_execution_scope_files"] == ["tests/test_service.py"]
    assert review_context["runtime_review_scope_files"] == ["src/service.py", "tests/test_service.py"]
    assert review_context["task_card_files"] == ["src/service.py", "tests/test_service.py"]
    assert review_context["task_card"]["files_to_change"] == ["src/service.py", "tests/test_service.py"]


def test_execution_verify_failure_ingests_prompt_lessons(engine: AutopilotEngine) -> None:
    engine.config.executor_model = "mimo-v2.5-pro"
    engine.config.executor_backend = "openai_tool_use"
    engine.state.run_id = "verify-run-1"
    engine._task_card_payload = {"allowed_test_mutations": []}
    runtime = SimpleNamespace(task_id="T072")
    verify_output = """\
================================== FAILURES ===================================
_______________________ test_audio_status _______________________

tests/test_audio.py:42: in test_audio_status
    assert resp.status_code == 503
E   assert 200 == 503

========================== short test summary info ============================
FAILED tests/test_audio.py::test_audio_status
"""
    result = {
        "execution_result": {
            "error_code": "VERIFY_FAILED_RETRYABLE",
            "verify_summary": {"stdout_excerpt": verify_output},
        }
    }

    first = engine._ingest_execution_backend_prompt_lessons(
        runtime=runtime,
        result=result,
        backend_error_code="VERIFY_FAILED_RETRYABLE",
    )
    engine.state.run_id = "verify-run-2"
    second = engine._ingest_execution_backend_prompt_lessons(
        runtime=runtime,
        result=result,
        backend_error_code="VERIFY_FAILED_RETRYABLE",
    )

    assert first["processed"] == 2
    assert second["promoted"] == 2
    planner_text = render_prompt_lessons_for_prompt(engine.config.project_root, role="planner", family_candidates=["default"])
    executor_text = render_prompt_lessons_for_prompt(engine.config.project_root, role="executor", family_candidates=["mimo", "default"])
    assert "list every affected legacy test" in planner_text
    assert "patch the exact stale assertions" in executor_text


def test_executor_stall_recovery_ingests_prompt_lesson(engine: AutopilotEngine) -> None:
    engine.config.executor_model = "mimo-v2.5-pro"
    runtime = SimpleNamespace(task_id="T072")
    stall_report = {
        "error_code": "EXECUTOR_STALLED_NO_WRITE_PROGRESS",
        "counters": {"no_write_iterations": 13},
    }

    engine.state.run_id = "stall-run-1"
    first = engine._ingest_executor_stall_prompt_lessons(
        runtime=runtime,
        stall_report=stall_report,
        decision={"action": "escalate_to_human"},
    )
    engine.state.run_id = "stall-run-2"
    second = engine._ingest_executor_stall_prompt_lessons(
        runtime=runtime,
        stall_report=stall_report,
        decision={"action": "escalate_to_human"},
    )

    assert first["processed"] == 1
    assert second["promoted"] == 1
    text = render_prompt_lessons_for_prompt(engine.config.project_root, role="executor", family_candidates=["mimo", "default"])
    assert "After recovery expands write scope" in text


def test_verify_setup_recovery_uses_config_and_updates_state(engine: AutopilotEngine) -> None:
    result = engine._recover_verify_setup_error("fixture not found")

    assert result["attempted"] is True
    assert result["recovered"] is True
    assert result["cleanup_strategy"] == "aggressive"
    assert result["retry_interval_seconds"] == 5
    assert result["fallback_strategy"] is False
    assert engine.state.verify_setup_recovery_attempted == 1
    assert engine.state.verify_setup_recovery_succeeded == 1
    assert engine.state.verify_setup_recovery_last_error == "fixture not found"


def test_verify_failed_backend_block_can_use_executor_recovery(engine: AutopilotEngine) -> None:
    assert engine._recoverable_executor_block("EXECUTOR_STALLED_FRAGMENTED_READS") is True
    assert engine._recoverable_executor_block("VERIFY_FAILED_RETRYABLE") is True
    assert engine._recoverable_executor_block("VERIFY_FAILED") is True
    assert engine._recoverable_executor_block("MAX_SAME_TOOL_CALLS_PER_PATH") is True
    assert engine._recoverable_executor_block("MAX_TOOL_CALLS_PER_RESPONSE") is True
    assert engine._recoverable_executor_block("PATCH_PRECONDITION_MISMATCH") is True
    assert engine._recoverable_executor_block("PATCH_PLAN_PARTIAL_VERIFY_FAILED") is True


def test_engine_resumes_pending_executor_recovery_card_after_retryable_block(tmp_path: Path) -> None:
    planning_dir = tmp_path / "planning" / "newsapp"
    planning_dir.mkdir(parents=True)
    active_card = {
        "schema_version": "contract_first.task_card.v1",
        "task_id": "T02",
        "task_name": "Original task",
        "files_to_change": ["src/good_module.py"],
        "invariants": [],
    }
    recovery_card = {
        "schema_version": "contract_first.task_card.v1",
        "task_id": "T02_RECOVERY",
        "task_name": "Recovery for T02",
        "files_to_change": ["src/good_module.py"],
        "invariants": [],
        "recovery": {"schema_version": "execution.recovery_card.v1", "source_action": "narrow_patch_plan"},
        "patch_plan": [
            {
                "id": "p1",
                "operation": "str_replace",
                "path": "src/good_module.py",
                "old_text": "old",
                "new_text": "new",
            }
        ],
    }
    (planning_dir / "TASK_CARD_ACTIVE.json").write_text(json.dumps(active_card), encoding="utf-8")
    (planning_dir / ".execution_recovery_card.json").write_text(json.dumps(recovery_card), encoding="utf-8")
    (planning_dir / ".execution_result.json").write_text(
        json.dumps({"schema_version": "execution.result.v1", "status": "BLOCKED", "error_code": "VERIFY_FAILED_RETRYABLE"}),
        encoding="utf-8",
    )

    engine = AutopilotEngine(
        AutopilotConfig(project_root=tmp_path, feature="newsapp"),
        pattern_registry=_DummyPatternRegistry(),
    )

    assert engine._task_card_payload["task_id"] == "T02_RECOVERY"
    assert engine._task_card_payload["patch_plan"][0]["id"] == "p1"


def test_engine_does_not_resume_exhausted_executor_recovery_card(tmp_path: Path) -> None:
    planning_dir = tmp_path / "planning" / "newsapp"
    planning_dir.mkdir(parents=True)
    active_card = {
        "schema_version": "contract_first.task_card.v1",
        "task_id": "T02",
        "task_name": "Original task",
        "files_to_change": ["src/good_module.py"],
        "invariants": [],
    }
    recovery_card = {
        "schema_version": "contract_first.task_card.v1",
        "task_id": "T02_RECOVERY",
        "task_name": "Recovery for T02",
        "files_to_change": ["src/good_module.py"],
        "invariants": [],
        "recovery": {"schema_version": "execution.recovery_card.v1", "source_action": "narrow_patch_plan"},
        "patch_plan": [
            {
                "id": "p1",
                "operation": "str_replace",
                "path": "src/good_module.py",
                "old_text": "old",
                "new_text": "new",
            }
        ],
    }
    (planning_dir / "TASK_CARD_ACTIVE.json").write_text(json.dumps(active_card), encoding="utf-8")
    (planning_dir / ".execution_recovery_card.json").write_text(json.dumps(recovery_card), encoding="utf-8")
    (planning_dir / ".execution_result.json").write_text(
        json.dumps({"schema_version": "execution.result.v1", "status": "BLOCKED", "error_code": "VERIFY_FAILED_RETRYABLE"}),
        encoding="utf-8",
    )
    state = AutopilotState(feature="newsapp", project_root=tmp_path)
    state.last_stage_status = "executor_recovery_exhausted"
    state.last_error = "Executor recovery attempts exhausted (2/2) for failure signature T02:VERIFY_FAILED_RETRYABLE:abc"

    engine = AutopilotEngine(
        AutopilotConfig(project_root=tmp_path, feature="newsapp"),
        pattern_registry=_DummyPatternRegistry(),
        state=state,
    )

    assert engine._task_card_payload["task_id"] == "T02"
    assert "patch_plan" not in engine._task_card_payload


def test_engine_preserves_pending_executor_recovery_card_after_lock_busy(tmp_path: Path) -> None:
    planning_dir = tmp_path / "planning" / "newsapp"
    planning_dir.mkdir(parents=True)
    active_card = {
        "schema_version": "contract_first.task_card.v1",
        "task_id": "T02",
        "task_name": "Original task",
        "files_to_change": ["src/good_module.py"],
        "invariants": [],
    }
    recovery_card = {
        "schema_version": "contract_first.task_card.v1",
        "task_id": "T02_RECOVERY",
        "task_name": "Recovery for T02",
        "files_to_change": ["src/good_module.py"],
        "invariants": [],
        "recovery": {"schema_version": "execution.recovery_card.v1", "source_action": "narrow_patch_plan"},
        "patch_plan": [
            {"id": "p1", "operation": "str_replace", "path": "src/good_module.py", "old_text": "old", "new_text": "new"}
        ],
    }
    (planning_dir / "TASK_CARD_ACTIVE.json").write_text(json.dumps(active_card), encoding="utf-8")
    (planning_dir / ".execution_recovery_card.json").write_text(json.dumps(recovery_card), encoding="utf-8")
    (planning_dir / ".execution_result.json").write_text(
        json.dumps({"schema_version": "execution.result.v1", "status": "BLOCKED", "error_code": "EXECUTION_RUN_LOCK_BUSY"}),
        encoding="utf-8",
    )

    engine = AutopilotEngine(
        AutopilotConfig(project_root=tmp_path, feature="newsapp"),
        pattern_registry=_DummyPatternRegistry(),
    )

    assert engine._task_card_payload["task_id"] == "T02_RECOVERY"


def test_engine_resume_recovery_card_records_prior_scratch_workspace(tmp_path: Path) -> None:
    planning_dir = tmp_path / "planning" / "newsapp"
    planning_dir.mkdir(parents=True)
    scratch_root = tmp_path / ".workflow" / ".executor_scratch" / "run-1"
    workspace = scratch_root / "workspace"
    workspace.mkdir(parents=True)
    active_card = {
        "schema_version": "contract_first.task_card.v1",
        "task_id": "T02",
        "task_name": "Original task",
        "files_to_change": ["src/good_module.py"],
        "invariants": [],
    }
    recovery_card = {
        "schema_version": "contract_first.task_card.v1",
        "task_id": "T02_RECOVERY",
        "task_name": "Recovery for T02",
        "files_to_change": ["src/good_module.py"],
        "invariants": [],
        "recovery": {"schema_version": "execution.recovery_card.v1", "source_action": "narrow_patch_plan"},
        "patch_plan": [
            {"id": "p1", "operation": "str_replace", "path": "src/good_module.py", "old_text": "old", "new_text": "new"}
        ],
    }
    (planning_dir / "TASK_CARD_ACTIVE.json").write_text(json.dumps(active_card), encoding="utf-8")
    (planning_dir / ".execution_recovery_card.json").write_text(json.dumps(recovery_card), encoding="utf-8")
    (planning_dir / ".execution_result.json").write_text(
        json.dumps(
            {
                "schema_version": "execution.result.v1",
                "status": "BLOCKED",
                "error_code": "PATCH_PRECONDITION_MISMATCH",
                "scratch_root": str(scratch_root),
            }
        ),
        encoding="utf-8",
    )

    engine = AutopilotEngine(
        AutopilotConfig(project_root=tmp_path, feature="newsapp"),
        pattern_registry=_DummyPatternRegistry(),
    )

    assert engine._task_card_payload["task_id"] == "T02_RECOVERY"
    assert engine._task_card_payload["recovery"]["base_workspace_path"] == str(workspace.resolve())


def test_engine_resume_recovery_card_overwrites_stale_base_workspace(tmp_path: Path) -> None:
    planning_dir = tmp_path / "planning" / "newsapp"
    planning_dir.mkdir(parents=True)
    stale_workspace = tmp_path / ".workflow" / ".executor_scratch" / "run-stale" / "workspace"
    latest_scratch_root = tmp_path / ".workflow" / ".executor_scratch" / "run-latest"
    latest_workspace = latest_scratch_root / "workspace"
    stale_workspace.mkdir(parents=True)
    latest_workspace.mkdir(parents=True)
    active_card = {
        "schema_version": "contract_first.task_card.v1",
        "task_id": "T02",
        "task_name": "Original task",
        "files_to_change": ["src/good_module.py"],
        "invariants": [],
    }
    recovery_card = {
        "schema_version": "contract_first.task_card.v1",
        "task_id": "T02_NO_WRITE_STALL_RECOVERY",
        "task_name": "No-write recovery for T02",
        "files_to_change": ["src/good_module.py"],
        "invariants": [],
        "recovery": {
            "schema_version": "execution.recovery_card.v1",
            "source_action": "executor_no_write_stall_retry",
            "base_workspace_path": str(stale_workspace),
        },
    }
    (planning_dir / "TASK_CARD_ACTIVE.json").write_text(json.dumps(active_card), encoding="utf-8")
    (planning_dir / ".execution_recovery_card.json").write_text(json.dumps(recovery_card), encoding="utf-8")
    (planning_dir / ".execution_result.json").write_text(
        json.dumps(
            {
                "schema_version": "execution.result.v1",
                "status": "BLOCKED",
                "error_code": "EXECUTOR_STALLED_NO_WRITE_PROGRESS",
                "scratch_root": str(latest_scratch_root),
            }
        ),
        encoding="utf-8",
    )

    engine = AutopilotEngine(
        AutopilotConfig(project_root=tmp_path, feature="newsapp"),
        pattern_registry=_DummyPatternRegistry(),
    )

    assert engine._task_card_payload["task_id"] == "T02_NO_WRITE_STALL_RECOVERY"
    assert engine._task_card_payload["recovery"]["base_workspace_path"] == str(latest_workspace.resolve())


def test_engine_resume_recovery_card_clears_stale_base_when_project_scope_is_newer(tmp_path: Path) -> None:
    planning_dir = tmp_path / "planning" / "newsapp"
    planning_dir.mkdir(parents=True)
    stale_workspace = tmp_path / ".workflow" / ".executor_scratch" / "run-stale" / "workspace"
    stale_file = stale_workspace / "src" / "good_module.py"
    project_file = tmp_path / "src" / "good_module.py"
    stale_file.parent.mkdir(parents=True)
    project_file.parent.mkdir(parents=True, exist_ok=True)
    stale_file.write_text("old scratch\n", encoding="utf-8")
    project_file.write_text("manual root repair\n", encoding="utf-8")
    os.utime(stale_file, (1, 1))
    os.utime(project_file, (2, 2))
    active_card = {
        "schema_version": "contract_first.task_card.v1",
        "task_id": "T02",
        "task_name": "Original task",
        "files_to_change": ["src/good_module.py"],
        "invariants": [],
    }
    recovery_card = {
        "schema_version": "contract_first.task_card.v1",
        "task_id": "T02_NO_WRITE_STALL_RECOVERY",
        "task_name": "No-write recovery for T02",
        "files_to_change": ["src/good_module.py"],
        "invariants": [],
        "recovery": {
            "schema_version": "execution.recovery_card.v1",
            "source_action": "executor_no_write_stall_retry",
            "base_workspace_path": str(stale_workspace),
        },
    }
    (planning_dir / "TASK_CARD_ACTIVE.json").write_text(json.dumps(active_card), encoding="utf-8")
    (planning_dir / ".execution_recovery_card.json").write_text(json.dumps(recovery_card), encoding="utf-8")
    (planning_dir / ".execution_result.json").write_text(
        json.dumps(
            {
                "schema_version": "execution.result.v1",
                "status": "BLOCKED",
                "error_code": "EXECUTOR_STALLED_NO_WRITE_PROGRESS",
                "scratch_root": str(stale_workspace.parent),
            }
        ),
        encoding="utf-8",
    )

    engine = AutopilotEngine(
        AutopilotConfig(project_root=tmp_path, feature="newsapp"),
        pattern_registry=_DummyPatternRegistry(),
    )

    assert engine._task_card_payload["task_id"] == "T02_NO_WRITE_STALL_RECOVERY"
    assert "base_workspace_path" not in engine._task_card_payload["recovery"]


def test_engine_resumes_pending_executor_recovery_card_without_patch_plan_after_retryable_block(tmp_path: Path) -> None:
    planning_dir = tmp_path / "planning" / "newsapp"
    planning_dir.mkdir(parents=True)
    scratch_root = tmp_path / ".workflow" / ".executor_scratch" / "run-no-write"
    workspace = scratch_root / "workspace"
    workspace.mkdir(parents=True)
    active_card = {
        "schema_version": "contract_first.task_card.v1",
        "task_id": "T02",
        "task_name": "Original task",
        "files_to_change": ["src/good_module.py"],
        "new_files": ["src/good_module.py"],
        "invariants": [],
    }
    recovery_card = {
        "schema_version": "contract_first.task_card.v1",
        "task_id": "T02_NO_WRITE_STALL_RECOVERY",
        "task_name": "No-write recovery for T02",
        "files_to_change": ["src/good_module.py"],
        "new_files": ["src/good_module.py"],
        "invariants": [],
        "recovery": {
            "schema_version": "execution.recovery_card.v1",
            "source_action": "executor_no_write_stall_retry",
            "must_fix": ["Write the scoped file before another broad read."],
        },
    }
    (planning_dir / "TASK_CARD_ACTIVE.json").write_text(json.dumps(active_card), encoding="utf-8")
    (planning_dir / ".execution_recovery_card.json").write_text(json.dumps(recovery_card), encoding="utf-8")
    (planning_dir / ".execution_result.json").write_text(
        json.dumps(
            {
                "schema_version": "execution.result.v1",
                "status": "BLOCKED",
                "error_code": "EXECUTOR_STALLED_NO_WRITE_PROGRESS",
                "scratch_root": str(scratch_root),
            }
        ),
        encoding="utf-8",
    )

    engine = AutopilotEngine(
        AutopilotConfig(project_root=tmp_path, feature="newsapp"),
        pattern_registry=_DummyPatternRegistry(),
    )

    assert engine._task_card_payload["task_id"] == "T02_NO_WRITE_STALL_RECOVERY"
    assert "patch_plan" not in engine._task_card_payload
    assert engine._task_card_payload["recovery"]["base_workspace_path"] == str(workspace.resolve())


def test_engine_resume_recovery_card_after_runtime_error_with_scratch(tmp_path: Path) -> None:
    planning_dir = tmp_path / "planning" / "newsapp"
    planning_dir.mkdir(parents=True)
    scratch_root = tmp_path / ".workflow" / ".executor_scratch" / "run-runtime-error"
    (scratch_root / "workspace").mkdir(parents=True)
    active_card = {
        "schema_version": "contract_first.task_card.v1",
        "task_id": "T02",
        "task_name": "Original task",
        "files_to_change": ["src/good_module.py"],
        "invariants": [],
    }
    recovery_card = {
        "schema_version": "contract_first.task_card.v1",
        "task_id": "T02_RECOVERY",
        "task_name": "Recovery for T02",
        "files_to_change": ["src/good_module.py"],
        "invariants": [],
        "recovery": {"schema_version": "execution.recovery_card.v1", "source_action": "narrow_patch_plan"},
        "patch_plan": [
            {"id": "p1", "operation": "str_replace", "path": "src/good_module.py", "old_text": "old", "new_text": "new"}
        ],
    }
    (planning_dir / "TASK_CARD_ACTIVE.json").write_text(json.dumps(active_card), encoding="utf-8")
    (planning_dir / ".execution_recovery_card.json").write_text(json.dumps(recovery_card), encoding="utf-8")
    (planning_dir / ".execution_result.json").write_text(
        json.dumps(
            {
                "schema_version": "execution.result.v1",
                "status": "BLOCKED",
                "error_code": "OPENAI_TOOL_USE_ERROR",
                "scratch_root": str(scratch_root),
            }
        ),
        encoding="utf-8",
    )

    engine = AutopilotEngine(
        AutopilotConfig(project_root=tmp_path, feature="newsapp"),
        pattern_registry=_DummyPatternRegistry(),
    )

    assert engine._task_card_payload["task_id"] == "T02_RECOVERY"


def test_executor_recovery_card_records_prior_scratch_workspace(engine: AutopilotEngine) -> None:
    scratch_root = engine.config.project_root / ".workflow" / ".executor_scratch" / "run-1"
    workspace = scratch_root / "workspace"
    workspace.mkdir(parents=True)
    card = {"recovery": {"schema_version": "execution.recovery_card.v1"}}
    runtime = SimpleNamespace(execution_result={"scratch_root": str(scratch_root)})

    engine._attach_recovery_base_workspace(card, runtime)  # type: ignore[arg-type]

    assert card["recovery"]["base_workspace_path"] == str(workspace.resolve())


def test_build_implementation_request_refreshes_active_recovery_base_workspace(engine: AutopilotEngine) -> None:
    stale_workspace = engine.config.project_root / ".workflow" / ".executor_scratch" / "run-stale" / "workspace"
    latest_scratch_root = engine.config.project_root / ".workflow" / ".executor_scratch" / "run-latest"
    latest_workspace = latest_scratch_root / "workspace"
    stale_workspace.mkdir(parents=True)
    latest_workspace.mkdir(parents=True)
    engine._task_card_payload = {
        "schema_version": "contract_first.task_card.v1",
        "task_id": "T02_RECOVERY",
        "task_name": "Recovery for T02",
        "files_to_change": ["src/good_module.py"],
        "invariants": [],
        "recovery": {
            "schema_version": "execution.recovery_card.v1",
            "source_action": "executor_no_write_stall_retry",
            "base_workspace_path": str(stale_workspace),
        },
    }
    context = build_collaboration_context("T02", "T02: recover executor")
    runtime = SimpleNamespace(
        task_label="T02: recover executor",
        task_scope="",
        context=context,
        pre_compact_payload={},
        pending_recovery_card=None,
        execution_result={"scratch_root": str(latest_scratch_root)},
    )

    request = engine._build_implementation_request(runtime, CollaborationAction.CODEX_FIX)  # type: ignore[arg-type]

    assert request["task_card"]["recovery"]["base_workspace_path"] == str(latest_workspace.resolve())


def test_build_implementation_request_clears_stale_base_workspace_when_project_scope_is_newer(
    engine: AutopilotEngine,
) -> None:
    stale_workspace = engine.config.project_root / ".workflow" / ".executor_scratch" / "run-stale" / "workspace"
    stale_file = stale_workspace / "src" / "good_module.py"
    project_file = engine.config.project_root / "src" / "good_module.py"
    stale_file.parent.mkdir(parents=True)
    project_file.parent.mkdir(parents=True, exist_ok=True)
    stale_file.write_text("old scratch\n", encoding="utf-8")
    project_file.write_text("manual root repair\n", encoding="utf-8")
    os.utime(stale_file, (1, 1))
    os.utime(project_file, (2, 2))
    engine._task_card_payload = {
        "schema_version": "contract_first.task_card.v1",
        "task_id": "T02_RECOVERY",
        "task_name": "Recovery for T02",
        "files_to_change": ["src/good_module.py"],
        "invariants": [],
        "recovery": {
            "schema_version": "execution.recovery_card.v1",
            "source_action": "executor_no_write_stall_retry",
            "base_workspace_path": str(stale_workspace),
        },
    }
    context = build_collaboration_context("T02", "T02: recover executor")
    runtime = SimpleNamespace(
        task_label="T02: recover executor",
        task_scope="",
        context=context,
        pre_compact_payload={},
        pending_recovery_card=None,
        execution_result={"scratch_root": str(stale_workspace.parent)},
    )

    request = engine._build_implementation_request(runtime, CollaborationAction.CODEX_FIX)  # type: ignore[arg-type]

    assert "base_workspace_path" not in request["task_card"]["recovery"]


def test_executor_recovery_source_root_prefers_latest_successful_write_workspace(engine: AutopilotEngine) -> None:
    write_scratch_root = engine.config.project_root / ".workflow" / ".executor_scratch" / "run-with-write"
    write_workspace = write_scratch_root / "workspace"
    no_write_scratch_root = engine.config.project_root / ".workflow" / ".executor_scratch" / "run-no-write"
    no_write_workspace = no_write_scratch_root / "workspace"
    write_workspace.mkdir(parents=True)
    no_write_workspace.mkdir(parents=True)
    engine._planning_dir.mkdir(parents=True, exist_ok=True)
    (engine._planning_dir / ".execution_tool_calls.jsonl").write_text(
        json.dumps(
            {
                "schema_version": "execution.tool_call.v1",
                "run_id": "run-with-write",
                "iteration": 4,
                "tool": "str_replace",
                "result": {"ok": True, "path": "src/good_module.py", "changed": True},
            }
        )
        + "\n"
        + json.dumps(
            {
                "schema_version": "execution.tool_call.v1",
                "run_id": "run-no-write",
                "iteration": 5,
                "tool": "search_file",
                "result": {"ok": True, "path": "src/good_module.py"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    runtime = SimpleNamespace(execution_result={"scratch_root": str(no_write_scratch_root)})

    source_root = engine._executor_recovery_source_root(runtime)  # type: ignore[arg-type]

    assert source_root == write_workspace.resolve()


def test_executor_recovery_source_root_prefers_newer_project_scope_over_stale_write_workspace(
    engine: AutopilotEngine,
) -> None:
    write_scratch_root = engine.config.project_root / ".workflow" / ".executor_scratch" / "run-with-write"
    write_workspace = write_scratch_root / "workspace"
    workspace_file = write_workspace / "src" / "good_module.py"
    project_file = engine.config.project_root / "src" / "good_module.py"
    workspace_file.parent.mkdir(parents=True)
    project_file.parent.mkdir(parents=True, exist_ok=True)
    workspace_file.write_text("old scratch\n", encoding="utf-8")
    project_file.write_text("manual project repair\n", encoding="utf-8")
    os.utime(workspace_file, (1, 1))
    os.utime(project_file, (2, 2))
    engine._task_card_payload = {
        "task_id": "T02_RECOVERY",
        "files_to_change": ["src/good_module.py"],
        "recovery": {"schema_version": "execution.recovery_card.v1"},
    }
    engine._planning_dir.mkdir(parents=True, exist_ok=True)
    (engine._planning_dir / ".execution_tool_calls.jsonl").write_text(
        json.dumps(
            {
                "schema_version": "execution.tool_call.v1",
                "run_id": "run-with-write",
                "iteration": 4,
                "tool": "str_replace",
                "result": {"ok": True, "path": "src/good_module.py", "changed": True},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    runtime = SimpleNamespace(execution_result={"scratch_root": str(write_scratch_root)})

    source_root = engine._executor_recovery_source_root(runtime)  # type: ignore[arg-type]

    assert source_root == engine.config.project_root.resolve()


def test_verify_setup_recovery_stops_after_max_attempts(engine: AutopilotEngine) -> None:
    first = engine._recover_verify_setup_error("fixture missing")
    second = engine._recover_verify_setup_error("fixture missing again")
    third = engine._recover_verify_setup_error("still missing")

    assert first["recovered"] is True
    assert second["recovered"] is True
    assert third["recovered"] is False
    assert third["attempts_used"] == 3


def test_protected_files_policy_blocks_critical_warns_noncritical(engine: AutopilotEngine) -> None:
    blocked = engine._check_protected_files(
        ["tests/conftest.py"],
        task_label="T001: fix tests",
        task_scope="",
    )
    assert blocked["blocked"] is True
    assert blocked["critical"] == ["tests/conftest.py"]

    authorized = engine._check_protected_files(
        ["profiles/newsapp.yaml"],
        task_label="T002: update profiles/newsapp.yaml for ranking",
        task_scope="touch profiles/newsapp.yaml only",
    )
    assert authorized["blocked"] is False

    workflow_policy = engine._check_protected_files(
        [".claude/workflow/gate_policy.yaml"],
        task_label="T002b: update .claude/workflow/gate_policy.yaml",
        task_scope="touch .claude/workflow/gate_policy.yaml only",
    )
    assert workflow_policy["blocked"] is True
    assert workflow_policy["critical"] == [".claude/workflow/gate_policy.yaml"]

    warning_only = engine._check_protected_files(
        ["README.md"],
        task_label="T003: improve docs",
        task_scope="",
    )
    assert warning_only["blocked"] is False
    assert warning_only["warning"] == ["README.md"]


def test_protected_files_check_can_be_disabled(engine: AutopilotEngine) -> None:
    engine.config.protected_files_check_enabled = False

    result = engine._check_protected_files(["tests/conftest.py", "README.md"])

    assert result == {"blocked": False, "critical": [], "warning": []}


def test_execute_cycle_respects_max_cycles(engine: AutopilotEngine) -> None:
    engine.config.max_cycles = 2

    assert engine.execute_cycle("T001")["stopped"] is False
    assert engine.execute_cycle("T001")["stopped"] is False

    final = engine.execute_cycle("T001")
    assert final["stopped"] is True
    assert final["reason"] == "MAX_CYCLES_REACHED"


def test_max_cycles_allows_recoverable_executor_fix_round(tmp_path: Path) -> None:
    engine = AutopilotEngine(
        AutopilotConfig(project_root=tmp_path, feature="newsapp", max_cycles=0),
        pattern_registry=_DummyPatternRegistry(),
    )
    runtime = engine._create_loop_runtime(
        task_label="T02: Retry executor recovery",
        task_scope="recoverable executor block",
        max_rounds=2,
    )
    engine._start_loop_session(runtime)
    runtime.context.review_feedback.must_fix = ["Retry with the executor recovery card."]
    runtime.execution_result = {"error_code": "EXECUTOR_STALLED_NO_WRITE_PROGRESS"}
    engine.state.cycle = 1
    round_record = engine._new_round_record(runtime, CollaborationAction.FIX_ROUND)

    result = engine._handle_max_cycles(
        runtime,
        action=CollaborationAction.FIX_ROUND,
        round_record=round_record,
    )

    assert result is None
    assert runtime.round_records == []
    assert round_record["details"]["max_cycles"]["allowed_executor_recovery"] is True
    assert round_record["details"]["max_cycles"]["error_code"] == "EXECUTOR_STALLED_NO_WRITE_PROGRESS"


def test_max_cycles_still_blocks_non_recovery_round(tmp_path: Path) -> None:
    engine = AutopilotEngine(
        AutopilotConfig(project_root=tmp_path, feature="newsapp", max_cycles=0),
        pattern_registry=_DummyPatternRegistry(),
    )
    runtime = engine._create_loop_runtime(
        task_label="T02: Implement original task",
        task_scope="ordinary implementation",
        max_rounds=2,
    )
    engine._start_loop_session(runtime)
    runtime.context.review_feedback.must_fix = ["This must not matter for IMPLEMENT."]
    runtime.execution_result = {"error_code": "EXECUTOR_STALLED_NO_WRITE_PROGRESS"}
    engine.state.cycle = 1
    round_record = engine._new_round_record(runtime, CollaborationAction.IMPLEMENT)

    result = engine._handle_max_cycles(
        runtime,
        action=CollaborationAction.IMPLEMENT,
        round_record=round_record,
    )

    assert result is not None
    assert result["reason"] == "MAX_CYCLES_REACHED"
    assert runtime.round_records[-1]["stage_status"] == "max_cycles"


def test_run_collaboration_loop_reaches_proceed_to_gate_and_tracks_decisions(tmp_path: Path) -> None:
    config = AutopilotConfig(project_root=tmp_path, feature="newsapp", max_cycles=12)
    engine = AutopilotEngine(config, requirements_text="ranking api with scoring")
    stale_review_evidence_path = engine._planning_dir / ".review_evidence.json"
    stale_review_evidence_path.parent.mkdir(parents=True, exist_ok=True)
    stale_review_evidence_path.write_text(
        json.dumps(
            {
                "schema_version": "review.evidence.v1",
                "generated_at": "2026-01-01T00:00:00+00:00",
                "feature": "newsapp",
                "planning_dir": str(engine._planning_dir.resolve()),
                "entrypoint": "kodawari autopilot",
                "status": "FAIL",
                "blocking_reason": "Missing Opus peer-review evidence.",
                "details": "Missing Opus peer-review evidence.",
                "issues": ["Missing Opus peer-review evidence."],
                "checks": {
                    "self_review_count": 0,
                    "peer_review_count": 0,
                    "must_fix_remaining": 0,
                    "required_self_review": False,
                    "required_peer_review": True,
                    "contract_known": True,
                },
                "review_contract": {
                    "execution_backend": "codex_cli",
                    "require_self_review": False,
                    "require_peer_review": True,
                    "contract_known": True,
                },
                "evidence": [
                    {
                        "file": ".task_run_result.json",
                        "rule": "review_evidence.contract",
                        "hit": "backend=codex_cli; require_self_review=False; require_peer_review=True",
                        "confidence": 1.0,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = engine.run_collaboration_loop(
        task_label="T010: Architecture for ranking algorithm API",
        task_scope="implement ranking endpoint with tests",
    )

    assert result["reason"] == "PROCEED_TO_GATE"
    assert engine.state.current_stage == Stage.GATE
    assert engine.state.last_stage_status == "ready_for_gate"
    assert engine.state.architecture_decisions
    assert result["collaboration_context"]["review_feedback"]["approved"] is True
    assert result["pre_compact"]["feature"] == "newsapp"
    assert result["pre_compact"]["merged_absorption_status"] == {
        "planning_summary": "已吸收",
        "context_compact": "部分吸收",
        "instincts": "部分吸收",
    }
    assert result["peer_review_summary"]["approved"] is True
    assert result["peer_review_summary"]["review_round"] >= 1
    assert result["peer_review_summary"]["must_fix_remaining"] == 0
    assert result["codex_self_reviews"]
    assert result["verify_check"]["status"] == "PASS"
    assert result["verify_check"]["verify_cmd"] == "pytest -q"
    assert result["gate_check"]["total_status"] == "PASS"
    assert result["gate_check"]["profile"]["name"] == "blocking"
    assert result["architecture_decisions"]
    assert result["gate_recommendation"] == "PROCEED_TO_GATE"
    assert result["must_fix_open_items"] == []
    _assert_loop_runtime_semantics(result)
    assert result["post_execution_qa"]["status"] == "PASS"
    session_stop = result["hook_events"][-1]
    assert session_stop["event"] == "session_stop"
    assert session_stop["details"]["review_rounds_used"] == result["review_rounds_used"]
    assert session_stop["details"]["must_fix_remaining"] == len(result["must_fix_open_items"])
    assert session_stop["details"]["gate_recommendation"] == result["gate_recommendation"]
    assert all("round_id" in row and "cycle" in row for row in result["rounds"])
    gate_round = next(row for row in result["rounds"] if row["stage"] == "PROCEED_TO_GATE")
    verify_round = next(row for row in result["rounds"] if row["stage"] == "VERIFY")
    rules_gate_round = next(row for row in result["rounds"] if row["stage"] == "RULES_GATE")
    assert verify_round["stage_status"] == "pass"
    assert rules_gate_round["stage_status"] == "pass"
    assert gate_round["details"]["architecture_decision_ids"]
    assert gate_round["details"]["must_fix_remaining"] == 0
    assert gate_round["details"]["gate_recommendation"] == "PROCEED_TO_GATE"
    semantic_runtime = dict(result.get("semantic_compact_runtime") or {})
    assert semantic_runtime["status"] == "written"
    assert semantic_runtime["mode"] == "full"
    semantic_json = Path(semantic_runtime["artifacts"]["semantic_compact.json"])
    assert semantic_json.exists()
    semantic_payload = json.loads(semantic_json.read_text(encoding="utf-8"))
    assert semantic_payload["schema_version"] == "semantic_compact.v1"
    assert semantic_payload["feature"] == "newsapp"
    assert isinstance(semantic_payload["must_fix"], list)
    assert isinstance(semantic_payload["verify_targets"], list)
    review_evidence = json.loads(stale_review_evidence_path.read_text(encoding="utf-8"))
    assert review_evidence["status"] == "PASS"
    assert review_evidence["issues"] == []
    assert review_evidence["checks"]["peer_review_count"] >= 1


def test_run_collaboration_loop_handles_setup_error_and_stops_after_recovery_limit(tmp_path: Path) -> None:
    config = AutopilotConfig(
        project_root=tmp_path,
        feature="newsapp",
        max_cycles=12,
        verify_setup_recovery_max_attempts=1,
    )
    engine = AutopilotEngine(config, adapter=_AlwaysFailSetupAdapter())

    result = engine.run_collaboration_loop(
        task_label="T020: Implement API",
        task_scope="endpoint with fixtures",
    )

    assert result["reason"] == "IMPLEMENTATION_ERROR"
    assert engine.state.verify_setup_recovery_attempted == 2
    assert engine.state.verify_setup_recovery_succeeded == 1
    assert "fixture" in (engine.state.last_error or "")
    assert result["rounds"]
    assert all("assigned_role_after" in row for row in result["rounds"])
    assert result["loop_outcome"]["reason"] == "IMPLEMENTATION_ERROR"
    assert result["loop_outcome"]["stop_reason"] == "HARD_ERROR"
    assert result["loop_outcome"]["blocked"] is True
    assert result["loop_outcome"]["round_outcome"] == "error"
    assert result["loop_outcome"]["exit_category"] == "blocked"
    assert "fixture" in (result["loop_outcome"]["blocking_reason"] or "")
    assert result["runtime_semantics"]["self_review"]["count"] == 0
    assert result["self_review_summary"]["review_count"] == 0


def test_run_collaboration_loop_stops_on_permission_policy_block_at_runtime(
    tmp_path: Path,
) -> None:
    """Phase E regression: a BLOCK-tier path (e.g. config/.env.production)
    reaching the post-execution gate must stop the loop with PROTECTED_FILE_BLOCK,
    even when the planner's files_to_change check did not see it (simulating
    scope drift where the executor writes more than declared)."""
    config = AutopilotConfig(project_root=tmp_path, feature="newsapp", max_cycles=4)
    engine = AutopilotEngine(config, adapter=_PermissionPolicyBlockAdapter())

    result = engine.run_collaboration_loop(
        task_label="T031: Implement backend endpoint",
        task_scope="phase E permission policy runtime block regression",
    )

    assert result["reason"] == "PROTECTED_FILE_BLOCK"
    assert result["loop_outcome"]["blocked"] is True
    assert result["loop_outcome"]["stop_reason"] == "HARD_ERROR"
    # .env.production must appear in the blocking reason so a human knows why
    blocking_reason = str(result["loop_outcome"]["blocking_reason"] or "")
    assert ".env" in blocking_reason
    # Phase E(a) observability: the permission-policy rule that matched must
    # be surfaced (path_glob + reason) so a human can inspect the policy file
    # without grepping source code.
    assert "Blocked by permission policy" in blocking_reason
    assert "matches" in blocking_reason  # rule_path_glob intro phrase


def test_run_collaboration_loop_stops_on_protected_file_block_with_consistent_runtime_semantics(
    tmp_path: Path,
) -> None:
    config = AutopilotConfig(project_root=tmp_path, feature="newsapp", max_cycles=8)
    engine = AutopilotEngine(config, adapter=_ProtectedFileBlockAdapter())

    result = engine.run_collaboration_loop(
        task_label="T030: Implement API endpoint",
        task_scope="minimal blocked semantics regression",
    )

    assert result["reason"] == "PROTECTED_FILE_BLOCK"
    assert result["loop_outcome"]["reason"] == "PROTECTED_FILE_BLOCK"
    assert result["loop_outcome"]["stop_reason"] == "HARD_ERROR"
    assert result["loop_outcome"]["blocked"] is True
    assert result["loop_outcome"]["round_outcome"] == "blocked"
    assert result["loop_outcome"]["exit_category"] == "blocked"
    assert "Blocked protected files" in (result["loop_outcome"]["blocking_reason"] or "")
    assert "Blocked protected files" in (result["unified_status"]["blocking_reason"] or "")
    implement_round = next(row for row in result["rounds"] if row["stage"] == "IMPLEMENT")
    assert implement_round["stage_status"] == "blocked"
    assert implement_round["round_outcome"] == "blocked"
    assert result["runtime_semantics"]["peer_review"]["review_count"] == 0
    assert result["runtime_semantics"]["self_review"]["count"] == 0
    assert result["self_review_summary"]["review_count"] == 0
    assert result["runtime_semantics"]["compact_runtime"]["available"] is True
    session_stop = result["hook_events"][-1]
    assert session_stop["event"] == "session_stop"
    assert session_stop["details"]["reason"] == result["loop_outcome"]["reason"]


def test_run_collaboration_loop_supports_single_pass_mode(tmp_path: Path) -> None:
    config = AutopilotConfig(project_root=tmp_path, feature="newsapp", max_cycles=4)
    engine = AutopilotEngine(config)

    result = engine.run_collaboration_loop(
        task_label="T040: Implement quick fix",
        task_scope="single pass regression",
        enable_peer_review=False,
    )

    assert result["reason"] == "PROCEED_TO_GATE"
    assert result["review_rounds_used"] == 0
    assert result["peer_review_summary"]["skipped"] is True
    assert result["peer_review_summary"]["enabled"] is False
    assert result["verify_check"]["status"] == "PASS"
    assert result["gate_check"]["total_status"] == "PASS"
    assert all(row["stage"] != "PEER_REVIEW" for row in result["rounds"])
    assert [row["stage"] for row in result["rounds"]] == [
        "DESIGN",
        "IMPLEMENT",
        "VERIFY",
        "RULES_GATE",
        "PROCEED_TO_GATE",
    ]


def test_run_collaboration_loop_skips_self_review_for_backend_without_self_review_contract(
    tmp_path: Path,
) -> None:
    config = AutopilotConfig(
        project_root=tmp_path,
        feature="newsapp",
        executor_backend="claude_code",
        max_cycles=8,
    )
    engine = AutopilotEngine(config, adapter=_SimpleSuccessAdapter())

    result = engine.run_collaboration_loop(
        task_label="T042: Prepare schema contract",
        task_scope="claude_code executor should not require codex self review",
    )

    assert result["reason"] == "PROCEED_TO_GATE"
    assert result["loop_outcome"]["reason"] == "PROCEED_TO_GATE"
    assert result["loop_outcome"]["blocked"] is False
    assert result["collaboration_context"]["self_review_required"] is False
    assert result["runtime_semantics"]["self_review"]["count"] == 0
    assert result["self_review_summary"]["review_count"] == 0
    assert all(row["stage"] != "SELF_REVIEW" for row in result["rounds"])
    assert result["peer_review_summary"]["review_count"] == 1


def test_single_pass_strict_contract_does_not_require_unqueued_self_review(tmp_path: Path) -> None:
    _write_runtime_gate_fixture_files(tmp_path)
    config = AutopilotConfig(
        project_root=tmp_path,
        feature="newsapp",
        contract_first_mode="strict",
        executor_backend="external_cli",
        max_cycles=8,
    )
    engine = AutopilotEngine(config, adapter=_SimpleSuccessAdapter())
    engine._task_card_payload = {
        "id": "T049",
        "files_to_change": ["src/good_module.py", "tests/test_good_module.py"],
    }

    result = engine.run_collaboration_loop(
        task_label="T049: Single pass external executor",
        task_scope="strict contract single-pass should not require unqueued self review",
        enable_peer_review=False,
    )

    assert result["reason"] == "PROCEED_TO_GATE"
    assert result["loop_outcome"]["blocked"] is False
    assert result["peer_review_summary"]["enabled"] is False
    assert result["peer_review_summary"]["skipped"] is True
    assert result["self_review_summary"]["review_count"] == 0
    assert all(row["stage"] != "SELF_REVIEW" for row in result["rounds"])


def test_run_collaboration_loop_stops_immediately_when_real_opus_review_fails(tmp_path: Path) -> None:
    config = AutopilotConfig(
        project_root=tmp_path,
        feature="newsapp",
        max_cycles=8,
        require_real_peer_review=True,
    )
    engine = AutopilotEngine(config)

    result = engine.run_collaboration_loop(
        task_label="T045: Require real opus review",
        task_scope="stop when real opus review is unavailable",
    )

    assert result["reason"] == "OPUS_REVIEW_BLOCKED"
    assert result["loop_outcome"]["reason"] == "OPUS_REVIEW_BLOCKED"
    assert result["loop_outcome"]["stop_reason"] == "HARD_ERROR"
    assert result["loop_outcome"]["blocked"] is True
    assert result["loop_outcome"]["round_outcome"] == "blocked"
    assert "WORKFLOW_OPUS_GATEWAY" in (result["loop_outcome"]["blocking_reason"] or "")
    assert result["runtime_semantics"]["peer_review"]["mode"] == "real_required_failed"
    assert result["runtime_semantics"]["peer_review"]["real_requested"] is True
    assert result["runtime_semantics"]["peer_review"]["real_required"] is True
    assert result["runtime_semantics"]["peer_review"]["fallback_used"] is False
    assert result["runtime_semantics"]["self_review"]["count"] == 0
    assert result["self_review_summary"]["review_count"] == 0
    assert all(row["stage"] != "IMPLEMENT" for row in result["rounds"])
    assert all(row["stage"] != "FIX_ROUND" for row in result["rounds"])
    review_round = next(row for row in result["rounds"] if row["stage"] == "PEER_REVIEW")
    assert review_round["stage_status"] == "blocked"
    assert "WORKFLOW_OPUS_GATEWAY" in (review_round.get("last_error") or "")


def test_run_collaboration_loop_accepts_real_review_timeout_fallback_when_not_required(
    tmp_path: Path,
) -> None:
    _write_runtime_gate_fixture_files(tmp_path)
    config = AutopilotConfig(
        project_root=tmp_path,
        feature="newsapp",
        max_cycles=8,
        real_peer_review=True,
        require_real_peer_review=False,
    )
    engine = AutopilotEngine(config, adapter=_RealReviewFallbackAdapter())

    result = engine.run_collaboration_loop(
        task_label="T047: Real review requested with timeout fallback",
        task_scope="accept fallback if hard requirement is off",
    )

    assert result["reason"] == "PROCEED_TO_GATE"
    assert result["loop_outcome"]["reason"] == "PROCEED_TO_GATE"
    assert result["loop_outcome"]["blocked"] is False
    peer = result["runtime_semantics"]["peer_review"]
    assert peer["real_requested"] is True
    assert peer["real_required"] is False
    assert peer["fallback_used"] is True
    assert peer["mode"] == "simulate_local"


def test_run_collaboration_loop_blocks_real_review_timeout_fallback_when_required(
    tmp_path: Path,
) -> None:
    _write_runtime_gate_fixture_files(tmp_path)
    config = AutopilotConfig(
        project_root=tmp_path,
        feature="newsapp",
        max_cycles=8,
        real_peer_review=True,
        require_real_peer_review=True,
    )
    engine = AutopilotEngine(config, adapter=_RealReviewFallbackAdapter())

    result = engine.run_collaboration_loop(
        task_label="T048: Real review fallback with hard requirement",
        task_scope="fallback must block when real review is required",
    )

    assert result["reason"] == "OPUS_REVIEW_BLOCKED"
    assert result["loop_outcome"]["reason"] == "OPUS_REVIEW_BLOCKED"
    assert result["loop_outcome"]["blocked"] is True
    assert "reviewer timeout" in (result["loop_outcome"]["blocking_reason"] or "")
    peer = result["runtime_semantics"]["peer_review"]
    assert peer["real_requested"] is True
    assert peer["real_required"] is True
    assert peer["fallback_used"] is True
    assert peer["mode"] == "simulate_local"


def test_run_collaboration_loop_blocks_when_review_reports_scope_conflict(
    engine: AutopilotEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_file = engine.config.project_root / "src" / "good_module.py"
    test_file = engine.config.project_root / "tests" / "test_good_module.py"
    source_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.parent.mkdir(parents=True, exist_ok=True)
    source_file.write_text("def good_branch(x):\n    return x\n", encoding="utf-8")
    test_file.write_text("def test_ok():\n    assert True\n", encoding="utf-8")

    monkeypatch.setattr(
        engine.adapter,
        "implement",
        lambda task, context: {"status": "done", "changes": ["src/good_module.py", "tests/test_good_module.py"]},
    )
    monkeypatch.setattr(
        engine.adapter,
        "review",
        lambda **kwargs: {
            "approved": False,
            "summary": "Tests need a wider task scope.",
            "must_fix": [],
            "should_fix": ["Split or widen the task scope before adding tests."],
            "blocking_items": [
                "scoped tests are required but current task scope does not include any test files"
            ],
            "severity": "high",
            "score": 72,
            "target_score": 95,
            "min_dimension_score": 80,
            "gate_recommendation": "REVIEW_SCOPE_CONFLICT",
            "blocking_reason": "scoped tests are required but current task scope does not include any test files",
            "reviewer": "opus",
            "source": "kodawari",
        },
    )

    result = engine.run_collaboration_loop(
        task_label="T046: Scope-conflict review",
        task_scope="single-file implementation",
    )

    assert result["reason"] == "OPUS_REVIEW_BLOCKED"
    assert result["loop_outcome"]["reason"] == "OPUS_REVIEW_BLOCKED"
    assert result["loop_outcome"]["blocking_reason"] == "scoped tests are required but current task scope does not include any test files"
    assert all(row["stage"] != "FIX_ROUND" for row in result["rounds"])


def test_runtime_gate_block_reopens_fix_loop_before_proceeding(tmp_path: Path) -> None:
    _write_runtime_gate_fixture_files(tmp_path)

    config = AutopilotConfig(project_root=tmp_path, feature="newsapp", max_cycles=8)
    engine = AutopilotEngine(config, adapter=_RuntimeGateRetryAdapter())

    result = engine.run_collaboration_loop(
        task_label="T050: Implement runtime gate retry",
        task_scope="exercise runtime gate blocked semantics",
    )

    verify_rounds = [row for row in result["rounds"] if row["stage"] == "VERIFY"]
    gate_rounds = [row for row in result["rounds"] if row["stage"] == "RULES_GATE"]
    assert result["reason"] == "PROCEED_TO_GATE"
    assert len(verify_rounds) == 2
    assert all(row["stage_status"] == "pass" for row in verify_rounds)
    assert len(gate_rounds) == 2
    assert gate_rounds[0]["stage_status"] == "blocked"
    assert gate_rounds[0]["round_outcome"] == "blocked"
    assert gate_rounds[1]["stage_status"] == "pass"
    assert any(row["stage"] == "SELF_REVIEW" for row in result["rounds"])
    assert result["gate_check"]["total_status"] == "PASS"
    assert result["runtime_semantics"]["gate"]["passed"] is True
    assert any(row["stage"] == "FIX_ROUND" for row in result["rounds"])
    assert result["changed_files"] == [
        "src/bad_module.py",
        "tests/test_good_module.py",
        "src/good_module.py",
    ]


def test_lite_review_round_limit_allows_one_fix_round_before_stopping(tmp_path: Path) -> None:
    adapter = _ReviewFailThenPassAdapter()
    config = AutopilotConfig(
        project_root=tmp_path,
        feature="lite-review-fix-round",
        max_cycles=8,
        collaboration_max_rounds=LITE_LANE.review_max_rounds,
    )
    engine = AutopilotEngine(config, adapter=adapter)

    result = engine.run_collaboration_loop(
        task_label="T-LITE: tighten task-local review assertion",
        task_scope="single source/test pair",
    )

    stages = [record["stage"] for record in result["rounds"]]
    review_rounds = [record for record in result["rounds"] if record["stage"] == "PEER_REVIEW"]
    assert LITE_LANE.review_max_rounds == 3
    assert result["reason"] == "PROCEED_TO_GATE"
    assert adapter.implement_calls == 2
    assert adapter.review_calls == 2
    assert "FIX_ROUND" in stages
    assert [record["stage_status"] for record in review_rounds] == ["changes_requested", "pass"]
    assert [record["review_round"] for record in review_rounds] == [1, 2]
    assert result["review_rounds_used"] == 2
    assert result["must_fix_open_items"] == []


def test_loop_runtime_seeds_initial_changed_files(tmp_path: Path) -> None:
    config = AutopilotConfig(
        project_root=tmp_path,
        feature="newsapp",
        initial_changed_files=[
            "./src/provider.py",
            "tests\\test_provider.py",
            "src/provider.py",
        ],
    )
    engine = AutopilotEngine(config)

    runtime = engine._create_loop_runtime(
        task_label="T070: Resume provider task",
        task_scope="same task retry",
        max_rounds=2,
    )

    assert runtime.last_changed_files == ["src/provider.py", "tests/test_provider.py"]


def test_run_collaboration_loop_stops_when_token_budget_is_exceeded(tmp_path: Path) -> None:
    config = AutopilotConfig(project_root=tmp_path, feature="newsapp", max_cycles=8, token_budget=10)
    engine = AutopilotEngine(config, adapter=_HighTokenAdapter())

    result = engine.run_collaboration_loop(
        task_label="T060: Exhaust token budget",
        task_scope="stop after implementation when budget is exhausted",
        enable_peer_review=False,
    )

    assert result["reason"] == "TOKEN_BUDGET_EXCEEDED"
    assert result["tokens_used"] == 25
    assert result["token_budget"] == 10
    assert result["budget_exhausted"] is True
    assert result["loop_outcome"]["stop_reason"] == "TOKEN_BUDGET"
    assert result["loop_outcome"]["blocked"] is True
    assert result["runtime_semantics"]["verify"]["status"] == ""


def test_run_collaboration_loop_routes_executor_stall_through_configurable_recovery(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("WORKFLOW_AUTOPILOT_RECOVERY_SYNTHESIZER", "1")
    _write_runtime_gate_fixture_files(tmp_path)
    adapter = _ExecutorStallThenRecoveryAdapter()
    config = AutopilotConfig(project_root=tmp_path, feature="newsapp", max_cycles=8)
    engine = AutopilotEngine(config, adapter=adapter)

    result = engine.run_collaboration_loop(
        task_label="T061: Recover stalled executor",
        task_scope="retry executor with a synthesized narrow recovery card",
        enable_peer_review=True,
    )

    assert result["reason"] == "PROCEED_TO_GATE"
    assert adapter.recovery_calls == 1
    assert adapter.contexts[1]["recovery_card"]["task_id"].endswith("_RECOVERY")  # type: ignore[index]
    assert adapter.contexts[1]["recovery_card"]["patch_plan"][0]["id"] == "p1"  # type: ignore[index]
    assert any(row["stage_status"] == "needs_recovery" for row in result["rounds"])
    assert any(row["stage"] == "FIX_ROUND" for row in result["rounds"])


def test_run_collaboration_loop_accepts_no_write_resume_when_scoped_verify_passes(tmp_path: Path) -> None:
    _write_runtime_gate_fixture_files(tmp_path)
    adapter = _NoWriteResumeAdapter()
    config = AutopilotConfig(
        project_root=tmp_path,
        feature="newsapp",
        max_cycles=8,
        verify_cmd="pytest tests/test_good_module.py -q",
    )
    engine = AutopilotEngine(config, adapter=adapter)
    engine._task_card_payload = {
        "task_id": "T061",
        "files_to_change": ["src/good_module.py", "tests/test_good_module.py"],
        "new_files": ["src/good_module.py", "tests/test_good_module.py"],
        "invariants": [],
    }

    result = engine.run_collaboration_loop(
        task_label="T061: Resume completed task artifacts",
        task_scope="resume should verify existing task-scope files instead of forcing recovery",
        enable_peer_review=True,
    )

    assert result["reason"] == "PROCEED_TO_GATE"
    assert adapter.calls == 1
    assert adapter.recovery_calls == 0
    assert not any(row["stage_status"] == "needs_recovery" for row in result["rounds"])
    implement_round = next(row for row in result["rounds"] if row["stage"] == "IMPLEMENT")
    assert implement_round["stage_status"] == "ok"
    assert implement_round["details"]["resume_verify_only"]["accepted"] is True
    assert implement_round["details"]["execution_result"]["backend"] == "openai_tool_use"
    assert result["runtime_semantics"]["verify"]["status"] == "PASS"
    assert result["changed_files"] == ["src/good_module.py", "tests/test_good_module.py"]


def test_run_collaboration_loop_routes_expand_scope_recovery_into_retry_card(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("WORKFLOW_AUTOPILOT_RECOVERY_SYNTHESIZER", "1")
    _write_runtime_gate_fixture_files(tmp_path)
    (tmp_path / "src" / "router.py").write_text("from src.good_module import good_branch\n", encoding="utf-8")
    adapter = _ExecutorStallThenScopeExpansionAdapter()
    config = AutopilotConfig(project_root=tmp_path, feature="newsapp", max_cycles=8)
    engine = AutopilotEngine(config, adapter=adapter)

    result = engine.run_collaboration_loop(
        task_label="T063: Recover by expanding scope",
        task_scope="retry executor with a controlled scope expansion",
        enable_peer_review=True,
    )

    assert result["reason"] == "PROCEED_TO_GATE"
    assert adapter.recovery_calls == 1
    recovery_card = adapter.contexts[1]["recovery_card"]  # type: ignore[index]
    assert recovery_card["recovery"]["source_action"] == "expand_scope_request"  # type: ignore[index]
    assert recovery_card["files_to_change"] == ["src/router.py"]  # type: ignore[index]


def test_run_collaboration_loop_can_resynthesize_after_expanded_recovery_card_stalls(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("WORKFLOW_AUTOPILOT_RECOVERY_SYNTHESIZER", "1")
    _write_runtime_gate_fixture_files(tmp_path)
    (tmp_path / "src" / "router.py").write_text("old\n", encoding="utf-8")
    adapter = _ExecutorScopeExpansionThenPatchPlanAdapter()
    config = AutopilotConfig(project_root=tmp_path, feature="newsapp", max_cycles=8)
    engine = AutopilotEngine(config, adapter=adapter)
    engine._task_card_payload = {
        "files_to_change": ["src/good_module.py", "tests/test_good_module.py"],
        "runtime_caps": {"max_recovery_attempts": 1},
        "invariants": [],
    }

    result = engine.run_collaboration_loop(
        task_label="T064: Recover expanded scope stall",
        task_scope="retry recovery synthesizer after an expanded card also stalls",
        enable_peer_review=True,
    )

    assert result["reason"] == "PROCEED_TO_GATE"
    assert adapter.recovery_calls == 2
    second_recovery_card = adapter.contexts[2]["recovery_card"]  # type: ignore[index]
    assert second_recovery_card["recovery"]["source_action"] == "narrow_patch_plan"  # type: ignore[index]
    assert second_recovery_card["patch_plan"][0]["id"] == "p2"  # type: ignore[index]
    assert "src/router.py" in second_recovery_card["files_to_change"]  # type: ignore[index]


def test_executor_recovery_attempt_limit_is_per_failure_signature(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("WORKFLOW_AUTOPILOT_RECOVERY_SYNTHESIZER", "1")
    _write_runtime_gate_fixture_files(tmp_path)
    adapter = _ExecutorDifferentFailuresThenSuccessAdapter()
    config = AutopilotConfig(project_root=tmp_path, feature="newsapp", max_cycles=12)
    engine = AutopilotEngine(config, adapter=adapter)
    engine._task_card_payload = {
        "files_to_change": ["src/good_module.py", "tests/test_good_module.py"],
        "runtime_caps": {"max_recovery_attempts": 1},
        "invariants": [],
    }

    result = engine.run_collaboration_loop(
        task_label="T065: Recover distinct executor failures",
        task_scope="retry recovery when a later failure has a new signature",
        enable_peer_review=True,
    )

    assert result["reason"] == "PROCEED_TO_GATE"
    assert adapter.recovery_calls == 3
    assert adapter.contexts[1]["recovery_card"]["patch_plan"][0]["id"] == "p1"  # type: ignore[index]
    assert adapter.contexts[2]["recovery_card"]["patch_plan"][0]["id"] == "p2"  # type: ignore[index]
    assert adapter.contexts[3]["recovery_card"]["patch_plan"][0]["id"] == "p3"  # type: ignore[index]
    assert not any(
        row.get("last_error") and "recovery attempts exhausted" in str(row.get("last_error")).lower()
        for row in result["rounds"]
    )


def test_single_pass_executor_stall_uses_executor_recovery_before_gate(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("WORKFLOW_AUTOPILOT_RECOVERY_SYNTHESIZER", "1")
    _write_runtime_gate_fixture_files(tmp_path)
    adapter = _ExecutorStallThenRecoveryAdapter()
    config = AutopilotConfig(project_root=tmp_path, feature="newsapp", max_cycles=8)
    engine = AutopilotEngine(config, adapter=adapter)

    result = engine.run_collaboration_loop(
        task_label="T062: Stall without peer loop",
        task_scope="single pass must not gate after a stalled executor",
        enable_peer_review=False,
    )

    assert result["reason"] == "PROCEED_TO_GATE"
    assert adapter.calls == 2
    assert adapter.recovery_calls == 1
    assert any(row["stage_status"] == "needs_recovery" for row in result["rounds"])
    assert any(row["stage"] == "CODEX_FIX" for row in result["rounds"])


def test_apply_opus_review_global_fail_overrides_approved_and_keeps_verdict_fields(
    tmp_path: Path,
) -> None:
    """full_feature scope: global_consistency_verdict=FAIL always overrides approved."""
    engine = AutopilotEngine(
        AutopilotConfig(project_root=tmp_path, feature="newsapp"),
        requirements_text="review verdict plumbing",
    )
    context = build_collaboration_context("T070", "T070: Apply peer review verdicts")
    context.review_scope = "full_feature"

    engine._apply_opus_review(
        context,
        {
            "approved": True,
            "summary": "Implementation is locally fine.",
            "must_fix": [],
            "should_fix": [],
            "blocking_items": [],
            "severity": "low",
            "score": 98,
            "target_score": 95,
            "min_dimension_score": 80,
            "dimension_scores": {"architecture": 96},
            "gate_recommendation": "PROCEED_TO_GATE",
            "global_consistency_verdict": "FAIL",
            "local_implementation_verdict": "PASS",
            "deterministic_finding_responses": [
                {
                    "finding_type": "out_of_scope_files",
                    "acknowledged": True,
                    "assessment": "Detected in deterministic findings.",
                }
            ],
            "evidence_refs": [
                {
                    "artifact": ".review_bundle.json",
                    "field_path": "deterministic_findings.out_of_scope_files",
                    "reason": "Global scope mismatch",
                }
            ],
        },
    )

    assert context.review_feedback.approved is False
    assert context.review_feedback.global_consistency_verdict == "FAIL"
    assert context.review_feedback.local_implementation_verdict == "PASS"
    assert context.review_feedback.gate_recommendation == "REVIEW_FIX_REQUIRED"
    assert context.review_feedback.evidence_refs[0]["artifact"] == ".review_bundle.json"
    assert context.review_history[-1].global_consistency_verdict == "FAIL"

    summary = engine._update_peer_review_summary(
        SimpleNamespace(peer_reviews=[{"approved": True}], context=context),
        {"approved": True, "gate_recommendation": "PROCEED_TO_GATE"},
    )
    assert summary["approved"] is False
    assert summary["last_gate_recommendation"] == "REVIEW_FIX_REQUIRED"
    assert summary["global_consistency_verdict"] == "FAIL"


def test_apply_opus_review_single_task_scope_ignores_sibling_global_fail(
    tmp_path: Path,
) -> None:
    """single_task scope: global_consistency_verdict=FAIL without local invariant violation
    must NOT override approved — sibling tasks being incomplete is not this task's defect."""
    engine = AutopilotEngine(
        AutopilotConfig(project_root=tmp_path, feature="newsapp"),
        requirements_text="review scope single_task isolation",
    )
    context = build_collaboration_context("T071", "T071: single task scope test")
    context.review_scope = "single_task"

    # A correctly-prompted reviewer in single_task mode puts sibling-task gaps in
    # should_fix (not blocking_items / must_fix) per the scope rules in the prompt.
    engine._apply_opus_review(
        context,
        {
            "approved": True,
            "summary": "This task is fine; T2/T3 not implemented yet.",
            "must_fix": [],
            "should_fix": ["T2 social_thread_snapshots not yet updated — pending sibling task"],
            "blocking_items": [],
            "severity": "low",
            "score": 90,
            "target_score": 85,
            "min_dimension_score": 75,
            "dimension_scores": {},
            "gate_recommendation": "PROCEED_TO_GATE",
            "global_consistency_verdict": "FAIL",
            "local_implementation_verdict": "PASS",
        },
    )

    # blocking_items=[] + no local invariant violation → approved stays True
    assert context.review_feedback.approved is True
    assert context.review_feedback.global_consistency_verdict == "FAIL"
    assert context.review_feedback.gate_recommendation != "REVIEW_FIX_REQUIRED"


def test_apply_opus_review_single_task_scope_blocks_on_invariant_violation(
    tmp_path: Path,
) -> None:
    """single_task scope: blocks when blocking_items contain an actual invariant violation."""
    engine = AutopilotEngine(
        AutopilotConfig(project_root=tmp_path, feature="newsapp"),
        requirements_text="review scope invariant enforcement",
    )
    context = build_collaboration_context("T072", "T072: invariant violation test")
    context.review_scope = "single_task"

    engine._apply_opus_review(
        context,
        {
            "approved": True,
            "summary": "Local code violates layer boundary.",
            "must_fix": [],
            "should_fix": [],
            "blocking_items": ["route layer references repository directly (layer boundary violation)"],
            "severity": "high",
            "score": 60,
            "target_score": 85,
            "min_dimension_score": 75,
            "dimension_scores": {},
            "gate_recommendation": "PROCEED_TO_GATE",
            "global_consistency_verdict": "FAIL",
            "local_implementation_verdict": "FAIL",
        },
    )

    # "layer boundary" keyword in blocking_items → override must fire
    assert context.review_feedback.approved is False
    assert context.review_feedback.gate_recommendation == "REVIEW_FIX_REQUIRED"
