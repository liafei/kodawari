"""Generic CRUD implementation pattern."""

from __future__ import annotations

import re

from kodawari.patterns.registry import TaskPattern


class CRUDPattern(TaskPattern):
    pattern_id = "crud"
    title = "CRUD Pattern"
    rationale = "Task looks like a standard create/read/update/delete implementation."
    confidence = 0.66
    checklist = [
        "Define or update the data model.",
        "Implement the handler/service methods.",
        "Validate inputs and error paths.",
        "Add API or service tests.",
    ]
    verify_hints = ["test_*crud*.py", "test_*model*.py"]
    triggers = [
        re.compile(r"\bcrud\b", re.IGNORECASE),
        re.compile(r"\bcreate\b.*\bupdate\b", re.IGNORECASE),
        re.compile(r"\bdelete\b", re.IGNORECASE),
    ]
