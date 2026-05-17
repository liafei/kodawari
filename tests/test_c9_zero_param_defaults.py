"""Tests for C9 — zero-param defaults for active policy lanes.

Covers:
  - _apply_policy_to_args backfills --executor-backend=claude_code when empty
  - self-review stays local-default when no explicit backend is supplied
  - User-provided backends NOT overridden
  - --tier=auto is load-bearing and receives the same defaults as explicit tiers
  - _policy_auto_eval gates auto_eval on policy_active + eval_required
  - _attempt_auto_telemetry swallows import/runtime errors cleanly
"""

from __future__ import annotations

import argparse

import pytest

from kodawari.cli.autopilot_cmd import (
    _apply_policy_to_args,
    _resolve_tier_and_policy,
)
from kodawari.cli.autopilot_release_flow import _policy_auto_eval
from kodawari.cli.delivery_release import (
    _attempt_auto_telemetry,
)


def _ns(**kwargs):
    # Sentinel semantics: task_cycle=None means "no user flag", letting
    # policy own the value. Tests that need user-explicit override pass
    # task_cycle=True/False explicitly.
    base = dict(
        tier="auto", task="", task_cycle=None,
        executor_backend="", self_review_backend="",
        feature="f1",
    )
    base.update(kwargs)
    return argparse.Namespace(**base)


@pytest.fixture(autouse=True)
def _default_backend_stub(monkeypatch):
    monkeypatch.setattr("kodawari.cli.autopilot_cmd._default_executor_backend", lambda: "claude_code")


# ---------------------------------------------------------------------------
# Executor and self-review backfill
# ---------------------------------------------------------------------------


def test_explicit_lite_backfills_executor_default():
    args = _ns(tier="lite")
    _, policy = _resolve_tier_and_policy(
        args=args, feature="f1", requirements_text="", changed_files=(),
    )
    _apply_policy_to_args(args=args, policy=policy)
    assert args.executor_backend == "claude_code"


def test_explicit_lite_leaves_self_review_backend_unset_for_local_default():
    args = _ns(tier="lite")
    _, policy = _resolve_tier_and_policy(
        args=args, feature="f1", requirements_text="", changed_files=(),
    )
    _apply_policy_to_args(args=args, policy=policy)
    assert args.self_review_backend == ""


def test_user_explicit_executor_not_overridden():
    args = _ns(tier="lite", executor_backend="codex_cli")
    _, policy = _resolve_tier_and_policy(
        args=args, feature="f1", requirements_text="", changed_files=(),
    )
    _apply_policy_to_args(args=args, policy=policy)
    assert args.executor_backend == "codex_cli"


def test_user_explicit_self_review_not_overridden():
    args = _ns(tier="standard", self_review_backend="external_cli")
    _, policy = _resolve_tier_and_policy(
        args=args, feature="f1", requirements_text="", changed_files=(),
    )
    _apply_policy_to_args(args=args, policy=policy)
    assert args.self_review_backend == "external_cli"


def test_auto_tier_backfills_executor():
    args = _ns(tier="auto", executor_backend="")
    _, policy = _resolve_tier_and_policy(
        args=args, feature="f1", requirements_text="", changed_files=(),
    )
    _apply_policy_to_args(args=args, policy=policy)
    assert args.executor_backend == "claude_code"


def test_auto_tier_keeps_self_review_backend_unset():
    args = _ns(tier="auto", self_review_backend="")
    _, policy = _resolve_tier_and_policy(
        args=args, feature="f1", requirements_text="", changed_files=(),
    )
    _apply_policy_to_args(args=args, policy=policy)
    assert args.self_review_backend == ""


def test_all_three_tiers_backfill_when_empty():
    for tier in ("lite", "standard", "heavy"):
        args = _ns(tier=tier)
        _, policy = _resolve_tier_and_policy(
            args=args, feature="f1", requirements_text="", changed_files=(),
        )
        _apply_policy_to_args(args=args, policy=policy)
        assert args.executor_backend == "claude_code"
        assert args.self_review_backend == ""


def test_lite_sets_collaboration_max_rounds_3():
    args = _ns(tier="lite")
    _, policy = _resolve_tier_and_policy(args=args, feature="f1", requirements_text="", changed_files=())
    _apply_policy_to_args(args=args, policy=policy)
    assert args.collaboration_max_rounds == 3


def test_standard_sets_collaboration_max_rounds_2():
    args = _ns(tier="standard")
    _, policy = _resolve_tier_and_policy(args=args, feature="f1", requirements_text="", changed_files=())
    _apply_policy_to_args(args=args, policy=policy)
    assert args.collaboration_max_rounds == 2


def test_heavy_sets_collaboration_max_rounds_3():
    args = _ns(tier="heavy")
    _, policy = _resolve_tier_and_policy(args=args, feature="f1", requirements_text="", changed_files=())
    _apply_policy_to_args(args=args, policy=policy)
    assert args.collaboration_max_rounds == 3


def test_auto_tier_sets_collaboration_max_rounds_from_detected_policy():
    args = _ns(tier="auto")
    _, policy = _resolve_tier_and_policy(args=args, feature="f1", requirements_text="", changed_files=())
    _apply_policy_to_args(args=args, policy=policy)
    assert args.collaboration_max_rounds == policy.review_max_rounds


# ---------------------------------------------------------------------------
# _policy_auto_eval
# ---------------------------------------------------------------------------


def test_policy_auto_eval_false_when_policy_inactive():
    runtime = {"policy_active": False, "workflow_policy": None}
    assert _policy_auto_eval(runtime) is False


def test_policy_auto_eval_false_when_no_policy():
    runtime = {"policy_active": True, "workflow_policy": None}
    assert _policy_auto_eval(runtime) is False


def test_policy_auto_eval_false_for_lite_policy():
    args = _ns(tier="lite")
    _, policy = _resolve_tier_and_policy(
        args=args, feature="f1", requirements_text="", changed_files=(),
    )
    runtime = {"policy_active": True, "workflow_policy": policy}
    assert _policy_auto_eval(runtime) is False  # lite.eval_required = False


def test_policy_auto_eval_false_for_standard_policy():
    args = _ns(tier="standard")
    _, policy = _resolve_tier_and_policy(
        args=args, feature="f1", requirements_text="", changed_files=(),
    )
    runtime = {"policy_active": True, "workflow_policy": policy}
    assert _policy_auto_eval(runtime) is False  # standard.eval_required = False


def test_policy_auto_eval_true_for_heavy_policy():
    args = _ns(tier="heavy")
    _, policy = _resolve_tier_and_policy(
        args=args, feature="f1", requirements_text="", changed_files=(),
    )
    runtime = {"policy_active": True, "workflow_policy": policy}
    assert _policy_auto_eval(runtime) is True  # heavy.eval_required = True


# ---------------------------------------------------------------------------
# _attempt_auto_telemetry resilience
# ---------------------------------------------------------------------------


def test_attempt_auto_telemetry_returns_dict_on_failure(tmp_path, monkeypatch):
    """Missing telemetry module must not raise."""
    def broken_import(*a, **kw):
        raise ImportError("module_missing")

    monkeypatch.setattr(
        "kodawari.cli.telemetry_cmd.run_telemetry_command",
        lambda args: (_ for _ in ()).throw(ImportError("x")),
    )
    result = _attempt_auto_telemetry(
        project_root=tmp_path, planning_dir=tmp_path / "planning" / "f1",
    )
    assert result["status"] in {"ERROR", "PASS"}
    assert "status" in result
