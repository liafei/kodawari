"""Tests for the project-level CLI defaults loaded from defaults.yaml."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from kodawari.cli.runtime.workflow_defaults import (
    BUILTIN_DEFAULTS,
    apply_workflow_defaults,
    load_workflow_defaults,
    render_default_defaults_yaml,
)


def _write_defaults(tmp_path: Path, content: str) -> Path:
    config_dir = tmp_path / ".claude" / "workflow"
    config_dir.mkdir(parents=True)
    path = config_dir / "defaults.yaml"
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def test_load_returns_empty_when_file_missing(tmp_path: Path) -> None:
    assert load_workflow_defaults(tmp_path) == {}


def test_load_parses_int_bool_string_scalars(tmp_path: Path) -> None:
    _write_defaults(tmp_path, "\n".join([
        "max_cycles: 7",
        "max_wall_clock_seconds: 1800",
        "real_peer_review: false",
        "rollback_on_failure: yes",
        "planner_route: model",
        "gate_profile: blocking",
    ]))
    loaded = load_workflow_defaults(tmp_path)
    assert loaded["max_cycles"] == 7
    assert loaded["max_wall_clock_seconds"] == 1800
    assert loaded["real_peer_review"] is False
    assert loaded["rollback_on_failure"] is True  # 'yes' → True
    assert loaded["planner_route"] == "model"
    assert loaded["gate_profile"] == "blocking"


def test_load_skips_comments_and_blanks(tmp_path: Path) -> None:
    _write_defaults(tmp_path, "\n".join([
        "# Top comment",
        "",
        "max_cycles: 4    # inline comment",
        "",
        "# Another comment",
        "real_peer_review: true",
    ]))
    loaded = load_workflow_defaults(tmp_path)
    assert loaded == {"max_cycles": 4, "real_peer_review": True}


def test_load_returns_empty_on_unparseable_line(tmp_path: Path) -> None:
    """Malformed defaults.yaml should soft-fail to empty — never crash the
    CLI on a hand-edited config error."""
    _write_defaults(tmp_path, "max_cycles 7\n")  # missing colon
    loaded = load_workflow_defaults(tmp_path)
    assert loaded == {}


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


def test_apply_fills_none_values_from_builtin_when_no_yaml(tmp_path: Path) -> None:
    args = argparse.Namespace(
        max_cycles=None,
        max_wall_clock_seconds=None,
        real_peer_review=None,
        rollback_on_failure=None,
        planner_route=None,
        gate_profile=None,
    )
    apply_workflow_defaults(args, tmp_path)
    assert args.max_cycles == BUILTIN_DEFAULTS["max_cycles"]
    assert args.max_wall_clock_seconds == BUILTIN_DEFAULTS["max_wall_clock_seconds"]
    assert args.real_peer_review is BUILTIN_DEFAULTS["real_peer_review"]


def test_apply_yaml_overrides_builtin(tmp_path: Path) -> None:
    _write_defaults(tmp_path, "max_cycles: 10\nreal_peer_review: false\n")
    args = argparse.Namespace(
        max_cycles=None,
        max_wall_clock_seconds=None,
        real_peer_review=None,
    )
    apply_workflow_defaults(args, tmp_path)
    assert args.max_cycles == 10  # yaml wins
    assert args.max_wall_clock_seconds == BUILTIN_DEFAULTS["max_wall_clock_seconds"]  # builtin
    assert args.real_peer_review is False  # yaml wins


def test_apply_does_not_overwrite_explicit_cli_value(tmp_path: Path) -> None:
    """The whole point: a user-passed CLI flag must win over both yaml and
    builtin. Non-None values are sacrosanct."""
    _write_defaults(tmp_path, "max_cycles: 10\n")
    args = argparse.Namespace(
        max_cycles=3,  # user passed --max-cycles 3
        max_wall_clock_seconds=None,
    )
    apply_workflow_defaults(args, tmp_path)
    assert args.max_cycles == 3, "explicit CLI value must NOT be overridden by yaml/builtin"
    assert args.max_wall_clock_seconds == BUILTIN_DEFAULTS["max_wall_clock_seconds"]


def test_apply_handles_missing_attributes_gracefully(tmp_path: Path) -> None:
    """Most CLI commands won't have all six default keys on their namespace.
    Apply must not crash when a key is missing — it should just be a no-op
    for those commands."""
    args = argparse.Namespace(max_cycles=None)  # only one attribute
    apply_workflow_defaults(args, tmp_path)
    assert args.max_cycles == BUILTIN_DEFAULTS["max_cycles"]
    assert not hasattr(args, "max_wall_clock_seconds")


# ---------------------------------------------------------------------------
# Rendered template
# ---------------------------------------------------------------------------


def test_rendered_template_round_trips_through_loader(tmp_path: Path) -> None:
    """Whatever init-wizard emits MUST parse cleanly back into the same
    values as the builtin defaults — otherwise the wizard would write a
    file that immediately produces drift on the first load."""
    rendered = render_default_defaults_yaml()
    _write_defaults(tmp_path, rendered)
    loaded = load_workflow_defaults(tmp_path)
    for key, value in BUILTIN_DEFAULTS.items():
        assert loaded[key] == value, f"defaults.yaml round-trip lost {key}: {loaded.get(key)!r} vs {value!r}"


def test_rendered_template_contains_explanatory_comments() -> None:
    rendered = render_default_defaults_yaml()
    assert "# Project-level CLI defaults" in rendered
    assert "Generated by `kodawari init-wizard`" in rendered
    assert "CLI flags always override" in rendered
