"""Tests for `kodawari init-wizard` — interactive + non-interactive config bootstrap."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Iterator

import pytest

from kodawari.cli.runtime import init_wizard_cmd


def _args(project_root: Path, **overrides) -> argparse.Namespace:
    defaults: dict = {
        "project_root": str(project_root),
        "preset": "",
        "yes": False,
        "overwrite": False,
        "output": None,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


# ---------------------------------------------------------------------------
# Non-interactive (CI-friendly) mode
# ---------------------------------------------------------------------------


def test_non_interactive_requires_preset(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = init_wizard_cmd.run_init_wizard_command(_args(tmp_path, yes=True))
    payload = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert payload["status"] == "FAIL"
    assert "--preset" in payload["error"]


def test_unknown_preset_rejected(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = init_wizard_cmd.run_init_wizard_command(_args(tmp_path, preset="not-a-preset"))
    payload = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert payload["status"] == "FAIL"


def test_claude_subscription_preset_generates_no_key_config(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = init_wizard_cmd.run_init_wizard_command(
        _args(tmp_path, preset="claude-subscription", yes=True),
    )
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["preset"] == "claude-subscription"

    models = (tmp_path / ".claude" / "workflow" / "models.yaml").read_text(encoding="utf-8")
    env_example = (tmp_path / ".env.example").read_text(encoding="utf-8")

    # claude-subscription must NOT mention API key env vars in env.example —
    # the whole point of the preset is no-key auth via the CLI.
    assert "claude_subscription" in models
    assert "driver: claude_code" in models
    assert "api_key_env" not in models
    assert "WORKFLOW_API_KEY" not in env_example
    # ...but should suggest running auth login.
    assert "claude auth login" in env_example or "auth login" in models or "claude auth" in "\n".join(payload["next_steps"])


def test_openai_compatible_preset_uses_single_transport(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = init_wizard_cmd.run_init_wizard_command(
        _args(tmp_path, preset="openai-compatible", yes=True),
    )
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0

    models = (tmp_path / ".claude" / "workflow" / "models.yaml").read_text(encoding="utf-8")
    # Single transport reused by all 4 roles (planner + plan_reviewer + impl_reviewer + executor).
    assert models.count("primary_tool_use") >= 5  # transport definition + 4 role.transport refs + compat
    assert "kind: http" in models
    assert "api_format: openai_chat" in models

    env_example = (tmp_path / ".env.example").read_text(encoding="utf-8")
    assert "WORKFLOW_API_KEY=<paste-your-key-here>" in env_example


def test_multi_provider_preset_emits_three_distinct_transports(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = init_wizard_cmd.run_init_wizard_command(
        _args(tmp_path, preset="multi-provider", yes=True),
    )
    assert rc == 0
    capsys.readouterr()

    models = (tmp_path / ".claude" / "workflow" / "models.yaml").read_text(encoding="utf-8")
    assert "planner_tool_use" in models
    assert "reviewer_tool_use" in models
    assert "executor_tool_use" in models

    env_example = (tmp_path / ".env.example").read_text(encoding="utf-8")
    # Each role has its own key in multi-provider mode.
    assert "WORKFLOW_PLANNER_API_KEY" in env_example
    assert "WORKFLOW_REVIEWER_API_KEY" in env_example
    assert "WORKFLOW_EXECUTOR_API_KEY" in env_example


# ---------------------------------------------------------------------------
# Interactive mode
# ---------------------------------------------------------------------------


def _scripted_reader(answers: list[str]):
    iterator: Iterator[str] = iter(answers)

    def _reader(_prompt: str) -> str:
        return next(iterator)

    return _reader


def test_prompt_preset_accepts_numeric_choice() -> None:
    reader = _scripted_reader(["1"])
    assert init_wizard_cmd._prompt_preset(reader=reader) == "claude-subscription"


def test_prompt_preset_accepts_text_choice() -> None:
    reader = _scripted_reader(["openai-compatible"])
    assert init_wizard_cmd._prompt_preset(reader=reader) == "openai-compatible"


def test_prompt_preset_rejects_invalid_then_accepts(capsys: pytest.CaptureFixture[str]) -> None:
    reader = _scripted_reader(["banana", "3"])
    result = init_wizard_cmd._prompt_preset(reader=reader)
    captured = capsys.readouterr().out
    assert result == "multi-provider"
    assert "Unrecognized choice" in captured


def test_prompt_openai_compatible_overrides_defaults(tmp_path: Path) -> None:
    reader = _scripted_reader(["https://my.gateway/v1", "MY_KEY", "deepseek-v4"])
    answer = init_wizard_cmd._prompt_openai_compatible(project_root=tmp_path, reader=reader)
    assert answer.base_url == "https://my.gateway/v1"
    assert answer.api_key_env == "MY_KEY"
    assert answer.model == "deepseek-v4"


def test_prompt_openai_compatible_accepts_blanks_as_defaults(tmp_path: Path) -> None:
    reader = _scripted_reader(["", "", ""])
    answer = init_wizard_cmd._prompt_openai_compatible(project_root=tmp_path, reader=reader)
    assert answer.base_url == "https://api.openai.com/v1"
    assert answer.api_key_env == "WORKFLOW_API_KEY"
    assert answer.model == "gpt-4o"


# ---------------------------------------------------------------------------
# Filesystem behavior
# ---------------------------------------------------------------------------


def test_existing_models_yaml_is_backed_up_not_clobbered(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config_dir = tmp_path / ".claude" / "workflow"
    config_dir.mkdir(parents=True)
    existing = config_dir / "models.yaml"
    existing.write_text("schema_version: existing\n", encoding="utf-8")

    rc = init_wizard_cmd.run_init_wizard_command(
        _args(tmp_path, preset="claude-subscription", yes=True),
    )
    capsys.readouterr()
    assert rc == 0
    backup = config_dir / "models.yaml.bak.before_wizard"
    assert backup.exists(), "Pre-existing models.yaml must be backed up before overwrite"
    assert backup.read_text(encoding="utf-8") == "schema_version: existing\n"
    # New content was written.
    assert "claude_subscription" in existing.read_text(encoding="utf-8")


def test_overwrite_flag_skips_backup(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config_dir = tmp_path / ".claude" / "workflow"
    config_dir.mkdir(parents=True)
    (config_dir / "models.yaml").write_text("schema_version: existing\n", encoding="utf-8")

    rc = init_wizard_cmd.run_init_wizard_command(
        _args(tmp_path, preset="claude-subscription", yes=True, overwrite=True),
    )
    capsys.readouterr()
    assert rc == 0
    backup = config_dir / "models.yaml.bak.before_wizard"
    assert not backup.exists(), "--overwrite must skip the backup"


def test_output_path_writes_json_artifact(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out_path = tmp_path / "wizard.json"
    rc = init_wizard_cmd.run_init_wizard_command(
        _args(tmp_path, preset="claude-subscription", yes=True, output=str(out_path)),
    )
    capsys.readouterr()
    assert rc == 0
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["preset"] == "claude-subscription"
    assert payload["status"] == "PASS"
    assert "next_steps" in payload
