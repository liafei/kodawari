"""Tests for `kodawari gate` scope resolution (decision 1).

Default behavior change: when `--path` is not given, `kodawari gate`
previously always scanned the entire project root, which drowned task-run
outputs in pre-existing tech debt. Now:

- `--path <file>`  → explicit targets (unchanged)
- `--scope=full`   → force full project_root scan (pre-release audit)
- `--scope=changed` → require `.execution_result.json` with changed_files;
                      fail loudly if missing
- `--scope=auto` (default) → read changed_files from
                      `.execution_result.json` if present; else full scan
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from kodawari.cli.gate_cmd import (
    _classify_scope_used,
    _read_changed_files_from_execution_result,
    _resolve_gate_targets,
)


def _args(
    *,
    project_root: Path,
    feature: str = "",
    path: list[str] | None = None,
    scope: str = "auto",
    planning_dir: str | None = None,
) -> argparse.Namespace:
    return argparse.Namespace(
        project_root=str(project_root),
        feature=feature,
        planning_dir=planning_dir,
        path=path or [],
        scope=scope,
    )


def _write_execution_result(planning_dir: Path, changed_files: list[str]) -> None:
    planning_dir.mkdir(parents=True, exist_ok=True)
    (planning_dir / ".execution_result.json").write_text(
        json.dumps({"changed_files": changed_files, "status": "PASS"}),
        encoding="utf-8",
    )


class TestReadChangedFiles:
    def test_returns_none_when_file_missing(self, tmp_path: Path) -> None:
        assert _read_changed_files_from_execution_result(tmp_path) is None

    def test_returns_none_when_changed_files_missing(self, tmp_path: Path) -> None:
        (tmp_path / ".execution_result.json").write_text(
            json.dumps({"status": "PASS"}), encoding="utf-8"
        )
        assert _read_changed_files_from_execution_result(tmp_path) is None

    def test_returns_none_when_changed_files_empty(self, tmp_path: Path) -> None:
        _write_execution_result(tmp_path, [])
        assert _read_changed_files_from_execution_result(tmp_path) is None

    def test_returns_list_when_populated(self, tmp_path: Path) -> None:
        _write_execution_result(tmp_path, ["a.py", "b.py"])
        assert _read_changed_files_from_execution_result(tmp_path) == ["a.py", "b.py"]

    def test_returns_none_when_malformed_json(self, tmp_path: Path) -> None:
        (tmp_path / ".execution_result.json").write_text("not json{", encoding="utf-8")
        assert _read_changed_files_from_execution_result(tmp_path) is None

    def test_whitespace_entries_filtered(self, tmp_path: Path) -> None:
        _write_execution_result(tmp_path, ["  ", "real.py", ""])
        assert _read_changed_files_from_execution_result(tmp_path) == ["real.py"]


class TestResolveGateTargetsExplicitPath:
    def test_explicit_path_overrides_scope(self, tmp_path: Path) -> None:
        feature_dir = tmp_path / "planning" / "feat"
        _write_execution_result(feature_dir, ["a.py", "b.py"])
        targets = _resolve_gate_targets(
            _args(
                project_root=tmp_path,
                feature="feat",
                path=["explicit.py"],
                scope="auto",
            ),
            tmp_path,
        )
        assert len(targets) == 1
        assert targets[0].name == "explicit.py"

    def test_relative_paths_resolved_under_project_root(self, tmp_path: Path) -> None:
        targets = _resolve_gate_targets(
            _args(project_root=tmp_path, path=["sub/main.py"]),
            tmp_path,
        )
        assert targets[0] == (tmp_path / "sub" / "main.py").resolve()


class TestResolveGateTargetsScopeAuto:
    def test_auto_uses_changed_files_when_execution_result_present(
        self, tmp_path: Path
    ) -> None:
        feature_dir = tmp_path / "planning" / "feat"
        _write_execution_result(feature_dir, ["backend/main.py", "tests/test_main.py"])
        targets = _resolve_gate_targets(
            _args(project_root=tmp_path, feature="feat", scope="auto"),
            tmp_path,
        )
        assert [t.name for t in targets] == ["main.py", "test_main.py"]

    def test_auto_falls_back_to_project_root_when_no_evidence(
        self, tmp_path: Path
    ) -> None:
        targets = _resolve_gate_targets(
            _args(project_root=tmp_path, feature="no-such", scope="auto"),
            tmp_path,
        )
        assert targets == [tmp_path]


class TestResolveGateTargetsScopeFull:
    def test_full_ignores_changed_files(self, tmp_path: Path) -> None:
        feature_dir = tmp_path / "planning" / "feat"
        _write_execution_result(feature_dir, ["backend/main.py"])
        targets = _resolve_gate_targets(
            _args(project_root=tmp_path, feature="feat", scope="full"),
            tmp_path,
        )
        assert targets == [tmp_path]


class TestResolveGateTargetsScopeChanged:
    def test_changed_raises_when_no_execution_result(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="--scope=changed requires"):
            _resolve_gate_targets(
                _args(project_root=tmp_path, feature="no-such", scope="changed"),
                tmp_path,
            )

    def test_changed_uses_execution_result_when_present(self, tmp_path: Path) -> None:
        feature_dir = tmp_path / "planning" / "feat"
        _write_execution_result(feature_dir, ["only-this.py"])
        targets = _resolve_gate_targets(
            _args(project_root=tmp_path, feature="feat", scope="changed"),
            tmp_path,
        )
        assert len(targets) == 1
        assert targets[0].name == "only-this.py"


class TestClassifyScopeUsed:
    def test_explicit_path(self, tmp_path: Path) -> None:
        args = _args(project_root=tmp_path, path=["a.py"])
        used, source = _classify_scope_used(
            args, project_root=tmp_path, targets=[tmp_path / "a.py"]
        )
        assert used == "explicit"
        assert source == "--path"

    def test_full_explicit(self, tmp_path: Path) -> None:
        args = _args(project_root=tmp_path, scope="full")
        used, source = _classify_scope_used(
            args, project_root=tmp_path, targets=[tmp_path]
        )
        assert used == "full"
        assert source == "--scope=full"

    def test_auto_fallback_to_full(self, tmp_path: Path) -> None:
        """When auto found no .execution_result.json and fell through, label
        the payload so operators know why a full scan happened."""
        args = _args(project_root=tmp_path, scope="auto")
        used, source = _classify_scope_used(
            args, project_root=tmp_path, targets=[tmp_path]
        )
        assert used == "full"
        assert "auto_fallback" in source

    def test_auto_with_evidence(self, tmp_path: Path) -> None:
        args = _args(project_root=tmp_path, scope="auto")
        used, source = _classify_scope_used(
            args, project_root=tmp_path, targets=[tmp_path / "a.py"]
        )
        assert used == "changed"
        assert ".execution_result.json" in source
