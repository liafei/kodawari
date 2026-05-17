"""Unit tests for the post-implement fidelity gate."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from kodawari.autopilot.verify.fidelity_gate import (
    FidelityResult,
    check_migration_files_exist,
    check_task_name_token_coverage,
    check_test_negative_assertion_drift,
    run_fidelity_gate,
)


def _write(root: Path, rel: str, body: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


# ---------------------------------------------------------------------------
# task_name_token_coverage
# ---------------------------------------------------------------------------


def test_token_coverage_passes_when_all_tokens_present() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        # Multi-char tokens match via substring; the 1-char token "X" must
        # appear with non-word-char boundaries (here as ``'x'`` string lit
        # or as a CLI option ``-x``).
        _write(
            root,
            "backend/foo.py",
            "def fetch_google_trending(): pass\n"
            "PROVIDER = 'x'\n"
            "def fetch_yahoo_trending(): pass\n",
        )
        tc = {"task_name": "Add provider rendering for Google, X, and Yahoo"}
        findings = check_task_name_token_coverage(tc, ["backend/foo.py"], root)
        assert findings == [], f"expected pass; got {[f.message for f in findings]}"


def test_token_coverage_flags_missing_enum_member() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        # Only mentions google — X and yahoo missing
        _write(root, "backend/foo.py", "def fetch_google_trending(): pass\n")
        tc = {"task_name": "Add provider rendering for Google, X, and Yahoo"}
        findings = check_task_name_token_coverage(tc, ["backend/foo.py"], root)
        missing = sorted(f.evidence["missing_token"].lower() for f in findings)
        assert "x" in missing
        assert "yahoo" in missing
        assert "google" not in missing
        for f in findings:
            assert f.severity == "block"
            assert f.kind == "missing_task_name_token"


def test_token_coverage_ignores_test_files() -> None:
    """Tokens appearing ONLY in tests don't count as delivered."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write(root, "tests/test_x.py", "# Google + X + Yahoo references in test only\n")
        tc = {"task_name": "Add rendering for Google, X, and Yahoo"}
        findings = check_task_name_token_coverage(tc, ["tests/test_x.py"], root)
        # Test-only changed_files: function early-returns; no findings emitted.
        assert findings == []


def test_token_coverage_skips_stopword_verbs() -> None:
    """Leading verbs like 'Bring' / 'Collapse' must not be required."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write(root, "backend/foo.py", "def whatever(): pass\n")
        tc = {"task_name": "Bring external-trends service contract in line with PRD"}
        # 'Bring' is a stopword; 'PRD' is the only candidate (and missing).
        findings = check_task_name_token_coverage(tc, ["backend/foo.py"], root)
        kinds = {f.evidence["missing_token"] for f in findings}
        assert "Bring" not in kinds  # stopword filtered
        # PRD is uppercase and not in stopwords — should be flagged as missing
        assert "PRD" in kinds


# ---------------------------------------------------------------------------
# migration_files_exist
# ---------------------------------------------------------------------------


def test_migration_present_passes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write(
            root,
            "backend/db/migrations.py",
            'MigrationDefinition(filename="20260516_999_xxx.sql")\n',
        )
        _write(root, "backend/db/migration_sql/20260516_999_xxx.sql", "-- ok\n")
        findings = check_migration_files_exist(["backend/db/migrations.py"], root)
        assert findings == []


def test_migration_missing_sql_flagged() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write(
            root,
            "backend/db/migrations.py",
            'MigrationDefinition(filename="20260516_999_missing.sql")\n',
        )
        # NO SQL file on disk
        findings = check_migration_files_exist(["backend/db/migrations.py"], root)
        assert len(findings) == 1
        assert findings[0].kind == "missing_migration_file"
        assert findings[0].severity == "block"
        assert "20260516_999_missing.sql" in findings[0].message


def test_migration_check_ignores_non_migration_files() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write(root, "backend/api/handler.py", 'msg = "filename=\\"never.sql\\""\n')
        findings = check_migration_files_exist(["backend/api/handler.py"], root)
        assert findings == []


# ---------------------------------------------------------------------------
# test_negative_assertion_drift
# ---------------------------------------------------------------------------


def test_negative_assertion_flagged_when_subject_matches_task_token() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write(
            root,
            "tests/test_x.py",
            '''
def test_no_yahoo():
    assert "/external-trends/yahoo" not in index_html
def test_no_x():
    assert "/external-trends/x" not in index_html
''',
        )
        tc = {"task_name": "Add provider rendering for Google, X, and Yahoo"}
        findings = check_test_negative_assertion_drift(tc, ["tests/test_x.py"], root)
        subjects = sorted(f.evidence["negative_assertion_subject"] for f in findings)
        assert "/external-trends/yahoo" in subjects
        assert "/external-trends/x" in subjects
        for f in findings:
            assert f.kind == "test_drift_negative_assertion"
            assert f.severity == "block"


def test_negative_assertion_not_flagged_when_unrelated() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write(
            root,
            "tests/test_x.py",
            'def test_no_debug():\n    assert "DEBUG_FLAG" not in env\n',
        )
        tc = {"task_name": "Add provider rendering for Google, X, and Yahoo"}
        findings = check_test_negative_assertion_drift(tc, ["tests/test_x.py"], root)
        assert findings == []


def test_negative_assertion_dedups_duplicate_subject_in_file() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write(
            root,
            "tests/test_x.py",
            '''
def test_a():
    assert "/external-trends/yahoo" not in html_a
def test_b():
    assert "/external-trends/yahoo" not in html_b
def test_c():
    assert "/external-trends/yahoo" not in html_c
''',
        )
        tc = {"task_name": "Add provider rendering for Yahoo"}
        findings = check_test_negative_assertion_drift(tc, ["tests/test_x.py"], root)
        # Subject "/external-trends/yahoo" should be reported once per (file, subject)
        assert len(findings) == 1


def test_negative_assertion_pulls_tokens_from_invariants_too() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write(root, "tests/test_x.py", 'assert "kol-platform" not in html\n')
        tc = {
            "task_name": "Persist snapshot",
            "invariants": ["Kol-platform field must appear in payload"],
        }
        findings = check_test_negative_assertion_drift(tc, ["tests/test_x.py"], root)
        # token "Kol-platform" is two words connected by hyphen; tokens
        # would be "Kol" and "platform" via _PROPER_RE. Subject lowercased
        # is "kol-platform" which contains "kol".
        assert len(findings) == 1


# ---------------------------------------------------------------------------
# run_fidelity_gate aggregation
# ---------------------------------------------------------------------------


def test_run_fidelity_gate_returns_aggregated_result() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        # No drift at all
        _write(root, "backend/foo.py", "def google(): pass\n")
        tc = {"task_name": "Add Google integration"}
        result = run_fidelity_gate(project_root=root, task_card=tc, changed_files=["backend/foo.py"])
        assert result.passed
        assert result.blocking_count == 0


def test_run_fidelity_gate_aggregates_all_check_findings() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        # T3-like scenario: missing tokens + negative-assertion drift
        _write(root, "mobile/www/index.html", "<a href='/external-trends/google'></a>\n")
        _write(root, "tests/test_x.py", 'assert "/external-trends/yahoo" not in html\n')
        tc = {"task_name": "Add provider entries for Google, X, and Yahoo"}
        result = run_fidelity_gate(
            project_root=root,
            task_card=tc,
            changed_files=["mobile/www/index.html", "tests/test_x.py"],
        )
        assert not result.passed
        kinds = {f.kind for f in result.findings}
        assert "missing_task_name_token" in kinds
        assert "test_drift_negative_assertion" in kinds


def test_fidelity_gate_skip_silences_specific_check() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write(root, "tests/test_x.py", 'assert "/external-trends/yahoo" not in html\n')
        tc = {
            "task_name": "Add provider entries for Yahoo",
            "fidelity_gate_skip": ["test_negative_assertion_drift"],
        }
        result = run_fidelity_gate(
            project_root=root,
            task_card=tc,
            changed_files=["tests/test_x.py"],
        )
        # task_name_token_coverage check is still on; tests-only changed_files
        # mean it early-returns; combined with the disabled negative-assertion
        # check, no findings should remain.
        assert result.passed


def test_must_fix_messages_only_blocking() -> None:
    """``must_fix_messages`` should expose only block-severity messages so
    they can be piped straight into the engine's review_feedback."""
    result = FidelityResult()
    from kodawari.autopilot.verify.fidelity_gate import FidelityFinding
    result.findings.append(FidelityFinding(kind="x", severity="warn", message="warn msg"))
    result.findings.append(FidelityFinding(kind="y", severity="block", message="block msg"))
    msgs = result.must_fix_messages()
    assert msgs == ["block msg"]
