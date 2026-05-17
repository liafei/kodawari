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


def test_materializer_writes_three_docs(tmp_path: Path) -> None:
    materializer = SpecMaterializer()
    output = materializer.materialize([_demo_spec("daily.prd.f1.api.00000001")], str(tmp_path))

    assert Path(output["plan"]).exists()
    assert Path(output["tasks"]).exists()
    assert Path(output["acceptance"]).exists()
    assert "SPEC: daily.prd.f1.api.00000001" in (tmp_path / "PLAN.md").read_text(encoding="utf-8")


def test_materializer_is_deterministic(tmp_path: Path) -> None:
    materializer = SpecMaterializer()
    specs = [_demo_spec("daily.prd.f1.api.00000001"), _demo_spec("daily.prd.f2.api.00000002")]
    first = materializer.materialize(specs, str(tmp_path))
    first_plan = Path(first["plan"]).read_text(encoding="utf-8")
    first_tasks = Path(first["tasks"]).read_text(encoding="utf-8")
    first_acceptance = Path(first["acceptance"]).read_text(encoding="utf-8")

    second = materializer.materialize(specs, str(tmp_path))
    second_plan = Path(second["plan"]).read_text(encoding="utf-8")
    second_tasks = Path(second["tasks"]).read_text(encoding="utf-8")
    second_acceptance = Path(second["acceptance"]).read_text(encoding="utf-8")

    assert first_plan == second_plan
    assert first_tasks == second_tasks
    assert first_acceptance == second_acceptance


def test_planning_summary_helper_matches_historical_contract() -> None:
    payload = summarize_plan(
        "ws113-demo",
        sections=["PLAN.md", "TASKS.md", "ACCEPTANCE.md"],
        caller="test-suite",
        contract_version="ws114.v2",
    )
    assert payload == {
        "feature": "ws113-demo",
        "sections": ["PLAN.md", "TASKS.md", "ACCEPTANCE.md"],
        "section_count": 3,
        "options": {
            "caller": "test-suite",
            "contract_version": "ws114.v2",
        },
    }


def test_materializer_summary_helper_requires_explicit_sections(tmp_path: Path) -> None:
    materializer = SpecMaterializer()
    output = materializer.materialize([_demo_spec("daily.prd.f1.api.00000001")], str(tmp_path))
    assert Path(output["plan"]).exists()
    assert Path(output["tasks"]).exists()
    assert Path(output["acceptance"]).exists()

    summary = materializer.summarize_plan("ws113-demo")
    assert summary["feature"] == "ws113-demo"
    assert summary["sections"] == []
    assert summary["section_count"] == 0


def test_planning_summary_helper_normalizes_sections_and_options_copy() -> None:
    payload = summarize_plan(
        "ws113-helper",
        sections=["PLAN.md", Path("TASKS.md")],
        source="contract-bridge",
    )
    assert payload["feature"] == "ws113-helper"
    assert payload["sections"] == ["PLAN.md", "TASKS.md"]
    assert payload["section_count"] == 2
    assert payload["options"] == {"source": "contract-bridge"}
