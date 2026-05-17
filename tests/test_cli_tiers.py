from __future__ import annotations

import pytest

from kodawari.cli.main import main
from kodawari.cli.parser_registry import build_parser, command_tier


def test_command_tiers_are_explicit() -> None:
    assert command_tier("work") == "user"
    assert command_tier("lane-triage") == "operator"
    assert command_tier("compact") == "debug"


def test_default_top_level_help_only_lists_user_commands() -> None:
    parser = build_parser(help_all=False)
    help_text = parser.format_help()

    assert "work-all" in help_text
    assert "serve" in help_text
    assert "lane-triage" not in help_text
    assert "compact" not in help_text


def test_help_all_lists_operator_and_debug_commands() -> None:
    parser = build_parser(help_all=True)
    help_text = parser.format_help()

    assert "lane-triage" in help_text
    assert "compact" in help_text


def test_hidden_operator_command_still_parses() -> None:
    parser = build_parser(help_all=False)
    args = parser.parse_args(["lane-triage", "--project-root", "."])

    assert args.command == "lane-triage"
    assert args.command_tier == "operator"


def test_main_help_all_prints_full_help(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["--help-all"])

    assert rc == 0
    output = capsys.readouterr().out
    assert "lane-triage" in output
    assert "compact" in output
