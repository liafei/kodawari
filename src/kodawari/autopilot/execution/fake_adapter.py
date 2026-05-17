"""Simple fake adapter utilities for reconstructed tests."""

from __future__ import annotations

from dataclasses import dataclass, field

from kodawari.autopilot.execution.execution_artifacts import is_test_environment


class FakeAdapterProductionUseError(RuntimeError):
    """Raised when FakeCodexAdapter is instantiated/used in production.

    The fake adapter exists for unit tests only — its review payload is
    canned, never a real LLM call. No-fake-run policy Fix 2 makes
    production use a loud failure instead of a silent fake-pass.
    """


@dataclass
class FakeAdapterConfig:
    should_succeed: bool = True
    changed_files: list[str] = field(default_factory=lambda: ["src/app.py"])
    error_message: str = "implementation failed"
    review_approved: bool = True
    review_summary: str = "Review approved."
    review_must_fix: list[str] = field(default_factory=list)
    review_should_fix: list[str] = field(default_factory=list)
    review_blocking_items: list[str] = field(default_factory=list)
    review_severity: str = "low"
    review_score: int = 96


class FakeCodexAdapter:
    def __init__(self, config: FakeAdapterConfig | None = None) -> None:
        self.config = config or FakeAdapterConfig()

    def check_health(self) -> tuple[bool, str]:
        return True, "healthy"

    def implement(self, task: str, context: dict[str, object]) -> dict[str, object]:
        del task, context
        if self.config.should_succeed:
            return {"status": "done", "changes": list(self.config.changed_files), "attempt": 1}
        return {"status": "error", "error": self.config.error_message, "changes": [], "attempt": 1}

    def review(
        self,
        *,
        task: str,
        context: dict[str, object],
        changed_files: list[str],
        review_iteration: int = 0,
    ) -> dict[str, object]:
        del task, context, changed_files, review_iteration
        # No-fake-run policy Fix 2: refuse to fabricate a review payload
        # in production. The fake adapter is unit-test only; outside of
        # PYTEST_CURRENT_TEST / WORKFLOW_SDK_TEST_MODE this method must
        # not silently return approved=true with a forged opus label.
        if not is_test_environment():
            raise FakeAdapterProductionUseError(
                "FakeCodexAdapter.review() invoked outside a test environment. "
                "This adapter returns canned data without calling any real LLM. "
                "Set WORKFLOW_SDK_TEST_MODE=1 if this is a deliberate test run, "
                "or configure a real adapter for production."
            )
        # Honest review_runtime block so downstream classifiers can tag
        # this as fake_adapter (review_quality="simulated", fake_evidence=True)
        # instead of being misled by the reviewer="opus" label that
        # callers still expect for boundary-enforcer compatibility.
        fake_runtime = {
            "mode": "fake_adapter",
            "source": "FakeCodexAdapter.review",
            "real_requested": False,
            "real_required": False,
            "fallback_used": False,
            "error": "",
            "review_quality": "simulated",
            "semantic_review_performed": False,
            "fake_evidence": True,
        }
        return {
            "approved": self.config.review_approved,
            "summary": self.config.review_summary,
            "must_fix": list(self.config.review_must_fix),
            "should_fix": list(self.config.review_should_fix),
            "blocking_items": list(self.config.review_blocking_items),
            "severity": self.config.review_severity,
            "score": self.config.review_score,
            "target_score": 95,
            "min_dimension_score": 80,
            "gate_recommendation": "PROCEED_TO_GATE" if self.config.review_approved else "REVIEW_FIX_REQUIRED",
            "reviewer": "opus",  # kept for enforce_reviewer_boundary compat per spec v4
            "review_runtime": fake_runtime,
        }

    def on_hook_event(
        self,
        *,
        event: str,
        task: str,
        context: dict[str, object],
        payload: dict[str, object],
    ) -> dict[str, object]:
        del context, payload
        return {"status": "ok", "details": {"event": event, "task": task}}
