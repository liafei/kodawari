from __future__ import annotations

import re
from pathlib import Path

from .models import Clause, PRD


CLAUSE_ID_PATTERN = re.compile(r"\bF(\d{1,3})\b", re.IGNORECASE)
HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.*)$")
PRIORITY_PATTERN = re.compile(r"\b(P0|P1|P2)\b", re.IGNORECASE)


class PRDParser:
    def _updated_heading(self, raw: str, current_heading: str) -> str:
        heading_match = HEADING_PATTERN.match(raw.strip())
        if not heading_match:
            return current_heading
        return heading_match.group(2).strip()

    def _priority_from_line(self, raw: str) -> str:
        priority_match = PRIORITY_PATTERN.search(raw)
        if priority_match is None:
            return "P1"
        return priority_match.group(1).upper()

    def _clause_from_line(self, raw: str, current_heading: str) -> Clause | None:
        clause_match = CLAUSE_ID_PATTERN.search(raw)
        if clause_match is None:
            return None
        clause_id_num = clause_match.group(1)
        clause_id = f"F{clause_id_num}"
        return Clause(
            id=clause_id,
            title=raw.strip(),
            content="",
            epic=current_heading or "General",
            priority=self._priority_from_line(raw),
        )

    def _flush_clause(
        self,
        *,
        current_clause: Clause | None,
        content_lines: list[str],
        clauses: list[Clause],
    ) -> tuple[Clause | None, list[str]]:
        if current_clause is None:
            return None, content_lines
        merged = "\n".join(content_lines).strip()
        current_clause.content = merged
        clauses.append(current_clause)
        return None, []

    def parse_prd(self, prd_path: str) -> PRD:
        path = Path(prd_path)
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        clauses: list[Clause] = []
        current_heading = ""
        current_clause: Clause | None = None
        content_lines: list[str] = []

        for raw in lines:
            current_heading = self._updated_heading(raw, current_heading)
            new_clause = self._clause_from_line(raw, current_heading)
            if new_clause is not None:
                current_clause, content_lines = self._flush_clause(
                    current_clause=current_clause,
                    content_lines=content_lines,
                    clauses=clauses,
                )
                current_clause = new_clause
                content_lines.append(raw)
                continue

            if current_clause is not None:
                content_lines.append(raw)

        self._flush_clause(
            current_clause=current_clause,
            content_lines=content_lines,
            clauses=clauses,
        )
        return PRD(source_path=str(path), clauses=clauses)

    def extract_p0_clauses(self, prd: PRD) -> list[Clause]:
        return [clause for clause in prd.clauses if clause.priority.upper() == "P0"]
