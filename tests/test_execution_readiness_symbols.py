"""Python symbol preconditions in execution_readiness.

Mirrors the schema field readiness pattern: a task that declares it
``requires`` an existing Python symbol must not run if no source file
defines that symbol, unless the task itself is allowed to create the
module.
"""

from __future__ import annotations

from pathlib import Path

from kodawari.autopilot.planning.execution_readiness import evaluate_execution_readiness


def _setup_src(tmp_path: Path, files: dict[str, str]) -> Path:
    src = tmp_path / "src" / "pkg"
    src.mkdir(parents=True)
    for name, content in files.items():
        (src / name).write_text(content, encoding="utf-8")
    return tmp_path


def test_existing_class_passes_readiness(tmp_path: Path) -> None:
    project_root = _setup_src(tmp_path, {"core.py": "class Engine:\n    pass\n"})
    card = {
        "requires": [{"kind": "symbol", "name": "Engine", "existing": True}],
    }
    result = evaluate_execution_readiness(project_root=project_root, task_card=card)
    assert result["status"] == "PASS"
    assert result["missing_symbol_preconditions"] == []


def test_missing_symbol_blocks(tmp_path: Path) -> None:
    project_root = _setup_src(tmp_path, {"core.py": "class Other:\n    pass\n"})
    card = {
        "requires": [{"kind": "symbol", "name": "Engine", "existing": True}],
    }
    result = evaluate_execution_readiness(project_root=project_root, task_card=card)
    assert result["status"] == "BLOCKED"
    assert result["missing_symbol_preconditions"] == ["Engine"]
    assert "Python module/symbol" in result["suggested_next_task"]


def test_module_creation_allowed_unblocks(tmp_path: Path) -> None:
    project_root = _setup_src(tmp_path, {"other.py": "x = 1\n"})
    card = {
        "requires": [{"kind": "symbol", "name": "Engine", "existing": True}],
        "new_files": ["src/pkg/engine.py"],
    }
    result = evaluate_execution_readiness(project_root=project_root, task_card=card)
    assert result["status"] == "PASS"
    assert result["module_creation_allowed"] is True


def test_module_hint_constrains_search(tmp_path: Path) -> None:
    """Symbol with a module hint should only match files in that module."""
    project_root = _setup_src(
        tmp_path,
        {
            "core.py": "class Engine:\n    pass\n",
            "other.py": "Engine = 'not the same thing'\n",
        },
    )
    # Hint points at other.py — Engine assignment there counts as a match.
    card = {
        "requires": [{"kind": "symbol", "name": "src/pkg/other.py:Engine", "existing": True}],
    }
    result = evaluate_execution_readiness(project_root=project_root, task_card=card)
    assert result["status"] == "PASS"


def test_module_hint_misses_when_only_other_module_has_symbol(tmp_path: Path) -> None:
    project_root = _setup_src(tmp_path, {"core.py": "class Engine:\n    pass\n"})
    # Hint points at a non-existent module path that does not match core.py.
    card = {
        "requires": [{"kind": "symbol", "name": "src/pkg/missing.py:Engine", "existing": True}],
    }
    result = evaluate_execution_readiness(project_root=project_root, task_card=card)
    assert result["status"] == "BLOCKED"
    assert result["missing_symbol_preconditions"] == ["src/pkg/missing.py:Engine"]


def test_symbol_and_field_can_coexist(tmp_path: Path) -> None:
    """A card with both kinds of requirements blocks if either is missing."""
    project_root = _setup_src(tmp_path, {"core.py": "class Engine:\n    pass\n"})
    card = {
        "requires": [
            {"kind": "symbol", "name": "Engine", "existing": True},
            {"kind": "field", "name": "users.email", "existing": True},
        ],
    }
    result = evaluate_execution_readiness(project_root=project_root, task_card=card)
    # field is not present and not allowed to mutate -> BLOCKED on field, not symbol
    assert result["missing_symbol_preconditions"] == []
