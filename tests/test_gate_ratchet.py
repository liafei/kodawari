import json
from pathlib import Path

from kodawari.gate.gate_ratchet import compare_against_baseline, update_baseline_snapshot


def test_compare_against_baseline_detects_regression_and_skips_missing_metrics() -> None:
    current = {"metrics": {"files_over_1000_lines": 1, "functions_over_50_lines": 2, "total_duplicate_blocks": None}}
    baseline = {"metrics": {"files_over_1000_lines": 0, "functions_over_50_lines": 2, "total_duplicate_blocks": None}}

    result = compare_against_baseline(current, baseline).to_dict()

    assert result["status"] == "FAIL"
    assert result["regression_count"] == 1
    assert result["regressions"][0]["metric"] == "files_over_1000_lines"
    assert any(item["metric"] == "total_duplicate_blocks" for item in result["skipped_metrics"])


def test_update_baseline_snapshot_only_lowers_metrics() -> None:
    current = {
        "generated_at": "2026-04-02T00:00:00Z",
        "source_commit": "new",
        "metrics": {"files_over_1000_lines": 0, "functions_over_50_lines": 1},
    }
    baseline = {
        "generated_at": "2026-04-01T00:00:00Z",
        "source_commit": "old",
        "metrics": {"files_over_1000_lines": 1, "functions_over_50_lines": 1},
    }

    updated, changes = update_baseline_snapshot(current, baseline)

    assert updated["metrics"]["files_over_1000_lines"] == 0
    assert updated["metrics"]["functions_over_50_lines"] == 1
    assert changes == [{"metric": "files_over_1000_lines", "old": 1.0, "new": 0.0}]


def test_update_code_health_baseline_script_updates_file(tmp_path: Path) -> None:
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(
        json.dumps({"metrics": {"files_over_1000_lines": 2, "functions_over_50_lines": 4}}, ensure_ascii=False),
        encoding="utf-8",
    )
    current_path = tmp_path / "current.json"
    current_path.write_text(
        json.dumps({"metrics": {"files_over_1000_lines": 1, "functions_over_50_lines": 5}}, ensure_ascii=False),
        encoding="utf-8",
    )

    from importlib.util import module_from_spec, spec_from_file_location

    script_path = Path(__file__).resolve().parents[1] / "scripts" / "update_code_health_baseline.py"
    spec = spec_from_file_location("update_code_health_baseline_script", script_path)
    module = module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    parser = module.build_parser()
    args = parser.parse_args(["--baseline", str(baseline_path), "--current", str(current_path)])

    rc = module.run(args)
    updated = json.loads(baseline_path.read_text(encoding="utf-8"))

    assert rc == 0
    assert updated["metrics"]["files_over_1000_lines"] == 1
    assert updated["metrics"]["functions_over_50_lines"] == 4
