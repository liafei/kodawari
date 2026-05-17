"""Auto-replan on readiness BLOCK — hint plumbing.

Verify that:
  1. ``collect_planning_context`` picks up ``.precondition_replan_hint.json``
     and stamps ``precondition_replan_hint`` onto the planning context payload
  2. ``render_context_for_prompt`` surfaces the hint as a high-priority
     instruction so the planner cannot miss it
"""

from __future__ import annotations

import json
from pathlib import Path

from kodawari.autopilot.planning.planning_context import (
    PRECONDITION_REPLAN_HINT_FILENAME,
    collect_planning_context,
    render_context_for_prompt,
)


def _seed_repo_inventory() -> dict:
    return {
        "archetype": "python_service",
        "project_layout": {"code_roots": ["src"]},
        "capabilities": [],
    }


def test_collect_picks_up_hint_file_when_present(tmp_path: Path) -> None:
    planning_dir = tmp_path / "planning" / "feat"
    planning_dir.mkdir(parents=True)
    hint = {
        "schema_version": "planning.precondition_hint.v1",
        "missing_field_preconditions": ["social_thread_snapshots.crawl_provider_kind"],
        "missing_symbol_preconditions": [],
        "suggested_next_task": "Add or authorize a schema/migration task for: social_thread_snapshots.crawl_provider_kind",
    }
    (planning_dir / PRECONDITION_REPLAN_HINT_FILENAME).write_text(json.dumps(hint), encoding="utf-8")

    context = collect_planning_context(
        project_root=tmp_path,
        repo_inventory=_seed_repo_inventory(),
        prd_path=None,
        task_direction="add a thing",
        feature="feat",
        planning_dir=planning_dir,
    )

    assert context["precondition_replan_hint"]["missing_field_preconditions"] == [
        "social_thread_snapshots.crawl_provider_kind"
    ]


def test_collect_returns_empty_hint_when_file_absent(tmp_path: Path) -> None:
    planning_dir = tmp_path / "planning" / "feat"
    planning_dir.mkdir(parents=True)

    context = collect_planning_context(
        project_root=tmp_path,
        repo_inventory=_seed_repo_inventory(),
        prd_path=None,
        task_direction="add a thing",
        feature="feat",
        planning_dir=planning_dir,
    )

    assert context["precondition_replan_hint"] == {}


def test_collect_tolerates_corrupt_hint_file(tmp_path: Path) -> None:
    planning_dir = tmp_path / "planning" / "feat"
    planning_dir.mkdir(parents=True)
    (planning_dir / PRECONDITION_REPLAN_HINT_FILENAME).write_text("not valid json", encoding="utf-8")

    context = collect_planning_context(
        project_root=tmp_path,
        repo_inventory=_seed_repo_inventory(),
        prd_path=None,
        task_direction="add a thing",
        feature="feat",
        planning_dir=planning_dir,
    )
    assert context["precondition_replan_hint"] == {}


def test_render_includes_hint_when_present() -> None:
    context = {
        "task_direction": "add x",
        "precondition_replan_hint": {
            "missing_field_preconditions": ["social_thread_snapshots.crawl_provider_kind"],
            "missing_symbol_preconditions": ["pkg.svc:RoutingService"],
            "suggested_next_task": "Add migration",
        },
    }
    rendered = render_context_for_prompt(context)
    assert "PRECONDITION REPLAN HINT" in rendered
    assert "social_thread_snapshots.crawl_provider_kind" in rendered
    assert "pkg.svc:RoutingService" in rendered
    assert "Add migration" in rendered


def test_render_surfaces_ddl_evidence_when_present() -> None:
    """v2 hint payload includes structural DDL evidence per missing field —
    the planner sees CREATE/ALTER counts so it can weigh "code mentions
    column" against "DDL never adds column"."""

    context = {
        "task_direction": "add x",
        "precondition_replan_hint": {
            "missing_field_preconditions": ["events.crawl_provider_kind"],
            "field_evidence": {
                "events.crawl_provider_kind": {
                    "ddl_create_matches": 0,
                    "ddl_alter_matches": 0,
                    "code_string_files": [
                        "backend/services/feed.py",
                        "backend/services/event.py",
                    ],
                    "conclusion": (
                        "column does NOT exist in any CREATE TABLE or ALTER TABLE; "
                        "code-string mentions are JSON keys, not real columns"
                    ),
                }
            },
        },
    }
    rendered = render_context_for_prompt(context)
    assert "CREATE TABLE matches=0" in rendered
    assert "ALTER TABLE ADD COLUMN matches=0" in rendered
    assert "code-string mentions (2 files" in rendered
    assert "NOT proof of column existence" in rendered


def test_render_includes_concrete_prereq_task_template_for_missing_fields() -> None:
    """The planner gets a concrete task example so it stops just saying
    "add a prereq task" without the right shape."""

    context = {
        "task_direction": "add x",
        "precondition_replan_hint": {
            "missing_field_preconditions": ["events.user_interest_align"],
        },
    }
    rendered = render_context_for_prompt(context)
    assert "Concrete prereq task shape" in rendered
    assert "T0_ADD_USER_INTEREST_ALIGN" in rendered
    assert "backend/db/migration_sql/" in rendered
    assert "PRAGMA table_info(events)" in rendered


def test_render_uses_concrete_migration_path_when_project_root_has_migrations(tmp_path: Path) -> None:
    """The hint must give the planner a concrete next migration filename so it
    cannot paste a ``<NEXT>`` placeholder into ``files_to_change``. The path
    guard rejects placeholder strings, so a literal ``<NEXT>`` triggers
    PATH_OUT_OF_SCOPE downstream."""

    migration_dir = tmp_path / "backend" / "db" / "migration_sql"
    migration_dir.mkdir(parents=True)
    (migration_dir / "20260101_007_seed.sql").write_text("-- seed", encoding="utf-8")
    (migration_dir / "20260415_017_other.sql").write_text("-- other", encoding="utf-8")
    (migration_dir / "README.md").write_text("not a migration", encoding="utf-8")

    context = {
        "project_root": str(tmp_path),
        "task_direction": "add x",
        "precondition_replan_hint": {
            "missing_field_preconditions": ["events.user_interest_align"],
        },
    }
    rendered = render_context_for_prompt(context)
    # The JSON template lines must NOT contain a placeholder — the path guard
    # would reject it. The CRITICAL warning may still mention <NEXT> as
    # something the planner is told to avoid.
    template_lines = [
        line for line in rendered.splitlines() if line.lstrip().startswith('"files_to_change"')
    ]
    assert template_lines, "expected files_to_change line in rendered template"
    assert all("<NEXT>" not in line for line in template_lines)
    # The next number must be 018 (highest existing 017 + 1) and the date must
    # not be a placeholder.
    assert "_018_add_user_interest_align.sql" in rendered
    assert "<USE_REAL_DATE>" not in rendered


def test_render_falls_back_when_no_migration_dir(tmp_path: Path) -> None:
    """When project_root has no ``backend/db/migration_sql`` directory, fall
    back to a placeholder-shaped path that explicitly tells the planner to
    fill in a real date and number."""

    context = {
        "project_root": str(tmp_path),
        "task_direction": "add x",
        "precondition_replan_hint": {
            "missing_field_preconditions": ["events.user_interest_align"],
        },
    }
    rendered = render_context_for_prompt(context)
    assert "<USE_REAL_DATE>" in rendered
    assert "<NEXT_NUMBER>" in rendered


def test_render_omits_hint_section_when_empty() -> None:
    context = {"task_direction": "add x", "precondition_replan_hint": {}}
    rendered = render_context_for_prompt(context)
    assert "PRECONDITION REPLAN HINT" not in rendered


def test_render_omits_hint_when_no_actionable_fields() -> None:
    """Empty lists + empty suggested → no hint section."""
    context = {
        "task_direction": "add x",
        "precondition_replan_hint": {
            "missing_field_preconditions": [],
            "missing_symbol_preconditions": [],
            "suggested_next_task": "",
        },
    }
    rendered = render_context_for_prompt(context)
    assert "PRECONDITION REPLAN HINT" not in rendered
