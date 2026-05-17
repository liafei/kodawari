from kodawari.spec_generator import (
    Clause,
    CoverageMatrix,
    CoverageMatrixItem,
    Spec,
    ValidationMessage,
    ValidationResult,
)


def test_models_can_serialize_to_dict() -> None:
    clause = Clause(
        id="5.1.F1",
        title="Daily Edition",
        content="Generate a fixed daily edition.",
        epic="Daily",
        priority="P0",
        spec_types=["algorithm", "api"],
        acceptance_criteria=["returns ten items"],
    )
    spec = Spec(
        spec_id="daily.prd2.5_1_f1.algorithm_api",
        spec_version="1.0",
        prd_clause="5.1 F1",
        epic="Daily",
        priority="P0",
        spec_types=["algorithm", "api"],
    )
    matrix = CoverageMatrix(
        items=[
            CoverageMatrixItem(
                prd_clause="5.1 F1",
                epic="Daily",
                priority="P0",
                status="PASS",
                spec_id=spec.spec_id,
                test_ids=["test_daily_feed_smoke"],
            )
        ]
    )
    result = ValidationResult(
        valid=True,
        warnings=[ValidationMessage(level="warning", message="example")],
    )

    assert clause.to_dict()["id"] == "5.1.F1"
    assert spec.to_dict()["spec_id"] == "daily.prd2.5_1_f1.algorithm_api"
    assert matrix.to_dict()["items"][0]["status"] == "PASS"
    assert result.to_dict()["warnings"][0]["message"] == "example"
