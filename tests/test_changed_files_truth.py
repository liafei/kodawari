from __future__ import annotations

import json
from pathlib import Path

from kodawari.cli import changed_files_truth


def _write_baseline(planning_dir: Path, *, dirty_files: list[str] | None = None) -> None:
    planning_dir.mkdir(parents=True, exist_ok=True)
    (planning_dir / ".worktree_baseline.json").write_text(
        json.dumps(
            {
                "schema_version": changed_files_truth.WORKTREE_BASELINE_SCHEMA_VERSION,
                "captured_at": "2026-03-28T00:00:00+00:00",
                "feature": planning_dir.name,
                "planning_dir": str(planning_dir),
                "command": "task-run",
                "mode": "warn",
                "status": "PASS",
                "dirty_files": dirty_files or [],
                "tracked_dirty_files": dirty_files or [],
                "untracked_files": [],
                "allowed_files": [],
                "core_dirty_files": [],
                "details": "Worktree clean at baseline capture.",
            }
        ),
        encoding="utf-8",
    )


def test_resolve_task_delta_ignores_runtime_internal_paths(tmp_path: Path, monkeypatch: object) -> None:
    project_root = tmp_path / "repo"
    planning_dir = project_root / "planning" / "feature"
    planning_dir.mkdir(parents=True, exist_ok=True)
    (project_root / "app").mkdir(parents=True, exist_ok=True)
    (project_root / "app" / "main.py").write_text("print('ok')\n", encoding="utf-8")

    monkeypatch.setattr(changed_files_truth, "git_tracked_dirty_files", lambda _root: [])
    monkeypatch.setattr(changed_files_truth, "git_untracked_files", lambda _root: [])
    changed_files_truth.capture_worktree_baseline(
        project_root=project_root,
        planning_dir=planning_dir,
        feature="feature",
        command="task-run",
        mode="warn",
        allowed_files=[],
    )

    monkeypatch.setattr(
        changed_files_truth,
        "git_worktree_changed_files",
        lambda _root: ["app/main.py", ".workflow/instincts.json"],
    )
    changed, source = changed_files_truth.resolve_task_delta_changed_files(
        project_root=project_root,
        planning_dir=planning_dir,
        fallback_candidates=[],
    )

    assert changed == ["app/main.py"]
    assert source == "baseline_delta:git_worktree"


def test_resolve_task_delta_filters_runtime_internal_paths_from_fallback(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    planning_dir = project_root / "planning" / "feature"
    planning_dir.mkdir(parents=True, exist_ok=True)
    service_path = project_root / "app" / "service.py"
    service_path.parent.mkdir(parents=True, exist_ok=True)
    service_path.write_text("print('service')\n", encoding="utf-8")
    baseline_path = planning_dir / ".worktree_baseline.json"
    baseline_path.write_text(
        json.dumps(
            {
                "schema_version": changed_files_truth.WORKTREE_BASELINE_SCHEMA_VERSION,
                "captured_at": "2026-03-28T00:00:00+00:00",
                "feature": "feature",
                "planning_dir": str(planning_dir),
                "command": "task-run",
                "mode": "warn",
                "status": "PASS",
                "dirty_files": [],
                "tracked_dirty_files": [],
                "untracked_files": [],
                "allowed_files": [],
                "core_dirty_files": [],
                "details": "Worktree clean at baseline capture.",
            }
        ),
        encoding="utf-8",
    )

    changed, source = changed_files_truth.resolve_task_delta_changed_files(
        project_root=project_root,
        planning_dir=planning_dir,
        fallback_candidates=[
            (
                "runtime",
                [".workflow/instincts.json", "app/service.py"],
            )
        ],
    )

    assert changed == ["app/service.py"]
    assert source == "runtime:existing"


def test_resolve_task_delta_prefers_executor_truth_over_baseline_noise(tmp_path: Path, monkeypatch: object) -> None:
    project_root = tmp_path / "repo"
    planning_dir = project_root / "planning" / "feature"
    docs_path = project_root / "docs" / "A.md"
    noise_path = project_root / "src" / "B.py"
    docs_path.parent.mkdir(parents=True, exist_ok=True)
    noise_path.parent.mkdir(parents=True, exist_ok=True)
    docs_path.write_text("executor truth\n", encoding="utf-8")
    noise_path.write_text("worktree noise\n", encoding="utf-8")
    _write_baseline(planning_dir)
    monkeypatch.setattr(changed_files_truth, "git_worktree_changed_files", lambda _root: ["src/B.py"])
    diagnostics: list[dict[str, object]] = []

    changed, source = changed_files_truth.resolve_task_delta_changed_files(
        project_root=project_root,
        planning_dir=planning_dir,
        fallback_candidates=[("execution_result", ["docs/A.md"])],
        baseline_diagnostic_callback=diagnostics.append,
    )

    assert changed == ["docs/A.md"]
    assert source == "execution_result:existing"
    assert diagnostics == [
        {
            "code": "baseline_delta_disagrees_with_executor",
            "executor_changed_files": ["docs/A.md"],
            "executor_changed_files_source": "execution_result:existing",
            "baseline_delta": ["src/B.py"],
            "extras_in_baseline_only": ["src/B.py"],
            "missing_in_baseline": ["docs/A.md"],
        }
    ]


def test_resolve_task_delta_uses_baseline_when_executor_truth_missing(tmp_path: Path, monkeypatch: object) -> None:
    project_root = tmp_path / "repo"
    planning_dir = project_root / "planning" / "feature"
    (project_root / "src").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "B.py").write_text("delta\n", encoding="utf-8")
    _write_baseline(planning_dir)
    monkeypatch.setattr(changed_files_truth, "git_worktree_changed_files", lambda _root: ["src/B.py"])

    changed, source = changed_files_truth.resolve_task_delta_changed_files(
        project_root=project_root,
        planning_dir=planning_dir,
        fallback_candidates=[("execution_result", [])],
    )

    assert changed == ["src/B.py"]
    assert source == "baseline_delta:git_worktree"


def test_resolve_task_delta_empty_when_no_truth_or_delta(tmp_path: Path, monkeypatch: object) -> None:
    project_root = tmp_path / "repo"
    planning_dir = project_root / "planning" / "feature"
    _write_baseline(planning_dir)
    monkeypatch.setattr(changed_files_truth, "git_worktree_changed_files", lambda _root: [])

    changed, source = changed_files_truth.resolve_task_delta_changed_files(
        project_root=project_root,
        planning_dir=planning_dir,
        fallback_candidates=[],
    )

    assert changed == []
    assert source == "none"


def test_resolve_task_delta_keeps_git_worktree_as_last_resort(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    planning_dir = project_root / "planning" / "feature"
    file_path = project_root / "src" / "B.py"
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text("delta\n", encoding="utf-8")

    changed, source = changed_files_truth.resolve_task_delta_changed_files(
        project_root=project_root,
        planning_dir=planning_dir,
        fallback_candidates=[("git_worktree", ["src/B.py"])],
    )

    assert changed == ["src/B.py"]
    assert source == "git_worktree:existing"


def test_resolve_task_delta_no_warning_when_executor_and_baseline_agree(tmp_path: Path, monkeypatch: object) -> None:
    project_root = tmp_path / "repo"
    planning_dir = project_root / "planning" / "feature"
    file_path = project_root / "x.py"
    project_root.mkdir(parents=True, exist_ok=True)
    file_path.write_text("delta\n", encoding="utf-8")
    _write_baseline(planning_dir)
    monkeypatch.setattr(changed_files_truth, "git_worktree_changed_files", lambda _root: ["x.py"])
    diagnostics: list[dict[str, object]] = []

    changed, source = changed_files_truth.resolve_task_delta_changed_files(
        project_root=project_root,
        planning_dir=planning_dir,
        fallback_candidates=[("execution_result", ["x.py"])],
        baseline_diagnostic_callback=diagnostics.append,
    )

    assert changed == ["x.py"]
    assert source == "execution_result:existing"
    assert diagnostics == []


def test_resolve_task_delta_replays_sdk_optimization_fixture(tmp_path: Path, monkeypatch: object) -> None:
    fixture_dir = Path(__file__).parent / "fixtures" / "changed_files_truth" / "sdk_optimization_p0p1"
    execution_result = json.loads((fixture_dir / ".execution_result.json").read_text(encoding="utf-8"))
    task_run_result = json.loads((fixture_dir / ".task_run_result.json").read_text(encoding="utf-8"))
    project_root = tmp_path / "repo"
    planning_dir = project_root / "planning" / "sdk-optimization-p0p1"
    doc_path = project_root / "docs" / "ENV_VAR_MIGRATION.md"
    stale_schema_path = project_root / "src" / "kodawari" / "schemas" / "review" / "peer_review_response.schema.json"
    doc_path.parent.mkdir(parents=True, exist_ok=True)
    stale_schema_path.parent.mkdir(parents=True, exist_ok=True)
    doc_path.write_text("executor changed file\n", encoding="utf-8")
    stale_schema_path.write_text("{}\n", encoding="utf-8")
    _write_baseline(planning_dir)
    monkeypatch.setattr(
        changed_files_truth,
        "git_worktree_changed_files",
        lambda _root: list(task_run_result["task_delta_changed_files"]),
    )
    diagnostics: list[dict[str, object]] = []

    changed, source = changed_files_truth.resolve_task_delta_changed_files(
        project_root=project_root,
        planning_dir=planning_dir,
        fallback_candidates=[("execution_result", list(execution_result["changed_files"]))],
        baseline_diagnostic_callback=diagnostics.append,
    )

    assert changed == ["docs/ENV_VAR_MIGRATION.md"]
    assert source == "execution_result:existing"
    assert diagnostics[0]["code"] == "baseline_delta_disagrees_with_executor"
    assert diagnostics[0]["extras_in_baseline_only"] == [
        "src/kodawari/schemas/review/peer_review_response.schema.json"
    ]


def test_filter_planning_dir_paths_drops_all_planning_internals(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    planning_dir = project_root / "planning" / "feature-x"
    planning_dir.mkdir(parents=True, exist_ok=True)
    raw = [
        "backend/main.py",
        "planning/feature-x/.worktree_baseline.json",
        "planning/feature-x/TASK_CARD_ACTIVE.json",
        "planning/feature-x/COMPACT_CONTEXT.md",
        "planning/feature-x/semantic_compact.json",
        "planning/feature-x/.execution_request.json",
        "planning/other-feature/file.json",  # not in the current planning_dir — keep
    ]
    filtered = changed_files_truth.filter_planning_dir_paths(project_root, planning_dir, raw)
    assert filtered == ["backend/main.py", "planning/other-feature/file.json"]


def test_resolve_task_delta_filters_planning_dir_files_from_git_worktree(tmp_path: Path, monkeypatch: object) -> None:
    project_root = tmp_path / "repo"
    planning_dir = project_root / "planning" / "feature-y"
    planning_dir.mkdir(parents=True, exist_ok=True)
    (project_root / "src").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "app.py").write_text("ok\n", encoding="utf-8")

    monkeypatch.setattr(changed_files_truth, "git_tracked_dirty_files", lambda _root: [])
    monkeypatch.setattr(changed_files_truth, "git_untracked_files", lambda _root: [])
    changed_files_truth.capture_worktree_baseline(
        project_root=project_root,
        planning_dir=planning_dir,
        feature="feature-y",
        command="task-run",
        mode="warn",
        allowed_files=[],
    )

    # git now reports planning-dir files AND a real source file dirty
    monkeypatch.setattr(
        changed_files_truth,
        "git_worktree_changed_files",
        lambda _root: [
            "src/app.py",
            "planning/feature-y/.worktree_baseline.json",
            "planning/feature-y/TASK_CARD_ACTIVE.json",
            "planning/feature-y/.autopilot_state.json",
        ],
    )
    changed, source = changed_files_truth.resolve_task_delta_changed_files(
        project_root=project_root,
        planning_dir=planning_dir,
        fallback_candidates=[],
    )
    assert changed == ["src/app.py"]
    assert source == "baseline_delta:git_worktree"


def test_filter_runtime_internal_drops_parallel_workers_segment() -> None:
    raw = [
        "backend/api/v1/services/impact_tags_slugs.py",
        "planning/p1a-event-tagger/.parallel_workers/claude_code/t1-1e900132/src/foo.py",
        "planning/p1a-event-tagger/.parallel_workers/claude_code/t1-1e900132",
        "tests/test_t081_event_tagger_contracts.py",
    ]
    filtered = changed_files_truth.filter_runtime_internal_paths(raw)
    assert filtered == [
        "backend/api/v1/services/impact_tags_slugs.py",
        "tests/test_t081_event_tagger_contracts.py",
    ]


def test_git_diff_filters_paths_outside_project_root(tmp_path: Path, monkeypatch: object) -> None:
    project_root = tmp_path / "watercare-app"
    project_root.mkdir(parents=True, exist_ok=True)
    (project_root / "app").mkdir(parents=True, exist_ok=True)
    (project_root / "app" / "main.py").write_text("print('ok')\n", encoding="utf-8")

    def _fake_run(command: list[str], check: bool, capture_output: bool, text: bool, **kwargs: object) -> object:
        class _Result:
            def __init__(self, stdout: str) -> None:
                self.stdout = stdout

        if command[-2:] == ["rev-parse", "--show-prefix"]:
            return _Result("watercare-app/\n")
        return _Result("watercare-app/app/main.py\nnewsapp/app.py\nkodawari/src/kodawari/cli/review_cmd.py\n")

    monkeypatch.setattr(changed_files_truth.subprocess, "run", _fake_run)

    changed = changed_files_truth.git_base_branch_diff_files(project_root, "main")

    assert changed == ["app/main.py"]
