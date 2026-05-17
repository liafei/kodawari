"""Schema migration pattern for database changes."""

from __future__ import annotations

import re

from kodawari.patterns.registry import TaskPattern


class SchemaMigrationPattern(TaskPattern):
    pattern_id = "schema-migration"
    title = "Schema Migration Pattern"
    rationale = "Task changes storage schema and needs compatibility, migration, rollback, and tests."
    confidence = 0.9
    checklist = [
        "Define the target schema change.",
        "Write forward migration steps.",
        "Check backward compatibility.",
        "Plan data backfill or migration safety.",
        "Add rollback strategy.",
        "Update ORM or repository models.",
        "Add migration tests.",
    ]
    verify_hints = ["test_*schema*.py", "test_*migration*.py"]
    triggers = [
        re.compile(r"(schema|migration|alter table|add column|modify field|create table|drop table|drop column)", re.IGNORECASE),
    ]
