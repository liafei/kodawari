"""Tests for kodawari.autopilot.core.model_config."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from kodawari.autopilot.core.model_config import (
    WorkflowModelConfig,
    WorkflowModelConfigError,
    load_model_config,
    migrate_v1_to_v2,
)

_YAML_DIR = ".claude/workflow"
_YAML_NAME = "models.yaml"


def _write_models_yaml(tmp_path: Path, content: str) -> Path:
    d = tmp_path / _YAML_DIR
    d.mkdir(parents=True, exist_ok=True)
    p = d / _YAML_NAME
    p.write_text(textwrap.dedent(content), encoding="utf-8")
    return p


def test_load_model_config_no_file_returns_empty(tmp_path: Path) -> None:
    mc = load_model_config(tmp_path)
    assert mc == WorkflowModelConfig()


def test_codex_opus_mimo_template_loads_as_models_v2(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    template = repo_root / "docs" / "operations" / "models_v2_codex_opus_mimo_template.yaml"
    _write_models_yaml(tmp_path, template.read_text(encoding="utf-8"))

    mc = load_model_config(tmp_path)

    assert mc.roles["executor"].model == "mimo-v2.5-pro"
    assert mc.roles["executor"].execution_protocol == "exact_str_replace_v1"
    assert mc.transports["mimo_openai"].executor_backend() == "openai_tool_use"
    assert mc.roles["planner"].model == "gpt-5.5"
    assert mc.roles["impl_reviewer"].model == "claude-opus-4-7"


def test_load_model_config_full_yaml(tmp_path: Path) -> None:
    _write_models_yaml(tmp_path, """
        schema_version: "models.v1"
        planner_model: claude-sonnet-4-6
        executor_model: gpt-5.4
        reviewer_model: gpt-5.4
        plan_reviewer_model: gpt-5.4
        reviewer_backend: codex
        review_enabled: true
    """)
    mc = load_model_config(tmp_path)
    assert mc.planner_model == "claude-sonnet-4-6"
    assert mc.executor_model == "gpt-5.4"
    assert mc.reviewer_model == "gpt-5.4"
    assert mc.plan_reviewer_model == "gpt-5.4"
    assert mc.reviewer_backend == "codex"
    assert mc.review_enabled is True


def test_load_model_config_partial_yaml(tmp_path: Path) -> None:
    _write_models_yaml(tmp_path, """
        schema_version: "models.v1"
        planner_model: claude-sonnet-4-6
    """)
    mc = load_model_config(tmp_path)
    assert mc.planner_model == "claude-sonnet-4-6"
    assert mc.executor_model == ""
    assert mc.reviewer_model == ""
    assert mc.review_enabled is None


def test_load_model_config_wrong_schema_returns_empty(tmp_path: Path) -> None:
    _write_models_yaml(tmp_path, """
        schema_version: "models.v99"
        planner_model: some-model
    """)
    mc = load_model_config(tmp_path)
    assert mc == WorkflowModelConfig()


def test_load_model_config_invalid_backend_ignored(tmp_path: Path) -> None:
    _write_models_yaml(tmp_path, """
        schema_version: "models.v1"
        reviewer_backend: invalid-backend
    """)
    mc = load_model_config(tmp_path)
    assert mc.reviewer_backend == ""


def test_load_model_config_review_enabled_false(tmp_path: Path) -> None:
    _write_models_yaml(tmp_path, """
        schema_version: "models.v1"
        review_enabled: false
    """)
    mc = load_model_config(tmp_path)
    assert mc.review_enabled is False


def test_load_model_config_review_enabled_string_variants(tmp_path: Path) -> None:
    for truthy in ("true", "1", "yes", "on"):
        _write_models_yaml(tmp_path, f"schema_version: 'models.v1'\nreview_enabled: {truthy}")
        assert load_model_config(tmp_path).review_enabled is True
    for falsy in ("false", "0", "no", "off"):
        _write_models_yaml(tmp_path, f"schema_version: 'models.v1'\nreview_enabled: {falsy}")
        assert load_model_config(tmp_path).review_enabled is False


def test_load_model_config_missing_schema_returns_empty(tmp_path: Path) -> None:
    _write_models_yaml(tmp_path, "planner_model: something")
    mc = load_model_config(tmp_path)
    assert mc == WorkflowModelConfig()


def test_load_model_config_accepts_path_str(tmp_path: Path) -> None:
    _write_models_yaml(tmp_path, """
        schema_version: "models.v1"
        executor_model: gpt-5.4
    """)
    mc = load_model_config(str(tmp_path))
    assert mc.executor_model == "gpt-5.4"


class TestExecutorModelsPerBackend:
    """Regression: backend-aware executor_model resolution.

    Before this was added, yaml had one flat `executor_model` field; switching
    executor backend via `--executor-backend` CLI override would still pass the
    wrong model (e.g. a codex model to `claude --model ...`), blocking the
    entire IMPLEMENT phase.
    """

    def test_per_backend_entry_wins_over_flat(self, tmp_path: Path) -> None:
        _write_models_yaml(tmp_path, """
            schema_version: "models.v1"
            executor_model: gpt-5.3-codex
            executor_models:
              claude_code: claude-sonnet-4-6
              codex_cli: gpt-5.3-codex
        """)
        mc = load_model_config(tmp_path)
        assert mc.resolve_executor_model("claude_code") == "claude-sonnet-4-6"
        assert mc.resolve_executor_model("codex_cli") == "gpt-5.3-codex"

    def test_backend_missing_falls_back_to_flat(self, tmp_path: Path) -> None:
        _write_models_yaml(tmp_path, """
            schema_version: "models.v1"
            executor_model: gpt-5.3-codex
            executor_models:
              codex_cli: gpt-5.3-codex
        """)
        mc = load_model_config(tmp_path)
        # No `claude_code` key in executor_models → fall back to flat value
        assert mc.resolve_executor_model("claude_code") == "gpt-5.3-codex"

    def test_empty_backend_returns_flat(self, tmp_path: Path) -> None:
        _write_models_yaml(tmp_path, """
            schema_version: "models.v1"
            executor_model: gpt-5.3-codex
            executor_models:
              claude_code: claude-sonnet-4-6
        """)
        mc = load_model_config(tmp_path)
        assert mc.resolve_executor_model("") == "gpt-5.3-codex"

    def test_invalid_executor_models_entries_skipped(self, tmp_path: Path) -> None:
        # Empty / null values must not crash the loader; malformed entries drop.
        _write_models_yaml(tmp_path, """
            schema_version: "models.v1"
            executor_model: gpt-5.3-codex
            executor_models:
              claude_code: claude-sonnet-4-6
              bogus: ""
              empty_key:
        """)
        mc = load_model_config(tmp_path)
        assert mc.executor_models == {"claude_code": "claude-sonnet-4-6"}

    def test_executor_models_not_mapping_ignored(self, tmp_path: Path) -> None:
        _write_models_yaml(tmp_path, """
            schema_version: "models.v1"
            executor_model: gpt-5.3-codex
            executor_models: "not a mapping"
        """)
        mc = load_model_config(tmp_path)
        assert mc.executor_models == {}
        # Flat value still resolves
        assert mc.resolve_executor_model("claude_code") == "gpt-5.3-codex"

    def test_key_normalization_lowercased(self, tmp_path: Path) -> None:
        _write_models_yaml(tmp_path, """
            schema_version: "models.v1"
            executor_models:
              CLAUDE_CODE: claude-sonnet-4-6
        """)
        mc = load_model_config(tmp_path)
        assert mc.resolve_executor_model("claude_code") == "claude-sonnet-4-6"
        assert mc.resolve_executor_model("Claude_Code") == "claude-sonnet-4-6"


class TestModelsV2Config:
    def test_loads_role_transport_pool_and_projects_legacy_fields(self, tmp_path: Path) -> None:
        _write_models_yaml(tmp_path, """
            schema_version: "models.v2"
            transports:
              codex_local:
                kind: subprocess
                driver: codex_cli
                interface: agent
                executable: codex
                provides: [repo.read_file, repo.grep, repo.glob, repo.write_file, shell.exec]
              claude_mcp_review:
                kind: subprocess
                driver: claude_cli
                interface: mcp
                host_executable: claude
                mcp_server: kodawari.autopilot.review.mcp_review_server
                provides: [repo.read_file, repo.grep, repo.glob]
            compatibility:
              - models: [gpt-5.5]
                transports: [codex_local]
                interfaces: [agent]
              - model: "claude-*"
                transports: [claude_mcp_review]
                interfaces: [mcp]
            roles:
              planner:
                transport: codex_local
                model: gpt-5.5
                requires: [repo.read_file, repo.grep, repo.glob]
                scope_mode: read_only
                on_unavailable: fail
              plan_reviewer:
                transport: codex_local
                model: gpt-5.5
                on_unavailable: fail
              self_reviewer:
                transport: codex_local
                model: gpt-5.5
                on_unavailable: degrade_to_simulate
              impl_reviewer:
                transport: claude_mcp_review
                model: claude-opus-4-7
                on_unavailable: fail
              executor:
                transport: codex_local
                model: gpt-5.5
                scope_mode: post_diff
                on_unavailable: fail
        """)

        mc = load_model_config(tmp_path)

        assert mc.schema_version == "models.v2"
        assert mc.planner_model == "gpt-5.5"
        assert mc.plan_reviewer_model == "gpt-5.5"
        assert mc.reviewer_model == "claude-opus-4-7"
        assert mc.reviewer_backend == "mcp"
        assert mc.resolve_executor_model("codex_cli") == "gpt-5.5"
        assert mc.role_driver("planner") == "codex_cli"
        assert mc.role_executable("impl_reviewer") == "claude"
        assert mc.get_role("self_reviewer") is not None

    def test_v2_ignores_v1_fields_without_merging(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        _write_models_yaml(tmp_path, """
            schema_version: "models.v2"
            planner_model: claude-ignored
            transports:
              codex_local:
                kind: subprocess
                driver: codex_cli
                interface: agent
                executable: codex
                provides: [repo.read_file, repo.grep, repo.glob, repo.write_file]
            compatibility:
              - {models: [gpt-5.5], transports: [codex_local], interfaces: [agent]}
            roles:
              planner: {transport: codex_local, model: gpt-5.5, requires: [repo.read_file], scope_mode: read_only, on_unavailable: fail}
              executor: {transport: codex_local, model: gpt-5.5, scope_mode: post_diff, on_unavailable: fail}
        """)

        mc = load_model_config(tmp_path)

        assert mc.planner_model == "gpt-5.5"
        assert "models.v2 ignores v1 fields" in caplog.text

    def test_v1_ignores_v2_fields_without_merging(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        _write_models_yaml(tmp_path, """
            schema_version: "models.v1"
            planner_model: claude-sonnet-4-6
            transports:
              codex_local: {kind: subprocess, driver: codex_cli, interface: agent, executable: codex}
            roles:
              planner: {transport: codex_local, model: gpt-5.5}
        """)

        mc = load_model_config(tmp_path)

        assert mc.planner_model == "claude-sonnet-4-6"
        assert mc.roles == {}
        assert "models.v1 ignores v2 fields" in caplog.text

    def test_incompatible_model_transport_tuple_hard_fails(self, tmp_path: Path) -> None:
        _write_models_yaml(tmp_path, """
            schema_version: "models.v2"
            transports:
              codex_local:
                kind: subprocess
                driver: codex_cli
                interface: agent
                executable: codex
                provides: [repo.read_file, repo.grep, repo.glob]
            compatibility:
              - {model: "gpt-*", transports: [codex_local], interfaces: [agent]}
            roles:
              planner: {transport: codex_local, model: claude-opus-4-7, requires: [repo.read_file], scope_mode: read_only, on_unavailable: fail}
        """)

        with pytest.raises(WorkflowModelConfigError, match="not compatible"):
            load_model_config(tmp_path)

    def test_planner_declared_tools_must_be_supported_by_transport(self, tmp_path: Path) -> None:
        _write_models_yaml(tmp_path, """
            schema_version: "models.v2"
            transports:
              mimo_api:
                kind: http
                driver: openai_compatible
                interface: chat
                api_format: openai
                base_url_env: WORKFLOW_MIMO_BASE_URL
                api_key_env: WORKFLOW_MIMO_KEY
            compatibility:
              - {model: "mimo-*", transports: [mimo_api], interfaces: [chat]}
            roles:
              planner:
                transport: mimo_api
                model: mimo-v2.5-pro
                requires: [repo.read_file, repo.grep, repo.glob]
                on_unavailable: fail
        """)

        with pytest.raises(WorkflowModelConfigError, match="requires .*repo.read_file"):
            load_model_config(tmp_path)

    def test_http_tool_use_planner_is_allowed(self, tmp_path: Path) -> None:
        _write_models_yaml(tmp_path, """
            schema_version: "models.v2"
            transports:
              mimo_tool_use:
                kind: http
                driver: openai_compatible
                interface: tool_use
                api_format: openai_chat
                base_url: https://example.test/v1
                api_key_env: WORKFLOW_MIMO_KEY
                provides: [interface.tool_use, repo.read_file, repo.grep, repo.glob]
            compatibility:
              - {model: "mimo-*", transports: [mimo_tool_use], interfaces: [tool_use], api_formats: [openai_chat]}
            roles:
              planner:
                transport: mimo_tool_use
                model: mimo-v2.5-pro
                requires: [interface.tool_use]
                on_unavailable: fail
        """)

        mc = load_model_config(tmp_path)

        assert mc.roles["planner"].transport == "mimo_tool_use"
        assert mc.transport_for_role("planner").interface == "tool_use"

    def test_chat_api_executor_is_rejected_until_patch_protocol_v3(self, tmp_path: Path) -> None:
        _write_models_yaml(tmp_path, """
            schema_version: "models.v2"
            transports:
              mimo_api:
                kind: http
                driver: openai_compatible
                interface: chat
                api_format: openai
                base_url_env: WORKFLOW_MIMO_BASE_URL
                api_key_env: WORKFLOW_MIMO_KEY
            compatibility:
              - {model: "mimo-*", transports: [mimo_api], interfaces: [chat]}
            roles:
              executor:
                transport: mimo_api
                model: mimo-v2.5-pro
                on_unavailable: fail
        """)

        with pytest.raises(WorkflowModelConfigError, match="patch-protocol execution is a v3 feature"):
            load_model_config(tmp_path)

    def test_agent_executor_requires_post_diff_scope_mode(self, tmp_path: Path) -> None:
        _write_models_yaml(tmp_path, """
            schema_version: "models.v2"
            transports:
              codex_local:
                kind: subprocess
                driver: codex_cli
                interface: agent
                executable: codex
            compatibility:
              - {model: "gpt-*", transports: [codex_local], interfaces: [agent]}
            roles:
              executor:
                transport: codex_local
                model: gpt-5.5
                scope_mode: inline_guard
                on_unavailable: fail
        """)

        with pytest.raises(WorkflowModelConfigError, match="expected 'post_diff'"):
            load_model_config(tmp_path)

    def test_tool_use_executor_requires_inline_guard_before_runner_support(self, tmp_path: Path) -> None:
        _write_models_yaml(tmp_path, """
            schema_version: "models.v2"
            transports:
              mimo_api:
                kind: http
                driver: openai_compatible
                interface: tool_use
                api_format: openai
                base_url_env: WORKFLOW_MIMO_BASE_URL
                api_key_env: WORKFLOW_MIMO_KEY
            compatibility:
              - {model: "mimo-*", transports: [mimo_api], interfaces: [tool_use]}
            roles:
              executor:
                transport: mimo_api
                model: mimo-v2.5-pro
                scope_mode: post_diff
                on_unavailable: fail
        """)

        with pytest.raises(WorkflowModelConfigError, match="expected 'inline_guard'"):
            load_model_config(tmp_path)

    def test_tool_use_executor_requires_runtime_caps(self, tmp_path: Path) -> None:
        _write_models_yaml(tmp_path, """
            schema_version: "models.v2"
            transports:
              mimo_api:
                kind: http
                driver: openai_compatible
                interface: tool_use
                api_format: openai
                base_url_env: WORKFLOW_MIMO_BASE_URL
                api_key_env: WORKFLOW_MIMO_KEY
            compatibility:
              - {model: "mimo-*", transports: [mimo_api], interfaces: [tool_use]}
            roles:
              executor:
                transport: mimo_api
                model: mimo-v2.5-pro
                scope_mode: inline_guard
                on_unavailable: fail
        """)

        with pytest.raises(WorkflowModelConfigError, match="runtime_caps missing"):
            load_model_config(tmp_path)

    def test_tool_use_executor_with_caps_selects_openai_tool_use_backend(self, tmp_path: Path) -> None:
        _write_models_yaml(tmp_path, """
            schema_version: "models.v2"
            transports:
              mimo_api:
                kind: http
                driver: openai_compatible
                interface: tool_use
                api_format: openai_chat
                base_url_env: WORKFLOW_MIMO_BASE_URL
                api_key_env: WORKFLOW_MIMO_KEY
                quota_group: mimo-paid
            compatibility:
              - {model: "mimo-*", transports: [mimo_api], interfaces: [tool_use]}
            roles:
              executor:
                transport: mimo_api
                model: mimo-v2.5-pro
                scope_mode: inline_guard
                runtime_caps:
                  max_tool_iterations: 30
                  max_token_budget: 200000
                  max_same_tool_calls_per_path: 5
                  max_tool_calls_per_response: 8
                  max_wall_clock_seconds: 1800
                  max_no_progress_iterations: 5
                  max_verify_retries: 2
                on_unavailable: fail
        """)

        mc = load_model_config(tmp_path)

        assert mc.roles["executor"].runtime_caps["max_verify_retries"] == 2
        assert mc.roles["executor"].execution_protocol == ""
        assert mc.transports["mimo_api"].executor_backend() == "openai_tool_use"

    def test_v2_accepts_executor_recovery_role_and_optional_budget_caps(self, tmp_path: Path) -> None:
        _write_models_yaml(tmp_path, """
            schema_version: "models.v2"
            transports:
              codex_local:
                kind: subprocess
                driver: codex_cli
                interface: agent
                executable: codex
                provides: [interface.agent, repo.read_file, repo.write_file]
              mimo_api:
                kind: http
                driver: openai_compatible
                interface: tool_use
                api_format: openai_chat
                base_url: https://example.test/v1
                api_key_env: WORKFLOW_MIMO_KEY
                provides: [interface.tool_use, repo.read_file, repo.write_file]
            compatibility:
              - {models: [gpt-5.5], transports: [codex_local], interfaces: [agent]}
              - {model: "mimo-*", transports: [mimo_api], interfaces: [tool_use], api_formats: [openai_chat]}
            roles:
              impl_reviewer:
                transport: codex_local
                model: gpt-5.5
                on_unavailable: fail
              executor_recovery:
                transport: codex_local
                model: gpt-5.5
                on_unavailable: fail
              executor:
                transport: mimo_api
                model: mimo-v2.5-pro
                requires: [interface.tool_use, repo.read_file, repo.write_file]
                scope_mode: inline_guard
                execution_protocol: exact_str_replace_v1
                runtime_caps:
                  max_tool_iterations: 30
                  max_token_budget: 200000
                  max_hard_token_budget: 1000000
                  max_same_tool_calls_per_path: 5
                  max_tool_calls_per_response: 8
                  max_wall_clock_seconds: 1800
                  max_no_progress_iterations: 5
                  max_no_write_iterations_under_budget_pressure: 2
                  max_redundant_read_count: 8
                  max_repeated_search_count: 6
                  max_patch_apply_failures: 3
                  max_recovery_attempts: 2
                  max_verify_retries: 2
                  verify_timeout_seconds: 300
                  http_timeout_seconds: 90
                  max_waf_retries: 1
                  max_full_read_tool_result_bytes: 24000
                on_unavailable: fail
        """)

        mc = load_model_config(tmp_path)

        assert mc.get_role("executor_recovery", fallback=False).model == "gpt-5.5"
        assert mc.roles["executor"].runtime_caps["max_hard_token_budget"] == 1000000
        assert mc.roles["executor"].runtime_caps["verify_timeout_seconds"] == 300
        assert mc.roles["executor"].runtime_caps["http_timeout_seconds"] == 90
        assert mc.roles["executor"].runtime_caps["max_waf_retries"] == 1
        assert mc.roles["executor"].runtime_caps["max_full_read_tool_result_bytes"] == 24000

    def test_tool_use_executor_accepts_exact_str_replace_protocol(self, tmp_path: Path) -> None:
        _write_models_yaml(tmp_path, """
            schema_version: "models.v2"
            transports:
              mimo_api:
                kind: http
                driver: openai_compatible
                interface: tool_use
                api_format: openai_chat
            compatibility:
              - {model: "mimo-*", transports: [mimo_api], interfaces: [tool_use]}
            roles:
              executor:
                transport: mimo_api
                model: mimo-v2.5-pro
                scope_mode: inline_guard
                execution_protocol: exact_str_replace_v1
                runtime_caps:
                  max_tool_iterations: 30
                  max_token_budget: 200000
                  max_same_tool_calls_per_path: 5
                  max_tool_calls_per_response: 8
                  max_wall_clock_seconds: 1800
                  max_no_progress_iterations: 5
                  max_verify_retries: 2
                on_unavailable: fail
        """)

        mc = load_model_config(tmp_path)

        assert mc.roles["executor"].execution_protocol == "exact_str_replace_v1"

    def test_agent_executor_rejects_exact_str_replace_protocol(self, tmp_path: Path) -> None:
        _write_models_yaml(tmp_path, """
            schema_version: "models.v2"
            transports:
              codex_local:
                kind: subprocess
                driver: codex_cli
                interface: agent
                executable: codex
            compatibility:
              - {model: "gpt-*", transports: [codex_local], interfaces: [agent]}
            roles:
              executor:
                transport: codex_local
                model: gpt-5.5
                scope_mode: post_diff
                execution_protocol: exact_str_replace_v1
                on_unavailable: fail
        """)

        with pytest.raises(WorkflowModelConfigError, match="execution_protocol"):
            load_model_config(tmp_path)

    def test_transport_quota_group_defaults_to_transport_name(self, tmp_path: Path) -> None:
        _write_models_yaml(tmp_path, """
            schema_version: "models.v2"
            transports:
              codex_local:
                kind: subprocess
                driver: codex_cli
                interface: agent
                executable: codex
            compatibility:
              - {model: "gpt-*", transports: [codex_local], interfaces: [agent]}
            roles:
              planner:
                transport: codex_local
                model: gpt-5.5
                on_unavailable: fail
        """)

        mc = load_model_config(tmp_path)

        assert mc.transports["codex_local"].quota_group == "codex_local"

    def test_role_specific_reviewer_falls_back_to_generic_reviewer(self, tmp_path: Path) -> None:
        _write_models_yaml(tmp_path, """
            schema_version: "models.v2"
            transports:
              codex_local:
                kind: subprocess
                driver: codex_cli
                interface: agent
                executable: codex
                provides: [repo.read_file, repo.grep, repo.glob]
            compatibility:
              - {model: "gpt-*", transports: [codex_local], interfaces: [agent]}
            roles:
              reviewer: {transport: codex_local, model: gpt-5.5, on_unavailable: fail}
              executor: {transport: codex_local, model: gpt-5.5, scope_mode: post_diff, on_unavailable: fail}
        """)

        mc = load_model_config(tmp_path)

        assert mc.plan_reviewer_model == "gpt-5.5"
        assert mc.reviewer_model == "gpt-5.5"
        assert mc.reviewer_backend == "codex"

    def test_exact_models_allow_list_can_reject_broad_gpt_family(self, tmp_path: Path) -> None:
        _write_models_yaml(tmp_path, """
            schema_version: "models.v2"
            transports:
              codex_local:
                kind: subprocess
                driver: codex_cli
                interface: agent
                executable: codex
                provides: [repo.read_file]
            compatibility:
              - {models: [gpt-5.5, gpt-5.3-codex], transports: [codex_local], interfaces: [agent]}
            roles:
              planner:
                transport: codex_local
                model: gpt-image-1
                requires: [repo.read_file]
                scope_mode: read_only
                on_unavailable: fail
        """)

        with pytest.raises(WorkflowModelConfigError, match="not compatible"):
            load_model_config(tmp_path)

    def test_force_compat_skips_matrix_with_warning(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        _write_models_yaml(tmp_path, """
            schema_version: "models.v2"
            transports:
              codex_local:
                kind: subprocess
                driver: codex_cli
                interface: agent
                executable: codex
                provides: [repo.read_file]
            compatibility:
              - {models: [gpt-5.5], transports: [codex_local], interfaces: [agent]}
            roles:
              planner:
                transport: codex_local
                model: custom-local-model
                requires: [repo.read_file]
                scope_mode: read_only
                force_compat: true
                on_unavailable: fail
        """)

        mc = load_model_config(tmp_path)

        assert mc.planner_model == "custom-local-model"
        assert "force_compat=true" in caplog.text

    def test_on_unavailable_is_required_for_v2_roles(self, tmp_path: Path) -> None:
        _write_models_yaml(tmp_path, """
            schema_version: "models.v2"
            transports:
              codex_local:
                kind: subprocess
                driver: codex_cli
                interface: agent
                executable: codex
                provides: [repo.read_file]
            compatibility:
              - {models: [gpt-5.5], transports: [codex_local], interfaces: [agent]}
            roles:
              planner:
                transport: codex_local
                model: gpt-5.5
                requires: [repo.read_file]
                scope_mode: read_only
        """)

        with pytest.raises(WorkflowModelConfigError, match="on_unavailable is required"):
            load_model_config(tmp_path)

    def test_noop_in_process_transport_is_valid_for_contract_doubles(self, tmp_path: Path) -> None:
        _write_models_yaml(tmp_path, """
            schema_version: "models.v2"
            transports:
              noop_planner:
                kind: in_process
                driver: noop
                interface: chat
                provides: []
            compatibility:
              - {models: [noop], transports: [noop_planner], interfaces: [chat]}
            roles:
              planner:
                transport: noop_planner
                model: noop
                on_unavailable: fail
        """)

        mc = load_model_config(tmp_path)

        assert mc.role_driver("planner") == "noop"
        assert mc.role_interface("planner") == "chat"

    def test_http_chat_planner_and_claude_agent_executor_are_valid(self, tmp_path: Path) -> None:
        _write_models_yaml(tmp_path, """
            schema_version: "models.v2"
            transports:
              mimo_chat:
                kind: http
                driver: openai_compatible
                interface: chat
                api_format: openai_chat
                base_url: https://example.test/v1
                api_key_env: WORKFLOW_MIMO_KEY
                provides: [interface.chat]
              codex_local:
                kind: subprocess
                driver: codex_cli
                interface: agent
                executable: codex
                provides: [interface.agent]
              claude_agent:
                kind: subprocess
                driver: claude_code
                interface: agent
                executable: claude
                provides: [interface.agent, repo.write_file]
            compatibility:
              - {models: [mimo-v2.5-pro], transports: [mimo_chat], interfaces: [chat], api_formats: [openai_chat]}
              - {models: [gpt-5.5], transports: [codex_local], interfaces: [agent]}
              - {models: [claude-sonnet-4-6], transports: [claude_agent], interfaces: [agent]}
            roles:
              planner:
                transport: mimo_chat
                model: mimo-v2.5-pro
                requires: [interface.chat]
                on_unavailable: fail
              plan_reviewer:
                transport: codex_local
                model: gpt-5.5
                requires: [interface.agent]
                on_unavailable: fail
              executor:
                transport: claude_agent
                model: claude-sonnet-4-6
                scope_mode: post_diff
                on_unavailable: fail
        """)

        mc = load_model_config(tmp_path)

        assert mc.role_driver("planner") == "openai_compatible"
        assert mc.role_interface("planner") == "chat"
        assert mc.executor_backend_for_role() == "claude_code"
        assert mc.resolve_executor_model("claude_code") == "claude-sonnet-4-6"

    def test_http_chat_planner_requires_api_key_env(self, tmp_path: Path) -> None:
        _write_models_yaml(tmp_path, """
            schema_version: "models.v2"
            transports:
              mimo_chat:
                kind: http
                driver: openai_compatible
                interface: chat
                api_format: openai_chat
                base_url: https://example.test/v1
            compatibility:
              - {models: [mimo-v2.5-pro], transports: [mimo_chat], interfaces: [chat], api_formats: [openai_chat]}
            roles:
              planner:
                transport: mimo_chat
                model: mimo-v2.5-pro
                on_unavailable: fail
        """)

        with pytest.raises(WorkflowModelConfigError, match="requires api_key_env"):
            load_model_config(tmp_path)

    def test_migrate_v1_to_v2_preserves_common_role_models(self, tmp_path: Path) -> None:
        import yaml

        migrated = migrate_v1_to_v2(
            {
                "schema_version": "models.v1",
                "planner_model": "claude-sonnet-4-6",
                "executor_model": "gpt-5.5",
                "reviewer_model": "claude-opus-4-7",
                "plan_reviewer_model": "gpt-5.5",
                "reviewer_backend": "mcp",
                "review_enabled": True,
            }
        )
        _write_models_yaml(tmp_path, yaml.safe_dump(migrated))

        mc = load_model_config(tmp_path)

        assert mc.schema_version == "models.v2"
        assert mc.planner_model == "claude-sonnet-4-6"
        assert mc.plan_reviewer_model == "gpt-5.5"
        assert mc.reviewer_model == "claude-opus-4-7"
        assert mc.reviewer_backend == "mcp"
        assert mc.resolve_executor_model("codex_cli") == "gpt-5.5"
        assert mc.review_enabled is True
