"""Project-local .env loader for the workflow CLI."""

from __future__ import annotations

import os
from pathlib import Path
import textwrap

import pytest

from kodawari.cli.dotenv_loader import find_dotenv, load_dotenv, parse_dotenv


def test_parse_dotenv_handles_quotes_and_comments() -> None:
    text = """# comment
WORKFLOW_REVIEWER_API_KEY="sk-secret"
WORKFLOW_REVIEWER_BASE_URL=https://reviewer.test/v1
WORKFLOW_PLANNER_MODEL='claude-opus-4-7'

# blank line above
export WORKFLOW_FORCE_MODEL_PLANNING=1
INVALID LINE WITHOUT EQUALS
"""
    parsed = parse_dotenv(text)
    assert parsed["WORKFLOW_REVIEWER_API_KEY"] == "sk-secret"
    assert parsed["WORKFLOW_REVIEWER_BASE_URL"] == "https://reviewer.test/v1"
    assert parsed["WORKFLOW_PLANNER_MODEL"] == "claude-opus-4-7"
    assert parsed["WORKFLOW_FORCE_MODEL_PLANNING"] == "1"
    assert "INVALID" not in parsed


def test_load_dotenv_setdefault_does_not_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".env").write_text("WORKFLOW_REVIEWER_MODEL=from-file\n", encoding="utf-8")
    monkeypatch.setenv("WORKFLOW_REVIEWER_MODEL", "from-shell")
    load_dotenv(tmp_path)
    # Shell wins.
    assert os.environ["WORKFLOW_REVIEWER_MODEL"] == "from-shell"


def test_load_dotenv_fills_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".env").write_text("WORKFLOW_REVIEWER_API_KEY=loaded\n", encoding="utf-8")
    monkeypatch.delenv("WORKFLOW_REVIEWER_API_KEY", raising=False)
    load_dotenv(tmp_path)
    assert os.environ.get("WORKFLOW_REVIEWER_API_KEY") == "loaded"


def test_find_dotenv_walks_upward(tmp_path: Path) -> None:
    nested = tmp_path / "feature" / "planning" / "feat-x"
    nested.mkdir(parents=True)
    (tmp_path / ".env").write_text("X=1\n", encoding="utf-8")
    found = find_dotenv(nested)
    assert found is not None
    assert found.resolve() == (tmp_path / ".env").resolve()


def test_load_dotenv_no_file_returns_none(tmp_path: Path) -> None:
    assert load_dotenv(tmp_path) is None


def test_load_dotenv_override_true(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".env").write_text("WORKFLOW_REVIEWER_MODEL=from-file\n", encoding="utf-8")
    monkeypatch.setenv("WORKFLOW_REVIEWER_MODEL", "from-shell")
    load_dotenv(tmp_path, override=True)
    assert os.environ["WORKFLOW_REVIEWER_MODEL"] == "from-file"


def test_cli_main_loads_project_root_dotenv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from kodawari.cli import main as cli_main

    caller = tmp_path / "caller"
    target = tmp_path / "target"
    caller.mkdir()
    (target / ".claude" / "workflow").mkdir(parents=True)
    (target / ".env").write_text(
        "WORKFLOW_MIMO_KEY=project-key\nWORKFLOW_MIMO_BASE_URL=https://example.test/v1\n",
        encoding="utf-8",
    )
    (target / ".claude" / "workflow" / "models.yaml").write_text(
        textwrap.dedent(
            """
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
              - {models: [mimo-v2.5-pro], transports: [mimo_chat], interfaces: [chat]}
              - {models: [manual], transports: [manual_exec], interfaces: [manual]}
            roles:
              planner:
                transport: mimo_chat
                model: mimo-v2.5-pro
                on_unavailable: fail
              executor:
                transport: manual_exec
                model: manual
                on_unavailable: fail
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(caller)
    monkeypatch.delenv("WORKFLOW_MIMO_KEY", raising=False)
    monkeypatch.delenv("WORKFLOW_MIMO_BASE_URL", raising=False)

    rc = cli_main.main(["doctor", "models", "--project-root", str(target), "--offline"])
    output = capsys.readouterr().out

    assert rc == 0
    assert os.environ["WORKFLOW_MIMO_KEY"] == "project-key"
    assert '"project_root": "' in output
    assert '"api_key_present": true' in output
