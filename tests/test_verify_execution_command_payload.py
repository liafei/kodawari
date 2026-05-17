"""Regression tests for verify_execution._command_payload shell-quoting handling.

Bug before fix (sdk-realworld-run-4 discovery):
    _command_payload() used plain str.split() to break a pytest command into
    argv, which destroyed the `-k "foo and bar"` boolean expression because
    the quotes were split away with the words. pytest then treated `and` and
    `bar` as positional file arguments and reported:
        ERROR: file or directory not found: and

Fix: use shlex.split(..., posix=False) to respect quoting, then strip the
preserved outer quotes so the argv token is exactly the boolean expression.
"""

from __future__ import annotations

import sys
import subprocess
from pathlib import Path
from typing import Any

from kodawari.autopilot.execution.verify_execution import (
    maybe_execute_verify_command,
    _command_payload,
    _strip_outer_quotes,
)


def test_pytest_with_quoted_k_expression_stays_one_argv_entry(tmp_path: Path) -> None:
    cmd = 'pytest -q tests/test_foo.py -k "clamp and percentage and 100"'
    payload = _command_payload(project_root=tmp_path, verify_cmd=cmd)
    assert isinstance(payload, list)
    # python -m pytest ...
    assert payload[0] == sys.executable
    assert payload[1:3] == ["-m", "pytest"]
    # The boolean expression must be a SINGLE argv element, not four tokens
    assert "clamp and percentage and 100" in payload
    # And `and` must NOT appear alone (which would make pytest treat it as a file)
    assert "and" not in payload


def test_pytest_with_single_quoted_k_expression(tmp_path: Path) -> None:
    cmd = "pytest -q tests/test_foo.py -k 'a or b'"
    payload = _command_payload(project_root=tmp_path, verify_cmd=cmd)
    assert isinstance(payload, list)
    assert "a or b" in payload
    assert "or" not in payload


def test_pytest_without_keyword_still_works(tmp_path: Path) -> None:
    cmd = "pytest -q tests/test_foo.py"
    payload = _command_payload(project_root=tmp_path, verify_cmd=cmd)
    assert payload == [sys.executable, "-m", "pytest", "-q", "tests/test_foo.py"]


def test_pytest_bare_still_works(tmp_path: Path) -> None:
    payload = _command_payload(project_root=tmp_path, verify_cmd="pytest")
    assert payload == [sys.executable, "-m", "pytest"]


def test_windows_backslash_paths_preserved(tmp_path: Path) -> None:
    # posix=False keeps backslashes intact (don't mangle Windows paths)
    cmd = r"pytest -q tests\test_foo.py"
    payload = _command_payload(project_root=tmp_path, verify_cmd=cmd)
    assert isinstance(payload, list)
    assert any("test_foo.py" in p for p in payload)


def test_malformed_quoting_falls_back_gracefully(tmp_path: Path) -> None:
    # Unclosed quote — must not raise
    cmd = 'pytest -q -k "unclosed'
    payload = _command_payload(project_root=tmp_path, verify_cmd=cmd)
    assert isinstance(payload, list)
    # Degraded but functional: runs *something* rather than crashing
    assert payload[0] == sys.executable


def test_strip_outer_quotes_double() -> None:
    assert _strip_outer_quotes('"foo and bar"') == "foo and bar"


def test_strip_outer_quotes_single() -> None:
    assert _strip_outer_quotes("'x or y'") == "x or y"


def test_strip_outer_quotes_no_quotes() -> None:
    assert _strip_outer_quotes("plain") == "plain"


def test_strip_outer_quotes_mismatched() -> None:
    # Opening " and closing ' — must not strip
    assert _strip_outer_quotes("\"abc'") == "\"abc'"


def test_strip_outer_quotes_only_one_char() -> None:
    assert _strip_outer_quotes('"') == '"'
    assert _strip_outer_quotes('') == ''


def test_verify_command_uses_configured_timeout(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_run(*_args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["timeout"] = kwargs.get("timeout")
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    payload = maybe_execute_verify_command(
        project_root=tmp_path,
        feature="feature",
        task_label="T001",
        verify_cmd="python -m pytest tests/test_example.py",
        changed_files=["backend/sample.py"],
        timeout_seconds=321,
    )

    assert captured["timeout"] == 321
    assert payload is not None
    assert payload["passed"] is True
    assert payload["timeout_seconds"] == 321


def test_failed_verify_keeps_enough_context_for_recovery(tmp_path: Path, monkeypatch) -> None:
    stdout = "\n".join([f"line {index}" for index in range(1, 75)])

    def fake_run(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=[], returncode=1, stdout=stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    payload = maybe_execute_verify_command(
        project_root=tmp_path,
        feature="feature",
        task_label="T001",
        verify_cmd="python -m pytest tests/test_example.py",
        changed_files=["backend/sample.py"],
    )

    assert payload is not None
    assert payload["passed"] is False
    assert "line 1" in payload["summary"]
    assert "line 74" in payload["summary"]
