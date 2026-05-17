"""Tests for the Codex CLI reviewer backend."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from kodawari.autopilot.codex_reviewer import (
    CodexReviewerConfig,
    codex_reviewer_available,
    request_codex_review,
    _build_codex_review_command,
    _extract_codex_content,
    _extract_fenced_json,
)


def _review_context(**overrides: object) -> dict:
    base: dict = {"task_id": "T-001", "task_label": "test task"}
    base.update(overrides)
    return base


_VALID_REVIEW_JSON = {
    "approved": True,
    "summary": "Looks good",
    "must_fix": [],
    "should_fix": [],
    "blocking_items": [],
    "severity": "low",
    "score": 96,
    "target_score": 95,
    "min_dimension_score": 80,
    "gate_recommendation": "APPROVED",
}


# --- Config defaults ---


def test_config_defaults() -> None:
    config = CodexReviewerConfig()
    assert config.executable == "codex"
    assert config.timeout_seconds == 180
    assert config.retry_attempts == 1


# --- Command building ---


def test_build_codex_review_command_structure() -> None:
    cmd = _build_codex_review_command(executable="codex")
    assert cmd[0] == "codex"
    assert "exec" in cmd
    assert "--skip-git-repo-check" in cmd
    # Security: must NOT have dangerous bypass
    assert "--dangerously-bypass-approvals-and-sandbox" not in cmd
    # Security: sandbox must be read-only to prevent writes during review
    # Exact ordering matters: "--sandbox" immediately followed by "read-only"
    sandbox_idx = cmd.index("--sandbox")
    assert cmd[sandbox_idx + 1] == "read-only"
    # Must NOT contain claude-specific flags
    assert "-p" not in cmd
    assert "--output-format" not in cmd
    assert "--max-turns" not in cmd
    # Prompt must NOT be in command args (it goes via stdin)
    assert len(cmd) == 5  # codex exec --skip-git-repo-check --sandbox read-only


def test_build_codex_review_command_includes_model_when_configured() -> None:
    cmd = _build_codex_review_command(executable="codex", model="gpt-5.3-codex")
    assert cmd[-2:] == ["--model", "gpt-5.3-codex"]


# --- Output extraction ---


def test_extract_codex_content_pure_json() -> None:
    raw = json.dumps(_VALID_REVIEW_JSON)
    result = _extract_codex_content(raw)
    assert json.loads(result) == _VALID_REVIEW_JSON


def test_extract_codex_content_fenced_code_block() -> None:
    raw = "Here is my review:\n```json\n" + json.dumps(_VALID_REVIEW_JSON) + "\n```\nDone."
    result = _extract_codex_content(raw)
    assert json.loads(result) == _VALID_REVIEW_JSON


def test_extract_codex_content_mixed_text_json() -> None:
    raw = "I reviewed the code. " + json.dumps(_VALID_REVIEW_JSON) + " That's all."
    result = _extract_codex_content(raw)
    parsed = json.loads(result)
    assert parsed["approved"] is True


def test_extract_codex_content_empty() -> None:
    assert _extract_codex_content("") == ""
    assert _extract_codex_content("   ") == ""


def test_extract_codex_content_no_json() -> None:
    raw = "No JSON here, just text."
    result = _extract_codex_content(raw)
    # Falls through to raw text for downstream handling
    assert result == raw


def test_extract_fenced_json_block() -> None:
    text = "prefix\n```json\n{\"a\": 1}\n```\nsuffix"
    assert _extract_fenced_json(text) == '{"a": 1}'


def test_extract_fenced_json_no_block() -> None:
    assert _extract_fenced_json("no code block here") == ""


# --- request_codex_review (mocked subprocess) ---


@mock.patch("kodawari.autopilot.codex_reviewer._executable_available", return_value=True)
@mock.patch("kodawari.autopilot.codex_reviewer._resolved_executable", return_value="codex")
@mock.patch("subprocess.run")
def test_request_codex_review_success(
    mock_run: mock.MagicMock,
    _mock_exe: mock.MagicMock,
    _mock_avail: mock.MagicMock,
) -> None:
    mock_run.return_value = subprocess.CompletedProcess(
        args=["codex"],
        returncode=0,
        stdout=json.dumps(_VALID_REVIEW_JSON),
        stderr="",
    )
    config = CodexReviewerConfig()
    payload, error = request_codex_review(
        config,
        task="review task",
        context=_review_context(),
        changed_files=["src/app.py"],
        review_iteration=0,
    )
    assert error == ""
    assert payload is not None
    assert payload["approved"] is True


@mock.patch("kodawari.autopilot.codex_reviewer._executable_available", return_value=True)
@mock.patch("kodawari.autopilot.codex_reviewer._resolved_executable", return_value="codex")
@mock.patch("subprocess.run")
def test_request_codex_review_fenced_output(
    mock_run: mock.MagicMock,
    _mock_exe: mock.MagicMock,
    _mock_avail: mock.MagicMock,
) -> None:
    mock_run.return_value = subprocess.CompletedProcess(
        args=["codex"],
        returncode=0,
        stdout="Review complete:\n```json\n" + json.dumps(_VALID_REVIEW_JSON) + "\n```\n",
        stderr="",
    )
    config = CodexReviewerConfig()
    payload, error = request_codex_review(
        config,
        task="review task",
        context=_review_context(),
        changed_files=["src/app.py"],
        review_iteration=0,
    )
    assert error == ""
    assert payload is not None
    assert payload["approved"] is True


@mock.patch("kodawari.autopilot.codex_reviewer._executable_available", return_value=False)
@mock.patch("kodawari.autopilot.codex_reviewer._resolved_executable", return_value="codex")
def test_request_codex_review_executable_not_found(
    _mock_exe: mock.MagicMock,
    _mock_avail: mock.MagicMock,
) -> None:
    config = CodexReviewerConfig()
    payload, error = request_codex_review(
        config,
        task="review task",
        context=_review_context(),
        changed_files=["src/app.py"],
        review_iteration=0,
    )
    assert payload is None
    assert "not found" in error


@mock.patch("kodawari.autopilot.codex_reviewer._executable_available", return_value=True)
@mock.patch("kodawari.autopilot.codex_reviewer._resolved_executable", return_value="codex")
@mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="codex", timeout=180))
def test_request_codex_review_timeout(
    _mock_run: mock.MagicMock,
    _mock_exe: mock.MagicMock,
    _mock_avail: mock.MagicMock,
) -> None:
    config = CodexReviewerConfig()
    payload, error = request_codex_review(
        config,
        task="review task",
        context=_review_context(),
        changed_files=["src/app.py"],
        review_iteration=0,
    )
    assert payload is None
    assert "timed out" in error


@mock.patch("kodawari.autopilot.codex_reviewer._executable_available", return_value=True)
@mock.patch("kodawari.autopilot.codex_reviewer._resolved_executable", return_value="codex")
@mock.patch("subprocess.run")
def test_request_codex_review_nonzero_exit(
    mock_run: mock.MagicMock,
    _mock_exe: mock.MagicMock,
    _mock_avail: mock.MagicMock,
) -> None:
    mock_run.return_value = subprocess.CompletedProcess(
        args=["codex"], returncode=1, stdout="", stderr="something went wrong",
    )
    config = CodexReviewerConfig()
    payload, error = request_codex_review(
        config,
        task="review task",
        context=_review_context(),
        changed_files=["src/app.py"],
        review_iteration=0,
    )
    assert payload is None
    assert "exited with code 1" in error


@mock.patch("kodawari.autopilot.codex_reviewer._executable_available", return_value=True)
@mock.patch("kodawari.autopilot.codex_reviewer._resolved_executable", return_value="codex")
@mock.patch("subprocess.run")
def test_request_codex_review_empty_output(
    mock_run: mock.MagicMock,
    _mock_exe: mock.MagicMock,
    _mock_avail: mock.MagicMock,
) -> None:
    mock_run.return_value = subprocess.CompletedProcess(
        args=["codex"], returncode=0, stdout="", stderr="",
    )
    config = CodexReviewerConfig()
    payload, error = request_codex_review(
        config,
        task="review task",
        context=_review_context(),
        changed_files=["src/app.py"],
        review_iteration=0,
    )
    assert payload is None
    assert "empty" in error


# --- codex_reviewer_available ---


@mock.patch("kodawari.autopilot.codex_reviewer._executable_available", return_value=True)
def test_codex_reviewer_available_when_found(_mock: mock.MagicMock) -> None:
    assert codex_reviewer_available() is True


@mock.patch("kodawari.autopilot.codex_reviewer._executable_available", return_value=False)
def test_codex_reviewer_available_when_not_found(_mock: mock.MagicMock) -> None:
    assert codex_reviewer_available() is False


def test_codex_reviewer_available_rejects_claude() -> None:
    config = CodexReviewerConfig(executable="claude")
    assert codex_reviewer_available(config) is False


def test_codex_reviewer_available_rejects_python() -> None:
    config = CodexReviewerConfig(executable="python")
    assert codex_reviewer_available(config) is False


@mock.patch("kodawari.autopilot.codex_reviewer._executable_available", return_value=True)
@mock.patch("kodawari.autopilot.codex_reviewer._resolved_executable", return_value="claude")
def test_request_codex_review_rejects_claude(
    _mock_exe: mock.MagicMock,
    _mock_avail: mock.MagicMock,
) -> None:
    config = CodexReviewerConfig(executable="claude")
    payload, error = request_codex_review(
        config,
        task="test",
        context=_review_context(),
        changed_files=["src/app.py"],
        review_iteration=0,
    )
    assert payload is None
    assert "only supports codex" in error


@mock.patch("kodawari.autopilot.codex_reviewer._executable_available", return_value=True)
@mock.patch("kodawari.autopilot.codex_reviewer._resolved_executable", return_value="codex")
@mock.patch("subprocess.run")
def test_request_codex_review_passes_prompt_via_stdin(
    mock_run: mock.MagicMock,
    _mock_exe: mock.MagicMock,
    _mock_avail: mock.MagicMock,
) -> None:
    """Verify prompt goes via stdin (input=), not as a command argument."""
    mock_run.return_value = subprocess.CompletedProcess(
        args=["codex"], returncode=0, stdout=json.dumps(_VALID_REVIEW_JSON), stderr="",
    )
    request_codex_review(
        CodexReviewerConfig(),
        task="test", context=_review_context(),
        changed_files=["src/app.py"], review_iteration=0,
    )
    call_kwargs = mock_run.call_args
    # Prompt must be in input=, not in the command list
    assert call_kwargs.kwargs.get("input") or call_kwargs[1].get("input")
    cmd_args = call_kwargs[0][0] if call_kwargs[0] else call_kwargs.kwargs.get("args", [])
    # Command should only be ["codex", "exec", "--skip-git-repo-check", "--sandbox", "read-only"]
    assert len(cmd_args) == 5


# --- REAL_REVIEW_MODES includes codex ---


def test_real_review_modes_includes_codex() -> None:
    from kodawari.autopilot.cli_reviewer import REAL_REVIEW_MODES

    assert "real_codex_reviewer" in REAL_REVIEW_MODES


# --- local_adapter routing ---


def test_resolved_reviewer_backend_explicit_codex() -> None:
    from kodawari.autopilot.local_adapter import LocalCodexAdapter, LocalCodexAdapterConfig

    config = LocalCodexAdapterConfig(opus_reviewer_backend="codex")
    adapter = LocalCodexAdapter(config)
    adapter.config.opus_reviewer_backend = "codex"
    assert adapter._resolved_reviewer_backend() == "codex"


def test_codex_review_mode_is_real_codex_reviewer() -> None:
    """When reviewer backend is codex, review_runtime.mode should be real_codex_reviewer."""
    from kodawari.autopilot.local_adapter import LocalCodexAdapter, LocalCodexAdapterConfig

    config = LocalCodexAdapterConfig(
        opus_reviewer_backend="codex",
        real_peer_review=True,
    )
    adapter = LocalCodexAdapter(config)
    adapter.config.opus_reviewer_backend = "codex"

    mock_review = dict(_VALID_REVIEW_JSON)
    mock_review.update({"reviewer": "opus", "source": "kodawari.real_opus_gateway"})

    with mock.patch(
        "kodawari.autopilot.local_adapter.request_codex_review",
        return_value=(mock_review, ""),
    ):
        review = adapter.review(
            task="test-task",
            context={"task_id": "T-001"},
            changed_files=["src/app.py", "tests/test_app.py"],
            review_iteration=0,
        )
    assert review["review_runtime"]["mode"] == "real_codex_reviewer"
    assert review["review_runtime"]["real_requested"] is True
    # Provenance must reflect the actual backend, not opus
    assert review["reviewer"] == "codex_reviewer"
    assert review["source"] == "kodawari.real_codex_reviewer"


# --- cwd and reviewer_capability wiring ---


@mock.patch("kodawari.autopilot.codex_reviewer._executable_available", return_value=True)
@mock.patch("kodawari.autopilot.codex_reviewer._resolved_executable", return_value="codex")
@mock.patch("subprocess.run")
def test_request_codex_review_passes_cwd_when_project_root_given(
    mock_run: mock.MagicMock,
    _mock_exe: mock.MagicMock,
    _mock_avail: mock.MagicMock,
    tmp_path: Path,
) -> None:
    mock_run.return_value = subprocess.CompletedProcess(
        args=["codex"], returncode=0, stdout=json.dumps(_VALID_REVIEW_JSON), stderr="",
    )
    config = CodexReviewerConfig()
    request_codex_review(
        config,
        task="t",
        context={},
        changed_files=["src/app.py"],
        review_iteration=0,
        project_root=tmp_path,
    )
    call_kwargs = mock_run.call_args[1]
    assert call_kwargs.get("cwd") == str(tmp_path.resolve())


@mock.patch("kodawari.autopilot.codex_reviewer._executable_available", return_value=True)
@mock.patch("kodawari.autopilot.codex_reviewer._resolved_executable", return_value="codex")
@mock.patch("subprocess.run")
def test_request_codex_review_sets_stable_codex_home_env(
    mock_run: mock.MagicMock,
    _mock_exe: mock.MagicMock,
    _mock_avail: mock.MagicMock,
    tmp_path: Path,
) -> None:
    mock_run.return_value = subprocess.CompletedProcess(
        args=["codex"], returncode=0, stdout=json.dumps(_VALID_REVIEW_JSON), stderr="",
    )
    config = CodexReviewerConfig()
    request_codex_review(
        config,
        task="t",
        context={},
        changed_files=["src/app.py"],
        review_iteration=0,
        project_root=tmp_path,
    )
    call_kwargs = mock_run.call_args[1]
    env = dict(call_kwargs.get("env") or {})
    expected_home = (tmp_path / ".workflow_runtime" / "reviewer_homes" / "reviewer_codex_home").resolve()
    assert env.get("CODEX_HOME") == str(expected_home)
    assert env.get("HOME") == str(expected_home)


@mock.patch("kodawari.autopilot.codex_reviewer._executable_available", return_value=True)
@mock.patch("kodawari.autopilot.codex_reviewer._resolved_executable", return_value="codex")
@mock.patch("subprocess.run")
def test_request_codex_review_no_cwd_when_project_root_none(
    mock_run: mock.MagicMock,
    _mock_exe: mock.MagicMock,
    _mock_avail: mock.MagicMock,
) -> None:
    mock_run.return_value = subprocess.CompletedProcess(
        args=["codex"], returncode=0, stdout=json.dumps(_VALID_REVIEW_JSON), stderr="",
    )
    config = CodexReviewerConfig()
    request_codex_review(
        config, task="t", context={}, changed_files=["src/app.py"], review_iteration=0,
    )
    call_kwargs = mock_run.call_args[1]
    assert call_kwargs.get("cwd") is None


@mock.patch("kodawari.autopilot.codex_reviewer._executable_available", return_value=True)
@mock.patch("kodawari.autopilot.codex_reviewer._resolved_executable", return_value="codex")
@mock.patch("subprocess.run")
def test_request_codex_review_prompt_uses_local_repo_read_capability(
    mock_run: mock.MagicMock,
    _mock_exe: mock.MagicMock,
    _mock_avail: mock.MagicMock,
    tmp_path: Path,
) -> None:
    mock_run.return_value = subprocess.CompletedProcess(
        args=["codex"], returncode=0, stdout=json.dumps(_VALID_REVIEW_JSON), stderr="",
    )
    config = CodexReviewerConfig()
    request_codex_review(
        config,
        task="t",
        context={},
        changed_files=["src/app.py"],
        review_iteration=0,
        project_root=tmp_path,
        review_bundle={"workspace_root": str(tmp_path)},
    )
    stdin_prompt = mock_run.call_args[1].get("input", "")
    assert "no filesystem access" not in stdin_prompt
    assert str(tmp_path) in stdin_prompt or "workspace" in stdin_prompt.lower()
