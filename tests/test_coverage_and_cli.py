from pathlib import Path
import json
import sys

from kodawari.spec_generator import CoverageGenerator, PRDParser, SpecGenerator
from kodawari.cli.spec import main as cli_main


def _run_spec_cli(monkeypatch, argv: list[str]) -> int:
    monkeypatch.setattr(sys, "argv", argv)
    return cli_main()


def _run_spec_pipeline(monkeypatch, *, prd_file: Path, spec_dir: Path, report_path: Path, coverage_path: Path) -> None:
    assert _run_spec_cli(
        monkeypatch,
        [
            "kodawari",
            "generate",
            "--prd",
            str(prd_file),
            "--output",
            str(spec_dir),
            "--priority",
            "P0",
        ],
    ) == 0
    assert _run_spec_cli(
        monkeypatch,
        [
            "kodawari",
            "validate",
            "--spec-dir",
            str(spec_dir),
            "--report",
            str(report_path),
        ],
    ) == 0
    assert _run_spec_cli(
        monkeypatch,
        [
            "kodawari",
            "coverage",
            "--prd",
            str(prd_file),
            "--spec-dir",
            str(spec_dir),
            "--output",
            str(coverage_path),
            "--format",
            "json",
        ],
    ) == 0


def _materialize_specs(monkeypatch, *, spec_dir: Path, materialize_dir: Path) -> None:
    assert _run_spec_cli(
        monkeypatch,
        [
            "kodawari",
            "materialize",
            "--spec-dir",
            str(spec_dir),
            "--output",
            str(materialize_dir),
        ],
    ) == 0


def test_coverage_generator_exports_markdown_and_json(tmp_path: Path) -> None:
    prd_file = tmp_path / "prd.md"
    prd_file.write_text(
        "\n".join(
            [
                "## 5 Core",
                "### F1 API contract P0",
                "Need endpoint request response",
            ]
        ),
        encoding="utf-8",
    )
    parser = PRDParser()
    prd = parser.parse_prd(str(prd_file))
    clauses = parser.extract_p0_clauses(prd)
    spec = SpecGenerator().generate_spec(clauses[0], prd_doc_slug="prd")
    matrix = CoverageGenerator().generate_matrix(clauses, [spec])

    json_out = tmp_path / "coverage.json"
    md_out = tmp_path / "coverage.md"
    CoverageGenerator().export_json(matrix, str(json_out))
    CoverageGenerator().export_markdown(matrix, str(md_out))

    assert json.loads(json_out.read_text(encoding="utf-8"))["items"][0]["status"] == "PASS"
    assert "| F1 |" in md_out.read_text(encoding="utf-8")


def test_cli_generate_validate_coverage(tmp_path: Path, monkeypatch) -> None:
    prd_file = tmp_path / "prd.md"
    prd_file.write_text(
        "\n".join(
            [
                "## 5 Core",
                "### F1 API P0",
                "Need API endpoint request response",
            ]
        ),
        encoding="utf-8",
    )
    spec_dir = tmp_path / "specs"
    report_path = tmp_path / "validation.json"
    coverage_path = tmp_path / "coverage.json"

    _run_spec_pipeline(
        monkeypatch,
        prd_file=prd_file,
        spec_dir=spec_dir,
        report_path=report_path,
        coverage_path=coverage_path,
    )
    payload = json.loads(coverage_path.read_text(encoding="utf-8"))
    assert payload["items"][0]["status"] in {"PASS", "PARTIAL", "FAIL"}

    materialize_dir = tmp_path / "materialized"
    _materialize_specs(monkeypatch, spec_dir=spec_dir, materialize_dir=materialize_dir)
    assert (materialize_dir / "PLAN.md").exists()
    assert (materialize_dir / "TASKS.md").exists()
    assert (materialize_dir / "ACCEPTANCE.md").exists()
