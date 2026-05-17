from __future__ import annotations

from pathlib import Path
from typing import Any

from .models import Spec


def summarize_plan(feature: str, sections: list[str] | None = None, **kwargs: Any) -> dict[str, Any]:
    """Keep the historical planning_summary helper contract explicit and lightweight."""
    resolved_sections = [str(item) for item in list(sections or [])]
    return {
        "feature": str(feature),
        "sections": resolved_sections,
        "section_count": len(resolved_sections),
        "options": dict(kwargs),
    }


class SpecMaterializer:
    def materialize(self, specs: list[Spec], output_dir: str) -> dict[str, str]:
        target = Path(output_dir)
        target.mkdir(parents=True, exist_ok=True)

        ordered_specs = sorted(specs, key=lambda item: (item.epic.lower(), item.spec_id))

        plan_path = target / "PLAN.md"
        tasks_path = target / "TASKS.md"
        acceptance_path = target / "ACCEPTANCE.md"

        plan_path.write_text(self._render_plan(ordered_specs), encoding="utf-8")
        tasks_path.write_text(self._render_tasks(ordered_specs), encoding="utf-8")
        acceptance_path.write_text(self._render_acceptance(ordered_specs), encoding="utf-8")

        return {
            "plan": str(plan_path),
            "tasks": str(tasks_path),
            "acceptance": str(acceptance_path),
        }

    def summarize_plan(self, feature: str, sections: list[str] | None = None, **kwargs: Any) -> dict[str, Any]:
        """Method form for callers that already hold the materializer object."""
        return summarize_plan(feature=feature, sections=sections, **kwargs)

    def _render_plan(self, specs: list[Spec]) -> str:
        lines: list[str] = ["# PLAN", ""]
        current_epic = ""
        for spec in specs:
            if spec.epic != current_epic:
                current_epic = spec.epic
                lines.append(f"## Epic: {spec.epic}")
                lines.append("")
            lines.append(f"### SPEC: {spec.spec_id}")
            lines.append(f"- PRD Clause: {spec.prd_clause}")
            lines.append(f"- Priority: {spec.priority}")
            lines.append(f"- Spec Types: {', '.join(spec.spec_types)}")
            if spec.algorithm:
                lines.append("- Algorithm: present")
            if spec.data_structure:
                lines.append("- Data Structure: present")
            if spec.api_contract:
                lines.append("- API Contract: present")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def _render_tasks(self, specs: list[Spec]) -> str:
        lines: list[str] = ["# TASKS", ""]
        for spec in specs:
            lines.append(f"## {spec.spec_id}")
            lines.append(f"- [ ] Implement {spec.prd_clause} core behavior")
            if spec.algorithm:
                lines.append("- [ ] Implement algorithm section")
            if spec.data_structure:
                lines.append("- [ ] Implement data structure section")
            if spec.api_contract:
                lines.append("- [ ] Implement API contract section")
            lines.append("- [ ] Implement acceptance tests")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def _render_acceptance(self, specs: list[Spec]) -> str:
        lines: list[str] = ["# ACCEPTANCE", ""]
        for spec in specs:
            lines.append(f"## {spec.spec_id}")
            for case in spec.acceptance_tests:
                lines.extend(self._acceptance_case_lines(case))
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def _acceptance_case_lines(self, case: dict[str, object]) -> list[str]:
        test_name = str(case.get("test_name") or "unnamed_test")
        test_target = str(case.get("test_target") or "")
        lines = [f"- [ ] `{test_name}`: {test_target}"]
        assertions = case.get("assertions", [])
        if isinstance(assertions, list):
            for assertion in assertions:
                if isinstance(assertion, dict):
                    lines.append(self._format_assertion_line(assertion))
        return lines

    def _format_assertion_line(self, assertion: dict[str, object]) -> str:
        assertion_type = str(assertion.get("type") or "unknown")
        field = str(assertion.get("field") or "")
        return f"- assertion: {assertion_type} {field}".rstrip()
