"""Tests for v5 P0: docs-only task short-circuit in deterministic review guard.

The deterministic guard previously fired ``REVIEW_SCOPE_CONFLICT`` whenever a
task changed source files but had no test files in scope. That mis-fires on
docs-first task splits (e.g. ``T099-A`` updates the plan markdown, ``T099-B``
implements + tests). v5 P0 adds a narrow short-circuit: when every changed
file is a docs/markdown artifact AND no other deterministic blocker exists,
the guard treats the missing-tests signal as advisory and approves the task.
The downstream code task is responsible for adding the tests.

A subtle bug in the original draft (caught on sub-agent review) was that
``_resolve_review_approved`` in collaboration_core forces ``approved=False``
when ``must_fix`` or ``blocking_items`` is non-empty. The guard therefore
must explicitly strip the deterministic-injected reason strings before
flipping approved=True; otherwise the approval gets silently reverted.
These tests pin both sides — short-circuit fires AND the cleared lists
survive ``_resolve_review_approved``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kodawari.autopilot.core.collaboration_core import _resolve_review_approved
from kodawari.autopilot.execution.local_adapter_review_runtime import (
    review_test_scope_conflict,
)
from kodawari.autopilot.review.review_precheck import (
    apply_deterministic_review_guard,
    compute_deterministic_findings,
    is_docs_only_path,
)


class TestIsDocsOnlyPath:
    """Strict positive/negative pinning. Path-shape only, no IO."""

    @pytest.mark.parametrize(
        "path",
        [
            "docs/任务计划_v1.1.md",
            "docs/architecture.rst",
            "docs/index.adoc",
            "README.md",
            "CHANGELOG.md",
            "src/foo/notes.md",  # any *.md is docs-only
        ],
    )
    def test_positive_cases(self, path: str) -> None:
        assert is_docs_only_path(path) is True

    @pytest.mark.parametrize(
        "path",
        [
            "src/docs_helper.py",  # substring "docs" elsewhere — not docs
            "src/design_system/button.py",  # design/ as prefix elsewhere — not docs
            "docker/compose.yaml",  # docker prefix shape match — not docs
            "requirements.txt",  # bait: txt requirements file
            "requirements-dev.txt",  # bait variant
            "tests/fixtures/sample.md",  # docs inside tests/fixtures/
            "backend/fixtures/legacy.md",  # fixtures/ anywhere
            "tests/test_main.py",  # plain test file
            "docs/__tests__/example.md",  # nested test dir trumps
            "",  # empty
        ],
    )
    def test_negative_cases(self, path: str) -> None:
        assert is_docs_only_path(path) is False


class TestComputeDeterministicFindingsDocsOnlyChanges:
    def test_all_docs_changed_files(self, tmp_path: Path) -> None:
        planning_dir = tmp_path / "planning" / "demo"
        planning_dir.mkdir(parents=True)
        findings = compute_deterministic_findings(
            planning_dir=planning_dir,
            changed_files=["docs/任务计划_v1.1.md", "README.md"],
            task_card_files=["docs/任务计划_v1.1.md", "README.md"],
            invariants=[],
        )
        assert findings["docs_only_changes"] is True

    def test_mixed_changes_is_not_docs_only(self, tmp_path: Path) -> None:
        planning_dir = tmp_path / "planning" / "demo"
        planning_dir.mkdir(parents=True)
        findings = compute_deterministic_findings(
            planning_dir=planning_dir,
            changed_files=["docs/foo.md", "src/main.py"],
            task_card_files=["docs/foo.md", "src/main.py"],
            invariants=[],
        )
        assert findings["docs_only_changes"] is False

    def test_empty_changes_is_not_docs_only(self, tmp_path: Path) -> None:
        """Zero-change run is NOT docs-only — that path belongs to
        ``review_no_changes`` and must keep its own treatment."""
        planning_dir = tmp_path / "planning" / "demo"
        planning_dir.mkdir(parents=True)
        findings = compute_deterministic_findings(
            planning_dir=planning_dir,
            changed_files=[],
            task_card_files=["docs/x.md"],
            invariants=[],
        )
        assert findings["docs_only_changes"] is False


class TestApplyDeterministicReviewGuardDocsOnly:
    def _findings(
        self,
        *,
        test_scope_unavailable_files: list[str] | None = None,
        docs_only_changes: bool = False,
        out_of_scope_files: list[str] | None = None,
        missing_test_files: list[str] | None = None,
        verify_surface_gaps: list[str] | None = None,
        invariant_conflicts: list[str] | None = None,
    ) -> dict:
        return {
            "schema_version": "review.precheck.v1",
            "out_of_scope_files": out_of_scope_files or [],
            "missing_test_files": missing_test_files or [],
            "test_scope_unavailable_files": test_scope_unavailable_files or [],
            "cross_boundary_files": [],
            "verify_surface_gaps": verify_surface_gaps or [],
            "invariant_conflicts": invariant_conflicts or [],
            "docs_only_changes": docs_only_changes,
        }

    def test_docs_only_short_circuit_approves_and_clears_must_fix(self) -> None:
        review = {"approved": False, "must_fix": [], "blocking_items": [], "should_fix": []}
        findings = self._findings(
            test_scope_unavailable_files=["docs/任务计划_v1.1.md"],
            docs_only_changes=True,
        )

        guarded = apply_deterministic_review_guard(review, deterministic_findings=findings)

        assert guarded["approved"] is True
        assert guarded["gate_recommendation"] == "PROCEED_TO_GATE"
        assert guarded.get("docs_only_proceed") is True
        # The deterministic-injected SCOPE_CONFLICT reason must NOT remain
        # in must_fix/blocking_items, otherwise collaboration_core would
        # flip approved back to False.
        assert guarded["must_fix"] == []
        assert guarded["blocking_items"] == []
        assert any("Docs-only task" in item for item in guarded["should_fix"])
        # Critical: confirm collaboration_core does not revert approved.
        assert (
            _resolve_review_approved(
                guarded,
                must_fix=guarded["must_fix"],
                blocking_items=guarded["blocking_items"],
            )
            is True
        )

    def test_docs_only_does_not_strip_reviewer_supplied_must_fix(self) -> None:
        """Reviewer's own must_fix items must survive the docs-only short-circuit.
        Only the deterministic-injected reason strings get cleared."""
        reviewer_msg = "Update the PRD section header to reflect the new scope."
        review = {
            "approved": False,
            "must_fix": [reviewer_msg],
            "blocking_items": [reviewer_msg],
            "should_fix": [],
        }
        findings = self._findings(
            test_scope_unavailable_files=["docs/任务计划_v1.1.md"],
            docs_only_changes=True,
        )

        guarded = apply_deterministic_review_guard(review, deterministic_findings=findings)

        # Reviewer's own item is preserved.
        assert reviewer_msg in guarded["must_fix"]
        # And because must_fix is still non-empty, _resolve_review_approved
        # rightfully returns False — the docs-only short-circuit did NOT
        # silently bypass reviewer's own findings.
        assert (
            _resolve_review_approved(
                guarded,
                must_fix=guarded["must_fix"],
                blocking_items=guarded["blocking_items"],
            )
            is False
        )

    def test_other_blocker_blocks_short_circuit(self) -> None:
        """When out_of_scope_files (or any other deterministic reason) is
        non-empty, the docs-only short-circuit MUST NOT fire — the guard
        falls through to REVIEW_SCOPE_CONFLICT/REVIEW_FIX_REQUIRED."""
        review = {"approved": False, "must_fix": [], "blocking_items": [], "should_fix": []}
        findings = self._findings(
            test_scope_unavailable_files=["docs/任务计划_v1.1.md"],
            docs_only_changes=True,
            out_of_scope_files=["src/rogue.py"],
        )

        guarded = apply_deterministic_review_guard(review, deterministic_findings=findings)

        assert guarded["approved"] is False
        assert guarded.get("docs_only_proceed") is not True
        assert guarded["gate_recommendation"] == "REVIEW_SCOPE_CONFLICT"

    def test_non_docs_changes_still_scope_conflict(self) -> None:
        review = {"approved": False, "must_fix": [], "blocking_items": [], "should_fix": []}
        findings = self._findings(
            test_scope_unavailable_files=["src/main.py"],
            docs_only_changes=False,
        )

        guarded = apply_deterministic_review_guard(review, deterministic_findings=findings)

        assert guarded["approved"] is False
        assert guarded["gate_recommendation"] == "REVIEW_SCOPE_CONFLICT"


class TestSimulatedReviewMirrorsShortCircuit:
    def test_docs_only_changes_approved_in_simulated_path(self) -> None:
        payload = review_test_scope_conflict(["docs/任务计划_v1.1.md", "README.md"])
        assert payload["approved"] is True
        assert payload["gate_recommendation"] == "PROCEED_TO_GATE"
        assert payload.get("docs_only_proceed") is True
        # Must not produce blocking lists that would re-flip approved.
        assert payload.get("blocking_items", []) == []
        assert payload["must_fix"] == []

    def test_mixed_changes_still_scope_conflict_in_simulated_path(self) -> None:
        payload = review_test_scope_conflict(["docs/foo.md", "src/main.py"])
        assert payload["approved"] is False
        assert payload["gate_recommendation"] == "REVIEW_SCOPE_CONFLICT"
        assert payload.get("docs_only_proceed") is not True

    def test_empty_changes_is_not_docs_only_in_simulated_path(self) -> None:
        """Empty list goes through the original SCOPE_CONFLICT path —
        the short-circuit requires every (non-empty) entry to be docs."""
        payload = review_test_scope_conflict([])
        assert payload["approved"] is False
        assert payload.get("docs_only_proceed") is not True


def test_compute_findings_round_trip_with_real_planning_dir(tmp_path: Path) -> None:
    """End-to-end: build TASK_CARD_ACTIVE, compute findings, apply guard.
    Verifies the actual T099-A scenario."""
    planning_dir = tmp_path / "planning" / "feature"
    planning_dir.mkdir(parents=True)
    (planning_dir / "TASK_CARD_ACTIVE.json").write_text(
        json.dumps(
            {
                "task_id": "T099-A",
                "files_to_change": ["docs/任务计划_v1.1.md"],
            }
        ),
        encoding="utf-8",
    )

    findings = compute_deterministic_findings(
        planning_dir=planning_dir,
        changed_files=["docs/任务计划_v1.1.md"],
        task_card_files=["docs/任务计划_v1.1.md"],
        invariants=[],
    )

    assert findings["docs_only_changes"] is True
    assert findings["test_scope_unavailable_files"] == ["docs/任务计划_v1.1.md"]

    review = {"approved": False, "must_fix": [], "blocking_items": [], "should_fix": []}
    guarded = apply_deterministic_review_guard(review, deterministic_findings=findings)

    assert guarded["approved"] is True
    assert guarded["gate_recommendation"] == "PROCEED_TO_GATE"
    assert guarded["must_fix"] == []
