"""Tests for v5 P4: docs-only-without-test-partner planning-time advisory.

The gate-time short-circuit in v5 P0 lets a docs-only task pass review
even when no test files are in scope — the assumption is a downstream
implementation+test task will follow. P4 adds a defensive plan-time
advisory: if the planner produces a docs-only task in a plan whose other
tasks declare no test files, surface a non-blocking warning so the
planner can revise (add the missing test task) before execution kicks off.

The advisory is pure observation: no severity escalation, no gate
behavior change. It rides on the plan_payload as ``plan_advisories``
and reaches reviewers + the next-round planner via the rendered context.
"""

from __future__ import annotations

from kodawari.autopilot.planning.planning_consistency import (
    detect_docs_only_without_test_coverage,
)


class TestDetectDocsOnlyWithoutTestCoverage:
    def test_returns_empty_for_no_tasks(self) -> None:
        assert detect_docs_only_without_test_coverage({}) == []
        assert detect_docs_only_without_test_coverage({"tasks": []}) == []

    def test_pure_code_plan_no_advisory(self) -> None:
        plan = {
            "tasks": [
                {"task_id": "T1", "files_to_change": ["src/main.py"]},
                {"task_id": "T2", "files_to_change": ["src/utils.py"]},
            ]
        }
        assert detect_docs_only_without_test_coverage(plan) == []

    def test_docs_only_task_without_test_peer_emits_advisory(self) -> None:
        """User's actual T099-A scenario in a plan that forgot the
        T099-B/C implementation+test partner task."""
        plan = {
            "tasks": [{"task_id": "T099-A", "files_to_change": ["docs/任务计划_v1.1.md"]}]
        }
        advisories = detect_docs_only_without_test_coverage(plan)
        assert len(advisories) == 1
        assert "T099-A" in advisories[0]
        assert "docs_only_task_without_test_partner" in advisories[0]

    def test_docs_only_with_downstream_test_partner_no_advisory(self) -> None:
        """The legitimate split: T1 updates the plan markdown; T2 implements
        and adds tests. No warning needed."""
        plan = {
            "tasks": [
                {"task_id": "T1", "files_to_change": ["docs/plan.md"]},
                {
                    "task_id": "T2",
                    "files_to_change": ["src/feature.py"],
                    "new_files": ["tests/test_feature.py"],
                },
            ]
        }
        assert detect_docs_only_without_test_coverage(plan) == []

    def test_docs_only_with_test_in_files_to_change_no_advisory(self) -> None:
        """``files_to_change`` containing a test file also satisfies the
        ``plan_has_test_task`` guard."""
        plan = {
            "tasks": [
                {"task_id": "T1", "files_to_change": ["docs/plan.md"]},
                {"task_id": "T2", "files_to_change": ["tests/test_feature.py"]},
            ]
        }
        assert detect_docs_only_without_test_coverage(plan) == []

    def test_multiple_docs_only_tasks_each_get_advisory(self) -> None:
        plan = {
            "tasks": [
                {"task_id": "TA", "files_to_change": ["docs/a.md"]},
                {"task_id": "TB", "files_to_change": ["README.md"]},
            ]
        }
        advisories = detect_docs_only_without_test_coverage(plan)
        assert len(advisories) == 2
        assert any("TA" in advisory for advisory in advisories)
        assert any("TB" in advisory for advisory in advisories)

    def test_mixed_task_files_not_docs_only(self) -> None:
        """A task that touches both docs and code is not a 'docs-only'
        task — no advisory."""
        plan = {
            "tasks": [
                {"task_id": "T1", "files_to_change": ["docs/plan.md", "src/main.py"]},
            ]
        }
        # Plan has one task, mixed files; no docs-only task detected.
        # Also no test task, but advisory only triggers on docs-only tasks.
        assert detect_docs_only_without_test_coverage(plan) == []

    def test_task_with_no_files_skipped(self) -> None:
        """A task with empty files_to_change is not docs-only and not a
        test task; it's just skipped during detection."""
        plan = {
            "tasks": [
                {"task_id": "T1", "files_to_change": []},
                {"task_id": "T2", "files_to_change": ["docs/plan.md"]},
            ]
        }
        # T1 contributes nothing; T2 is docs-only with no test peer.
        advisories = detect_docs_only_without_test_coverage(plan)
        assert len(advisories) == 1
        assert "T2" in advisories[0]

    def test_requirements_txt_does_not_count_as_docs(self) -> None:
        """``requirements.txt`` looks docs-shaped (.txt) but is not docs.
        A task editing only requirements.txt is not a 'docs-only' task."""
        plan = {
            "tasks": [
                {"task_id": "T1", "files_to_change": ["requirements.txt"]},
            ]
        }
        assert detect_docs_only_without_test_coverage(plan) == []

    def test_fixtures_md_does_not_count_as_docs(self) -> None:
        plan = {
            "tasks": [
                {"task_id": "T1", "files_to_change": ["tests/fixtures/sample.md"]},
            ]
        }
        # tests/fixtures/sample.md is excluded from docs detection AND
        # tests/ prefix makes it a test path → plan has a test task.
        assert detect_docs_only_without_test_coverage(plan) == []
