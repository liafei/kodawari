from __future__ import annotations
import json
from pathlib import Path
from typing import Any
from urllib import request as urlrequest

import pytest

from kodawari.autopilot.core.model_doctor import (
    RedirectBlocked,
    SafeRedirectHandler,
    doctor_models,
    load_model_config_diagnostic,
    normalize_openai_endpoint,
    probe_openai_tools,
    redact_secrets,
)
from kodawari.cli.main import build_parser


def _write_models_yaml(tmp_path: Path, content: str) -> None:
    import textwrap

    target = tmp_path / ".claude" / "workflow" / "models.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(textwrap.dedent(content), encoding="utf-8")


def _mimo_tool_config() -> str:
    return """
        schema_version: "models.v2"
        transports:
          noop_planner:
            kind: in_process
            driver: noop
            interface: chat
          mimo_api:
            kind: http
            driver: openai_compatible
            interface: tool_use
            api_format: openai_chat
            base_url_env: WORKFLOW_MIMO_BASE_URL
            api_key_env: WORKFLOW_MIMO_KEY
        compatibility:
          - {models: [noop], transports: [noop_planner], interfaces: [chat]}
          - {models: [mimo-v2.5-pro], transports: [mimo_api], interfaces: [tool_use]}
        roles:
          planner:
            transport: noop_planner
            model: noop
            on_unavailable: fail
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
    """


class _Response:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


def test_doctor_parser_registers_nested_models_command() -> None:
    args = build_parser().parse_args(["doctor", "models", "--project-root", ".", "--offline"])

    assert args.command == "doctor"
    assert args.doctor_command == "models"
    assert args.offline is True
    assert callable(args.handler)

    smoke_args = build_parser().parse_args(["doctor", "models", "--project-root", ".", "--smoke"])
    assert smoke_args.smoke == "local"
    patch_args = build_parser().parse_args(["doctor", "models", "--project-root", ".", "--smoke=patch-local"])
    assert patch_args.smoke == "patch-local"
    planner_args = build_parser().parse_args(["doctor", "models", "--project-root", ".", "--smoke=planner"])
    assert planner_args.smoke == "planner"


def test_diagnostic_loader_keeps_tool_use_executor_for_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_models_yaml(tmp_path, _mimo_tool_config())
    monkeypatch.setenv("WORKFLOW_MIMO_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("WORKFLOW_MIMO_KEY", "secret-key")

    loaded = load_model_config_diagnostic(tmp_path)

    assert loaded.fatal is False
    assert loaded.config.roles["executor"].transport == "mimo_api"
    assert not loaded.blockers


def test_doctor_models_probe_runs_for_openai_tool_use_executor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_models_yaml(tmp_path, _mimo_tool_config())
    monkeypatch.setenv("WORKFLOW_MIMO_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("WORKFLOW_MIMO_KEY", "secret-key")

    def _fake_urlopen(request: urlrequest.Request, timeout: int) -> _Response:
        assert timeout == 5
        assert json.loads(request.data.decode("utf-8"))["stream"] is False  # type: ignore[union-attr]
        return _Response(
            {
                "model": "mimo-v2.5-pro",
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "type": "function",
                                    "function": {
                                        "name": "echo_tool",
                                        "arguments": json.dumps({"ping": "doctor_probe"}),
                                    },
                                }
                            ]
                        }
                    }
                ],
            }
        )

    report = doctor_models(
        project_root=tmp_path,
        offline=False,
        probe_tools=True,
        no_cache=True,
        urlopen_fn=_fake_urlopen,
    )

    assert report["status"] == "PASS"
    assert report["schema_version"] == "doctor.report.v1"
    assert report["probes"]["executor"]["status"] == "PASS"
    assert not report["blockers"]


def test_doctor_models_missing_key_is_blocker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_models_yaml(tmp_path, _mimo_tool_config())
    monkeypatch.setenv("WORKFLOW_MIMO_BASE_URL", "https://example.test/v1")
    monkeypatch.delenv("WORKFLOW_MIMO_KEY", raising=False)

    report = doctor_models(project_root=tmp_path, offline=True)

    assert any(item["code"] == "api_key_missing" for item in report["blockers"])


def test_doctor_models_chat_auto_api_format_is_not_tool_use_blocker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_models_yaml(tmp_path, """
        schema_version: "models.v2"
        transports:
          noop_planner:
            kind: in_process
            driver: noop
            interface: chat
          manual_exec:
            kind: manual
            driver: manual
            interface: manual
          reviewer_api:
            kind: http
            driver: http
            interface: chat
            api_format: auto
            base_url_env: WORKFLOW_REVIEWER_BASE_URL
            api_key_env: WORKFLOW_REVIEWER_API_KEY
        compatibility:
          - {models: [noop], transports: [noop_planner], interfaces: [chat]}
          - {models: [manual], transports: [manual_exec], interfaces: [manual]}
          - {model: "claude-*", transports: [reviewer_api], interfaces: [chat]}
        roles:
          planner: {transport: noop_planner, model: noop, on_unavailable: fail}
          executor:
            transport: manual_exec
            model: manual
            scope_mode: none
            on_unavailable: fail
          impl_reviewer:
            transport: reviewer_api
            model: claude-sonnet-4-6
            on_unavailable: skip
    """)
    monkeypatch.setenv("WORKFLOW_REVIEWER_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("WORKFLOW_REVIEWER_API_KEY", "secret-key")

    report = doctor_models(project_root=tmp_path, offline=True)
    codes = {item["code"] for item in report["blockers"]}

    assert "tool_use_api_format_auto" not in codes
    assert "tool_use_api_format_not_openai" not in codes


def test_doctor_models_v1_reviewer_backend_env_blocks_v2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_models_yaml(tmp_path, _mimo_tool_config())
    monkeypatch.setenv("WORKFLOW_MIMO_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("WORKFLOW_MIMO_KEY", "secret-key")
    monkeypatch.setenv("WORKFLOW_REVIEWER_BACKEND", "mcp")

    report = doctor_models(project_root=tmp_path, offline=True)

    assert any(item["code"] == "v1_env_workflow_reviewer_backend" for item in report["blockers"])


def test_doctor_models_reports_quota_group_warnings(tmp_path: Path) -> None:
    _write_models_yaml(tmp_path, """
        schema_version: "models.v2"
        transports:
          codex_local:
            kind: subprocess
            driver: codex_cli
            interface: agent
            executable: python
            quota_group: shared
          codex_backup:
            kind: subprocess
            driver: codex_cli
            interface: agent
            executable: python
            quota_group: shared
        compatibility:
          - {model: "gpt-*", transports: [codex_local, codex_backup], interfaces: [agent]}
        roles:
          planner: {transport: codex_local, model: gpt-5.5, on_unavailable: fail}
          executor:
            transport: codex_local
            model: gpt-5.5
            scope_mode: post_diff
            on_unavailable: degrade_to
            degrade_to: codex_backup
    """)

    report = doctor_models(project_root=tmp_path, offline=True)
    codes = {item["code"] for item in report["warnings"]}

    assert "quota_group_shared" in codes
    assert "degrade_to_same_quota_group" in codes


def test_normalize_openai_endpoint_variants() -> None:
    assert normalize_openai_endpoint("https://api.example.test/v1", api_format="openai_chat")[0] == "https://api.example.test/v1/chat/completions"
    assert normalize_openai_endpoint("https://api.example.test", api_format="openai")[0] == "https://api.example.test/v1/chat/completions"
    assert normalize_openai_endpoint("https://api.example.test/anthropic", api_format="openai_chat")[1] == "openai_transport_points_to_anthropic_endpoint"
    assert normalize_openai_endpoint("http://api.example.test", api_format="openai")[1] == "non_https_endpoint"
    assert normalize_openai_endpoint("https://api.example.test/v1", api_format="auto")[1] == "tool_use_api_format_auto"


def test_doctor_models_local_smoke_runs_without_real_endpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_models_yaml(tmp_path, _mimo_tool_config())
    monkeypatch.setenv("WORKFLOW_MIMO_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("WORKFLOW_MIMO_KEY", "secret-key")

    report = doctor_models(project_root=tmp_path, offline=True, smoke="local")

    assert report["status"] == "PASS"
    assert report["smokes"]["executor"]["status"] == "PASS"
    assert report["smokes"]["executor"]["changed_files"] == ["sample.txt"]


def test_doctor_models_patch_local_smoke_runs_exact_str_replace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_models_yaml(tmp_path, _mimo_tool_config())
    monkeypatch.setenv("WORKFLOW_MIMO_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("WORKFLOW_MIMO_KEY", "secret-key")

    report = doctor_models(project_root=tmp_path, offline=True, smoke="patch-local")

    assert report["status"] in {"PASS", "WARN"}
    smoke = report["smokes"]["executor"]
    assert smoke["status"] == "PASS"
    assert smoke["execution_protocol"] == "exact_str_replace_v1"
    assert smoke["changed_files"] == ["sample.py"]


def test_doctor_models_offline_blocks_real_smoke(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_models_yaml(tmp_path, _mimo_tool_config())
    monkeypatch.setenv("WORKFLOW_MIMO_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("WORKFLOW_MIMO_KEY", "secret-key")

    report = doctor_models(project_root=tmp_path, offline=True, smoke="real")

    assert report["status"] == "BLOCKED"
    assert any(item["code"] == "smoke_real_requires_online" for item in report["blockers"])

    report = doctor_models(project_root=tmp_path, offline=True, smoke="patch-real")

    assert report["status"] == "BLOCKED"
    assert any(item["code"] == "smoke_real_requires_online" for item in report["blockers"])


def test_probe_openai_tools_requires_real_tool_calls() -> None:
    def _fake_urlopen(_request: urlrequest.Request, _timeout: int) -> _Response:
        return _Response({"choices": [{"message": {"content": '{"ping":"doctor_probe"}'}}]})

    result = probe_openai_tools(
        endpoint="https://example.test/v1/chat/completions",
        model="mimo-v2.5-pro",
        api_key="secret-key",
        urlopen_fn=_fake_urlopen,
    )

    assert result["reason"] == "tools_silently_dropped"


def test_doctor_models_probe_runs_chat_plan_for_http_planner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_models_yaml(tmp_path, """
        schema_version: "models.v2"
        transports:
          mimo_chat:
            kind: http
            driver: openai_compatible
            interface: chat
            api_format: openai_chat
            base_url_env: WORKFLOW_MIMO_BASE_URL
            api_key_env: WORKFLOW_MIMO_KEY
          manual_exec:
            kind: manual
            driver: manual
            interface: manual
        compatibility:
          - {models: [mimo-v2.5-pro], transports: [mimo_chat], interfaces: [chat], api_formats: [openai_chat]}
          - {models: [manual], transports: [manual_exec], interfaces: [manual]}
        roles:
          planner:
            transport: mimo_chat
            model: mimo-v2.5-pro
            on_unavailable: fail
          executor:
            transport: manual_exec
            model: manual
            scope_mode: none
            on_unavailable: fail
    """)
    monkeypatch.setenv("WORKFLOW_MIMO_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("WORKFLOW_MIMO_KEY", "secret-key")

    plan = {
        "summary": "doctor",
        "tasks": [
            {
                "task_id": "T1",
                "task_name": "Task",
                "files_to_change": ["README.md"],
                "invariants": ["safe"],
                "test_plan": "pytest -q",
                "verify_cmd": "pytest -q",
                "depends_on": [],
                "provides": [],
                "requires": [],
                "api_contracts": [],
            }
        ],
    }

    def _fake_urlopen(request: urlrequest.Request, _timeout: int) -> _Response:
        payload = json.loads(request.data.decode("utf-8"))  # type: ignore[union-attr]
        assert "tools" not in payload
        return _Response({"model": "mimo-v2.5-pro", "choices": [{"message": {"content": json.dumps(plan)}}]})

    report = doctor_models(
        project_root=tmp_path,
        offline=False,
        probe_tools=True,
        no_cache=True,
        urlopen_fn=_fake_urlopen,
    )

    assert report["status"] == "PASS"
    assert report["probes"]["planner"]["status"] == "PASS"
    assert report["probes"]["planner"]["reason"] == "chat_plan_supported"


def test_doctor_models_planner_smoke_runs_local_fake_endpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_models_yaml(tmp_path, """
        schema_version: "models.v2"
        transports:
          mimo_chat:
            kind: http
            driver: openai_compatible
            interface: chat
            api_format: openai_chat
            base_url_env: WORKFLOW_MIMO_BASE_URL
            api_key_env: WORKFLOW_MIMO_KEY
          manual_exec:
            kind: manual
            driver: manual
            interface: manual
        compatibility:
          - {models: [mimo-v2.5-pro], transports: [mimo_chat], interfaces: [chat], api_formats: [openai_chat]}
          - {models: [manual], transports: [manual_exec], interfaces: [manual]}
        roles:
          planner:
            transport: mimo_chat
            model: mimo-v2.5-pro
            on_unavailable: fail
          executor:
            transport: manual_exec
            model: manual
            scope_mode: none
            on_unavailable: fail
    """)
    monkeypatch.setenv("WORKFLOW_MIMO_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("WORKFLOW_MIMO_KEY", "secret-key")

    report = doctor_models(project_root=tmp_path, offline=True, smoke="planner")

    assert report["status"] == "PASS"
    assert report["smokes"]["planner"]["status"] == "PASS"
    assert report["smokes"]["planner"]["task_count"] == 1


def test_probe_openai_tools_warns_when_model_is_substituted() -> None:
    def _fake_urlopen(_request: urlrequest.Request, _timeout: int) -> _Response:
        return _Response(
            {
                "model": "mimo-default",
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "type": "function",
                                    "function": {
                                        "name": "echo_tool",
                                        "arguments": json.dumps({"ping": "doctor_probe"}),
                                    },
                                }
                            ]
                        }
                    }
                ],
            }
        )

    result = probe_openai_tools(
        endpoint="https://example.test/v1/chat/completions",
        model="mimo-v2.5-pro",
        api_key="secret-key",
        urlopen_fn=_fake_urlopen,
    )

    assert result["status"] == "PASS"
    assert result["model_warning"]["code"] == "probe_model_substituted"


def test_safe_redirect_handler_blocks_cross_host_redirect() -> None:
    handler = SafeRedirectHandler()
    request = urlrequest.Request("https://a.example.test/v1/chat/completions")

    with pytest.raises(RedirectBlocked):
        handler.redirect_request(
            request,
            fp=None,
            code=302,
            msg="Found",
            headers={},
            newurl="https://b.example.test/v1/chat/completions",
        )


def test_safe_redirect_handler_blocks_same_host_downgrade() -> None:
    handler = SafeRedirectHandler()
    request = urlrequest.Request("https://api.example.test/v1/chat/completions")

    with pytest.raises(RedirectBlocked):
        handler.redirect_request(
            request,
            fp=None,
            code=302,
            msg="Found",
            headers={},
            newurl="http://api.example.test/v1/chat/completions",
        )


def test_doctor_probe_cache_misses_after_key_rotation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_models_yaml(tmp_path, _mimo_tool_config())
    monkeypatch.setenv("WORKFLOW_MIMO_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("WORKFLOW_MIMO_KEY", "secret-key-one")
    calls = {"count": 0}

    def _fake_urlopen(_request: urlrequest.Request, _timeout: int) -> _Response:
        calls["count"] += 1
        return _Response(
            {
                "model": "mimo-v2.5-pro",
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "type": "function",
                                    "function": {
                                        "name": "echo_tool",
                                        "arguments": json.dumps({"ping": "doctor_probe"}),
                                    },
                                }
                            ]
                        }
                    }
                ],
            }
        )

    first = doctor_models(project_root=tmp_path, offline=False, probe_tools=True, urlopen_fn=_fake_urlopen)
    monkeypatch.setenv("WORKFLOW_MIMO_KEY", "secret-key-two")
    second = doctor_models(project_root=tmp_path, offline=False, probe_tools=True, urlopen_fn=_fake_urlopen)

    assert first["probes"]["executor"]["status"] == "PASS"
    assert second["probes"]["executor"]["status"] == "PASS"
    assert calls["count"] == 2


def test_redact_secrets_is_recursive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKFLOW_MIMO_KEY", "super-secret")
    payload = {"outer": [{"message": "Bearer super-secret"}]}

    assert redact_secrets(payload)["outer"][0]["message"] == "Bearer <redacted>"
