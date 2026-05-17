"""Tests for C3 — Big-Waste Kill Switches.

Verifies that:
  - WorkflowPolicy is active for explicit tiers and --tier=auto
  - With lite policy, args.task_cycle is forced False
  - With lite policy, maybe_run_release_tail short-circuits with "skipped"
  - With heavy policy, release_tail continues to run
  - User-explicit --tier heavy keeps full pipeline
"""

from __future__ import annotations

import argparse
from typing import Any

import pytest

from kodawari.cli.autopilot_cmd import (
    _apply_policy_to_args,
    _policy_should_override_runtime,
    _resolve_tier_and_policy,
)
from kodawari.cli.autopilot_release_flow import maybe_run_release_tail


def _ns(**kwargs: Any) -> argparse.Namespace:
    # Sentinel semantics: task_cycle=None means "user did not pass
    # --task-cycle/--no-task-cycle". Policy is then free to set it.
    base = dict(tier="auto", task="", task_cycle=None, feature="f1")
    base.update(kwargs)
    return argparse.Namespace(**base)


# ---------------------------------------------------------------------------
# _policy_should_override_runtime: active for every autopilot run
# ---------------------------------------------------------------------------


def test_policy_override_active_for_explicit_lite():
    assert _policy_should_override_runtime(_ns(tier="lite")) is True


def test_policy_override_active_for_explicit_standard():
    assert _policy_should_override_runtime(_ns(tier="standard")) is True


def test_policy_override_active_for_explicit_heavy():
    assert _policy_should_override_runtime(_ns(tier="heavy")) is True


def test_policy_override_active_for_auto_default():
    assert _policy_should_override_runtime(_ns(tier="auto")) is True
    assert _policy_should_override_runtime(_ns(tier="")) is True
    assert _policy_should_override_runtime(_ns(tier=None)) is True


def test_policy_override_active_for_missing_tier_attr():
    args = argparse.Namespace(feature="f1")
    assert _policy_should_override_runtime(args) is True


# ---------------------------------------------------------------------------
# _apply_policy_to_args: --tier lite disables task_cycle on args
# ---------------------------------------------------------------------------


def test_apply_policy_auto_uses_detected_lane():
    # task_cycle is None (sentinel) => policy wins.
    args = _ns(tier="auto")
    _, policy = _resolve_tier_and_policy(
        args=args, feature="f1", requirements_text="", changed_files=(),
    )
    _apply_policy_to_args(args=args, policy=policy)
    assert args.task_cycle is False


def test_apply_policy_lite_disables_task_cycle():
    """Explicit --tier lite (and no --task-cycle flag) must set args.task_cycle=False."""
    args = _ns(tier="lite")
    _, policy = _resolve_tier_and_policy(
        args=args, feature="f1", requirements_text="", changed_files=(),
    )
    _apply_policy_to_args(args=args, policy=policy)
    assert args.task_cycle is False


def test_apply_policy_heavy_keeps_task_cycle():
    """Explicit --tier heavy (and no --task-cycle flag) must enable task_cycle."""
    args = _ns(tier="heavy")
    _, policy = _resolve_tier_and_policy(
        args=args, feature="f1", requirements_text="", changed_files=(),
    )
    _apply_policy_to_args(args=args, policy=policy)
    assert args.task_cycle is True


def test_apply_policy_lite_disables_parallel_runtime():
    args = _ns(tier="lite")
    _, policy = _resolve_tier_and_policy(
        args=args, feature="f1", requirements_text="", changed_files=(),
    )
    _apply_policy_to_args(args=args, policy=policy)
    assert args.parallel_runtime_enabled is False


def test_apply_policy_heavy_enables_parallel_runtime():
    args = _ns(tier="heavy")
    _, policy = _resolve_tier_and_policy(
        args=args, feature="f1", requirements_text="", changed_files=(),
    )
    _apply_policy_to_args(args=args, policy=policy)
    assert args.parallel_runtime_enabled is True


def test_apply_policy_auto_sets_parallel_runtime():
    args = _ns(tier="auto")
    _, policy = _resolve_tier_and_policy(
        args=args, feature="f1", requirements_text="", changed_files=(),
    )
    _apply_policy_to_args(args=args, policy=policy)
    assert args.parallel_runtime_enabled == bool(policy.parallel_runtime_enabled)


def test_apply_policy_user_explicit_task_cycle_true_wins():
    """When user passed --task-cycle (args.task_cycle=True), lite policy must NOT flip it off."""
    args = _ns(tier="lite", task_cycle=True)
    _, policy = _resolve_tier_and_policy(
        args=args, feature="f1", requirements_text="", changed_files=(),
    )
    _apply_policy_to_args(args=args, policy=policy)
    assert args.task_cycle is True


def test_apply_policy_user_explicit_no_task_cycle_wins():
    """When user passed --no-task-cycle (args.task_cycle=False), heavy policy must NOT flip it on."""
    args = _ns(tier="heavy", task_cycle=False)
    _, policy = _resolve_tier_and_policy(
        args=args, feature="f1", requirements_text="", changed_files=(),
    )
    _apply_policy_to_args(args=args, policy=policy)
    assert args.task_cycle is False


def test_apply_policy_programmatic_namespace_without_task_cycle_attr():
    """A Namespace built in a test harness with no `task_cycle` attribute must not crash
    and policy should control the value (since attribute absence == sentinel None)."""
    args = argparse.Namespace(tier="lite", task="", feature="f1")
    _, policy = _resolve_tier_and_policy(
        args=args, feature="f1", requirements_text="", changed_files=(),
    )
    _apply_policy_to_args(args=args, policy=policy)
    # lite policy says task_cycle_enabled=False -> policy writes it.
    assert args.task_cycle is False


# ---------------------------------------------------------------------------
# WORKFLOW_AUTOPILOT_LEGACY=1 escape hatch: full opt-out across 3 gates
# ---------------------------------------------------------------------------


def test_legacy_env_disables_policy_override(monkeypatch):
    """`WORKFLOW_AUTOPILOT_LEGACY=1` makes _policy_should_override_runtime() return False."""
    monkeypatch.setenv("WORKFLOW_AUTOPILOT_LEGACY", "1")
    assert _policy_should_override_runtime(_ns(tier="lite")) is False
    assert _policy_should_override_runtime(_ns(tier="heavy")) is False
    assert _policy_should_override_runtime(_ns(tier="auto")) is False


def test_legacy_env_short_circuits_apply_policy(monkeypatch):
    """Under legacy mode, _apply_policy_to_args is a no-op: args.task_cycle stays None, parallel_runtime untouched."""
    monkeypatch.setenv("WORKFLOW_AUTOPILOT_LEGACY", "1")
    args = _ns(tier="lite")  # task_cycle is None sentinel
    _, policy = _resolve_tier_and_policy(
        args=args, feature="f1", requirements_text="", changed_files=(),
    )
    _apply_policy_to_args(args=args, policy=policy)
    # Without legacy, lite policy would set task_cycle=False. Under legacy
    # it stays None (sentinel preserved).
    assert args.task_cycle is None
    # And parallel_runtime_enabled must NOT be written onto args.
    assert not hasattr(args, "parallel_runtime_enabled")


def test_legacy_env_unset_restores_normal_behavior(monkeypatch):
    """Removing the env var restores policy application."""
    monkeypatch.delenv("WORKFLOW_AUTOPILOT_LEGACY", raising=False)
    args = _ns(tier="lite")
    _, policy = _resolve_tier_and_policy(
        args=args, feature="f1", requirements_text="", changed_files=(),
    )
    _apply_policy_to_args(args=args, policy=policy)
    assert args.task_cycle is False  # lite policy applied


def test_legacy_env_only_honors_literal_one(monkeypatch):
    """Value must be exactly '1'; other truthy-looking values do NOT trigger legacy."""
    for value in ("0", "true", "yes", "", "  "):
        monkeypatch.setenv("WORKFLOW_AUTOPILOT_LEGACY", value)
        assert _policy_should_override_runtime(_ns(tier="lite")) is True


def test_legacy_env_short_circuits_planning_env_override(monkeypatch):
    """Under legacy mode, _planning_env_override_for_tier yields without touching env."""
    from kodawari.cli.autopilot_cmd import _planning_env_override_for_tier
    monkeypatch.setenv("WORKFLOW_AUTOPILOT_LEGACY", "1")
    monkeypatch.delenv("WORKFLOW_PLAN_BLOCKING_SEVERITIES", raising=False)
    monkeypatch.delenv("WORKFLOW_PLAN_DECISION_POLICY", raising=False)
    args = argparse.Namespace(tier="lite", reviewer_model="")
    with _planning_env_override_for_tier(args):
        import os as _os
        # Must not set any planning env vars under legacy.
        assert _os.environ.get("WORKFLOW_PLAN_BLOCKING_SEVERITIES") is None
        assert _os.environ.get("WORKFLOW_PLAN_DECISION_POLICY") is None


# ---------------------------------------------------------------------------
# maybe_run_release_tail: skipped only when policy_active + lite
# ---------------------------------------------------------------------------


def _command_runtime(*, policy_active: bool, policy) -> dict[str, Any]:
    return {
        "planning_snapshot": None,
        "workflow_policy": policy,
        "policy_active": policy_active,
        "project_root": ".",
        "planning_dir": ".",
        "feature": "f1",
    }


def test_release_tail_skipped_for_explicit_lite():
    args = _ns(tier="lite")
    _, policy = _resolve_tier_and_policy(
        args=args, feature="f1", requirements_text="", changed_files=(),
    )
    runtime = _command_runtime(policy_active=True, policy=policy)
    payload = {"status": "ok", "run_reason": "PIPELINE_FINISH"}

    def stub_release_tail(**_):
        raise AssertionError("release_tail must NOT be invoked when policy disables it")

    out, rc = maybe_run_release_tail(
        args=args, command_runtime=runtime, payload=payload,
        run_release_tail=stub_release_tail,
    )
    assert out["release_tail"]["status"] == "skipped"
    assert "policy.release_tail_enabled=false" in out["release_tail"]["reason"]
    assert rc is None


def test_release_tail_skipped_when_auto_resolves_to_lite():
    args = _ns(tier="auto")
    _, policy = _resolve_tier_and_policy(
        args=args, feature="f1", requirements_text="", changed_files=(),
    )
    runtime = _command_runtime(policy_active=True, policy=policy)
    payload = {"status": "ok", "run_reason": "PIPELINE_FINISH"}

    out, rc = maybe_run_release_tail(
        args=args, command_runtime=runtime, payload=payload,
        run_release_tail=lambda **_: {"status": "ok"},
    )
    assert out["release_tail"]["status"] == "skipped"
    assert rc is None


def test_release_tail_runs_for_explicit_heavy():
    args = _ns(tier="heavy")
    _, policy = _resolve_tier_and_policy(
        args=args, feature="f1", requirements_text="", changed_files=(),
    )
    runtime = _command_runtime(policy_active=True, policy=policy)
    payload = {"status": "ok", "run_reason": "PIPELINE_FINISH", "risk_profile": "medium"}

    invoked = {"count": 0}

    def fake_release_tail(**_):
        invoked["count"] += 1
        return {"status": "ok"}

    out, rc = maybe_run_release_tail(
        args=args, command_runtime=runtime, payload=payload,
        run_release_tail=fake_release_tail,
    )
    assert invoked["count"] >= 1
    # Not the skip payload
    assert out.get("release_tail", {}).get("status") != "skipped"


def test_release_tail_skipped_does_not_affect_payload_status():
    args = _ns(tier="lite")
    _, policy = _resolve_tier_and_policy(
        args=args, feature="f1", requirements_text="", changed_files=(),
    )
    runtime = _command_runtime(policy_active=True, policy=policy)
    payload = {"status": "ok", "run_reason": "PIPELINE_FINISH"}

    out, rc = maybe_run_release_tail(
        args=args, command_runtime=runtime, payload=payload,
        run_release_tail=lambda **_: {"status": "ok"},
    )
    # Payload status preserved (skipping release_tail isn't a failure)
    assert out["status"] == "ok"
    assert rc is None
