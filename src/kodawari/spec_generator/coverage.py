from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import Clause, CoverageMatrix, CoverageMatrixItem, Spec


class CoverageGenerator:
    def _spec_by_clause(self, specs: list[Spec]) -> dict[str, Spec]:
        return {spec.prd_clause: spec for spec in specs}

    def _missing_spec_item(self, clause: Clause) -> CoverageMatrixItem:
        return CoverageMatrixItem(
            prd_clause=clause.id,
            epic=clause.epic,
            priority=clause.priority,
            status="FAIL",
            spec_id=None,
            test_ids=[],
            blocking_reason="SPEC not generated",
        )

    def _test_ids(self, spec: Spec) -> list[str]:
        return [
            str(test.get("test_name") or "")
            for test in spec.acceptance_tests
            if str(test.get("test_name") or "").strip()
        ]

    def _covered_item(self, clause: Clause, spec: Spec) -> CoverageMatrixItem:
        test_ids = self._test_ids(spec)
        status = "PASS" if len(test_ids) > 0 else "PARTIAL"
        reason = "" if status == "PASS" else "acceptance tests missing"
        return CoverageMatrixItem(
            prd_clause=clause.id,
            epic=clause.epic,
            priority=clause.priority,
            status=status,
            spec_id=spec.spec_id,
            test_ids=test_ids,
            blocking_reason=reason,
        )

    def generate_matrix(self, prd_clauses: list[Clause], specs: list[Spec]) -> CoverageMatrix:
        by_clause = self._spec_by_clause(specs)
        items: list[CoverageMatrixItem] = []
        for clause in prd_clauses:
            spec = by_clause.get(clause.id)
            if spec is None:
                items.append(self._missing_spec_item(clause))
                continue
            items.append(self._covered_item(clause, spec))
        return CoverageMatrix(items=items)

    def export_json(self, matrix: CoverageMatrix, output_path: str) -> None:
        path = Path(output_path)
        payload: dict[str, Any] = matrix.to_dict()
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def export_markdown(self, matrix: CoverageMatrix, output_path: str) -> None:
        lines = [
            "# PRD P0 Coverage Matrix",
            "",
            "| PRD Clause | Epic | Priority | SPEC ID | Tests | Status | Blocking Reason |",
            "|---|---|---|---|---|---|---|",
        ]
        for item in matrix.items:
            tests = ", ".join(item.test_ids) if item.test_ids else "-"
            spec_id = item.spec_id or "-"
            reason = item.blocking_reason or "-"
            lines.append(
                f"| {item.prd_clause} | {item.epic} | {item.priority} | {spec_id} | {tests} | {item.status} | {reason} |"
            )
        Path(output_path).write_text("\n".join(lines) + "\n", encoding="utf-8")
