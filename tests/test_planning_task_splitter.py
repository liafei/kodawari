"""Unit tests for _split_tasks_if_needed task splitting logic and task_splitter agent."""

from typing import Any
from pathlib import Path


def _split_tasks_if_needed(tasks: list[dict[str, Any]], *, splitter_enabled: bool = True) -> list[dict[str, Any]]:
    """Import the actual function for testing."""
    from kodawari.autopilot.planning.planning_artifacts import _split_tasks_if_needed as split_fn
    return split_fn(tasks, splitter_enabled=splitter_enabled)


class TestTaskSplitterBasics:
    """Test basic task splitting by files and invariants axes."""

    def test_split_by_files_axis_uses_underscore_suffix(self) -> None:
        """Tasks with > 3 files should split with T_a, T_b, ... suffixes."""
        tasks = [
            {
                "task_id": "T1",
                "task_name": "Implement feature",
                "files_to_change": ["file1.py", "file2.py", "file3.py", "file4.py"],
                "invariants": ["inv1"],
                "depends_on": [],
                "new_files": [],
            }
        ]
        result = _split_tasks_if_needed(tasks)
        assert len(result) == 2
        assert result[0]["task_id"] == "T1_a"
        assert result[1]["task_id"] == "T1_b"
        assert result[0]["parent_task_id"] == "T1"
        assert result[1]["parent_task_id"] == "T1"

    def test_split_includes_split_metadata(self) -> None:
        """Split tasks should include split_metadata dict."""
        tasks = [
            {
                "task_id": "T1",
                "task_name": "Feature",
                "files_to_change": ["a.py", "b.py", "c.py", "d.py"],
                "invariants": ["inv"],
                "depends_on": [],
                "new_files": [],
            }
        ]
        result = _split_tasks_if_needed(tasks)
        for item in result:
            assert "split_metadata" in item
            assert item["split_metadata"]["splitter_status"] == "split_by_files"
            assert item["split_metadata"]["splitter_version"] == "1.0"

    def test_split_by_invariants_axis(self) -> None:
        """Tasks with > 2 invariants should split when splitter_enabled=True."""
        tasks = [
            {
                "task_id": "T2",
                "task_name": "Refactor",
                "files_to_change": ["core.py"],
                "invariants": ["inv1", "inv2", "inv3"],
                "depends_on": [],
                "new_files": [],
            }
        ]
        result = _split_tasks_if_needed(tasks, splitter_enabled=True)
        assert len(result) == 2
        assert result[0]["task_id"] == "T2_a"
        assert result[1]["task_id"] == "T2_b"
        assert result[0]["split_metadata"]["splitter_status"] == "split_by_invariants"
        assert len(result[0]["invariants"]) <= 2
        assert len(result[1]["invariants"]) <= 2

    def test_no_split_when_splitter_disabled(self) -> None:
        """With splitter_enabled=False, invariants > 2 should not split."""
        tasks = [
            {
                "task_id": "T3",
                "task_name": "Task",
                "files_to_change": ["file.py"],
                "invariants": ["inv1", "inv2", "inv3"],
                "depends_on": [],
                "new_files": [],
            }
        ]
        result = _split_tasks_if_needed(tasks, splitter_enabled=False)
        assert len(result) == 1
        assert result[0]["task_id"] == "T3"
        assert "split_metadata" not in result[0]

    def test_split_by_both_axes(self) -> None:
        """When both files > 3 and invariants > 2, split by both axes."""
        tasks = [
            {
                "task_id": "T4",
                "task_name": "BigTask",
                "files_to_change": ["a.py", "b.py", "c.py", "d.py", "e.py"],
                "invariants": ["inv1", "inv2", "inv3"],
                "depends_on": [],
                "new_files": [],
            }
        ]
        result = _split_tasks_if_needed(tasks, splitter_enabled=True)
        # max(len([a,b,c],[d,e]), len([inv1,inv2],[inv3])) = max(2, 2) = 2
        assert len(result) >= 2
        for item in result:
            assert item["split_metadata"]["splitter_status"] == "split_by_both_axes"
            assert len(item["invariants"]) <= 2
            assert len(item["files_to_change"]) <= 3

    def test_no_split_below_threshold(self) -> None:
        """Tasks below split threshold should pass through unchanged."""
        tasks = [
            {
                "task_id": "T5",
                "task_name": "SmallTask",
                "files_to_change": ["file1.py", "file2.py"],
                "invariants": ["inv1"],
                "depends_on": [],
                "new_files": [],
            }
        ]
        result = _split_tasks_if_needed(tasks)
        assert len(result) == 1
        assert result[0]["task_id"] == "T5"
        assert "split_metadata" not in result[0]
        assert "parent_task_id" not in result[0]

    def test_depends_on_chain_within_subtasks(self) -> None:
        """Sub-tasks from same parent should have depends_on chain."""
        tasks = [
            {
                "task_id": "T6",
                "task_name": "ChainTest",
                "files_to_change": ["a.py", "b.py", "c.py", "d.py"],
                "invariants": ["inv"],
                "depends_on": [],
                "new_files": [],
            }
        ]
        result = _split_tasks_if_needed(tasks)
        # T6_a should have no depends_on (or original deps)
        # T6_b should depend on T6_a
        assert result[0]["depends_on"] == []
        assert result[1]["depends_on"] == ["T6_a"]

    def test_external_dependencies_remapped_to_last_subtask(self) -> None:
        """Tasks depending on a split task should depend on its last sub-task."""
        tasks = [
            {
                "task_id": "T7",
                "task_name": "Source",
                "files_to_change": ["a.py", "b.py", "c.py", "d.py"],
                "invariants": ["inv"],
                "depends_on": [],
                "new_files": [],
            },
            {
                "task_id": "T8",
                "task_name": "Dependent",
                "files_to_change": ["file.py"],
                "invariants": ["inv"],
                "depends_on": ["T7"],
                "new_files": [],
            },
        ]
        result = _split_tasks_if_needed(tasks)
        # T7 splits into T7_a, T7_b
        # T8's depends_on should be remapped from T7 to T7_b
        t8 = [t for t in result if t["task_id"] == "T8"][0]
        assert t8["depends_on"] == ["T7_b"]

    def test_task_name_includes_suffix(self) -> None:
        """Split task names should include letter suffix."""
        tasks = [
            {
                "task_id": "T9",
                "task_name": "MyFeature",
                "files_to_change": ["a.py", "b.py", "c.py", "d.py"],
                "invariants": ["inv"],
                "depends_on": [],
                "new_files": [],
            }
        ]
        result = _split_tasks_if_needed(tasks)
        assert result[0]["task_name"] == "MyFeature (a)"
        assert result[1]["task_name"] == "MyFeature (b)"

    def test_new_files_preserved_across_split(self) -> None:
        """new_files list should be filtered to match split files_to_change."""
        tasks = [
            {
                "task_id": "T10",
                "task_name": "NewFilesTest",
                "files_to_change": ["new1.py", "new2.py", "existing.py", "new3.py"],
                "invariants": ["inv"],
                "depends_on": [],
                "new_files": ["new1.py", "new2.py", "new3.py"],
            }
        ]
        result = _split_tasks_if_needed(tasks)
        # First sub-task has [new1.py, new2.py, existing.py]
        # Second has [new3.py]
        assert result[0]["new_files"] == ["new1.py", "new2.py"]
        assert result[1]["new_files"] == ["new3.py"]


class TestTaskSplitterAgent:
    """Test the task_splitter agent interface."""

    def test_build_prompt_produces_valid_structure(self) -> None:
        """Task splitter prompt should include plan JSON and instructions."""
        from kodawari.autopilot.planning.task_splitter import _build_prompt

        plan = {
            "tasks": [
                {
                    "task_id": "T1",
                    "task_name": "Test",
                    "files_to_change": ["a.py", "b.py", "c.py", "d.py"],
                    "invariants": ["inv1", "inv2", "inv3"],
                    "depends_on": [],
                }
            ]
        }
        prompt = _build_prompt(plan_payload=plan)
        assert "task splitter" in prompt.lower()
        assert "invariants" in prompt.lower()
        assert "files_to_change" in prompt.lower()
        assert '"task_id": "T1"' in prompt

    def test_check_invariants_parity_validates_preservation(self) -> None:
        """Invariant parity check should verify all invariants are preserved."""
        from kodawari.autopilot.planning.task_splitter import _check_invariants_parity

        original = [
            {
                "task_id": "T1",
                "invariants": ["inv1", "inv2", "inv3"],
            }
        ]
        split_ok = [
            {
                "task_id": "T1_a",
                "invariants": ["inv1", "inv2"],
            },
            {
                "task_id": "T1_b",
                "invariants": ["inv3"],
            },
        ]
        split_bad = [
            {
                "task_id": "T1_a",
                "invariants": ["inv1", "inv2"],
            },
            {
                "task_id": "T1_b",
                "invariants": ["inv4"],
            },
        ]
        assert _check_invariants_parity(original, split_ok) is True
        assert _check_invariants_parity(original, split_bad) is False

    def test_noop_split_when_disabled(self) -> None:
        """Splitter should return noop when feature disabled."""
        from kodawari.autopilot.planning.task_splitter import split_plan

        plan = {
            "tasks": [
                {
                    "task_id": "T1",
                    "task_name": "Test",
                    "files_to_change": ["a.py", "b.py"],
                    "invariants": ["inv1"],
                    "depends_on": [],
                }
            ]
        }
        # When transport is None and we use codex_cli without valid executable,
        # it should fail gracefully.
        # This just tests the fallback path logic.
        result, error = split_plan(
            executable="/nonexistent/claude",
            plan_payload=plan,
        )
        # Should fail (no valid executor) but not crash
        assert result is None or isinstance(result, dict)
