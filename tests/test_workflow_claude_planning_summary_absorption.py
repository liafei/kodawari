from __future__ import annotations

from pathlib import Path

from kodawari.spec_generator.materializer import SpecMaterializer, summarize_plan
from kodawari.spec_generator.models import Spec


def _demo_spec(spec_id: str) -> Spec:
    return Spec(
        spec_id=spec_id,
        spec_version="1.0",
        prd_clause="F1",
        epic="Daily Edition",
        priority="P0",
        spec_types=["algorithm", "api"],
        algorithm=[
            {
                "name": "baseline",
                "implementation": "heuristic",
                "parameters": {},
                "rationale": "deterministic",
            }
        ],
        api_contract=[
            {
                "endpoint": "GET /api/v1/daily",
                "request": {"query_params": {}},
                "response": {"status": "OK"},
                "error_cases": [],
            }
        ],
        acceptance_tests=[
            {
                "test_name": "test_daily_ok",
                "test_target": "daily endpoint returns OK",
                "assertions": [{"type": "equals", "field": "status", "value": "OK"}],
            }
        ],
    )


def test_planning_summary_absorption_matches_historical_minimal_contract() -> None:
    payload = summarize_plan(
        "ws113-absorption",
        sections=["PLAN.md", "TASKS.md"],
        source="focused-absorption-test",
    )
    assert payload == {
        "feature": "ws113-absorption",
        "sections": ["PLAN.md", "TASKS.md"],
        "section_count": 2,
        "options": {
            "source": "focused-absorption-test",
        },
    }


def test_planning_summary_absorption_does_not_infer_artifacts_from_materialize(tmp_path: Path) -> None:
    materializer = SpecMaterializer()
    materializer.materialize([_demo_spec("daily.prd.f1.api.00000001")], str(tmp_path))

    assert (tmp_path / "PLAN.md").exists()
    assert (tmp_path / "TASKS.md").exists()
    assert (tmp_path / "ACCEPTANCE.md").exists()

    summary = materializer.summarize_plan("ws113-absorption")
    assert summary == {
        "feature": "ws113-absorption",
        "sections": [],
        "section_count": 0,
        "options": {},
    }


def test_planning_summary_absorption_accepts_explicit_artifact_sections(tmp_path: Path) -> None:
    materializer = SpecMaterializer()
    materializer.materialize([_demo_spec("daily.prd.f1.api.00000001")], str(tmp_path))

    sections = [
        path.name
        for path in sorted(tmp_path.glob("*.md"))
        if path.name in {"PLAN.md", "TASKS.md", "ACCEPTANCE.md"}
    ]
    summary = materializer.summarize_plan(
        "ws113-absorption",
        sections=sections,
        source="materialized-artifacts",
    )

    assert sections == ["ACCEPTANCE.md", "PLAN.md", "TASKS.md"]
    assert summary["feature"] == "ws113-absorption"
    assert summary["sections"] == sections
    assert summary["section_count"] == 3
    assert summary["options"] == {"source": "materialized-artifacts"}
