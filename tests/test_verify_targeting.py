from pathlib import Path

from kodawari.autopilot.verify_targeting import resolve_verify_targeting


def test_resolve_verify_targeting_narrows_changed_test_file_with_feature_keyword() -> None:
    project_root = Path("E:/code_rebuild/watercare-app")

    payload = resolve_verify_targeting(
        project_root=project_root,
        verify_cmd="pytest -q",
        changed_files=["tests/test_api.py"],
        feature="medication-adherence-summary",
        task_label="T1: Prepare schema contract",
    )

    assert payload["verify_target_source"] == "task_keyword_match"
    assert payload["verify_targets"] == ["tests/test_api.py"]
    assert payload["verify_keyword_source"] == "feature_slug"
    assert payload["verify_keyword_expression"] == "medication_adherence_summary"
    # Keyword is quoted so pytest sees it as a single -k expression argument
    # even for single-word keywords (defensive: cheap, doesn't break pytest).
    assert payload["verify_cmd_resolved"] == 'pytest -q tests/test_api.py -k "medication_adherence_summary"'


def test_resolve_verify_targeting_falls_back_to_changed_test_file_when_no_keyword_match(tmp_path: Path) -> None:
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    (tests_dir / "test_api.py").write_text(
        "\n".join(
            [
                "def test_hydration_history_returns_recent_days_and_streak():",
                "    assert True",
                "",
                "def test_dashboard_includes_hydration_history_and_goal_streak():",
                "    assert True",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    payload = resolve_verify_targeting(
        project_root=tmp_path,
        verify_cmd="pytest -q",
        changed_files=["tests/test_api.py"],
        feature="medication-adherence-summary",
        task_label="T1: Prepare schema contract",
    )

    assert payload["verify_target_source"] == "changed_test_files"
    assert payload["verify_targets"] == ["tests/test_api.py"]
    assert payload["verify_cmd_resolved"] == "pytest -q tests/test_api.py"


def test_resolve_verify_targeting_derives_test_target_from_source_change(tmp_path: Path) -> None:
    src_dir = tmp_path / "src"
    tests_dir = tmp_path / "tests"
    src_dir.mkdir(parents=True, exist_ok=True)
    tests_dir.mkdir(parents=True, exist_ok=True)
    (src_dir / "sample.py").write_text("def sample() -> int:\n    return 1\n", encoding="utf-8")
    (tests_dir / "test_sample.py").write_text("def test_sample() -> None:\n    assert True\n", encoding="utf-8")

    payload = resolve_verify_targeting(
        project_root=tmp_path,
        verify_cmd="pytest -q",
        changed_files=["src/sample.py"],
        feature="verify-runtime",
        task_label="T10: tighten verify scoping",
    )

    assert payload["verify_target_source"] == "derived_test_files"
    assert payload["verify_targets"] == ["tests/test_sample.py"]
    assert payload["verify_cmd_resolved"] == "pytest -q tests/test_sample.py"


def test_keyword_expression_with_and_or_operators_is_quoted() -> None:
    """Regression: task labels like 'clamp_percentage_0_100' can derive -k
    expressions with words that include 'and'/'or', which shell splits unless
    the whole expression is a single quoted argument.

    Before fix: `pytest -q f.py -k clamp and percentage` → shell parses 'and'
    and 'percentage' as positional file arguments → pytest reports
    'ERROR: file or directory not found: and'.

    After fix: the -k value is wrapped in double quotes so pytest receives
    the full boolean expression as one argument.
    """
    from pathlib import Path
    from kodawari.autopilot.verify_targeting import _scoped_pytest_cmd
    cmd = _scoped_pytest_cmd(
        ["tests/test_foo.py"], keyword="clamp and percentage and 100"
    )
    # Must be a single quoted argument, not four raw tokens
    assert '-k "clamp and percentage and 100"' in cmd
    assert cmd.count('"') == 2


def test_keyword_expression_with_embedded_quote_is_escaped() -> None:
    from kodawari.autopilot.verify_targeting import _scoped_pytest_cmd
    cmd = _scoped_pytest_cmd(
        ["tests/test_foo.py"], keyword='weird"name'
    )
    # Embedded double quote must be escaped
    assert '-k "weird\\"name"' in cmd


def test_empty_keyword_produces_no_dash_k() -> None:
    from kodawari.autopilot.verify_targeting import _scoped_pytest_cmd
    cmd = _scoped_pytest_cmd(["tests/test_foo.py"], keyword="")
    assert "-k" not in cmd
