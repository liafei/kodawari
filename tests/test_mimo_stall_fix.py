"""Regression tests for the mimo executor cache-hit stall fix.

In a real strict-mode autopilot run on `e:\\wf-test\\newsapp` task
``social_dual_v_strict`` (2026-05-17), mimo-v2.5-pro completed 3 of 6
tasks with production-ready code, then stalled with 14 no-write
iterations on a later task. The deterministic recovery retry also
stalled, ending work-all with rc=1.

Two fixes per sub-agent convergence:

A — Per-model nudge policy: tighter mimo thresholds let the stall
    detector trigger earlier so deterministic recovery engages
    sooner (and codex/claude/gpt-5 paths stay unchanged).

B — action_only_mode flip on no_write_stall recovery card:
    engine_implementation_mixin tags the recovery card with
    ``action_only_on_start=True`` when the detector_name is
    ``no_write_stall``; execution_openai_tool_use._run_openai_tool_use
    reads that flag from request_payload.task_card and flips
    ``runtime.action_only_mode=True`` before the first chat turn so
    the tool schemas drop read tools from iteration 1, forcing the
    retry into write-or-finish mode.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest


def test_mimo_nudge_policy_loads_tighter_thresholds(tmp_path: Path) -> None:
    """Fix A: a project that ships a `.claude/workflow/prompts.yaml`
    with a ``mimo`` nudge policy must surface those values via
    ``nudge_policy_for_model`` so the tool-use runtime caps fire
    earlier than the codex/gpt defaults."""
    from kodawari.autopilot.core.prompt_profiles import nudge_policy_for_model

    workflow_dir = tmp_path / ".claude" / "workflow"
    workflow_dir.mkdir(parents=True)
    (workflow_dir / "prompts.yaml").write_text(
        """
profiles:
  nudge_policies:
    mimo:
      max_no_write_iterations: 6
      write_progress_nudge_iteration: 2
      missing_writable_remind_every: 2
      max_no_write_iterations_under_budget_pressure: 1
""".strip(),
        encoding="utf-8",
    )

    mimo_policy = nudge_policy_for_model(
        project_root=tmp_path,
        model="mimo-v2.5-pro",
        transport_name="mimo_tool_use",
        driver="openai_compatible",
    )
    assert mimo_policy["max_no_write_iterations"] == 6
    assert mimo_policy["write_progress_nudge_iteration"] == 2
    assert mimo_policy["missing_writable_remind_every"] == 2
    assert mimo_policy["max_no_write_iterations_under_budget_pressure"] == 1


def test_mimo_nudge_policy_isolated_from_other_families(tmp_path: Path) -> None:
    """Fix A safety: the mimo policy must NOT bleed into codex / claude
    / gpt-5 paths. Those models keep the codebase-default thresholds
    (max_no_write_iterations=12, nudge=4)."""
    from kodawari.autopilot.core.prompt_profiles import nudge_policy_for_model

    workflow_dir = tmp_path / ".claude" / "workflow"
    workflow_dir.mkdir(parents=True)
    (workflow_dir / "prompts.yaml").write_text(
        """
profiles:
  nudge_policies:
    mimo:
      max_no_write_iterations: 6
      write_progress_nudge_iteration: 2
""".strip(),
        encoding="utf-8",
    )

    claude_policy = nudge_policy_for_model(
        project_root=tmp_path,
        model="claude-opus-4-7",
        transport_name="yigou_tool_use",
        driver="openai_compatible",
    )
    # claude does NOT receive the mimo overrides.
    assert claude_policy.get("max_no_write_iterations") is None
    assert claude_policy.get("write_progress_nudge_iteration") is None


def test_no_write_stall_recovery_card_carries_action_only_flag() -> None:
    """Fix B (engine side): when ``pending_recovery_card`` was emitted
    by the ``no_write_stall`` detector, the engine layer must tag the
    card with ``action_only_on_start=True`` before propagating it as
    ``impl_context["task_card"]``. The retry executor reads this flag
    from ``request_payload.task_card``."""
    from kodawari.autopilot.engine.engine_implementation_mixin import (
        EngineImplementationMixin,
    )

    # Stub minimal runtime/state surface to invoke _build_implementation_context
    # without spinning up the full autopilot engine.
    class _ReviewFeedback:
        review_iteration = 0
        must_fix: list[str] = []

    class _Ctx:
        implementation_started = False
        architecture_decisions: list[Any] = []
        review_feedback = _ReviewFeedback()

    class _Runtime:
        def __init__(self, recovery_card: dict[str, Any] | None) -> None:
            self.context = _Ctx()
            self.task_label = "T1"
            self.task_scope = ""
            self.pending_recovery_card = recovery_card
            self.pre_compact_payload: dict[str, Any] = {}

    class _State:
        current_stage = None

    class _Dummy(EngineImplementationMixin):
        def __init__(self) -> None:
            self.state = _State()
        def _refresh_recovery_base_workspace_for_runtime(self, runtime: Any) -> None:
            pass
        def _build_implementation_context(self, task_label: str, task_scope: str) -> dict[str, Any]:
            return {"task_label": task_label, "task_scope": task_scope}

    dummy = _Dummy()

    # 1) Stall-recovery card flips the flag
    stall_card = {
        "files_to_change": ["backend/foo.py"],
        "detector_name": "no_write_stall",
        "detector_evidence": {"detector_name": "no_write_stall"},
        "reason": "executor stalled after repeated reads without writes",
    }
    runtime = _Runtime(stall_card)
    from kodawari.autopilot.core.collaboration import CollaborationAction
    ctx_stall = dummy._build_implementation_request(runtime, CollaborationAction.IMPLEMENT)
    assert ctx_stall["task_card"]["action_only_on_start"] is True
    assert ctx_stall["task_card"]["action_only_reason"] == "no_write_stall_recovery_retry"

    # 2) Non-stall recovery card (e.g. scope drift) does NOT flip the flag
    other_card = {
        "files_to_change": ["backend/bar.py"],
        "detector_name": "scope_drift",
    }
    runtime2 = _Runtime(other_card)
    ctx_other = dummy._build_implementation_request(runtime2, CollaborationAction.IMPLEMENT)
    assert "action_only_on_start" not in ctx_other["task_card"]

    # 3) No recovery card → no task_card flip
    runtime3 = _Runtime(None)
    ctx_clean = dummy._build_implementation_request(runtime3, CollaborationAction.IMPLEMENT)
    assert "task_card" not in ctx_clean or "action_only_on_start" not in ctx_clean.get("task_card", {})


def test_tool_use_runtime_flips_action_only_mode_from_request_payload(tmp_path: Path) -> None:
    """Fix B (runtime side): when ``request_payload["task_card"]`` carries
    ``action_only_on_start=True`` (set by the engine on no_write_stall
    recovery), the production helper
    ``_apply_recovery_card_action_only_mode`` must flip
    ``runtime.action_only_mode=True`` BEFORE the first iteration so tool
    schemas drop read tools from the very first chat turn.

    Exercises the REAL call-site helper (not a hand-mirror) so a future
    refactor that breaks the helper would surface here."""
    from kodawari.autopilot.execution.execution_openai_tool_use import (
        _apply_recovery_card_action_only_mode,
        _build_runtime,
    )

    project_root = tmp_path / "repo"
    project_root.mkdir()
    (project_root / "src").mkdir()
    (project_root / "src" / "foo.py").write_text("# placeholder\n", encoding="utf-8")

    class _Config:
        backend = "openai_tool_use"
        model = "mimo-v2.5-pro"
        transport_name = "mimo_tool_use"
        execution_protocol = "exact_str_replace_v1"
        base_url = "http://localhost"
        api_key_env = ""
        api_format = "openai_chat"
        runtime_caps: dict[str, Any] = {}
        max_tool_iterations = 8
        max_token_budget = 1_000_000
        max_hard_token_budget = 10_000_000
        max_same_tool_calls_per_path = 5
        max_tool_calls_per_response = 8

    request_payload = {
        "schema_version": "execution.request.v1",
        "feature": "demo",
        "task": "T1",
        "backend": "openai_tool_use",
        "project_root": str(project_root),
        "planning_dir": str(project_root),
        "files_to_change": ["src/foo.py"],
        "task_card": {
            "files_to_change": ["src/foo.py"],
            "action_only_on_start": True,
            "action_only_reason": "no_write_stall_recovery_retry",
        },
        "task_scope": "",
        "task_requirements": "",
        "verify_cmd": "",
        "archetype": "",
        "capabilities": [],
        "surface": "",
        "must_fix": [],
        "invariants": [],
        "scope_risk_warnings": [],
        "execution_timeout_hint": None,
        "executor_command": "",
        "review_round": 0,
        "attempt": 1,
        "task_id": "T1",
        "requested_action": "IMPLEMENT",
        "guard_decision": {},
        "backend_capabilities": {},
        "backend_capability_truth": {},
    }
    runtime = _build_runtime(
        config=_Config(),
        request_path=project_root / ".execution_request.json",
        request_payload=request_payload,
    )
    # _build_runtime alone does NOT flip the flag — only the call-site helper.
    assert runtime.action_only_mode is False

    # Exercise the production helper directly (the actual call-site code
    # in _run_openai_tool_use uses the same function).
    _apply_recovery_card_action_only_mode(runtime, request_payload)

    assert runtime.action_only_mode is True
    assert runtime.action_only_reason == "no_write_stall_recovery_retry"


def test_tool_use_runtime_does_not_flip_action_only_for_unrelated_task_card(tmp_path: Path) -> None:
    """Negative path: a normal execute (no recovery card, or recovery
    card without action_only_on_start) must leave action_only_mode=False
    so the executor keeps full read+write tools."""
    from kodawari.autopilot.execution.execution_openai_tool_use import (
        _apply_recovery_card_action_only_mode,
        _build_runtime,
    )

    project_root = tmp_path / "repo"
    project_root.mkdir()
    (project_root / "src").mkdir()
    (project_root / "src" / "foo.py").write_text("# placeholder\n", encoding="utf-8")

    class _Config:
        backend = "openai_tool_use"
        model = "claude-opus-4-7"
        transport_name = "yigou_tool_use"
        execution_protocol = "exact_str_replace_v1"
        base_url = "http://localhost"
        api_key_env = ""
        api_format = "anthropic_messages"
        runtime_caps: dict[str, Any] = {}
        max_tool_iterations = 8
        max_token_budget = 1_000_000
        max_hard_token_budget = 10_000_000
        max_same_tool_calls_per_path = 5
        max_tool_calls_per_response = 8

    request_payload = {
        "schema_version": "execution.request.v1",
        "feature": "demo",
        "task": "T1",
        "backend": "openai_tool_use",
        "project_root": str(project_root),
        "planning_dir": str(project_root),
        "files_to_change": ["src/foo.py"],
        "task_card": {"files_to_change": ["src/foo.py"]},
        "task_scope": "",
        "task_requirements": "",
        "verify_cmd": "",
        "archetype": "",
        "capabilities": [],
        "surface": "",
        "must_fix": [],
        "invariants": [],
        "scope_risk_warnings": [],
        "execution_timeout_hint": None,
        "executor_command": "",
        "review_round": 0,
        "attempt": 1,
        "task_id": "T1",
        "requested_action": "IMPLEMENT",
        "guard_decision": {},
        "backend_capabilities": {},
        "backend_capability_truth": {},
    }
    runtime = _build_runtime(
        config=_Config(),
        request_path=project_root / ".execution_request.json",
        request_payload=request_payload,
    )
    _apply_recovery_card_action_only_mode(runtime, request_payload)
    assert runtime.action_only_mode is False


def test_recovery_mixin_propagates_detector_name_onto_pending_card() -> None:
    """Source-of-truth fix per sub-agent a95476455 review: the
    deterministic recovery card stored in ``runtime.pending_recovery_card``
    used to be anonymous — only the *decision* dict carried detector_name.
    Downstream consumers (e.g. engine_implementation_mixin's action_only
    flip on no_write_stall) cannot recognise which detector produced the
    card if the field is missing. ``engine_recovery_mixin`` now copies
    ``detector_name`` and ``detector_evidence`` onto the card before
    storing it, so detector-specific consume-time handling is no longer
    dead code."""
    # Read engine_recovery_mixin source to verify the propagation lines
    # are present. We use source inspection because the full deterministic
    # recovery path requires runtime, registry, and adapter — overkill
    # for verifying a 2-line propagation.
    from pathlib import Path
    src_path = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "kodawari"
        / "autopilot"
        / "engine"
        / "engine_recovery_mixin.py"
    )
    body = src_path.read_text(encoding="utf-8")
    # The two key lines that ship the propagation.
    assert 'card["detector_name"] = deterministic_match.name' in body
    assert 'card["detector_evidence"] = dict(deterministic_match.evidence)' in body
