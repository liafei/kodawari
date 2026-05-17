"""Generic API endpoint pattern."""

from __future__ import annotations

import re

from kodawari.patterns.registry import TaskPattern


class APIEndpointPattern(TaskPattern):
    pattern_id = "api-endpoint"
    title = "API Endpoint Pattern"
    rationale = "Task appears to add or update an HTTP API contract."
    confidence = 0.74
    checklist = [
        "Confirm request and response contract.",
        "Implement handler and validation.",
        "Wire routing and service integration.",
        "Add endpoint contract tests.",
    ]
    verify_hints = ["test_*api*.py", "test_*endpoint*.py"]
    triggers = [
        re.compile(r"\bapi\b", re.IGNORECASE),
        re.compile(r"\bendpoint\b", re.IGNORECASE),
        re.compile(r"\broute\b", re.IGNORECASE),
    ]
