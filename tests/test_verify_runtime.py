from pathlib import Path

from kodawari.autopilot.engine import AutopilotConfig, AutopilotEngine
from kodawari.instincts import learn_from_globs


class _ExistingFilesAdapter:
    def implement(self, task: str, context: dict[str, object]) -> dict[str, object]:
        del task, context
        return {
            "status": "done",
            "changes": ["src/sample.py", "tests/test_sample.py"],
        }


class _SourceOnlyAdapter:
    def implement(self, task: str, context: dict[str, object]) -> dict[str, object]:
        del task, context
        return {
            "status": "done",
            "changes": ["src/sample.py"],
        }


class _FeatureKeywordAdapter:
    def implement(self, task: str, context: dict[str, object]) -> dict[str, object]:
        del task, context
        return {
            "status": "done",
            "changes": ["app/schemas.py", "tests/test_api.py"],
        }


def _write_verify_fixture_files(tmp_path: Path) -> None:
    src_file = tmp_path / "src" / "sample.py"
    test_file = tmp_path / "tests" / "test_sample.py"
    src_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.parent.mkdir(parents=True, exist_ok=True)
    src_file.write_text("def sample(value):\n    return value\n", encoding="utf-8")
    test_file.write_text("def test_sample():\n    assert True\n", encoding="utf-8")


def _write_instinct_verify_fixture_files(tmp_path: Path) -> None:
    src_file = tmp_path / "src" / "sample.py"
    test_file = tmp_path / "tests" / "test_instinct_scope.py"
    src_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.parent.mkdir(parents=True, exist_ok=True)
    src_file.write_text("def sample(value):\n    return value\n", encoding="utf-8")
    test_file.write_text("def test_instinct_scope():\n    assert True\n", encoding="utf-8")


def _write_feature_keyword_fixture_files(tmp_path: Path) -> None:
    src_file = tmp_path / "app" / "schemas.py"
    test_file = tmp_path / "tests" / "test_api.py"
    src_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.parent.mkdir(parents=True, exist_ok=True)
    src_file.write_text("class MedicationAdherenceSummary: ...\n", encoding="utf-8")
    test_file.write_text(
        "\n".join(
            [
                "def test_medication_adherence_summary_schema_contract():",
                "    assert True",
                "",
                "def test_medication_adherence_summary_schema_defaults():",
                "    assert True",
                "",
                "def test_hydration_unrelated_case():",
                "    assert False",
                "",
            ]
        ),
        encoding="utf-8",
    )


def test_engine_verify_can_execute_explicit_command(tmp_path: Path) -> None:
    _write_verify_fixture_files(tmp_path)
    config = AutopilotConfig(
        project_root=tmp_path,
        feature="verify-runtime",
        verify_cmd='python -c "print(\'verify ok\')"',
        max_cycles=8,
    )
    engine = AutopilotEngine(config, adapter=_ExistingFilesAdapter())

    result = engine.run_collaboration_loop(
        task_label="T001: execute explicit verify",
        task_scope="verify runtime command path",
    )

    assert result["reason"] == "PROCEED_TO_GATE"
    assert result["verify_check"]["source"] == "verify_command"
    assert result["verify_check"]["mode"] == "command"
    assert result["verify_check"]["command_executed"] is True
    assert result["verify_check"]["returncode"] == 0
    assert "verify ok" in result["verify_check"]["summary"]
    assert result["runtime_semantics"]["verify"]["command_executed"] is True


def test_engine_verify_blocks_when_explicit_command_fails(tmp_path: Path) -> None:
    _write_verify_fixture_files(tmp_path)
    config = AutopilotConfig(
        project_root=tmp_path,
        feature="verify-runtime",
        verify_cmd='python -c "import sys; print(\'verify fail\'); sys.exit(2)"',
        max_cycles=8,
    )
    engine = AutopilotEngine(config, adapter=_ExistingFilesAdapter())

    result = engine.run_collaboration_loop(
        task_label="T002: fail explicit verify",
        task_scope="verify runtime failure path",
    )

    assert result["reason"] == "VERIFY_BLOCKED"
    assert result["verify_check"]["source"] == "verify_command"
    assert result["verify_check"]["mode"] == "command"
    assert result["verify_check"]["command_executed"] is True
    assert result["verify_check"]["returncode"] == 2
    assert result["loop_outcome"]["blocked"] is True
    assert "verify fail" in result["verify_check"]["summary"]
    semantic_runtime = dict(result.get("semantic_compact_runtime") or {})
    assert semantic_runtime["mode"] == "incremental"
    assert semantic_runtime["trigger_event"] == "verify_blocked"


def test_engine_verify_scopes_to_changed_test_targets_by_default(tmp_path: Path) -> None:
    _write_verify_fixture_files(tmp_path)
    config = AutopilotConfig(
        project_root=tmp_path,
        feature="verify-runtime",
        max_cycles=8,
    )
    engine = AutopilotEngine(config, adapter=_ExistingFilesAdapter())

    result = engine.run_collaboration_loop(
        task_label="T003: scope verify by changed tests",
        task_scope="verify scoped target resolution",
    )

    assert result["reason"] == "PROCEED_TO_GATE"
    verify_check = result["verify_check"]
    assert verify_check["verify_cmd"] == "pytest -q"
    assert "tests/test_sample.py" in verify_check["verify_cmd_resolved"]
    assert verify_check["verify_target_source"] == "changed_test_files"
    assert verify_check["verify_targets"] == ["tests/test_sample.py"]
    assert verify_check["command_executed"] is True
    assert result["runtime_semantics"]["verify"]["target_source"] == "changed_test_files"
    assert result["runtime_semantics"]["verify"]["targets"] == ["tests/test_sample.py"]


def test_engine_verify_derives_scoped_tests_from_source_only_changes(tmp_path: Path) -> None:
    _write_verify_fixture_files(tmp_path)
    config = AutopilotConfig(
        project_root=tmp_path,
        feature="verify-runtime-derived",
        max_cycles=8,
    )
    engine = AutopilotEngine(config, adapter=_SourceOnlyAdapter())

    result = engine.run_collaboration_loop(
        task_label="T003b: derive verify targets from source-only delta",
        task_scope="verify source to test mapping",
        enable_peer_review=False,
    )

    assert result["reason"] == "PROCEED_TO_GATE"
    verify_check = result["verify_check"]
    assert verify_check["verify_cmd"] == "pytest -q"
    assert verify_check["verify_cmd_resolved"] == "pytest -q tests/test_sample.py"
    assert verify_check["verify_target_source"] == "derived_test_files"
    assert verify_check["verify_targets"] == ["tests/test_sample.py"]
    assert verify_check["command_executed"] is True
    assert verify_check["returncode"] == 0
    assert result["runtime_semantics"]["verify"]["target_source"] == "derived_test_files"
    assert result["runtime_semantics"]["verify"]["targets"] == ["tests/test_sample.py"]


def test_engine_verify_can_use_instinct_hints_for_scoped_targets(tmp_path: Path) -> None:
    _write_instinct_verify_fixture_files(tmp_path)
    learn_from_globs(tmp_path, ["tests/test_instinct_scope.py"])
    config = AutopilotConfig(
        project_root=tmp_path,
        feature="verify-runtime-instinct",
        max_cycles=8,
    )
    engine = AutopilotEngine(config, adapter=_SourceOnlyAdapter())

    result = engine.run_collaboration_loop(
        task_label="T004: scope verify via instinct hints",
        task_scope="instinct hints should provide test target",
        enable_peer_review=False,
    )

    assert result["reason"] == "PROCEED_TO_GATE"
    verify_check = result["verify_check"]
    assert verify_check["verify_cmd"] == "pytest -q"
    assert "tests/test_instinct_scope.py" in verify_check["verify_cmd_resolved"]
    assert verify_check["verify_target_source"] == "instinct_hints"
    assert verify_check["verify_targets"] == ["tests/test_instinct_scope.py"]
    assert "instinct hints" in verify_check["instinct_reason"].lower()
    assert "tests/test_instinct_scope.py" in verify_check["instinct_patterns"]
    assert verify_check["command_executed"] is True
    assert result["runtime_semantics"]["verify"]["target_source"] == "instinct_hints"
    assert result["runtime_semantics"]["verify"]["targets"] == ["tests/test_instinct_scope.py"]
    assert "instinct hints" in result["runtime_semantics"]["verify"]["instinct_reason"].lower()


def test_engine_verify_scopes_to_feature_keyword_within_changed_test_file(tmp_path: Path) -> None:
    _write_feature_keyword_fixture_files(tmp_path)
    config = AutopilotConfig(
        project_root=tmp_path,
        feature="medication-adherence-summary",
        max_cycles=8,
    )
    engine = AutopilotEngine(config, adapter=_FeatureKeywordAdapter())

    result = engine.run_collaboration_loop(
        task_label="T1: Prepare schema contract",
        task_scope="schema contract verify should stay task-scoped",
        enable_peer_review=False,
    )

    assert result["reason"] == "PROCEED_TO_GATE"
    verify_check = result["verify_check"]
    assert verify_check["verify_target_source"] == "task_keyword_match"
    assert verify_check["verify_targets"] == ["tests/test_api.py"]
    assert verify_check["verify_keyword_expression"] == "medication_adherence_summary"
    assert verify_check["verify_keyword_source"] == "feature_slug"
    assert verify_check["verify_keyword_match_count"] == 2
    assert verify_check["command_executed"] is True
    assert verify_check["returncode"] == 0
    assert '-k "medication_adherence_summary"' in verify_check["verify_cmd_resolved"]


def test_engine_verify_fail_injects_fix_round_then_exhausts_budget(tmp_path: Path) -> None:
    """Verify failure without rollback triggers fix_round retries up to max_verify_retries,
    then stops with VERIFY_BLOCKED once the budget is exhausted."""
    _write_verify_fixture_files(tmp_path)
    config = AutopilotConfig(
        project_root=tmp_path,
        feature="verify-fail-fix",
        verify_cmd='python -c "import sys; print(\'verify fail\'); sys.exit(2)"',
        max_cycles=8,
        max_verify_retries=1,  # allow 1 retry → 2 total verify failures before stop
    )
    engine = AutopilotEngine(config, adapter=_ExistingFilesAdapter())

    result = engine.run_collaboration_loop(
        task_label="T: verify fail fix round retry",
        task_scope="verify retry path",
    )

    # After exhausting retries the loop must still terminate with VERIFY_BLOCKED
    assert result["reason"] == "VERIFY_BLOCKED"
    assert result["loop_outcome"]["blocked"] is True
    # The engine must have attempted at least 2 rounds (implement + at least 1 fix_round)
    rounds = result.get("rounds") or []
    actions = [r.get("action") for r in rounds]
    assert "implement" in actions, "expected an implement round"
    assert "fix_round" in actions, "expected a fix_round retry after first verify failure"
