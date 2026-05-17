from __future__ import annotations

import json
from pathlib import Path


def test_lane_recipe_manifest_matches_script_entrypoints() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    recipe_path = repo_root / "scripts" / "test_lane_recipes.json"
    invoke_script = repo_root / "scripts" / "invoke_test_lane.ps1"
    always_on_script = repo_root / "scripts" / "run_always_on_lane.ps1"
    integration_script = repo_root / "scripts" / "run_integration_lane.ps1"
    stability_script = repo_root / "scripts" / "run_lane_stability.ps1"
    always_on_repeat_script = repo_root / "scripts" / "run_always_on_lane_repeat.ps1"
    integration_repeat_script = repo_root / "scripts" / "run_integration_lane_repeat.ps1"

    assert recipe_path.exists()
    assert invoke_script.exists()
    assert always_on_script.exists()
    assert integration_script.exists()
    assert stability_script.exists()
    assert always_on_repeat_script.exists()
    assert integration_repeat_script.exists()

    recipes = json.loads(recipe_path.read_text(encoding="utf-8"))
    assert set(recipes) == {
        "always-on",
        "integration",
        "real-review-success",
        "real-review-fail-closed",
        "models-v2-workall-real",
    }

    always_on = recipes["always-on"]
    integration = recipes["integration"]

    assert always_on["default_pytest_args"] == ["-q"]
    assert integration["default_pytest_args"] == ["-q"]
    assert integration["skip_if_env_missing"] == ["WORKFLOW_REVIEWER_API_KEY", "WORKFLOW_REVIEWER_BASE_URL"]
    assert recipes["real-review-success"]["pytest_targets"] == ["tests/test_generic_runtime_real_review_success.py"]
    assert recipes["real-review-fail-closed"]["pytest_targets"] == ["tests/test_generic_runtime_real_review.py"]
    assert "tests/test_model_config.py" in recipes["models-v2-workall-real"]["pytest_targets"]
    assert "tests/test_execution_openai_tool_use.py" in recipes["models-v2-workall-real"]["pytest_targets"]
    assert "tests/test_newsapp_benchmark_proof.py" not in always_on["pytest_targets"]
    assert "tests/test_newsapp_benchmark_real_review.py" not in integration["pytest_targets"]
    assert "tests/test_generic_runtime_proof.py" in always_on["pytest_targets"]
    assert "tests/test_generic_runtime_real_review.py" in integration["pytest_targets"]

    for lane_name, payload in recipes.items():
        assert payload["summary"]
        assert payload["pytest_targets"]
        for rel_path in payload["pytest_targets"]:
            assert (repo_root / rel_path).exists(), f"{lane_name} target missing: {rel_path}"

    invoke_text = invoke_script.read_text(encoding="utf-8")
    assert 'ValidateSet("always-on", "integration", "real-review-success", "real-review-fail-closed", "models-v2-workall-real")' in invoke_text
    assert "test_lane_recipes.json" in invoke_text
    assert "ConvertFrom-Json" in invoke_text
    assert '-m", "pytest"' in invoke_text
    assert ".workflow_runtime\\local-env\\.venv\\Scripts\\python.exe" in invoke_text
    assert "SKIP" in invoke_text
    assert "FailIfSkipped" in invoke_text
    assert "ResultPath" in invoke_text
    assert "Write-LaneResult" in invoke_text

    always_on_text = always_on_script.read_text(encoding="utf-8")
    assert 'invoke_test_lane.ps1' in always_on_text
    assert '-Lane "always-on"' in always_on_text
    assert "PytestArgs" in always_on_text
    assert "ListOnly" in always_on_text

    integration_text = integration_script.read_text(encoding="utf-8")
    assert 'invoke_test_lane.ps1' in integration_text
    assert '-Lane "integration"' in integration_text
    assert "PytestArgs" in integration_text
    assert "ListOnly" in integration_text
    assert "FailIfSkipped" in integration_text

    stability_text = stability_script.read_text(encoding="utf-8")
    assert 'ValidateSet("always-on", "integration", "real-review-success", "real-review-fail-closed", "models-v2-workall-real")' in stability_text
    assert "Repeat" in stability_text
    assert "SummaryPath" in stability_text
    assert "skipped_runs" in stability_text
    assert 'schema_version = "lane.stability.v1"' in stability_text
    assert 'summary_version = "lane.stability.v1"' in stability_text
    assert 'schema_version = "lane.triage.v1"' in stability_text
    assert 'triage_version = "lane.triage.v1"' in stability_text
    assert "lane_triage_" in stability_text
    assert "New-LaneTriagePayload" in stability_text
    assert "run_lane_stability.ps1" in always_on_repeat_script.read_text(encoding="utf-8")
    assert '-Lane "always-on"' in always_on_repeat_script.read_text(encoding="utf-8")
    assert "run_lane_stability.ps1" in integration_repeat_script.read_text(encoding="utf-8")
    assert '-Lane "integration"' in integration_repeat_script.read_text(encoding="utf-8")
    assert "FailIfSkipped" in integration_repeat_script.read_text(encoding="utf-8")


def test_operator_docs_pin_lane_wrappers_and_env_contract() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    entry_doc = (repo_root / "项目说明.md").read_text(encoding="utf-8")
    arch_doc = (repo_root / "docs" / "architecture" / "一、平台现状、架构与兼容总览.md").read_text(encoding="utf-8")
    ops_doc = (repo_root / "docs" / "operations" / "二、运行操作、门禁规则与后续路线.md").read_text(encoding="utf-8")

    assert "scripts\\run_always_on_lane.ps1" in entry_doc
    assert "scripts\\run_integration_lane.ps1" in entry_doc
    assert "scripts/test_lane_recipes.json" in entry_doc
    assert "kodawari lane-history-fetch" in entry_doc

    assert ".\\scripts\\run_always_on_lane.ps1" in ops_doc
    assert ".\\scripts\\run_integration_lane.ps1" in ops_doc
    assert ".\\scripts\\run_always_on_lane.ps1 -ListOnly" in ops_doc
    assert ".\\scripts\\run_integration_lane.ps1 -FailIfSkipped" in ops_doc
    assert ".\\scripts\\run_always_on_lane_repeat.ps1" in ops_doc
    assert ".\\scripts\\run_integration_lane_repeat.ps1" in ops_doc
    assert "lane_stability_always-on.json" in ops_doc
    assert "lane_stability_integration.json" in ops_doc
    assert "lane_triage_always-on.json" in ops_doc
    assert "lane_triage_integration.json" in ops_doc
    assert "lane_triage_always-on.md" in ops_doc
    assert "lane_triage_integration.md" in ops_doc
    assert "lane_history_manifest.json" in ops_doc
    assert "lane_weekly_trend.json" in ops_doc
    assert "lane_weekly_trend.md" in ops_doc
    assert "WORKFLOW_REVIEWER_API_KEY" in ops_doc
    assert "WORKFLOW_REVIEWER_BASE_URL" in ops_doc
    assert "tests/test_generic_runtime_real_review.py" in ops_doc
    assert "tests/test_generic_runtime_proof.py" in ops_doc
    assert "tests/test_generic_runtime_proof.py" in ops_doc
    assert ".github/workflows/kodawari-always-on.yml" in entry_doc
    assert ".github/workflows/kodawari-integration.yml" in entry_doc
    assert ".github/workflows/kodawari-standing-proof.yml" in entry_doc
    assert ".github/workflows/kodawari-standing-proof.yml" in arch_doc
    assert ".github/workflows/kodawari-always-on.yml" in ops_doc
    assert ".github/workflows/kodawari-integration.yml" in ops_doc
    assert ".github/workflows/kodawari-standing-proof.yml" in ops_doc
    assert "actions/upload-artifact@v4" in ops_doc
    assert "GITHUB_STEP_SUMMARY" in ops_doc
    assert "Repeat 1" in ops_doc
    assert "summary_version" in ops_doc
    assert "lane_stability_always-on.json" in entry_doc
    assert "lane_stability_integration.json" in entry_doc
    assert "lane_triage_always-on.json" in entry_doc
    assert "lane_triage_integration.json" in entry_doc
    assert "lane_history_manifest.json" in entry_doc
    assert "lane_weekly_trend.json" in entry_doc
    assert "lane_weekly_trend.md" in entry_doc


def test_github_actions_workflows_pin_lane_entrypoints_and_env_contract() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    always_on_workflow = (repo_root / ".github" / "workflows" / "kodawari-always-on.yml").read_text(encoding="utf-8")
    integration_workflow = (repo_root / ".github" / "workflows" / "kodawari-integration.yml").read_text(encoding="utf-8")
    standing_proof_workflow = (repo_root / ".github" / "workflows" / "kodawari-standing-proof.yml").read_text(encoding="utf-8")

    assert 'uses: actions/checkout@v4' in always_on_workflow
    assert 'uses: actions/setup-python@v5' in always_on_workflow
    assert 'python-version: "3.11"' in always_on_workflow
    assert "inputs:" in always_on_workflow
    assert "schedule:" in always_on_workflow
    assert '.\\scripts\\bootstrap_kodawari.ps1 -SkipPipUpgrade' in always_on_workflow
    assert '.\\scripts\\kodawari.ps1 gate --project-root . --path .\\src --planning-dir .\\planning\\ci_repo_health_src --profile strict --fail-on-block' in always_on_workflow
    assert '.\\scripts\\run_always_on_lane_repeat.ps1 -Repeat $repeat' in always_on_workflow
    assert 'runs-on: windows-latest' in always_on_workflow
    assert 'actions/upload-artifact@v4' in always_on_workflow
    assert 'planning/ci_repo_health_src/.gate_result.json' in always_on_workflow
    assert 'planning/ci_repo_health_src/GATE.md' in always_on_workflow
    assert 'planning/lane_stability_always-on.json' in always_on_workflow
    assert 'planning/lane_triage_always-on.json' in always_on_workflow
    assert 'planning/lane_triage_always-on.md' in always_on_workflow
    assert 'GITHUB_STEP_SUMMARY' in always_on_workflow
    assert "if: always()" in always_on_workflow

    assert 'uses: actions/checkout@v4' in integration_workflow
    assert 'uses: actions/setup-python@v5' in integration_workflow
    assert 'python-version: "3.11"' in integration_workflow
    assert "inputs:" in integration_workflow
    assert "schedule:" in integration_workflow
    assert '.\\scripts\\bootstrap_kodawari.ps1 -SkipPipUpgrade' in integration_workflow
    assert '.\\scripts\\kodawari.ps1 gate --project-root . --path .\\src --planning-dir .\\planning\\ci_repo_health_src --profile strict --fail-on-block' in integration_workflow
    assert '.\\scripts\\run_integration_lane_repeat.ps1 -Repeat $repeat -FailIfSkipped' in integration_workflow
    assert 'runs-on: windows-latest' in integration_workflow
    assert 'actions/upload-artifact@v4' in integration_workflow
    assert 'planning/ci_repo_health_src/.gate_result.json' in integration_workflow
    assert 'planning/ci_repo_health_src/GATE.md' in integration_workflow
    assert 'planning/lane_stability_integration.json' in integration_workflow
    assert 'planning/lane_triage_integration.json' in integration_workflow
    assert 'planning/lane_triage_integration.md' in integration_workflow
    assert 'GITHUB_STEP_SUMMARY' in integration_workflow
    assert 'WORKFLOW_REVIEWER_API_KEY' in integration_workflow
    assert 'WORKFLOW_REVIEWER_BASE_URL' in integration_workflow
    assert 'WORKFLOW_REVIEW_ENABLED: "1"' in integration_workflow
    assert 'WORKFLOW_REVIEW_REQUIRED: "1"' in integration_workflow

    assert 'uses: actions/checkout@v4' in standing_proof_workflow
    assert 'uses: actions/setup-python@v5' in standing_proof_workflow
    assert 'python-version: "3.11"' in standing_proof_workflow
    assert "workflow_dispatch:" in standing_proof_workflow
    assert "schedule:" in standing_proof_workflow
    assert 'actions: read' in standing_proof_workflow
    assert '.\\scripts\\bootstrap_kodawari.ps1 -SkipPipUpgrade' in standing_proof_workflow
    assert '.\\scripts\\kodawari.ps1 lane-history-fetch' in standing_proof_workflow
    assert '.\\scripts\\kodawari.ps1 lane-trend' in standing_proof_workflow
    assert '--artifacts-root .\\planning\\lane_history' in standing_proof_workflow
    assert '--fail-on-empty' in standing_proof_workflow
    assert '--fail-on-block' in standing_proof_workflow
    assert 'planning/lane_history_manifest.json' in standing_proof_workflow
    assert 'planning/lane_weekly_trend.json' in standing_proof_workflow
    assert 'planning/lane_weekly_trend.md' in standing_proof_workflow
    assert 'planning/lane_history' in standing_proof_workflow
    assert 'GITHUB_STEP_SUMMARY' in standing_proof_workflow
