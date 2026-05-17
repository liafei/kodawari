import json
from pathlib import Path

import pytest

from kodawari.cli.main import build_parser


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


def _write_long_file(path: Path, *, line_count: int = 1001) -> None:
    path.write_text("\n".join(["value = 1"] * line_count), encoding="utf-8")


def test_gate_cli_supports_minimal_run_and_output_file(tmp_path: Path, capsys) -> None:
    parser = build_parser()
    _write_complex_file(tmp_path / "module.py")
    output = tmp_path / "gate_report.json"

    args = parser.parse_args(
        [
            "gate",
            "--project-root",
            str(tmp_path),
            "--profile",
            "advisory",
            "--output",
            str(output),
        ]
    )
    rc = args.handler(args)

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["profile"]["name"] == "advisory"
    assert payload["profile"]["mode"] == "advisory"
    assert payload["profile"]["thresholds"] == {
        "file_max_lines": 1500,
        "function_max_lines": 10000,
        "nesting_max": 4,
        "complexity_max": 7,
        "complexity_warn": 7,
        "complexity_block": 10,
        "file_complexity_warn_lines": 1000,
        "file_complexity_warn_sum": 20,
        "file_complexity_block_lines": 1500,
        "file_complexity_block_sum": 30,
        "max_violations": 50,
        "severity": "WARNING",
    }
    assert payload["total_status"] == "PASS"
    assert output.exists()


def test_gate_cli_fail_on_block_returns_non_zero(tmp_path: Path, capsys) -> None:
    parser = build_parser()
    _write_complex_file(tmp_path / "module.py")

    args = parser.parse_args(
        [
            "gate",
            "--project-root",
            str(tmp_path),
            "--profile",
            "blocking",
            "--fail-on-block",
        ]
    )
    rc = args.handler(args)

    payload = json.loads(capsys.readouterr().out)
    assert payload["total_status"] == "BLOCKED"
    assert rc == 2


def test_gate_cli_strict_profile_is_canonical_blocking_alias(tmp_path: Path, capsys) -> None:
    parser = build_parser()
    _write_large_complex_file(tmp_path / "module.py")

    args = parser.parse_args(
        [
            "gate",
            "--project-root",
            str(tmp_path),
            "--profile",
            "strict",
            "--fail-on-block",
        ]
    )
    rc = args.handler(args)

    payload = json.loads(capsys.readouterr().out)
    assert payload["profile"]["name"] == "strict"
    assert payload["profile"]["mode"] == "blocking"
    assert payload["profile"]["thresholds"]["file_complexity_block_lines"] == 1500
    assert payload["total_status"] == "BLOCKED"
    assert payload["blocking_violations"] > 0
    assert rc == 2


def test_gate_cli_blocking_profile_ignores_large_declarative_file(tmp_path: Path, capsys) -> None:
    parser = build_parser()
    _write_long_file(tmp_path / "module.py", line_count=1601)

    args = parser.parse_args(
        [
            "gate",
            "--project-root",
            str(tmp_path),
            "--profile",
            "blocking",
            "--fail-on-block",
        ]
    )
    rc = args.handler(args)

    payload = json.loads(capsys.readouterr().out)
    assert payload["total_status"] == "PASS"
    assert payload["blocking_violations"] == 0
    assert rc == 0


def test_gate_help_is_registered() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["gate", "--help"])
    assert exc.value.code == 0


def test_gate_cli_ratchet_blocks_on_metric_regression(tmp_path: Path, capsys) -> None:
    parser = build_parser()
    _write_long_file(tmp_path / "module.py")
    baseline = tmp_path / "baseline.json"
    baseline.write_text(
        json.dumps({"metrics": {"files_over_1000_lines": 0, "functions_over_50_lines": 0}}, ensure_ascii=False),
        encoding="utf-8",
    )

    args = parser.parse_args(
        [
            "gate",
            "--project-root",
            str(tmp_path),
            "--ratchet",
            "--baseline",
            str(baseline),
        ]
    )
    rc = args.handler(args)

    payload = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert payload["total_status"] == "BLOCKED"
    assert payload["ratchet"]["status"] == "FAIL"
    assert payload["ratchet"]["regressions"][0]["metric"] == "files_over_1000_lines"


def test_gate_cli_ratchet_requires_baseline(tmp_path: Path, capsys) -> None:
    parser = build_parser()
    _write_complex_file(tmp_path / "module.py")

    args = parser.parse_args(
        [
            "gate",
            "--project-root",
            str(tmp_path),
            "--ratchet",
        ]
    )
    rc = args.handler(args)

    payload = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert payload["error_code"] == "gate_ratchet_requires_baseline"
