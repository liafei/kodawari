from pathlib import Path

from kodawari.spec_generator import ClauseAnalyzer, PRDParser, SpecGenerator, SpecValidator


def test_parser_extracts_clauses_and_p0(tmp_path: Path) -> None:
    prd = tmp_path / "PRD.md"
    prd.write_text(
        "\n".join(
            [
                "## 5 Core Requirements",
                "### F1 Daily API P0",
                "Need API endpoint and data structure.",
                "### F2 UI P1",
                "Need card page rendering.",
            ]
        ),
        encoding="utf-8",
    )
    parser = PRDParser()
    parsed = parser.parse_prd(str(prd))
    assert len(parsed.clauses) == 2
    assert parsed.clauses[0].id == "F1"
    p0 = parser.extract_p0_clauses(parsed)
    assert len(p0) == 1
    assert p0[0].id == "F1"


def test_analyzer_detects_multiple_sections() -> None:
    from kodawari.spec_generator.models import Clause

    clause = Clause(
        id="F7",
        title="F7 API + schema + ranking algorithm P0",
        content="Design API endpoint, table fields, and ranking algorithm for recommendation.",
        epic="Hot",
        priority="P0",
    )
    flags = ClauseAnalyzer().detect_sections(clause)
    assert flags.has_api_contract is True
    assert flags.has_data_structure is True
    assert flags.has_algorithm is True
    assert "api" in flags.spec_types()


def test_generator_and_validator_chain() -> None:
    from kodawari.spec_generator.models import Clause

    clause = Clause(
        id="F9",
        title="F9 Top list API",
        content="Provide endpoint with response fields and ranking score algorithm.",
        epic="Hot",
        priority="P0",
    )
    generator = SpecGenerator()
    spec = generator.generate_spec(clause, prd_doc_slug="prd2")
    assert spec.spec_id
    assert spec.acceptance_tests
    result = SpecValidator().validate_spec(spec)
    assert result.valid is True
