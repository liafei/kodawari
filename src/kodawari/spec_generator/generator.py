from __future__ import annotations

import hashlib
import re
from typing import Any

from .analyzer import ClauseAnalyzer
from .models import Clause, SectionFlags, Spec


def _slug(text: str) -> str:
    lowered = text.strip().lower()
    cleaned = re.sub(r"[^a-z0-9]+", "_", lowered).strip("_")
    return cleaned or "unknown"


class SpecGenerator:
    def __init__(self) -> None:
        self.analyzer = ClauseAnalyzer()

    def generate_spec_id(self, clause: Clause, flags: SectionFlags, prd_doc_slug: str = "prd") -> str:
        epic_slug = _slug(clause.epic)
        clause_slug = _slug(clause.id)
        type_suffix = "_".join(flags.spec_types())
        base = f"{epic_slug}.{_slug(prd_doc_slug)}.{clause_slug}.{type_suffix}"
        # Keep stable and collision-safe with a short deterministic suffix.
        short_hash = hashlib.sha1(base.encode("utf-8")).hexdigest()[:8]
        return f"{base}.{short_hash}"

    def generate_spec(self, clause: Clause, *, prd_doc_slug: str = "prd") -> Spec:
        flags = self.analyzer.detect_sections(clause)
        spec_id = self.generate_spec_id(clause, flags, prd_doc_slug=prd_doc_slug)

        spec = Spec(
            spec_id=spec_id,
            spec_version="1.0",
            prd_clause=clause.id,
            epic=clause.epic,
            priority=clause.priority.upper(),
            spec_types=flags.spec_types(),
        )

        if flags.has_algorithm:
            spec.algorithm = self._generate_algorithm(clause)
        if flags.has_data_structure:
            spec.data_structure = self._generate_data_structure(clause)
        if flags.has_api_contract:
            spec.api_contract = self._generate_api_contract(clause)

        spec.acceptance_tests = self._generate_tests(clause)
        spec.dependencies = self._identify_dependencies(clause)
        spec.risks = [{"risk": "rebuild baseline logic may be incomplete", "mitigation": "incremental test-first hardening"}]
        spec.assumptions = [{"assumption": "deterministic generation before LLM augmentation", "validation": "validator pass rate"}]
        return spec

    def _generate_algorithm(self, clause: Clause) -> list[dict[str, Any]]:
        return [
            {
                "name": "rule_based_baseline",
                "implementation": "deterministic heuristic",
                "parameters": {"clause_id": clause.id},
                "rationale": "Use deterministic fallback during rebuild to keep generation stable.",
            }
        ]

    def _generate_data_structure(self, clause: Clause) -> list[dict[str, Any]]:
        table_name = f"{_slug(clause.epic)}_{_slug(clause.id)}"
        return [
            {
                "table": table_name,
                "fields": {
                    "id": "VARCHAR(64) PRIMARY KEY",
                    "payload": "JSON",
                    "created_at": "TIMESTAMP",
                },
                "indexes": [f"INDEX idx_{table_name}_created_at ON (created_at)"],
            }
        ]

    def _generate_api_contract(self, clause: Clause) -> list[dict[str, Any]]:
        endpoint_name = _slug(clause.id)
        return [
            {
                "endpoint": f"GET /api/v1/{endpoint_name}",
                "request": {"query_params": {}},
                "response": {"status": "OK", "data": {"clause": clause.id}},
                "error_cases": [{"code": "NOT_FOUND", "reason": "resource missing"}],
            }
        ]

    def _generate_tests(self, clause: Clause) -> list[dict[str, Any]]:
        test_name = f"test_{_slug(clause.id)}_acceptance"
        return [
            {
                "test_name": test_name,
                "test_type": "integration",
                "test_target": f"Validate clause {clause.id} behavior",
                "input": {"clause_id": clause.id},
                "expected_output": {"status": "OK"},
                "assertions": [
                    {"type": "equals", "field": "status", "value": "OK"},
                    {"type": "field_exists", "field": "data"},
                ],
            }
        ]

    def _identify_dependencies(self, clause: Clause) -> list[dict[str, Any]]:
        probe = f"{clause.title}\n{clause.content}".lower()
        dependencies: list[dict[str, Any]] = []
        if "score" in probe or "打分" in probe:
            dependencies.append({"spec_id": "recommender.scoring", "reason": "requires scoring input"})
        if "source" in probe or "来源" in probe:
            dependencies.append({"spec_id": "data.sources", "reason": "requires source metadata"})
        return dependencies
