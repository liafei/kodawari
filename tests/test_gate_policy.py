"""Tests for P1 Policy-as-Code: gate/policy_loader.py + GateEngine integration."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from kodawari.gate.policy_loader import (
    GatePolicy,
    ScopeRule,
    load_gate_policy,
    _parse_policy,
    POLICY_SCHEMA_VERSION,
    POLICY_FILENAME,
    WORKFLOW_CONFIG_DIR,
    _DEFAULT_THRESHOLDS,
)
from kodawari.gate.models import GateThresholds, GateStatus
from kodawari.gate.engine import GateEngine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_policy(tmp_path: Path, content: str) -> Path:
    """Write a gate_policy.yaml into the expected config directory."""
    policy_dir = tmp_path / WORKFLOW_CONFIG_DIR
    policy_dir.mkdir(parents=True, exist_ok=True)
    policy_file = policy_dir / POLICY_FILENAME
    policy_file.write_text(textwrap.dedent(content), encoding="utf-8")
    return policy_file


def _make_policy(rules_yaml: str = "", defaults_yaml: str = "") -> GatePolicy:
    """Build a minimal raw dict and parse it."""
    raw: dict = {"schema_version": POLICY_SCHEMA_VERSION}
    if defaults_yaml:
        import yaml
        raw["defaults"] = yaml.safe_load(defaults_yaml)
    if rules_yaml:
        import yaml
        raw["rules"] = yaml.safe_load(rules_yaml)
    return _parse_policy(raw)


# ---------------------------------------------------------------------------
# TestPolicyLoader
# ---------------------------------------------------------------------------

class TestPolicyLoader:

    def test_load_missing_file_returns_none(self, tmp_path: Path) -> None:
        result = load_gate_policy(tmp_path)
        assert result is None

    def test_load_valid_yaml(self, tmp_path: Path) -> None:
        _write_policy(tmp_path, """
            schema_version: "gate.policy.v1"
            defaults:
              file_max_lines: 800
            rules:
              - scope: "backend/**/*.py"
                checks:
                  complexity_max: 4
        """)
        policy = load_gate_policy(tmp_path)
        assert policy is not None
        assert policy.schema_version == "gate.policy.v1"
        assert policy.defaults.get("file_max_lines") == 800
        assert len(policy.rules) == 1
        assert policy.rules[0].scope == "backend/**/*.py"

    def test_scope_rule_matching_glob(self, tmp_path: Path) -> None:
        _write_policy(tmp_path, """
            schema_version: "gate.policy.v1"
            rules:
              - scope: "backend/api/v1/services/auth_*.py"
                checks:
                  complexity_max: 4
        """)
        policy = load_gate_policy(tmp_path)
        assert policy is not None
        assert policy.resolve_for_file("backend/api/v1/services/auth_login.py") is not None
        assert policy.resolve_for_file("backend/api/v1/services/user_login.py") is None

    def test_effective_thresholds_merge_defaults_and_rule(self, tmp_path: Path) -> None:
        _write_policy(tmp_path, """
            schema_version: "gate.policy.v1"
            defaults:
              file_max_lines: 800
            rules:
              - scope: "backend/api/v1/services/auth_*.py"
                checks:
                  complexity_max: 3
                  nesting_max: 2
        """)
        policy = load_gate_policy(tmp_path)
        assert policy is not None

        # File matching the rule gets merged thresholds
        t = policy.effective_thresholds("backend/api/v1/services/auth_login.py")
        assert isinstance(t, GateThresholds)
        assert t.file_max_lines == 800       # from defaults
        assert t.complexity_max == 3         # from rule
        assert t.nesting_max == 2            # from rule
        assert t.function_max_lines == _DEFAULT_THRESHOLDS["function_max_lines"]  # default fallback

        # File NOT matching the rule uses defaults only
        t2 = policy.effective_thresholds("backend/other.py")
        assert t2.file_max_lines == 800
        assert t2.complexity_max == _DEFAULT_THRESHOLDS["complexity_max"]

    def test_bad_yaml_returns_none_not_crash(self, tmp_path: Path) -> None:
        policy_dir = tmp_path / WORKFLOW_CONFIG_DIR
        policy_dir.mkdir(parents=True, exist_ok=True)
        (policy_dir / POLICY_FILENAME).write_text(
            "schema_version: [unclosed\n  bad:\n",
            encoding="utf-8",
        )
        result = load_gate_policy(tmp_path)
        assert result is None

    def test_bad_yaml_logs_warning_and_falls_back(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        policy_dir = tmp_path / WORKFLOW_CONFIG_DIR
        policy_dir.mkdir(parents=True, exist_ok=True)
        (policy_dir / POLICY_FILENAME).write_text(
            "schema_version: [broken\n",
            encoding="utf-8",
        )
        with caplog.at_level("WARNING"):
            result = load_gate_policy(tmp_path)
        assert result is None
        assert any("gate policy: failed to load" in message for message in caplog.messages)

    def test_unknown_schema_version_still_loads(self, tmp_path: Path) -> None:
        _write_policy(tmp_path, """
            schema_version: "gate.policy.v999"
            defaults:
              file_max_lines: 500
        """)
        policy = load_gate_policy(tmp_path)
        assert policy is not None
        assert policy.schema_version == "gate.policy.v999"

    def test_bool_not_treated_as_int(self, tmp_path: Path) -> None:
        """True/False in YAML must NOT be treated as 1/0 for int thresholds."""
        _write_policy(tmp_path, """
            schema_version: "gate.policy.v1"
            defaults:
              file_max_lines: true
              function_max_lines: 60
            rules:
              - scope: "*.py"
                checks:
                  complexity_max: false
                  nesting_max: 3
        """)
        policy = load_gate_policy(tmp_path)
        assert policy is not None

        # defaults: file_max_lines=True (bool) must be ignored, fallback to _DEFAULT_THRESHOLDS
        t = policy.effective_thresholds("something.py")
        assert t.file_max_lines == _DEFAULT_THRESHOLDS["file_max_lines"]  # True must NOT override
        assert t.function_max_lines == 60  # valid int, must be used
        # rule: complexity_max=False (bool) must be ignored
        assert t.complexity_max == _DEFAULT_THRESHOLDS["complexity_max"]  # False must NOT override
        assert t.nesting_max == 3  # valid int, must be used

    def test_severity_string_applies_from_defaults_and_rule(self, tmp_path: Path) -> None:
        _write_policy(tmp_path, """
            schema_version: "gate.policy.v1"
            defaults:
              severity: WARNING
            rules:
              - scope: "backend/**/*.py"
                checks:
                  severity: INFO
        """)
        policy = load_gate_policy(tmp_path)
        assert policy is not None
        assert policy.effective_thresholds("other.py").severity == "WARNING"
        assert policy.effective_thresholds("backend/app.py").severity == "INFO"

    def test_unknown_checker_name_in_skip_ignored(self, tmp_path: Path) -> None:
        """Unknown checker names in skip list are stored but cause no crash."""
        _write_policy(tmp_path, """
            schema_version: "gate.policy.v1"
            rules:
              - scope: "*.py"
                checks:
                  skip: [nonexistent_checker, another_fake]
        """)
        policy = load_gate_policy(tmp_path)
        assert policy is not None
        skip = policy.skip_checkers_for_file("foo.py")
        assert "nonexistent_checker" in skip
        assert "another_fake" in skip

    def test_windows_backslash_path_normalized(self, tmp_path: Path) -> None:
        """Windows backslash paths must be normalized before matching."""
        _write_policy(tmp_path, """
            schema_version: "gate.policy.v1"
            rules:
              - scope: "backend/**/*.py"
                checks:
                  complexity_max: 4
        """)
        policy = load_gate_policy(tmp_path)
        assert policy is not None
        # Backslash path should match the same as forward-slash
        assert policy.resolve_for_file("backend\\api\\auth.py") is not None
        assert policy.resolve_for_file("backend/api/auth.py") is not None

    def test_multiple_rules_first_match_wins(self, tmp_path: Path) -> None:
        """When multiple rules match, the first one takes precedence."""
        _write_policy(tmp_path, """
            schema_version: "gate.policy.v1"
            rules:
              - scope: "backend/**/*.py"
                checks:
                  complexity_max: 3
              - scope: "backend/api/**/*.py"
                checks:
                  complexity_max: 10
        """)
        policy = load_gate_policy(tmp_path)
        assert policy is not None
        rule = policy.resolve_for_file("backend/api/auth.py")
        assert rule is not None
        assert rule.thresholds["complexity_max"] == 3  # first match wins

    def test_no_rules_section_uses_defaults_only(self, tmp_path: Path) -> None:
        _write_policy(tmp_path, """
            schema_version: "gate.policy.v1"
            defaults:
              file_max_lines: 750
        """)
        policy = load_gate_policy(tmp_path)
        assert policy is not None
        assert policy.rules == []
        t = policy.effective_thresholds("any/file.py")
        assert t.file_max_lines == 750
        assert t.function_max_lines == _DEFAULT_THRESHOLDS["function_max_lines"]
        assert t.max_violations == _DEFAULT_THRESHOLDS["max_violations"]

    def test_tier_fields_merge_and_backfill_complexity_max(self, tmp_path: Path) -> None:
        _write_policy(tmp_path, """
            schema_version: "gate.policy.v1"
            defaults:
              complexity_warn: 8
              file_complexity_warn_lines: 900
            rules:
              - scope: "backend/**/*.py"
                checks:
                  complexity_block: 12
                  file_complexity_block_sum: 40
        """)
        policy = load_gate_policy(tmp_path)
        assert policy is not None
        t = policy.effective_thresholds("backend/app.py")
        assert t.complexity_warn == 8
        assert t.complexity_block == 12
        # No explicit complexity_max override -> inferred from complexity_block.
        assert t.complexity_max == 12
        assert t.file_complexity_warn_lines == 900
        assert t.file_complexity_block_sum == 40

    def test_max_violations_default_is_50_not_100000(self) -> None:
        """_DEFAULT_THRESHOLDS max_violations must be 50, not some large number."""
        assert _DEFAULT_THRESHOLDS["max_violations"] == 50

    def test_globstar_tests_dir_matches_nested(self, tmp_path: Path) -> None:
        """tests/**/*.py should match tests/unit/test_foo.py (nested)."""
        _write_policy(tmp_path, """
            schema_version: "gate.policy.v1"
            rules:
              - scope: "tests/**/*.py"
                checks:
                  skip: [function_metrics]
        """)
        policy = load_gate_policy(tmp_path)
        assert policy is not None
        assert policy.resolve_for_file("tests/unit/test_foo.py") is not None

    def test_globstar_tests_dir_matches_direct(self, tmp_path: Path) -> None:
        """tests/**/*.py should match tests/test_foo.py (direct child)."""
        _write_policy(tmp_path, """
            schema_version: "gate.policy.v1"
            rules:
              - scope: "tests/**/*.py"
                checks:
                  skip: [function_metrics]
        """)
        policy = load_gate_policy(tmp_path)
        assert policy is not None
        assert policy.resolve_for_file("tests/test_foo.py") is not None

    def test_globstar_prefix_matches_nested(self, tmp_path: Path) -> None:
        """**/auth_*.py should match backend/api/auth_x.py (nested path)."""
        _write_policy(tmp_path, """
            schema_version: "gate.policy.v1"
            rules:
              - scope: "**/auth_*.py"
                checks:
                  complexity_max: 4
        """)
        policy = load_gate_policy(tmp_path)
        assert policy is not None
        assert policy.resolve_for_file("backend/api/auth_x.py") is not None

    def test_globstar_prefix_matches_root(self, tmp_path: Path) -> None:
        """**/auth_*.py should match auth_x.py at root (zero dir prefix)."""
        _write_policy(tmp_path, """
            schema_version: "gate.policy.v1"
            rules:
              - scope: "**/auth_*.py"
                checks:
                  complexity_max: 4
        """)
        policy = load_gate_policy(tmp_path)
        assert policy is not None
        assert policy.resolve_for_file("auth_x.py") is not None


# ---------------------------------------------------------------------------
# TestPolicyIntegration
# ---------------------------------------------------------------------------

class TestPolicyIntegration:

    def _write_complex_file(self, path: Path) -> None:
        """Write a file with non-trivial branching for complexity-policy tests."""
        lines = ["def complex_branch(x):"]
        lines.append("    score = 0")
        for i in range(8):
            lines.append(f"    if x > {i}:")
            lines.append(f"        score += {i}")
        lines.append("    return score")
        path.write_text("\n".join(lines), encoding="utf-8")

    def _write_long_file(self, path: Path, num_lines: int = 1200) -> None:
        """Write a file long enough to trigger file-length checks."""
        lines = [f"# line {i}" for i in range(num_lines)]
        path.write_text("\n".join(lines), encoding="utf-8")

    def test_auth_files_use_stricter_complexity(self, tmp_path: Path) -> None:
        """Files matching auth scope must use stricter complexity threshold from policy."""
        auth_dir = tmp_path / "backend" / "api" / "v1" / "services"
        auth_dir.mkdir(parents=True)
        auth_file = auth_dir / "auth_login.py"
        self._write_complex_file(auth_file)

        _write_policy(tmp_path, """
            schema_version: "gate.policy.v1"
            rules:
              - scope: "backend/api/v1/services/auth_*.py"
                checks:
                  complexity_max: 2
        """)
        policy = load_gate_policy(tmp_path)
        assert policy is not None

        rel = "backend/api/v1/services/auth_login.py"
        t = policy.effective_thresholds(rel)
        assert t.complexity_max == 2  # stricter than default 10

    def test_test_files_skip_function_metrics(self, tmp_path: Path) -> None:
        """Files under tests/ with skip:[function_metrics] should not be checked by function_metrics."""
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        test_file = tests_dir / "test_foo.py"
        self._write_complex_file(test_file)

        _write_policy(tmp_path, """
            schema_version: "gate.policy.v1"
            rules:
              - scope: "tests/**/*.py"
                checks:
                  skip: [function_metrics]
        """)
        policy = load_gate_policy(tmp_path)
        assert policy is not None

        skip = policy.skip_checkers_for_file("tests/test_foo.py")
        assert "function_metrics" in skip

        skip_nested = policy.skip_checkers_for_file("tests/unit/test_bar.py")
        assert "function_metrics" in skip_nested

    def test_no_policy_file_fallback_to_default_behavior(self, tmp_path: Path) -> None:
        """When no gate_policy.yaml exists, GateEngine.evaluate() behaves 100% as before."""
        source = tmp_path / "module.py"
        self._write_complex_file(source)

        # No policy file written — standard engine behavior
        engine = GateEngine(project_root=tmp_path)
        result = engine.evaluate(targets=[tmp_path], profile_name="advisory")

        assert result.total_status == GateStatus.PASS
        assert result.scanned_files >= 1
        assert result.total_violations >= 0  # advisory always passes

    def test_non_python_files_not_affected(self, tmp_path: Path) -> None:
        """Non-Python files are never flagged; policy scopes for .py don't affect them."""
        txt_file = tmp_path / "README.md"
        txt_file.write_text("# Hello\n" * 2000, encoding="utf-8")
        json_file = tmp_path / "config.json"
        json_file.write_text("{}\n", encoding="utf-8")

        _write_policy(tmp_path, """
            schema_version: "gate.policy.v1"
            rules:
              - scope: "**/*.md"
                checks:
                  file_max_lines: 10
        """)

        engine = GateEngine(project_root=tmp_path)
        result = engine.evaluate(targets=[tmp_path], profile_name="advisory")
        # Non-python files are not scanned; no violations from them
        assert result.scanned_files == 0
        assert result.total_violations == 0

    def test_policy_max_violations_affects_checker_status_and_payload(self, tmp_path: Path) -> None:
        source = tmp_path / "module.py"
        self._write_complex_file(source)
        _write_policy(tmp_path, """
            schema_version: "gate.policy.v1"
            defaults:
              max_violations: 0
              severity: WARNING
            rules:
              - scope: "*.py"
                checks:
                  file_complexity_block_lines: 10
                  file_complexity_block_sum: 1
        """)

        engine = GateEngine(project_root=tmp_path)
        result = engine.evaluate(targets=[tmp_path], profile_name="blocking")
        payload = result.to_dict()

        file_item = next(item for item in result.checker_results if item.checker == "file_length")
        assert file_item.status.value == "FAIL"
        assert payload["max_violations"] == 0
        assert payload["profile"]["thresholds"]["severity"] == "WARNING"
        assert file_item.violations
        assert all(v.severity == "WARNING" for v in file_item.violations)
