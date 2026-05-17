"""Tests for CLI-based reviewer and MCP review server."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from kodawari.autopilot.cli_reviewer import (
    CliReviewerConfig,
    cli_reviewer_available,
    request_cli_review,
    request_mcp_review,
    _build_command,
    _extract_content,
    _is_retryable_error,
)
from kodawari.autopilot.mcp_review_server import (
    McpReviewServerState,
    _handle_message,
)


# --- CliReviewerConfig ---


def test_cli_reviewer_config_defaults() -> None:
    config = CliReviewerConfig()
    assert config.executable == "claude"
    assert config.timeout_seconds == 120
    assert config.max_tokens == 4096
    assert config.retry_attempts == 1


# --- _build_command ---


def test_build_command_includes_required_flags() -> None:
    config = CliReviewerConfig(executable="claude")
    cmd = _build_command(executable="claude", config=config)
    assert cmd[0] == "claude"
    assert "-p" in cmd
    assert "--output-format" in cmd
    assert "json" in cmd
    assert "--max-turns" in cmd
    # Prompt is passed via stdin, not as argument
    assert "--dangerously-skip-permissions" not in cmd
    assert "review this" not in cmd


# --- _extract_content ---


def test_extract_content_plain_json() -> None:
    raw = '{"approved": true, "summary": "ok"}'
    result = _extract_content(raw)
    assert result == raw


def test_extract_content_envelope_with_result_string() -> None:
    envelope = json.dumps({"result": '{"approved": true}'})
    result = _extract_content(envelope)
    assert result == '{"approved": true}'


def test_extract_content_envelope_with_content_blocks() -> None:
    envelope = json.dumps({
        "result": [
            {"type": "text", "text": '{"approved": true}'},
        ]
    })
    result = _extract_content(envelope)
    assert '"approved": true' in result


def test_extract_content_empty() -> None:
    assert _extract_content("") == ""
    assert _extract_content("   ") == ""


# --- _is_retryable_error ---


def test_retryable_errors() -> None:
    assert _is_retryable_error("cli reviewer timed out") is True
    assert _is_retryable_error("timeout") is True
    assert _is_retryable_error("not found") is False
    assert _is_retryable_error("") is False


# --- request_cli_review (mocked subprocess) ---


def _mock_review_json() -> str:
    return json.dumps({
        "approved": True,
        "summary": "Looks good",
        "must_fix": [],
        "should_fix": [],
        "blocking_items": [],
        "severity": "low",
        "score": 95,
        "target_score": 95,
        "min_dimension_score": 80,
        "gate_recommendation": "APPROVED",
        "evidence": ["checked tests"],
    })


def _review_context() -> dict[str, Any]:
    return {
        "task_id": "T-001",
        "task_label": "test task",
        "task_scope": "src/",
    }


@mock.patch("kodawari.autopilot.cli_reviewer._executable_available", return_value=True)
@mock.patch("kodawari.autopilot.cli_reviewer._resolved_executable", return_value="claude")
@mock.patch("subprocess.run")
def test_request_cli_review_success(
    mock_run: mock.MagicMock,
    _mock_exe: mock.MagicMock,
    _mock_avail: mock.MagicMock,
) -> None:
    mock_run.return_value = subprocess.CompletedProcess(
        args=["claude"], returncode=0,
        stdout=_mock_review_json(), stderr="",
    )
    config = CliReviewerConfig()
    payload, error = request_cli_review(
        config,
        task="review task",
        context=_review_context(),
        changed_files=["src/app.py"],
        review_iteration=0,
    )
    assert error == ""
    assert payload is not None
    assert payload["approved"] is True
    assert payload["summary"] == "Looks good"


@mock.patch("kodawari.autopilot.cli_reviewer._executable_available", return_value=False)
@mock.patch("kodawari.autopilot.cli_reviewer._resolved_executable", return_value="claude")
def test_request_cli_review_executable_not_found(
    _mock_exe: mock.MagicMock,
    _mock_avail: mock.MagicMock,
) -> None:
    config = CliReviewerConfig()
    payload, error = request_cli_review(
        config,
        task="review task",
        context=_review_context(),
        changed_files=["src/app.py"],
        review_iteration=0,
    )
    assert payload is None
    assert "not found" in error


@mock.patch("kodawari.autopilot.cli_reviewer._executable_available", return_value=True)
@mock.patch("kodawari.autopilot.cli_reviewer._resolved_executable", return_value="claude")
@mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=120))
def test_request_cli_review_timeout(
    _mock_run: mock.MagicMock,
    _mock_exe: mock.MagicMock,
    _mock_avail: mock.MagicMock,
) -> None:
    config = CliReviewerConfig()
    payload, error = request_cli_review(
        config,
        task="review task",
        context=_review_context(),
        changed_files=["src/app.py"],
        review_iteration=0,
    )
    assert payload is None
    assert "timed out" in error


@mock.patch("kodawari.autopilot.cli_reviewer._executable_available", return_value=True)
@mock.patch("kodawari.autopilot.cli_reviewer._resolved_executable", return_value="claude")
@mock.patch("subprocess.run")
def test_request_cli_review_nonzero_exit(
    mock_run: mock.MagicMock,
    _mock_exe: mock.MagicMock,
    _mock_avail: mock.MagicMock,
) -> None:
    mock_run.return_value = subprocess.CompletedProcess(
        args=["claude"], returncode=1,
        stdout="", stderr="something went wrong",
    )
    config = CliReviewerConfig()
    payload, error = request_cli_review(
        config,
        task="review task",
        context=_review_context(),
        changed_files=["src/app.py"],
        review_iteration=0,
    )
    assert payload is None
    assert "exited with code 1" in error


@mock.patch("kodawari.autopilot.cli_reviewer._executable_available", return_value=True)
@mock.patch("kodawari.autopilot.cli_reviewer._resolved_executable", return_value="claude")
@mock.patch("subprocess.run")
def test_request_cli_review_empty_output(
    mock_run: mock.MagicMock,
    _mock_exe: mock.MagicMock,
    _mock_avail: mock.MagicMock,
) -> None:
    mock_run.return_value = subprocess.CompletedProcess(
        args=["claude"], returncode=0,
        stdout="", stderr="",
    )
    config = CliReviewerConfig()
    payload, error = request_cli_review(
        config,
        task="review task",
        context=_review_context(),
        changed_files=["src/app.py"],
        review_iteration=0,
    )
    assert payload is None
    assert "empty" in error


# --- MCP Review Server ---


def test_mcp_server_initialize() -> None:
    state = McpReviewServerState(bundle_path="/tmp/b.json", result_path="/tmp/r.json")
    response = _handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        state,
    )
    assert response is not None
    assert response["id"] == 1
    assert "protocolVersion" in response["result"]
    assert "tools" in response["result"]["capabilities"]


def test_mcp_server_tools_list() -> None:
    state = McpReviewServerState(bundle_path="/tmp/b.json", result_path="/tmp/r.json")
    response = _handle_message(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        state,
    )
    assert response is not None
    tools = response["result"]["tools"]
    tool_names = [t["name"] for t in tools]
    assert "get_review_bundle" in tool_names
    assert "submit_review" in tool_names


def test_mcp_server_get_review_bundle(tmp_path: Path) -> None:
    bundle = {"task": "test", "changed_files": ["a.py"]}
    bundle_file = tmp_path / "bundle.json"
    bundle_file.write_text(json.dumps(bundle), encoding="utf-8")
    state = McpReviewServerState(
        bundle_path=str(bundle_file),
        result_path=str(tmp_path / "result.json"),
    )
    response = _handle_message(
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "get_review_bundle", "arguments": {}}},
        state,
    )
    assert response is not None
    content = response["result"]["content"]
    assert len(content) == 1
    parsed = json.loads(content[0]["text"])
    assert parsed["task"] == "test"


def test_mcp_server_submit_review(tmp_path: Path) -> None:
    result_file = tmp_path / "result.json"
    state = McpReviewServerState(
        bundle_path=str(tmp_path / "bundle.json"),
        result_path=str(result_file),
    )
    review = {"approved": True, "summary": "ok", "must_fix": [], "should_fix": [],
              "blocking_items": [], "severity": "low", "score": 95}
    response = _handle_message(
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "submit_review", "arguments": review}},
        state,
    )
    assert response is not None
    assert result_file.exists()
    saved = json.loads(result_file.read_text(encoding="utf-8"))
    assert saved["approved"] is True


def test_mcp_server_unknown_tool() -> None:
    state = McpReviewServerState(bundle_path="/tmp/b.json", result_path="/tmp/r.json")
    response = _handle_message(
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
         "params": {"name": "unknown_tool", "arguments": {}}},
        state,
    )
    assert response is not None
    assert "error" in response
    assert response["error"]["code"] == -32601


def test_mcp_server_notification_returns_none() -> None:
    state = McpReviewServerState(bundle_path="/tmp/b.json", result_path="/tmp/r.json")
    response = _handle_message(
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        state,
    )
    assert response is None


def test_mcp_server_ping() -> None:
    state = McpReviewServerState(bundle_path="/tmp/b.json", result_path="/tmp/r.json")
    response = _handle_message(
        {"jsonrpc": "2.0", "id": 6, "method": "ping", "params": {}},
        state,
    )
    assert response is not None
    assert response["result"] == {}


# --- LocalCodexAdapter reviewer backend routing ---


def test_resolved_reviewer_backend_auto_with_api_key() -> None:
    from kodawari.autopilot.local_adapter import LocalCodexAdapter, LocalCodexAdapterConfig

    config = LocalCodexAdapterConfig(
        opus_reviewer_backend="auto",
        reviewer_api_key="sk-test-key",
    )
    adapter = LocalCodexAdapter(config)
    # Override env-resolved config to keep test deterministic
    adapter.config.reviewer_api_key = "sk-test-key"
    adapter.config.opus_reviewer_backend = "auto"
    assert adapter._resolved_reviewer_backend() == "api"


def test_resolved_reviewer_backend_auto_no_key_defaults_to_api() -> None:
    """auto mode always returns api for backward compatibility."""
    from kodawari.autopilot.local_adapter import LocalCodexAdapter, LocalCodexAdapterConfig

    config = LocalCodexAdapterConfig(
        opus_reviewer_backend="auto",
        reviewer_api_key="",
    )
    adapter = LocalCodexAdapter(config)
    adapter.config.reviewer_api_key = ""
    adapter.config.opus_reviewer_backend = "auto"
    assert adapter._resolved_reviewer_backend() == "api"


def test_resolved_reviewer_backend_explicit_cli() -> None:
    from kodawari.autopilot.local_adapter import LocalCodexAdapter, LocalCodexAdapterConfig

    config = LocalCodexAdapterConfig(
        opus_reviewer_backend="cli",
        reviewer_api_key="sk-test-key",
    )
    adapter = LocalCodexAdapter(config)
    adapter.config.opus_reviewer_backend = "cli"
    assert adapter._resolved_reviewer_backend() == "cli"


def test_resolved_reviewer_backend_explicit_mcp() -> None:
    from kodawari.autopilot.local_adapter import LocalCodexAdapter, LocalCodexAdapterConfig

    config = LocalCodexAdapterConfig(opus_reviewer_backend="mcp")
    adapter = LocalCodexAdapter(config)
    adapter.config.opus_reviewer_backend = "mcp"
    assert adapter._resolved_reviewer_backend() == "mcp"


# --- Executable compatibility checks ---


def test_cli_reviewer_available_rejects_codex() -> None:
    config = CliReviewerConfig(executable="codex")
    assert cli_reviewer_available(config) is False


def test_cli_reviewer_available_rejects_arbitrary_executable() -> None:
    config = CliReviewerConfig(executable="python")
    assert cli_reviewer_available(config) is False


@mock.patch("kodawari.autopilot.cli_reviewer._executable_available", return_value=True)
def test_cli_reviewer_available_accepts_claude(_mock: mock.MagicMock) -> None:
    config = CliReviewerConfig(executable="claude")
    assert cli_reviewer_available(config) is True


@mock.patch("kodawari.autopilot.cli_reviewer._executable_available", return_value=True)
@mock.patch("kodawari.autopilot.cli_reviewer._resolved_executable", return_value="codex")
def test_request_cli_review_rejects_codex(
    _mock_exe: mock.MagicMock,
    _mock_avail: mock.MagicMock,
) -> None:
    config = CliReviewerConfig(executable="codex")
    payload, error = request_cli_review(
        config,
        task="test",
        context=_review_context(),
        changed_files=["src/app.py"],
        review_iteration=0,
    )
    assert payload is None
    assert "only supports claude" in error


# --- request_mcp_review (mocked subprocess) ---


@mock.patch("kodawari.autopilot.cli_reviewer._executable_available", return_value=True)
@mock.patch("kodawari.autopilot.cli_reviewer._resolved_executable", return_value="claude")
@mock.patch("subprocess.run")
def test_request_mcp_review_reads_result_file(
    mock_run: mock.MagicMock,
    _mock_exe: mock.MagicMock,
    _mock_avail: mock.MagicMock,
) -> None:
    """MCP mode: when subprocess writes result file, parse it."""
    review_json = {
        "approved": True,
        "summary": "MCP review passed",
        "must_fix": [],
        "should_fix": [],
        "blocking_items": [],
        "severity": "low",
        "score": 96,
        "target_score": 95,
        "min_dimension_score": 80,
        "gate_recommendation": "APPROVED",
    }

    def _fake_run(cmd, **kwargs):
        # Find --mcp-config arg to locate the tmp dir
        for i, arg in enumerate(cmd):
            if arg == "--mcp-config" and i + 1 < len(cmd):
                config_path = Path(cmd[i + 1])
                result_path = config_path.parent / "review_result.json"
                result_path.write_text(json.dumps(review_json), encoding="utf-8")
                break
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    mock_run.side_effect = _fake_run
    config = CliReviewerConfig()
    payload, error = request_mcp_review(
        config,
        task="review task",
        context=_review_context(),
        changed_files=["src/app.py"],
        review_iteration=0,
    )
    assert error == ""
    assert payload is not None
    assert payload["approved"] is True


@mock.patch("kodawari.autopilot.cli_reviewer._executable_available", return_value=True)
@mock.patch("kodawari.autopilot.cli_reviewer._resolved_executable", return_value="claude")
@mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=120))
def test_request_mcp_review_timeout(
    _mock_run: mock.MagicMock,
    _mock_exe: mock.MagicMock,
    _mock_avail: mock.MagicMock,
) -> None:
    config = CliReviewerConfig()
    payload, error = request_mcp_review(
        config,
        task="review task",
        context=_review_context(),
        changed_files=["src/app.py"],
        review_iteration=0,
    )
    assert payload is None
    assert "timed out" in error


# --- REAL_REVIEW_MODES ---


def test_real_review_modes_includes_all_backends() -> None:
    from kodawari.autopilot.cli_reviewer import REAL_REVIEW_MODES

    assert "real_opus_gateway" in REAL_REVIEW_MODES
    assert "real_cli_reviewer" in REAL_REVIEW_MODES
    assert "real_mcp_reviewer" in REAL_REVIEW_MODES
    assert "real_codex_reviewer" in REAL_REVIEW_MODES
    assert "simulate_local" not in REAL_REVIEW_MODES


# --- CLI lane review_runtime.mode integration ---


def test_cli_review_mode_is_real_cli_reviewer() -> None:
    """When reviewer backend is cli, review_runtime.mode should be real_cli_reviewer."""
    from kodawari.autopilot.local_adapter import LocalCodexAdapter, LocalCodexAdapterConfig

    config = LocalCodexAdapterConfig(
        opus_reviewer_backend="cli",
        real_peer_review=True,
    )
    adapter = LocalCodexAdapter(config)
    adapter.config.opus_reviewer_backend = "cli"

    # Mock the CLI review to succeed
    mock_review = {
        "approved": True,
        "summary": "ok",
        "must_fix": [],
        "should_fix": [],
        "blocking_items": [],
        "severity": "low",
        "score": 95,
        "target_score": 95,
        "min_dimension_score": 80,
        "gate_recommendation": "APPROVED",
        "reviewer": "opus",
        "source": "kodawari.real_opus_gateway",
    }
    with mock.patch(
        "kodawari.autopilot.local_adapter.request_cli_review",
        return_value=(mock_review, ""),
    ):
        review = adapter.review(
            task="test-task",
            context={"task_id": "T-001"},
            changed_files=["src/app.py", "tests/test_app.py"],
            review_iteration=0,
        )
    assert review["review_runtime"]["mode"] == "real_cli_reviewer"
    assert review["review_runtime"]["real_requested"] is True


# --- cwd and reviewer_capability wiring ---


@mock.patch("kodawari.autopilot.cli_reviewer._executable_available", return_value=True)
@mock.patch("kodawari.autopilot.cli_reviewer._resolved_executable", return_value="claude")
@mock.patch("subprocess.run")
def test_request_cli_review_passes_cwd_when_project_root_given(
    mock_run: mock.MagicMock,
    _mock_exe: mock.MagicMock,
    _mock_avail: mock.MagicMock,
    tmp_path: "Path",
) -> None:
    mock_run.return_value = subprocess.CompletedProcess(
        args=["claude"], returncode=0, stdout=_mock_review_json(), stderr="",
    )
    config = CliReviewerConfig()
    request_cli_review(
        config,
        task="t",
        context={},
        changed_files=["src/app.py"],
        review_iteration=0,
        project_root=tmp_path,
    )
    call_kwargs = mock_run.call_args[1]
    assert call_kwargs.get("cwd") == str(tmp_path.resolve())


@mock.patch("kodawari.autopilot.cli_reviewer._executable_available", return_value=True)
@mock.patch("kodawari.autopilot.cli_reviewer._resolved_executable", return_value="claude")
@mock.patch("subprocess.run")
def test_request_cli_review_sets_stable_claude_home_env(
    mock_run: mock.MagicMock,
    _mock_exe: mock.MagicMock,
    _mock_avail: mock.MagicMock,
    tmp_path: "Path",
) -> None:
    mock_run.return_value = subprocess.CompletedProcess(
        args=["claude"], returncode=0, stdout=_mock_review_json(), stderr="",
    )
    config = CliReviewerConfig()
    request_cli_review(
        config,
        task="t",
        context={},
        changed_files=["src/app.py"],
        review_iteration=0,
        project_root=tmp_path,
    )
    call_kwargs = mock_run.call_args[1]
    env = dict(call_kwargs.get("env") or {})
    expected_home = (tmp_path / ".workflow_runtime" / "reviewer_homes" / "reviewer_claude_home").resolve()
    assert env.get("CLAUDE_HOME") == str(expected_home)
    assert env.get("HOME") == str(expected_home)


@mock.patch("kodawari.autopilot.cli_reviewer._executable_available", return_value=True)
@mock.patch("kodawari.autopilot.cli_reviewer._resolved_executable", return_value="claude")
@mock.patch("subprocess.run")
def test_request_cli_review_no_cwd_when_project_root_none(
    mock_run: mock.MagicMock,
    _mock_exe: mock.MagicMock,
    _mock_avail: mock.MagicMock,
) -> None:
    mock_run.return_value = subprocess.CompletedProcess(
        args=["claude"], returncode=0, stdout=_mock_review_json(), stderr="",
    )
    config = CliReviewerConfig()
    request_cli_review(
        config, task="t", context={}, changed_files=["src/app.py"], review_iteration=0,
    )
    call_kwargs = mock_run.call_args[1]
    assert call_kwargs.get("cwd") is None


@mock.patch("kodawari.autopilot.cli_reviewer._executable_available", return_value=True)
@mock.patch("kodawari.autopilot.cli_reviewer._resolved_executable", return_value="claude")
@mock.patch("subprocess.run")
def test_request_cli_review_prompt_uses_bundle_only_capability(
    mock_run: mock.MagicMock,
    _mock_exe: mock.MagicMock,
    _mock_avail: mock.MagicMock,
) -> None:
    mock_run.return_value = subprocess.CompletedProcess(
        args=["claude"], returncode=0, stdout=_mock_review_json(), stderr="",
    )
    config = CliReviewerConfig()
    request_cli_review(
        config, task="t", context={}, changed_files=["src/app.py"], review_iteration=0,
    )
    stdin_prompt = mock_run.call_args[1].get("input", "")
    assert "no filesystem access" in stdin_prompt
    assert "You may use Read" not in stdin_prompt
