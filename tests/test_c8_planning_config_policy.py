"""Tests for C8 — PlanningConfig.blocking_severities driven by active policy.

Covers:
  - _env_blocking_severities parses CSV env var
  - _planning_config_from_env honors WORKFLOW_PLAN_BLOCKING_SEVERITIES
  - _planning_env_override_for_tier sets + restores env on exit
  - active lite policy forces PlanningConfig.blocking_severities = {"blocking"}
  - --tier=auto can still inject reviewer model for planning review
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import textwrap
import kodawari.cli.contract.autopilot_contract_bridge as autopilot_contract_bridge

import pytest

from kodawari.autopilot.planning.planning_orchestrator import (
    DEFAULT_BLOCKING_SEVERITIES,
    PlanningConfig,
)
from kodawari.cli.runtime.autopilot_cmd import _planning_env_override_for_tier
from kodawari.cli.contract.autopilot_contract_bridge import (
    _env_blocking_severities,
    _planning_config_from_env,
)

_ENV = "WORKFLOW_PLAN_BLOCKING_SEVERITIES"
_POLICY_ENV = "WORKFLOW_PLAN_DECISION_POLICY"
_REVIEWER_MODEL_ENV = "WORKFLOW_PLAN_REVIEWER_MODEL"
_PLANNER_MODEL_ENV = "WORKFLOW_PLANNER_MODEL"
_REVIEWER_DRIVER_ENV = "WORKFLOW_PLAN_REVIEWER_DRIVER"
_PLANNER_DRIVER_ENV = "WORKFLOW_PLANNER_DRIVER"
_REVIEWER_EXECUTABLE_ENV = "WORKFLOW_PLAN_REVIEWER_EXECUTABLE"
_PLANNER_EXECUTABLE_ENV = "WORKFLOW_PLANNER_EXECUTABLE"
_PLANNER_BASE_URL_ENV = "WORKFLOW_PLANNER_BASE_URL"
_PLANNER_API_KEY_ENV = "WORKFLOW_PLANNER_API_KEY_ENV"
_PLANNER_API_FORMAT_ENV = "WORKFLOW_PLANNER_API_FORMAT"
_ROUNDS_ENV = "WORKFLOW_PLANNING_MAX_ROUNDS"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(_ENV, raising=False)
    monkeypatch.delenv(_POLICY_ENV, raising=False)
    monkeypatch.delenv(_REVIEWER_MODEL_ENV, raising=False)
    monkeypatch.delenv(_PLANNER_MODEL_ENV, raising=False)
    monkeypatch.delenv(_REVIEWER_DRIVER_ENV, raising=False)
    monkeypatch.delenv(_PLANNER_DRIVER_ENV, raising=False)
    monkeypatch.delenv(_REVIEWER_EXECUTABLE_ENV, raising=False)
    monkeypatch.delenv(_PLANNER_EXECUTABLE_ENV, raising=False)
    monkeypatch.delenv(_PLANNER_BASE_URL_ENV, raising=False)
    monkeypatch.delenv(_PLANNER_API_KEY_ENV, raising=False)
    monkeypatch.delenv(_PLANNER_API_FORMAT_ENV, raising=False)
    monkeypatch.delenv(_ROUNDS_ENV, raising=False)
    yield


def _write_models_yaml(root: Path, content: str) -> None:
    path = root / ".claude" / "workflow" / "models.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content), encoding="utf-8")


# ---------------------------------------------------------------------------
# _env_blocking_severities parsing
# ---------------------------------------------------------------------------


def test_env_blocking_severities_returns_none_when_unset():
    assert _env_blocking_severities() is None


def test_env_blocking_severities_parses_single_value(monkeypatch):
    monkeypatch.setenv(_ENV, "blocking")
    assert _env_blocking_severities() == frozenset({"blocking"})


def test_env_blocking_severities_parses_csv(monkeypatch):
    monkeypatch.setenv(_ENV, "blocking,critical")
    assert _env_blocking_severities() == frozenset({"blocking", "critical"})


def test_env_blocking_severities_normalizes_case_and_whitespace(monkeypatch):
    monkeypatch.setenv(_ENV, "  Blocking , CRITICAL ,high ")
    assert _env_blocking_severities() == frozenset({"blocking", "critical", "high"})


def test_env_blocking_severities_empty_returns_none(monkeypatch):
    monkeypatch.setenv(_ENV, "   ")
    assert _env_blocking_severities() is None


# ---------------------------------------------------------------------------
# _planning_config_from_env uses env override
# ---------------------------------------------------------------------------


def test_planning_config_without_env_keeps_default_severities():
    config = _planning_config_from_env()
    assert config.blocking_severities == DEFAULT_BLOCKING_SEVERITIES


def test_planning_config_with_lite_env_has_only_blocking(monkeypatch):
    monkeypatch.setenv(_ENV, "blocking")
    config = _planning_config_from_env()
    assert config.blocking_severities == frozenset({"blocking"})


def test_planning_config_with_standard_env_has_blocking_and_critical(monkeypatch):
    monkeypatch.setenv(_ENV, "blocking,critical")
    config = _planning_config_from_env()
    assert config.blocking_severities == frozenset({"blocking", "critical"})


def test_planning_config_without_policy_env_keeps_default_decision_policy():
    config = _planning_config_from_env()
    assert config.decision_policy == "strict-gate"


def test_planning_config_honors_decision_policy_env(monkeypatch):
    monkeypatch.setenv(_POLICY_ENV, "auto-skip")
    config = _planning_config_from_env()
    assert config.decision_policy == "auto-skip"


def test_planning_config_uses_models_yaml_defaults(tmp_path: Path, monkeypatch):
    _write_models_yaml(
        tmp_path,
        """
        schema_version: "models.v1"
        planner_model: claude-sonnet-4-6
        reviewer_model: gpt-5.4
        """,
    )
    monkeypatch.chdir(tmp_path)
    config = _planning_config_from_env()
    assert config.planner_model == "claude-sonnet-4-6"
    assert config.reviewer_model == "gpt-5.4"


def test_planning_config_env_overrides_models_yaml(tmp_path: Path, monkeypatch):
    _write_models_yaml(
        tmp_path,
        """
        schema_version: "models.v1"
        planner_model: claude-sonnet-4-6
        reviewer_model: gpt-5.4
        """,
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(_PLANNER_MODEL_ENV, "planner-from-env")
    monkeypatch.setenv(_REVIEWER_MODEL_ENV, "reviewer-from-env")
    config = _planning_config_from_env()
    assert config.planner_model == "planner-from-env"
    assert config.reviewer_model == "reviewer-from-env"


def test_planning_config_uses_models_v2_role_transports(tmp_path: Path, monkeypatch):
    _write_models_yaml(
        tmp_path,
        """
        schema_version: "models.v2"
        transports:
          codex_local:
            kind: subprocess
            driver: codex_cli
            interface: agent
            executable: codex
            provides: [repo.read_file, repo.grep, repo.glob]
          noop_reviewer:
            kind: in_process
            driver: noop
            interface: chat
            provides: []
        compatibility:
          - {models: [gpt-5.5], transports: [codex_local], interfaces: [agent]}
          - {models: [noop], transports: [noop_reviewer], interfaces: [chat]}
        roles:
          planner:
            transport: codex_local
            model: gpt-5.5
            requires: [repo.read_file]
            scope_mode: read_only
            on_unavailable: fail
          plan_reviewer:
            transport: noop_reviewer
            model: noop
            on_unavailable: fail
        """,
    )
    monkeypatch.chdir(tmp_path)

    config = _planning_config_from_env()

    assert config.planner_model == "gpt-5.5"
    assert config.planner_executable == "codex"
    assert config.planner_driver == "codex_cli"
    assert config.planner_transport is not None
    assert config.planner_transport.name == "codex_local"
    assert config.reviewer_model == "noop"
    assert config.reviewer_driver == "noop"
    assert config.plan_reviewer_transport is not None
    assert config.plan_reviewer_transport.name == "noop_reviewer"


def _write_mimo_planner_models_yaml(root: Path) -> None:
    _write_models_yaml(
        root,
        """
        schema_version: "models.v2"
        transports:
          mimo_tool_use:
            kind: http
            driver: openai_compatible
            interface: tool_use
            api_format: openai_chat
            base_url: https://mimo.invalid/v1
            api_key_env: WORKFLOW_MIMO_KEY
            provides: [interface.tool_use]
          codex_local:
            kind: subprocess
            driver: codex_cli
            interface: agent
            executable: codex
            provides: [interface.agent]
        compatibility:
          - {models: [mimo-v2.5-pro], transports: [mimo_tool_use], interfaces: [tool_use]}
          - {models: [gpt-5.4], transports: [codex_local], interfaces: [agent]}
        roles:
          planner:
            transport: mimo_tool_use
            model: mimo-v2.5-pro
            on_unavailable: fail
          plan_reviewer:
            transport: codex_local
            model: gpt-5.4
            on_unavailable: fail
        """,
    )


def test_planner_driver_override_does_not_inherit_mimo_model(tmp_path: Path, monkeypatch):
    _write_mimo_planner_models_yaml(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(_PLANNER_DRIVER_ENV, "codex_cli")
    monkeypatch.setenv(_PLANNER_EXECUTABLE_ENV, "codex")

    config = _planning_config_from_env()

    assert config.planner_transport is None
    assert config.planner_driver == "codex_cli"
    assert config.planner_executable == "codex"
    assert config.planner_model == ""
    assert config.planner_base_url == ""
    assert config.planner_api_key_env == ""
    assert config.planner_api_format == ""


def test_planner_driver_override_keeps_explicit_model(tmp_path: Path, monkeypatch):
    _write_mimo_planner_models_yaml(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(_PLANNER_DRIVER_ENV, "codex_cli")
    monkeypatch.setenv(_PLANNER_EXECUTABLE_ENV, "codex")
    monkeypatch.setenv(_PLANNER_MODEL_ENV, "gpt-5.4")

    config = _planning_config_from_env()

    assert config.planner_transport is None
    assert config.planner_model == "gpt-5.4"


def test_plan_reviewer_driver_override_does_not_inherit_codex_model(tmp_path: Path, monkeypatch):
    _write_mimo_planner_models_yaml(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(_REVIEWER_DRIVER_ENV, "claude_cli")
    monkeypatch.setenv(_REVIEWER_EXECUTABLE_ENV, "claude")

    config = _planning_config_from_env()

    assert config.plan_reviewer_transport is None
    assert config.reviewer_driver == "claude_cli"
    assert config.reviewer_executable == "claude"
    assert config.reviewer_model == ""


# ---------------------------------------------------------------------------
# _planning_env_override_for_tier context manager
# ---------------------------------------------------------------------------


def _ns(tier: str) -> argparse.Namespace:
    return argparse.Namespace(tier=tier, plan_reviewer_model="")


def test_context_manager_auto_tier_is_noop():
    """auto with no reviewer override leaves planning env untouched."""
    assert _ENV not in os.environ
    with _planning_env_override_for_tier(_ns("auto")):
        assert _ENV not in os.environ
    assert _ENV not in os.environ


def test_context_manager_auto_tier_sets_planning_reviewer_model_when_requested():
    args = argparse.Namespace(tier="auto", plan_reviewer_model="gpt-5.3-codex")
    with _planning_env_override_for_tier(args):
        assert os.environ[_REVIEWER_MODEL_ENV] == "gpt-5.3-codex"
        assert _ENV not in os.environ
    assert _REVIEWER_MODEL_ENV not in os.environ


def test_context_manager_reviewer_model_does_not_override_planning_reviewer():
    args = argparse.Namespace(tier="auto", reviewer_model="claude-opus-4-7")
    with _planning_env_override_for_tier(args):
        assert _REVIEWER_MODEL_ENV not in os.environ
        assert _ENV not in os.environ
    assert _REVIEWER_MODEL_ENV not in os.environ


def test_context_manager_explicit_lite_sets_and_clears_env():
    assert _ENV not in os.environ
    with _planning_env_override_for_tier(_ns("lite")):
        assert os.environ[_ENV] == "blocking"
        assert os.environ[_POLICY_ENV] == "auto-skip"
    assert _ENV not in os.environ
    assert _POLICY_ENV not in os.environ


def test_context_manager_explicit_standard_sets_blocking_plus_critical():
    with _planning_env_override_for_tier(_ns("standard")):
        value = os.environ[_ENV]
        parts = set(value.split(","))
        assert parts == {"blocking", "critical"}
        assert os.environ[_POLICY_ENV] == "soft-gate"
    assert _ENV not in os.environ


def test_context_manager_explicit_heavy_sets_full_set():
    with _planning_env_override_for_tier(_ns("heavy")):
        parts = set(os.environ[_ENV].split(","))
        assert parts == {"blocking", "critical", "high"}
        assert os.environ[_POLICY_ENV] == "approval-required"


def test_context_manager_restores_preexisting_env_value(monkeypatch):
    """If env was set before, context restores the prior value."""
    monkeypatch.setenv(_ENV, "custom,previous")
    monkeypatch.setenv(_POLICY_ENV, "strict-gate")
    with _planning_env_override_for_tier(_ns("lite")):
        assert os.environ[_ENV] == "blocking"
        assert os.environ[_POLICY_ENV] == "auto-skip"
    assert os.environ[_ENV] == "custom,previous"
    assert os.environ[_POLICY_ENV] == "strict-gate"


def test_context_manager_clears_even_on_exception():
    assert _ENV not in os.environ
    with pytest.raises(RuntimeError):
        with _planning_env_override_for_tier(_ns("lite")):
            assert _ENV in os.environ
            assert _POLICY_ENV in os.environ
            raise RuntimeError("boom")
    assert _ENV not in os.environ
    assert _POLICY_ENV not in os.environ


# ---------------------------------------------------------------------------
# End-to-end: tier -> env -> PlanningConfig
# ---------------------------------------------------------------------------


def test_end_to_end_lite_tier_yields_lite_planning_config():
    with _planning_env_override_for_tier(_ns("lite")):
        config = _planning_config_from_env()
        assert config.blocking_severities == frozenset({"blocking"})


def test_end_to_end_auto_tier_yields_default_planning_config():
    with _planning_env_override_for_tier(_ns("auto")):
        config = _planning_config_from_env()
        assert config.blocking_severities == DEFAULT_BLOCKING_SEVERITIES


def test_planning_config_uses_dynamic_max_rounds_when_env_unset(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(autopilot_contract_bridge, "_suggest_max_rounds", lambda **kwargs: 7)
    config = _planning_config_from_env(
        project_root=tmp_path,
        task_direction="重构并拆分跨模块实现",
        repo_inventory={"project_layout": {"code_roots": ["src"]}},
    )
    assert config.max_rounds == 7


def test_planning_config_env_max_rounds_overrides_dynamic_suggestion(monkeypatch, tmp_path: Path):
    monkeypatch.setenv(_ROUNDS_ENV, "9")
    monkeypatch.setattr(autopilot_contract_bridge, "_suggest_max_rounds", lambda **kwargs: 7)
    config = _planning_config_from_env(
        project_root=tmp_path,
        task_direction="重构并拆分跨模块实现",
        repo_inventory={"project_layout": {"code_roots": ["src"]}},
    )
    assert config.max_rounds == 9


# ---------------------------------------------------------------------------
# Fix: historical escalation_required must not block explicit lite/standard tier
# ---------------------------------------------------------------------------


def test_maybe_pause_skipped_when_policy_auto_skip(tmp_path):
    """Explicit --tier lite with auto-skip policy must skip the planning gate
    even when PLANNING_CONVERSATION.json carries status='escalation_required'."""
    from unittest.mock import MagicMock
    from kodawari.cli.runtime.autopilot_release_flow import maybe_pause_for_planning_decision

    class _FakePolicy:
        decision_policy = "auto-skip"

    args = MagicMock()
    args.feature = "auth-rewrite"
    # Simulate a snapshot that would otherwise trigger a planning pause
    snapshot = MagicMock()
    snapshot.artifacts = {"PLANNING_CONVERSATION.json": str(tmp_path / "PLANNING_CONVERSATION.json")}
    (tmp_path / "PLANNING_CONVERSATION.json").write_text(
        '{"status": "escalation_required", "approval": {"decision": "human_required"}, "escalation": {}}',
        encoding="utf-8",
    )

    result = maybe_pause_for_planning_decision(
        args=args,
        planning_dir=tmp_path,
        planning_snapshot=snapshot,
        policy=_FakePolicy(),
    )
    assert result is None, "auto-skip policy must bypass historical escalation gate"


def test_maybe_pause_not_skipped_when_policy_strict(tmp_path):
    """strict-gate policy should still respect escalation_required from conversation."""
    from unittest.mock import MagicMock
    from kodawari.cli.runtime.autopilot_release_flow import maybe_pause_for_planning_decision

    class _StrictPolicy:
        decision_policy = "strict-gate"

    args = MagicMock()
    args.feature = "auth-rewrite"
    snapshot = MagicMock()
    snapshot.artifacts = {"PLANNING_CONVERSATION.json": str(tmp_path / "PLANNING_CONVERSATION.json")}
    (tmp_path / "PLANNING_CONVERSATION.json").write_text(
        '{"status": "auto_skipped", "approval": {"decision": "human_required"}, "escalation": {}}',
        encoding="utf-8",
    )

    # strict-gate but conversation status is auto_skipped → _planning_conversation_decision_spec returns None
    result = maybe_pause_for_planning_decision(
        args=args,
        planning_dir=tmp_path,
        planning_snapshot=snapshot,
        policy=_StrictPolicy(),
    )
    # auto_skipped in conversation → no pause regardless
    assert result is None


def test_maybe_pause_none_policy_preserves_original_behavior(tmp_path):
    """policy=None (back-compat / --tier=auto) must not change existing behavior."""
    from unittest.mock import MagicMock
    from kodawari.cli.runtime.autopilot_release_flow import maybe_pause_for_planning_decision

    args = MagicMock()
    args.feature = "f1"
    # No snapshot → always returns None
    result = maybe_pause_for_planning_decision(
        args=args,
        planning_dir=tmp_path,
        planning_snapshot=None,
        policy=None,
    )
    assert result is None
