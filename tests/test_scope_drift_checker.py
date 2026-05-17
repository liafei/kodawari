from kodawari.gate.checkers import check_scope_drift


def test_scope_drift_allows_core_files_and_related_tests() -> None:
    result = check_scope_drift(
        changed_files=["src/service.py", "tests/test_service.py"],
        allowed_files=["src/service.py"],
    )

    assert result["status"] == "PASS"
    assert result["drifted"] is False
    assert result["out_of_scope_files"] == []


def test_scope_drift_reports_expected_fields_on_violation() -> None:
    result = check_scope_drift(
        changed_files=["src/service.py", "src/other.py"],
        allowed_files=["src/service.py"],
    )

    assert result["status"] == "FAIL"
    assert result["drifted"] is True
    assert result["allowed_files"]
    assert result["changed_files"] == ["src/service.py", "src/other.py"]
    assert result["out_of_scope_files"] == ["src/other.py"]
