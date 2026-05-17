from __future__ import annotations

from pathlib import Path

from kodawari.autopilot.planning.execution_readiness import (
    evaluate_execution_readiness,
    evaluate_plan_execution_readiness,
)


def test_execution_readiness_blocks_missing_existing_schema_field_outside_scope(tmp_path: Path) -> None:
    sql = tmp_path / "backend" / "db" / "migration_sql" / "001.sql"
    sql.parent.mkdir(parents=True)
    sql.write_text(
        "CREATE TABLE IF NOT EXISTS events (\n"
        "  event_id TEXT PRIMARY KEY,\n"
        "  source_count INTEGER NOT NULL\n"
        ");\n",
        encoding="utf-8",
    )
    card = {
        "files_to_change": ["backend/api/v1/services/channel_upgrade_scorer.py"],
        "forbidden_changes": ["Do not add new database migrations"],
        "requires": [
            {"kind": "field", "name": "events.event_id", "source": "existing"},
            {"kind": "field", "name": "events.user_interest_align", "source": "existing"},
        ],
    }

    readiness = evaluate_execution_readiness(project_root=tmp_path, task_card=card)

    assert readiness["status"] == "BLOCKED"
    assert readiness["missing_preconditions"] == ["events.user_interest_align"]
    assert "schema/migration task" in readiness["suggested_next_task"]


def test_execution_readiness_allows_missing_field_when_schema_file_is_in_scope(tmp_path: Path) -> None:
    sql = tmp_path / "backend" / "db" / "migration_sql" / "001.sql"
    sql.parent.mkdir(parents=True)
    sql.write_text(
        "CREATE TABLE IF NOT EXISTS events (\n"
        "  event_id TEXT PRIMARY KEY\n"
        ");\n",
        encoding="utf-8",
    )
    card = {
        "files_to_change": ["backend/db_schema.py"],
        "requires": [{"kind": "field", "name": "events.user_interest_align", "source": "existing"}],
    }

    readiness = evaluate_execution_readiness(project_root=tmp_path, task_card=card)

    assert readiness["status"] == "PASS"
    assert readiness["missing_preconditions"] == ["events.user_interest_align"]
    assert readiness["schema_mutation_allowed"] is True


def test_execution_readiness_ignores_non_existing_cross_task_requirements(tmp_path: Path) -> None:
    card = {
        "files_to_change": ["src/service.py"],
        "requires": [{"kind": "field", "name": "events.new_column", "source": "task"}],
    }

    readiness = evaluate_execution_readiness(project_root=tmp_path, task_card=card)

    assert readiness["status"] == "PASS"
    assert readiness["checked_preconditions"] == []


def test_execution_readiness_does_not_block_json_payload_field_requirements(tmp_path: Path) -> None:
    sql = tmp_path / "backend" / "db" / "schema.sql"
    sql.parent.mkdir(parents=True)
    sql.write_text(
        "CREATE TABLE social_thread_snapshots (\n"
        "  id TEXT PRIMARY KEY,\n"
        "  thread_id TEXT NOT NULL,\n"
        "  published_at TEXT,\n"
        "  snapshot_payload_json TEXT NOT NULL\n"
        ");\n",
        encoding="utf-8",
    )
    card = {
        "files_to_change": ["backend/api/v1/services/social_event_aggregation_service.py"],
        "requires": [
            {"kind": "field", "name": "social_thread_snapshots.thread_id", "source": "existing"},
            # JSON-payload tolerance is now opt-in: planner must declare
            # accessor=json_payload (or json_payload_key=true) to mark a
            # name as a JSON key inside an existing JSON column. Without
            # the opt-in this would BLOCK as a missing real column.
            {
                "kind": "field",
                "name": "social_thread_snapshots.engagement_score",
                "source": "existing",
                "accessor": "json_payload",
            },
        ],
    }

    readiness = evaluate_execution_readiness(project_root=tmp_path, task_card=card)

    assert readiness["status"] == "PASS"
    assert readiness["missing_preconditions"] == []
    assert readiness["json_payload_field_preconditions"] == ["social_thread_snapshots.engagement_score"]
    assert readiness["checked_preconditions"] == [
        "social_thread_snapshots.thread_id",
        "social_thread_snapshots.engagement_score",
    ]


def test_plan_execution_readiness_blocks_missing_existing_schema_field(tmp_path: Path) -> None:
    sql = tmp_path / "backend" / "db" / "schema.sql"
    sql.parent.mkdir(parents=True)
    sql.write_text(
        "CREATE TABLE events (\n"
        "  event_id TEXT PRIMARY KEY\n"
        ");\n",
        encoding="utf-8",
    )
    plan = {
        "tasks": [
            {
                "task_id": "T108",
                "files_to_change": ["backend/api/v1/services/channel_upgrade_scorer.py"],
                "requires": [
                    {"kind": "field", "name": "events.event_id", "source": "existing"},
                    {"kind": "field", "name": "events.user_interest_align", "source": "existing"},
                ],
            }
        ]
    }

    readiness = evaluate_plan_execution_readiness(project_root=tmp_path, plan_payload=plan)

    assert readiness["status"] == "BLOCKED"
    assert readiness["blocked_tasks"] == ["T108"]
    assert readiness["missing_preconditions"] == ["events.user_interest_align"]


def test_plan_execution_readiness_allows_missing_field_when_dependent_task_mutates_schema(tmp_path: Path) -> None:
    """A task that *depends_on* a schema-mutating task tolerates missing
    existing-field preconditions: the dependency will run first and make
    the field real before this task executes."""

    sql = tmp_path / "backend" / "db" / "schema.sql"
    sql.parent.mkdir(parents=True)
    sql.write_text(
        "CREATE TABLE events (\n"
        "  event_id TEXT PRIMARY KEY\n"
        ");\n",
        encoding="utf-8",
    )
    plan = {
        "tasks": [
            {
                "task_id": "T001",
                "files_to_change": ["backend/db_schema.py"],
                "requires": [],
            },
            {
                "task_id": "T002",
                "files_to_change": ["backend/service.py"],
                "depends_on": ["T001"],
                "requires": [
                    {"kind": "field", "name": "events.user_interest_align", "source": "existing"}
                ],
            },
        ]
    }

    readiness = evaluate_plan_execution_readiness(project_root=tmp_path, plan_payload=plan)

    assert readiness["status"] == "PASS"
    assert readiness["schema_mutation_allowed"] is True
    assert readiness["missing_preconditions"] == ["events.user_interest_align"]


def test_plan_execution_readiness_blocks_when_task_does_not_depend_on_schema_mutator(tmp_path: Path) -> None:
    """A task that does NOT declare a dependency on the schema-mutating
    task must NOT silently inherit the mutation allowance. The previous
    rule was "any task in plan mutates schema → every task is exempt",
    which let buggy or dishonest plans (e.g. Mimo declaring source=existing
    for non-existent columns) slide through. The fixed rule requires an
    explicit dependency edge."""

    sql = tmp_path / "backend" / "db" / "schema.sql"
    sql.parent.mkdir(parents=True)
    sql.write_text(
        "CREATE TABLE events (\n"
        "  event_id TEXT PRIMARY KEY\n"
        ");\n",
        encoding="utf-8",
    )
    plan = {
        "tasks": [
            {
                "task_id": "T001",
                "files_to_change": ["backend/db_schema.py"],
                "requires": [],
            },
            {
                "task_id": "T002",
                "files_to_change": ["backend/service.py"],
                "requires": [
                    {"kind": "field", "name": "events.user_interest_align", "source": "existing"}
                ],
            },
        ]
    }

    readiness = evaluate_plan_execution_readiness(project_root=tmp_path, plan_payload=plan)

    assert readiness["status"] == "BLOCKED"
    assert readiness["blocked_tasks"] == ["T002"]
    assert readiness["missing_preconditions"] == ["events.user_interest_align"]
