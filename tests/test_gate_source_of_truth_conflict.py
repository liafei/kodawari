import json
from pathlib import Path
from types import SimpleNamespace

import kodawari.gate.ast_checker as ast_checker
import kodawari.gate.checkers as checkers


def _write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_sot_conflict_ignores_existing_writes_when_no_new_write(tmp_path, monkeypatch):
    project_root = tmp_path
    file_path = project_root / "app" / "main.py"
    # existing file already contains write SQL to another table
    _write_file(
        file_path,
        "\n".join(
            [
                "def existing():",
                "    # legacy write path",
                "    db.execute(\"INSERT INTO legacy_orders VALUES (1)\")",
                "",
                "def new_read():",
                "    return True",
            ]
        ),
    )

    # only new read-only lines are added in this change
    monkeypatch.setattr(checkers, "_git_added_lines", lambda root, rel: ["def new_read():", "    return True"])
    monkeypatch.setattr(checkers, "check_source_of_truth_conflict_ast", lambda *_args, **_kwargs: [])

    violations = checkers.check_source_of_truth_conflict(
        ["app/main.py"],
        project_root,
        declared_sot=["db.patient_settings.daily_water_goal_ml"],
    )
    assert violations == []


def test_sot_conflict_blocks_new_undeclared_write(tmp_path, monkeypatch):
    project_root = tmp_path
    file_path = project_root / "app" / "main.py"
    _write_file(
        file_path,
        "\n".join(
            [
                "def handler():",
                "    return True",
            ]
        ),
    )

    # New change introduces an undeclared write
    monkeypatch.setattr(
        checkers,
        "_git_added_lines",
        lambda root, rel: ["def handler():", "    db.execute(\"INSERT INTO secrets VALUES (1)\")"],
    )
    monkeypatch.setattr(checkers, "check_source_of_truth_conflict_ast", lambda *_args, **_kwargs: [])

    violations = checkers.check_source_of_truth_conflict(
        ["app/main.py"],
        project_root,
        declared_sot=["db.allowed_table"],
    )
    assert any("db.secrets" in item for item in violations)


def test_git_added_lines_decodes_utf8_bytes_without_locale_crash(tmp_path, monkeypatch):
    project_root = tmp_path

    def _fake_run(*_args, **_kwargs):
        return SimpleNamespace(stdout="+++ b/app/main.py\n+print('饮水提醒')\n".encode("utf-8"))

    monkeypatch.setattr(checkers.subprocess, "run", _fake_run)

    added_lines = checkers._git_added_lines(project_root, "app/main.py")

    assert added_lines == ["print('饮水提醒')"]


def test_ast_sot_conflict_ignores_legacy_writes_when_added_lines_are_read_only(tmp_path, monkeypatch):
    project_root = tmp_path
    file_path = project_root / "app" / "main.py"
    _write_file(
        file_path,
        "\n".join(
            [
                "def existing_write():",
                "    db.execute(\"INSERT INTO caregivers VALUES (1)\")",
                "",
                "def new_read():",
                "    return True",
            ]
        ),
    )

    monkeypatch.setattr(ast_checker, "_git_added_lines", lambda *_args, **_kwargs: ["def new_read():", "    return True"])

    violations = ast_checker.check_source_of_truth_conflict_ast(
        ["app/main.py"],
        project_root,
        declared_sot=["db.patient_settings"],
    )

    assert violations == []
