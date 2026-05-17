import ast
from pathlib import Path

import pytest
from code_redline import Tier, evaluate_file

from kodawari.gate.checkers import check_cache_consistency, check_runtime_contract_scatter
from kodawari.gate.engine import GateEngine
from kodawari.gate.models import GateStatus, ItemStatus
from kodawari.gate.profiles import get_profile


def _write_complex_file(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "def complex_branch(x):",
                "    score = 0",
                "    if x > 0:",
                "        score += 1",
                "    if x > 1:",
                "        score += 1",
                "    if x > 2:",
                "        score += 1",
                "    if x > 3:",
                "        score += 1",
                "    if x > 4:",
                "        score += 1",
                "    if x > 5:",
                "        score += 1",
                "    if x > 6:",
                "        score += 1",
                "    if x > 7:",
                "        score += 1",
                "    if x > 8:",
                "        score += 1",
                "    if x > 9:",
                "        score += 1",
                "    if x > 10:",
                "        score += 1",
                "    return score",
            ]
        ),
        encoding="utf-8",
    )


def _write_medium_length_file(path: Path) -> None:
    lines = ["def medium_length():", "    value = 0"]
    for index in range(34):
        lines.append(f"    value += {index}")
    lines.append("    return value")
    path.write_text("\n".join(lines), encoding="utf-8")


def _write_large_complex_file(path: Path) -> None:
    lines = [f"# filler {index}" for index in range(1520)]
    lines.extend(
        [
            "def large_complex_branch(x):",
            "    score = 0",
        ]
    )
    for index in range(31):
        lines.extend(
            [
                f"    if x > {index}:",
                f"        score += {index}",
            ]
        )
    lines.append("    return score")
    path.write_text("\n".join(lines), encoding="utf-8")


def _file_complexity_sum(path: Path) -> int:
    source = path.read_text(encoding="utf-8-sig")
    tree = ast.parse(source)
    total = 0
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        complexity = 1
        for child in ast.walk(node):
            if isinstance(
                child,
                (
                    ast.If,
                    ast.For,
                    ast.AsyncFor,
                    ast.While,
                    ast.Try,
                    ast.ExceptHandler,
                    ast.With,
                    ast.AsyncWith,
                    ast.IfExp,
                    ast.Match,
                    ast.comprehension,
                ),
            ):
                complexity += 1
            if isinstance(child, ast.BoolOp):
                complexity += max(0, len(child.values) - 1)
        total += complexity
    return total


def test_default_profile_uses_non_blocking_modern_redline() -> None:
    profile = get_profile("advisory")

    assert profile.mode.value == "advisory"
    assert profile.thresholds.file_max_lines == 1500
    assert profile.thresholds.function_max_lines == 10000
    assert profile.thresholds.nesting_max == 4
    assert profile.thresholds.complexity_max == 7
    assert profile.thresholds.complexity_warn == 7
    assert profile.thresholds.complexity_block == 10
    assert profile.thresholds.max_violations == 50
    assert profile.thresholds.severity == "WARNING"


def test_tiered_profile_alias_matches_blocking_thresholds() -> None:
    tiered = get_profile("tiered")
    blocking = get_profile("blocking")

    assert tiered.mode.value == "blocking"
    assert tiered.thresholds.to_dict() == blocking.thresholds.to_dict()


def test_strict_profile_alias_matches_blocking_thresholds() -> None:
    strict = get_profile("strict")
    blocking = get_profile("blocking")

    assert strict.mode.value == "blocking"
    assert strict.thresholds.to_dict() == blocking.thresholds.to_dict()


def test_advisory_profile_reports_partial_items_but_total_pass(tmp_path: Path) -> None:
    source = tmp_path / "module.py"
    _write_complex_file(source)

    report = GateEngine(project_root=tmp_path).evaluate(targets=[tmp_path], profile_name="advisory")

    assert report.total_status == GateStatus.PASS
    assert report.total_violations > 0
    assert any(item.status == ItemStatus.PARTIAL for item in report.checker_results)


def test_blocking_profile_blocks_when_violations_exist(tmp_path: Path) -> None:
    source = tmp_path / "module.py"
    _write_complex_file(source)

    report = GateEngine(project_root=tmp_path).evaluate(targets=[tmp_path], profile_name="blocking")

    assert report.total_status == GateStatus.BLOCKED
    assert report.blocking_violations == report.total_violations
    assert report.total_violations > 0


def test_blocking_profile_does_not_block_large_declarative_file(tmp_path: Path) -> None:
    source = tmp_path / "module.py"
    source.write_text("\n".join(["value = 1"] * 1601), encoding="utf-8")

    report = GateEngine(project_root=tmp_path).evaluate(targets=[tmp_path], profile_name="blocking")

    assert report.total_status == GateStatus.PASS
    assert report.total_violations == 0


def test_strict_profile_uses_canonical_blocking_redline(tmp_path: Path) -> None:
    source = tmp_path / "module.py"
    _write_large_complex_file(source)

    report = GateEngine(project_root=tmp_path).evaluate(targets=[tmp_path], profile_name="strict")

    assert report.profile.name == "strict"
    assert report.total_status == GateStatus.BLOCKED
    assert report.blocking_violations > 0


def test_strict_historical_profile_is_not_supported_anymore(tmp_path: Path) -> None:
    source = tmp_path / "module.py"
    _write_medium_length_file(source)

    advisory = GateEngine(project_root=tmp_path).evaluate(targets=[tmp_path], profile_name="advisory")
    assert advisory.total_status == GateStatus.PASS
    assert advisory.total_violations == 0

    with pytest.raises(ValueError) as exc:
        GateEngine(project_root=tmp_path).evaluate(targets=[tmp_path], profile_name="strict-historical")
    assert "Unsupported gate profile" in str(exc.value)


def test_repo_src_python_files_respect_canonical_file_shape_redline() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    src_root = repo_root / "src"
    violations: list[str] = []

    for path in src_root.rglob("*.py"):
        line_count = len(path.read_text(encoding="utf-8-sig").splitlines())
        complexity_sum = _file_complexity_sum(path)
        tier = evaluate_file(line_count=line_count, complexity_sum=complexity_sum)
        if tier == Tier.BLOCK:
            rel_path = path.resolve().relative_to(repo_root.resolve()).as_posix()
            violations.append(f"{rel_path}:{line_count}:{complexity_sum}")

    assert violations == []


def test_cache_consistency_checker_passes_when_project_has_no_cache_semantics(tmp_path: Path) -> None:
    target = tmp_path / "src" / "service.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("def write_item(item):\n    db.session.add(item)\n", encoding="utf-8")

    result = check_cache_consistency(["src/service.py"], tmp_path)
    assert result["status"] == "PASS"
    assert result["mode"] == "ast_association_v2"
    assert result["warn_files"] == []
    assert result["suspicious_files"] == []


def test_cache_consistency_checker_detects_unlinked_invalidation_as_fail(tmp_path: Path) -> None:
    target = tmp_path / "src" / "service.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "\n".join(
            [
                "def write_item(item):",
                "    db.session.add(item)",
                "",
                "def invalidate_feed_cache(feed_id):",
                "    cache.invalidate(feed_id)",
            ]
        ),
        encoding="utf-8",
    )

    result = check_cache_consistency(["src/service.py"], tmp_path)
    assert result["status"] == "FAIL"
    assert result["fail_files"] == ["src/service.py"]
    assert result["suspicious_files"] == ["src/service.py"]
    assert any("write function" in str(item.get("hit") or "") for item in result["evidence"])


def test_cache_consistency_checker_passes_when_write_links_to_invalidation(tmp_path: Path) -> None:
    target = tmp_path / "src" / "service.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "\n".join(
            [
                "def write_item(item):",
                "    db.session.add(item)",
                "    invalidate_feed_cache(item.id)",
                "",
                "def invalidate_feed_cache(feed_id):",
                "    cache.invalidate(feed_id)",
            ]
        ),
        encoding="utf-8",
    )

    result = check_cache_consistency(["src/service.py"], tmp_path)
    assert result["status"] == "PASS"
    assert result["suspicious_files"] == []
    assert result["pass_files"] == ["src/service.py"]


def test_runtime_contract_scatter_checker_reports_conflict_fields(tmp_path: Path) -> None:
    schema_a = tmp_path / "a.schema.json"
    schema_b = tmp_path / "b.schema.json"
    schema_a.write_text(
        '{"type":"object","properties":{"phase_mode":{"type":"string","enum":["analyze","implement"]}}}',
        encoding="utf-8",
    )
    schema_b.write_text(
        '{"type":"object","properties":{"phase_mode":{"type":"integer"}}}',
        encoding="utf-8",
    )

    result = check_runtime_contract_scatter([str(schema_a), str(schema_b)])
    assert result["status"] == "FAIL"
    assert result["mode"] == "structural_rule_v3"
    assert result["conflict_fields"] == ["phase_mode"]
    assert result["conflict_files"]["phase_mode"] == [str(schema_a.resolve()), str(schema_b.resolve())]
    assert result["conflicts"]["phase_mode"]["dimensions"] == ["type", "enum"]
    assert result["evidence"]


def test_runtime_contract_scatter_checker_passes_when_structures_match(tmp_path: Path) -> None:
    schema_a = tmp_path / "a.schema.json"
    schema_b = tmp_path / "b.schema.json"
    payload = (
        '{"type":"object","required":["phase_mode"],'
        '"properties":{"phase_mode":{"type":"string","enum":["analyze","implement"],"description":"phase"}}}'
    )
    schema_a.write_text(payload, encoding="utf-8")
    schema_b.write_text(payload, encoding="utf-8")

    result = check_runtime_contract_scatter([str(schema_a), str(schema_b)])
    assert result["status"] == "PASS"
    assert result["conflict_fields"] == []


def test_runtime_contract_scatter_metadata_drift_is_non_blocking(tmp_path: Path) -> None:
    schema_a = tmp_path / "a.schema.json"
    schema_b = tmp_path / "b.schema.json"
    schema_a.write_text(
        '{"type":"object","required":["phase_mode"],"properties":{"phase_mode":{"type":"string","description":"required in source"}}}',
        encoding="utf-8",
    )
    schema_b.write_text(
        '{"type":"object","properties":{"phase_mode":{"type":"string","description":"optional in derived artifact"}}}',
        encoding="utf-8",
    )

    result = check_runtime_contract_scatter([str(schema_a), str(schema_b)])

    assert result["status"] == "PASS"
    assert result["conflict_fields"] == []
    assert result["metadata_drifts"]["phase_mode"]["dimensions"] == ["required", "description"]


def test_gate_scan_skips_parallel_workers_isolation_dir(tmp_path: Path) -> None:
    """Regression: Phase B isolation workspaces under planning/.../
    .parallel_workers/<backend>/<task>/ contain per-task copies of the project
    tree. If gate scans them, violations get double-counted (once in the real
    tree, once in every isolation copy).

    Observed in sdk-realworld-run-4: scanning 2085 files reported 1206
    violations, ~99% of which came from leftover .parallel_workers/ copies
    of already-over-limit newsapp files.
    """
    from kodawari.gate.engine import _iter_python_files, _is_skipped_path

    # Set up: a real source file + a parallel_workers copy of it
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend" / "main.py").write_text("pass\n", encoding="utf-8")

    workers = tmp_path / "planning" / "feat" / ".parallel_workers" / "codex_cli" / "t1-abcdef"
    workers.mkdir(parents=True)
    (workers / "main.py").write_text("pass\n", encoding="utf-8")
    (workers / "copied_source.py").write_text("pass\n", encoding="utf-8")

    files = _iter_python_files(tmp_path)
    # Must NOT include anything under .parallel_workers
    assert all(".parallel_workers" not in str(p) for p in files), (
        f"gate scanner leaked into .parallel_workers: {files}"
    )
    # But MUST include the real source
    assert any(p.name == "main.py" and "parallel_workers" not in str(p) for p in files)


def test_is_skipped_path_rejects_parallel_workers() -> None:
    from pathlib import Path
    from kodawari.gate.engine import _is_skipped_path
    assert _is_skipped_path(
        Path("planning/feat/.parallel_workers/codex_cli/t1-abc/main.py")
    )
    assert _is_skipped_path(
        Path("E:/repo/planning/x/.parallel_workers/claude_code/t2/y.py")
    )
    # Normal project paths stay included
    assert not _is_skipped_path(Path("backend/main.py"))
    assert not _is_skipped_path(Path("planning/feat/TASK_CARD_T1.json"))


# ---------------------------------------------------------------------------
# _violation_message remediation hints
# ---------------------------------------------------------------------------

def test_complexity_violation_message_includes_remediation() -> None:
    from kodawari.autopilot.runtime_checks import _violation_message  # noqa: PLC0415

    violation = {
        "path": "backend/api/v1/services/source_metadata.py",
        "message": "Function _coerce_int_or_none complexity 15 exceeds 10.",
        "metric": "complexity",
        "actual": 15,
        "limit": 10,
    }
    msg = _violation_message(violation)
    assert "Remediation:" in msg
    assert "smaller helpers" in msg
    # Must be single-line (no embedded newlines that would break downstream consumers)
    assert "\n" not in msg


def test_nesting_violation_message_includes_remediation() -> None:
    from kodawari.autopilot.runtime_checks import _violation_message  # noqa: PLC0415

    violation = {
        "path": "backend/api/v1/services/crawler.py",
        "message": "Function run nesting depth 5 exceeds 4.",
        "metric": "nesting",
        "actual": 5,
        "limit": 4,
    }
    msg = _violation_message(violation)
    assert "Remediation:" in msg
    assert "early returns" in msg.lower() or "guard clauses" in msg.lower()
    assert "\n" not in msg


def test_unknown_metric_violation_has_no_remediation() -> None:
    from kodawari.autopilot.runtime_checks import _violation_message  # noqa: PLC0415

    violation = {
        "path": "backend/main.py",
        "message": "File exceeds 1000 lines.",
        "metric": "file_lines",
    }
    msg = _violation_message(violation)
    assert "Remediation:" not in msg
    assert "\n" not in msg
