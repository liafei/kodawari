"""Readiness must distinguish "column declared in DDL" from "column name
appears somewhere in code".

Real-run regression: prd11-mimo-social-aggregation falsely PASSed because
``crawl_provider_kind`` appeared in service Python files even though no
CREATE / ALTER TABLE ever added it to ``social_thread_snapshots``. The
loose substring fallback in ``_field_exists`` is removed; ALTER TABLE
ADD COLUMN is now recognized so legitimate post-CREATE additions still
pass.
"""

from __future__ import annotations

from pathlib import Path

from kodawari.autopilot.planning.execution_readiness import evaluate_execution_readiness


def _seed(tmp_path: Path, sql: dict[str, str], py: dict[str, str] | None = None) -> Path:
    sql_dir = tmp_path / "backend" / "db" / "migration_sql"
    sql_dir.mkdir(parents=True)
    for name, content in sql.items():
        (sql_dir / name).write_text(content, encoding="utf-8")
    if py:
        py_dir = tmp_path / "backend" / "services"
        py_dir.mkdir(parents=True)
        for name, content in py.items():
            (py_dir / name).write_text(content, encoding="utf-8")
    return tmp_path


def test_alter_table_add_column_counts_as_existing(tmp_path: Path) -> None:
    project_root = _seed(
        tmp_path,
        {
            "001_base.sql": "CREATE TABLE social_thread_snapshots (\n    id TEXT,\n    snapshot_state TEXT\n);\n",
            "018_kol.sql": "ALTER TABLE social_thread_snapshots ADD COLUMN cluster_id TEXT;\n",
        },
    )
    card = {
        "files_to_change": ["tests/test_x.py"],
        "requires": [
            {"kind": "field", "name": "social_thread_snapshots.snapshot_state", "source": "existing"},
            {"kind": "field", "name": "social_thread_snapshots.cluster_id", "source": "existing"},
        ],
    }
    result = evaluate_execution_readiness(project_root=project_root, task_card=card)
    assert result["status"] == "PASS", result
    assert result["missing_field_preconditions"] == []


def test_substring_in_unrelated_python_does_not_pass(tmp_path: Path) -> None:
    """The crawl_provider_kind regression: column referenced in service code
    but never added to the table by any DDL must NOT pass readiness."""

    project_root = _seed(
        tmp_path,
        {
            "001_base.sql": "CREATE TABLE social_thread_snapshots (\n    id TEXT,\n    snapshot_state TEXT\n);\n",
        },
        py={
            # The column name appears here as a string literal — old loose
            # heuristic falsely passed because of this kind of usage.
            "feed_service.py": (
                "def render_card(row):\n"
                "    return {'crawl_provider_kind': row.get('crawl_provider_kind', 'unknown')}\n"
            ),
        },
    )
    card = {
        "files_to_change": ["tests/test_x.py"],
        "requires": [
            {
                "kind": "field",
                "name": "social_thread_snapshots.crawl_provider_kind",
                "source": "existing",
            }
        ],
    }
    result = evaluate_execution_readiness(project_root=project_root, task_card=card)
    assert result["status"] == "BLOCKED", result
    assert "social_thread_snapshots.crawl_provider_kind" in result["missing_field_preconditions"]


def test_unknown_table_is_not_a_schema_candidate(tmp_path: Path) -> None:
    """If the table name never appears in any CREATE TABLE, the field is
    not schema-checked at all — readiness short-circuits to PASS instead
    of falsely matching on substring."""

    project_root = _seed(
        tmp_path,
        {
            "001_base.sql": "CREATE TABLE other_table (id TEXT);\n",
        },
    )
    card = {
        "files_to_change": ["src/x.py"],
        "requires": [
            {"kind": "field", "name": "no_such_table.no_such_field", "source": "existing"}
        ],
    }
    result = evaluate_execution_readiness(project_root=project_root, task_card=card)
    # The requirement is dropped from the schema-candidate set — PASS.
    assert result["status"] == "PASS"
    assert result["missing_field_preconditions"] == []


def test_json_payload_tolerance_requires_explicit_accessor_optin(tmp_path: Path) -> None:
    """Regression: prd11-mimo-social-aggregation false-PASSed
    crawl_provider_kind because the table owns snapshot_payload_json.
    Tolerance is now opt-in via accessor=json_payload — without it,
    a missing real column must BLOCK."""

    project_root = _seed(
        tmp_path,
        {
            "001.sql": (
                "CREATE TABLE social_thread_snapshots (\n"
                "  event_id TEXT,\n"
                "  snapshot_payload_json TEXT\n"
                ");\n"
            ),
        },
    )
    # No accessor → planner means a real DDL column → BLOCK.
    card_strict = {
        "files_to_change": ["src/x.py"],
        "requires": [
            {
                "kind": "field",
                "name": "social_thread_snapshots.crawl_provider_kind",
                "source": "existing",
            }
        ],
    }
    strict = evaluate_execution_readiness(project_root=project_root, task_card=card_strict)
    assert strict["status"] == "BLOCKED"
    assert "social_thread_snapshots.crawl_provider_kind" in strict["missing_field_preconditions"]
    assert strict["json_payload_field_preconditions"] == []

    # accessor=json_payload → planner explicitly opts into the tolerance.
    card_opted = dict(card_strict)
    card_opted["requires"] = [
        {
            "kind": "field",
            "name": "social_thread_snapshots.crawl_provider_kind",
            "source": "existing",
            "accessor": "json_payload",
        }
    ]
    opted = evaluate_execution_readiness(project_root=project_root, task_card=card_opted)
    assert opted["status"] == "PASS"
    assert opted["json_payload_field_preconditions"] == ["social_thread_snapshots.crawl_provider_kind"]


def test_blocked_requirement_lists_table_with_no_alter_path(tmp_path: Path) -> None:
    """If the table exists but the column is not added by any DDL, BLOCK."""

    project_root = _seed(
        tmp_path,
        {
            "001_base.sql": "CREATE TABLE social_thread_snapshots (id TEXT);\n",
        },
    )
    card = {
        "files_to_change": ["src/x.py"],
        "requires": [
            {
                "kind": "field",
                "name": "social_thread_snapshots.crawl_provider_kind",
                "source": "existing",
            }
        ],
    }
    result = evaluate_execution_readiness(project_root=project_root, task_card=card)
    assert result["status"] == "BLOCKED"
    assert "social_thread_snapshots.crawl_provider_kind" in result["missing_field_preconditions"]
