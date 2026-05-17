from __future__ import annotations

import json
from pathlib import Path

from kodawari.gate import checker_duplication


def _write_python_file(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def test_duplication_checker_warns_when_pylint_is_unavailable(tmp_path: Path, monkeypatch) -> None:
    _write_python_file(tmp_path / "src" / "a.py", "def alpha():\n    return 1\n")
    monkeypatch.setattr(checker_duplication, "_resolve_pylint_command", lambda: None)

    report = checker_duplication.run_duplication_checker([tmp_path], project_root=tmp_path)

    assert report.status == "WARN"
    assert report.tool_available is False
    assert report.duplicate_blocks == []
    assert report.checked_files == 1
    assert report.evidence
    assert report.evidence[0].metadata["reason"] == "pylint unavailable"


def test_duplication_checker_passes_when_no_duplicate_blocks_are_reported(tmp_path: Path, monkeypatch) -> None:
    _write_python_file(tmp_path / "src" / "a.py", "def alpha():\n    return 1\n")
    _write_python_file(tmp_path / "src" / "b.py", "def beta():\n    return 2\n")
    monkeypatch.setattr(checker_duplication, "_resolve_pylint_command", lambda: ["pylint"])
    monkeypatch.setattr(
        checker_duplication,
        "_run_pylint",
        lambda command: checker_duplication.PylintRunResult(
            command=list(command),
            returncode=0,
            stdout="[]",
            stderr="",
        ),
    )

    report = checker_duplication.run_duplication_checker([tmp_path / "src"], project_root=tmp_path)

    assert report.status == "PASS"
    assert report.tool_available is True
    assert report.checked_files == 2
    assert report.duplicate_blocks == []
    assert report.evidence == []
    assert report.to_dict()["duplicate_block_count"] == 0


def test_duplication_checker_parses_duplicate_code_blocks_from_json(tmp_path: Path, monkeypatch) -> None:
    _write_python_file(tmp_path / "src" / "a.py", "def alpha():\n    return 1\n")
    _write_python_file(tmp_path / "src" / "b.py", "def beta():\n    return 2\n")
    duplicate_message = "\n".join(
        [
            "Similar lines in 2 files",
            "==src/a.py:1",
            "==src/b.py:1",
        ]
    )
    payload = [
        {
            "type": "refactor",
            "module": "pkg.a",
            "obj": "alpha",
            "line": 1,
            "column": 0,
            "path": "src/a.py",
            "symbol": "duplicate-code",
            "message-id": "R0801",
            "message": duplicate_message,
        },
        {
            "type": "refactor",
            "module": "pkg.b",
            "obj": "beta",
            "line": 1,
            "column": 0,
            "path": "src/b.py",
            "symbol": "duplicate-code",
            "message-id": "R0801",
            "message": duplicate_message,
        },
        {
            "type": "warning",
            "module": "pkg.c",
            "obj": "",
            "line": 1,
            "column": 0,
            "path": "src/c.py",
            "symbol": "unused-argument",
            "message-id": "W0613",
            "message": "Unused argument value",
        },
    ]
    monkeypatch.setattr(checker_duplication, "_resolve_pylint_command", lambda: ["pylint"])
    monkeypatch.setattr(
        checker_duplication,
        "_run_pylint",
        lambda command: checker_duplication.PylintRunResult(
            command=list(command),
            returncode=8,
            stdout=json.dumps(payload),
            stderr="",
        ),
    )

    report = checker_duplication.run_duplication_checker([tmp_path / "src"], project_root=tmp_path)

    assert report.status == "FAIL"
    assert report.tool_available is True
    assert report.duplicate_blocks
    assert len(report.duplicate_blocks) == 1
    block = report.duplicate_blocks[0]
    assert block.message_id == "R0801"
    assert block.occurrence_count == 2
    assert block.related_paths == ["src/a.py", "src/b.py"]
    assert report.evidence
    evidence = report.evidence[0]
    assert evidence.metadata["occurrence_count"] == 2
    assert evidence.metadata["related_paths"] == ["src/a.py", "src/b.py"]
    payload_dict = report.to_dict()
    assert payload_dict["duplicate_block_count"] == 1
    assert payload_dict["duplicate_occurrence_count"] == 2
