from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from kodawari.autopilot.core.collaboration import CollaborationAction
from kodawari.autopilot.engine.loop_runner import run_peer_review_loop


class _State:
    def __init__(self) -> None:
        self.cycle = 0
        self.last_error = ""
        self.last_stage_status = ""
        self.completed: tuple[Any, str] | None = None

    def add_error(self, message: str, **_kwargs: Any) -> None:
        self.last_error = message

    def mark_completed(self, reason: Any, status: str) -> None:
        self.completed = (reason, status)


class _Context:
    def __init__(self, action: CollaborationAction) -> None:
        self.action = action
        self.review_feedback = SimpleNamespace(
            approved=False,
            review_iteration=0,
            must_fix=["fix the review item"],
            gate_recommendation="REVIEW_FIX_REQUIRED",
        )

    def next_action(self) -> CollaborationAction:
        return self.action


class _FakeEngine:
    def __init__(
        self,
        *,
        action: CollaborationAction = CollaborationAction.FIX_ROUND,
        changes_by_call: list[list[str]] | None = None,
        initial_changed_files: list[str] | None = None,
        round_status_by_call: list[str] | None = None,
        runtime_caps: dict[str, int] | None = None,
        stop_after: int | None = None,
    ) -> None:
        self.state = _State()
        self.context = _Context(action)
        self.runtime = SimpleNamespace(
            context=self.context,
            last_changed_files=list(initial_changed_files or []),
            peer_review_policy={"max_rounds": 99},
            round_records=[],
        )
        self.changes_by_call = list(changes_by_call or [])
        self.round_status_by_call = list(round_status_by_call or [])
        self.dispatch_calls = 0
        self.stop_after = stop_after
        self._task_card_payload = {"runtime_caps": dict(runtime_caps or {})}
        self.adapter = SimpleNamespace(config=SimpleNamespace(executor_runtime_caps={}))

    def _create_loop_runtime(self, **_kwargs: Any) -> Any:
        return self.runtime

    def _start_loop_session(self, _runtime: Any) -> None:
        return None

    def _preflight_peer_review(self, _runtime: Any) -> None:
        return None

    def _new_round_record(self, _runtime: Any, _action: CollaborationAction) -> dict[str, Any]:
        return {}

    def _handle_max_cycles(self, _runtime: Any, **_kwargs: Any) -> None:
        return None

    def _dispatch_round_action(self, runtime: Any, **_kwargs: Any) -> dict[str, Any] | None:
        self.dispatch_calls += 1
        if self.dispatch_calls <= len(self.changes_by_call):
            additions = self.changes_by_call[self.dispatch_calls - 1]
        else:
            additions = []
        for path in additions:
            if path not in runtime.last_changed_files:
                runtime.last_changed_files.append(path)
        status = ""
        if self.dispatch_calls <= len(self.round_status_by_call):
            status = self.round_status_by_call[self.dispatch_calls - 1]
        runtime.round_records.append(
            {
                "stage_status": status,
                "details": {
                    "changes": list(additions),
                    "recovery": {"requested": status == "needs_recovery"},
                },
            }
        )
        if self.stop_after is not None and self.dispatch_calls >= self.stop_after:
            return {
                "reason": "DISPATCH_LIMIT",
                "last_error": None,
                "action": None,
                "dispatch_calls": self.dispatch_calls,
                "changed_files": list(runtime.last_changed_files),
            }
        return None

    def _finish_loop(self, runtime: Any, **kwargs: Any) -> dict[str, Any]:
        action = kwargs.get("action")
        return {
            "reason": kwargs.get("reason"),
            "last_error": kwargs.get("last_error"),
            "action": action.value if isinstance(action, CollaborationAction) else action,
            "dispatch_calls": self.dispatch_calls,
            "changed_files": list(runtime.last_changed_files),
        }


def test_peer_review_loop_stops_after_two_zero_write_fix_rounds() -> None:
    engine = _FakeEngine(changes_by_call=[[], []])

    result = run_peer_review_loop(engine, task_label="T1", task_scope=None)

    assert result["reason"] == "EXECUTOR_FIX_ROUND_UNPRODUCTIVE"
    assert result["dispatch_calls"] == 2
    assert engine.state.last_stage_status == "executor_fix_round_unproductive"
    assert "no new file changes" in result["last_error"]


def test_zero_write_detector_counts_per_round_write_events() -> None:
    engine = _FakeEngine(
        initial_changed_files=["src/existing.py"],
        changes_by_call=[[], ["src/new.py"], [], []],
    )

    result = run_peer_review_loop(engine, task_label="T1", task_scope=None)

    assert result["reason"] == "EXECUTOR_FIX_ROUND_UNPRODUCTIVE"
    assert result["dispatch_calls"] == 4
    assert result["changed_files"] == ["src/existing.py", "src/new.py"]


def test_zero_write_detector_treats_rewrite_of_tracked_file_as_progress() -> None:
    engine = _FakeEngine(
        initial_changed_files=["src/existing.py"],
        changes_by_call=[[], ["src/existing.py"], [], []],
    )

    result = run_peer_review_loop(engine, task_label="T1", task_scope=None)

    assert result["reason"] == "EXECUTOR_FIX_ROUND_UNPRODUCTIVE"
    assert result["dispatch_calls"] == 4
    assert result["changed_files"] == ["src/existing.py"]


def test_zero_write_limit_can_be_overridden_by_env(monkeypatch: Any) -> None:
    monkeypatch.setenv("WORKFLOW_UNPRODUCTIVE_FIX_ROUND_LIMIT", "3")
    engine = _FakeEngine(changes_by_call=[[], [], []])

    result = run_peer_review_loop(engine, task_label="T1", task_scope=None)

    assert result["reason"] == "EXECUTOR_FIX_ROUND_UNPRODUCTIVE"
    assert result["dispatch_calls"] == 3


def test_zero_write_limit_can_be_overridden_by_runtime_caps() -> None:
    engine = _FakeEngine(
        action=CollaborationAction.CODEX_FIX,
        changes_by_call=[[]],
        runtime_caps={"max_unproductive_fix_rounds": 1},
    )

    result = run_peer_review_loop(engine, task_label="T1", task_scope=None)

    assert result["reason"] == "EXECUTOR_FIX_ROUND_UNPRODUCTIVE"
    assert result["action"] == CollaborationAction.CODEX_FIX.value
    assert result["dispatch_calls"] == 1


def test_zero_write_detector_does_not_count_executor_recovery_requests() -> None:
    engine = _FakeEngine(
        changes_by_call=[[], [], [], []],
        round_status_by_call=["needs_recovery", "needs_recovery", "", ""],
    )

    result = run_peer_review_loop(engine, task_label="T1", task_scope=None)

    assert result["reason"] == "EXECUTOR_FIX_ROUND_UNPRODUCTIVE"
    assert result["dispatch_calls"] == 4


# ---------------------------------------------------------------------------
# Reviewer-drift detector
#
# Bumping LITE_LANE.review_max_rounds from 2 to 4 gives the executor more
# fix attempts before the round limit fires. The drift detector bounds the
# downside: when the reviewer raises a *different* must_fix signature for
# N consecutive rounds, terminate as REVIEWER_DRIFT_DETECTED instead of
# burning the budget on a moving target.
# ---------------------------------------------------------------------------


class _DriftContext:
    """PEER_REVIEW context whose must_fix list cycles through scripted values.
    When the reviewer approves (script exhausted), next_action switches to
    PROCEED_TO_GATE so the loop can finish."""

    def __init__(self, must_fix_per_call: list[list[str]]) -> None:
        self._must_fix_per_call = list(must_fix_per_call)
        self.review_feedback = SimpleNamespace(
            approved=False,
            review_iteration=0,
            must_fix=[],
            gate_recommendation="REVIEW_FIX_REQUIRED",
        )

    def next_action(self) -> CollaborationAction:
        # Once the reviewer approves, transition out of PEER_REVIEW so the
        # loop's _round_limit_reached/drift checks stop firing.
        if self.review_feedback.approved:
            return CollaborationAction.PROCEED_TO_GATE
        return CollaborationAction.PEER_REVIEW

    def apply_round_result(self, round_index: int) -> None:
        """Set must_fix and approved flags for the round that JUST dispatched.
        Called by ``_DriftEngine._dispatch_round_action`` so the drift detector
        reads the post-dispatch state of THIS round (mirrors real engine
        behavior where the reviewer call sets review_feedback before the
        detector check)."""
        self.review_feedback.review_iteration = round_index + 1
        if round_index < len(self._must_fix_per_call):
            scripted = list(self._must_fix_per_call[round_index])
        else:
            scripted = []
        self.review_feedback.must_fix = scripted
        self.review_feedback.approved = not scripted


class _DriftEngine:
    def __init__(
        self,
        *,
        must_fix_per_call: list[list[str]],
        runtime_caps: dict[str, int] | None = None,
        max_rounds: int = 99,
    ) -> None:
        self.state = _State()
        self.context = _DriftContext(must_fix_per_call)
        self.runtime = SimpleNamespace(
            context=self.context,
            last_changed_files=[],
            peer_review_policy={"max_rounds": max_rounds},
            round_records=[],
            peer_review_summary={},
        )
        self.dispatch_calls = 0
        self._task_card_payload = {"runtime_caps": dict(runtime_caps or {})}
        self.adapter = SimpleNamespace(config=SimpleNamespace(executor_runtime_caps={}))

    def _create_loop_runtime(self, **_kwargs: Any) -> Any:
        return self.runtime

    def _start_loop_session(self, _runtime: Any) -> None:
        return None

    def _preflight_peer_review(self, _runtime: Any) -> None:
        return None

    def _new_round_record(self, _runtime: Any, _action: CollaborationAction) -> dict[str, Any]:
        return {}

    def _handle_max_cycles(self, _runtime: Any, **_kwargs: Any) -> None:
        return None

    def _dispatch_round_action(
        self,
        runtime: Any,
        *,
        action: CollaborationAction,
        round_record: Any | None = None,
    ) -> dict[str, Any] | None:
        if action == CollaborationAction.PROCEED_TO_GATE:
            return {
                "reason": "PROCEED_TO_GATE",
                "last_error": "",
                "action": action.value,
                "dispatch_calls": self.dispatch_calls,
            }
        self.dispatch_calls += 1
        # Set THIS round's reviewer output before the drift detector reads it.
        runtime.context.apply_round_result(self.dispatch_calls - 1)
        runtime.round_records.append(
            {
                "stage_status": "changes_requested" if runtime.context.review_feedback.must_fix else "pass",
                "details": {"changes": []},
            }
        )
        return None

    def _finish_loop(self, runtime: Any, **kwargs: Any) -> dict[str, Any]:
        action = kwargs.get("action")
        return {
            "reason": kwargs.get("reason"),
            "last_error": kwargs.get("last_error"),
            "action": action.value if isinstance(action, CollaborationAction) else action,
            "dispatch_calls": self.dispatch_calls,
        }


def test_reviewer_drift_detector_terminates_when_must_fix_topics_shift_each_round() -> None:
    """Reviewer raises different must_fix topics (token-bag Jaccard < 0.5)
    for 2 consecutive rounds — the executor cannot converge on a moving
    target, so terminate early."""
    engine = _DriftEngine(
        must_fix_per_call=[
            ["scoped tests missing for handler module"],
            ["docstring conventions wrong in service"],   # disjoint topic → drift_count=1
            ["concurrency invariant unclear at queue"],   # disjoint topic → drift_count=2 → trip
        ]
    )

    result = run_peer_review_loop(engine, task_label="T1", task_scope=None)

    assert result["reason"] == "REVIEWER_DRIFT_DETECTED"
    assert engine.state.last_stage_status == "reviewer_drift_detected"
    assert engine.dispatch_calls == 3
    assert "moving target" in result["last_error"]


def test_reviewer_drift_detector_does_not_trip_on_rephrasing() -> None:
    """Token-bag Jaccard treats rephrasing of the same topic as the same
    signature — only a genuine topic shift counts as drift. Two rounds
    of "scoped tests missing" worded differently must NOT trip drift."""
    engine = _DriftEngine(
        must_fix_per_call=[
            ["scoped tests missing for handler module"],
            ["the scoped tests are missing for the handler module"],  # rephrase
            ["please add scoped tests for handler module"],  # rephrase
        ],
        max_rounds=3,
    )

    result = run_peer_review_loop(engine, task_label="T1", task_scope=None)

    # Reviewer never approves; round_limit fires (not drift).
    assert result["reason"] == "COLLABORATION_ROUND_LIMIT"


def test_reviewer_drift_detector_treats_superset_as_stuck_not_drift() -> None:
    """Round 1: must_fix = [A, B]. Round 2: must_fix = [A, B, C].
    The C is new but A and B linger — that is "stuck-with-creep", not
    pure goalpost-moving. Must NOT trip drift."""
    engine = _DriftEngine(
        must_fix_per_call=[
            ["scoped tests missing for handler", "boundary violation in service"],
            [
                "scoped tests missing for handler",
                "boundary violation in service",
                "new docstring inconsistency in queue module",
            ],
        ],
        max_rounds=2,
    )

    result = run_peer_review_loop(engine, task_label="T1", task_scope=None)

    # 2/3 token overlap → similarity above threshold → not drift.
    assert result["reason"] == "COLLABORATION_ROUND_LIMIT"


def test_reviewer_drift_detector_does_not_trigger_when_signature_repeats() -> None:
    """Reviewer raises the SAME must_fix for many rounds — that is the
    'stuck' case the COLLABORATION_ROUND_LIMIT path handles, not drift.
    The drift detector should NOT fire when signatures repeat."""
    engine = _DriftEngine(
        must_fix_per_call=[
            ["fix issue alpha"],
            ["fix issue alpha"],  # same → drift_count stays 0
            ["fix issue alpha"],
            ["fix issue alpha"],
        ],
        max_rounds=4,
    )

    result = run_peer_review_loop(engine, task_label="T1", task_scope=None)

    # Reviewer never approves; round_limit fires, NOT drift.
    assert result["reason"] == "COLLABORATION_ROUND_LIMIT"
    assert engine.state.last_stage_status == "round_limit"


def test_reviewer_drift_detector_resets_on_approval() -> None:
    """An empty must_fix (reviewer approved) should reset the streak."""
    engine = _DriftEngine(
        must_fix_per_call=[
            ["scoped tests missing for handler module"],
            [],                                              # approved → reset
            ["docstring conventions wrong in service"],      # new topic, post-reset → no drift
            ["concurrency invariant unclear at queue"],      # disjoint → drift=1
            [],                                              # approved → reset, finish
        ]
    )

    result = run_peer_review_loop(engine, task_label="T1", task_scope=None)

    # Did NOT trigger drift detector (streak hit 1 then reset).
    assert result["reason"] != "REVIEWER_DRIFT_DETECTED"


def test_reviewer_drift_limit_can_be_overridden_by_env(monkeypatch: Any) -> None:
    monkeypatch.setenv("WORKFLOW_REVIEWER_DRIFT_LIMIT", "1")
    engine = _DriftEngine(
        must_fix_per_call=[
            ["scoped tests missing for handler module"],
            ["docstring conventions wrong in service module"],  # disjoint → drift=1, trip
        ]
    )

    result = run_peer_review_loop(engine, task_label="T1", task_scope=None)

    assert result["reason"] == "REVIEWER_DRIFT_DETECTED"
    assert engine.dispatch_calls == 2


def test_reviewer_drift_limit_can_be_overridden_by_runtime_caps() -> None:
    engine = _DriftEngine(
        must_fix_per_call=[
            ["scoped tests missing for handler module"],
            ["docstring conventions wrong in service module"],
            ["concurrency invariant unclear at queue layer"],
        ],
        runtime_caps={"max_reviewer_drift_rounds": 3},  # require 3 distinct rounds
        # With limit=3 and only 2 distinct-signature transitions, drift does
        # NOT fire; the loop exhausts and falls through to round_limit.
        max_rounds=3,
    )

    result = run_peer_review_loop(engine, task_label="T1", task_scope=None)

    assert result["reason"] == "COLLABORATION_ROUND_LIMIT"


def test_verification_only_context_does_not_trigger_unproductive_fix_limit() -> None:
    """An executor_must_not_edit / verification_only_noop task makes zero file
    changes by design.  The unproductive-fix-round limit must NOT fire even
    after the default 2 consecutive zero-write rounds."""
    engine = _FakeEngine(changes_by_call=[[], [], [], []], stop_after=4)
    engine._task_card_payload["verification_only_noop"] = True

    result = run_peer_review_loop(engine, task_label="T-VONLY", task_scope=None)

    assert result["reason"] == "DISPATCH_LIMIT"
    assert result["dispatch_calls"] == 4
    assert engine.state.last_stage_status != "executor_fix_round_unproductive"
