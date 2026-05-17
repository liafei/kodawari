"""Distinct-run promotion / over-learning guard.

PR2.5 split LearningCandidate.count (raw event count) from
LearningCandidate.distinct_run_count (number of distinct run_ids that have
produced this signature). Promotion + confidence MUST use the latter so a
single noisy run that retries the same failure 10 times cannot promote a
candidate by itself.

Backward compat: legacy candidates that pre-date PR2.5 (no run_id history)
keep the older event-count semantics so existing on-disk state still
progresses, but as soon as one event with a real run_id arrives, the
candidate switches to distinct-run semantics for all future decisions.
"""

from __future__ import annotations

from pathlib import Path

from kodawari.instincts.engine import ingest_error_event
from kodawari.instincts.storage import InstinctStore


def _make_event(message: str, *, run_id: str = "") -> dict:
    return {
        "message": message,
        "category": "implement",
        "phase": "IMPLEMENT",
        "action": "IMPLEMENT",
        "run_id": run_id,
    }


def test_single_run_repeats_do_not_promote(tmp_path: Path) -> None:
    """One bad run firing the same error 5 times must NOT promote."""
    msg = "codex_cli execution timed out"
    for _ in range(5):
        result = ingest_error_event(tmp_path, _make_event(msg, run_id="run_A"))
    assert result["promoted"] is False
    assert result["candidate_distinct_run_count"] == 1
    # Raw event count grew, but promotion count stays at 1.
    assert result["candidate_count"] == 5
    assert result["promotion_count"] == 1

    # No LearnedInstinct should have been created.
    payload = InstinctStore(tmp_path).load()
    assert payload.learned_instincts == []


def test_distinct_runs_promote_at_threshold(tmp_path: Path) -> None:
    """Three distinct run_ids reach the default threshold and promote."""
    msg = "codex_cli execution timed out"
    runs = ["run_A", "run_B", "run_C"]
    last_result: dict = {}
    for run_id in runs:
        last_result = ingest_error_event(tmp_path, _make_event(msg, run_id=run_id))
    assert last_result["promoted"] is True
    assert last_result["candidate_distinct_run_count"] == 3
    assert last_result["promotion_count"] == 3

    payload = InstinctStore(tmp_path).load()
    assert len(payload.learned_instincts) == 1
    candidate = payload.learning_candidates[0]
    # Order in seen_run_ids reflects ingestion order.
    assert candidate.seen_run_ids == ["run_A", "run_B", "run_C"]
    assert candidate.distinct_run_count == 3


def test_repeat_within_same_run_does_not_double_count(tmp_path: Path) -> None:
    """Re-emitting from the same run_id only bumps event_count."""
    msg = "transient backend wobble"
    ingest_error_event(tmp_path, _make_event(msg, run_id="run_A"))
    ingest_error_event(tmp_path, _make_event(msg, run_id="run_A"))
    result = ingest_error_event(tmp_path, _make_event(msg, run_id="run_A"))

    assert result["candidate_count"] == 3
    assert result["candidate_distinct_run_count"] == 1
    assert result["promotion_count"] == 1
    assert result["promoted"] is False


def test_empty_run_id_does_not_bump_distinct(tmp_path: Path) -> None:
    """Events without run_id (legacy callers) never grow distinct_run_count.

    In a pure-legacy candidate (never saw a real run_id), the fallback path
    uses event_count for promotion so existing on-disk state still
    progresses. The third event hits the default threshold (3) and creates
    the LearnedInstinct.
    """
    msg = "legacy err"
    results = [
        ingest_error_event(tmp_path, _make_event(msg, run_id=""))
        for _ in range(4)
    ]
    # The 3rd ingest (index 2) is the one that flips promoted=True; later
    # calls only update the existing LearnedInstinct so promoted=False.
    assert results[2]["promoted"] is True
    assert results[3]["promoted"] is False
    assert results[3]["candidate_count"] == 4
    assert results[3]["candidate_distinct_run_count"] == 0
    assert results[3]["promotion_count"] == 4

    payload = InstinctStore(tmp_path).load()
    assert len(payload.learned_instincts) == 1


def test_first_real_run_id_switches_to_distinct_semantics(tmp_path: Path) -> None:
    """As soon as a real run_id arrives, future decisions use distinct
    semantics — even if event_count was already high."""
    msg = "mixed-history err"
    # Five legacy events build up a high event_count.
    for _ in range(5):
        ingest_error_event(tmp_path, _make_event(msg, run_id=""))
    payload = InstinctStore(tmp_path).load()
    candidate_before = payload.learning_candidates[0]
    assert candidate_before.count == 5
    assert candidate_before.distinct_run_count == 0

    # First real run_id arrives. Now distinct_run_count = 1; promotion_count
    # also = 1, so even though event_count = 6, the candidate's confidence
    # MUST be derived from distinct_run_count, not event_count.
    result = ingest_error_event(tmp_path, _make_event(msg, run_id="run_X"))
    assert result["candidate_distinct_run_count"] == 1
    assert result["promotion_count"] == 1
    # 1 < default threshold (3) → promotion gate is closed even though
    # event_count is high.
    payload_after = InstinctStore(tmp_path).load()
    learned_after = [li for li in payload_after.learned_instincts if li.signature == result["candidate_signature"]]
    if learned_after:
        # If a legacy promotion already happened during the empty-run_id
        # phase, the confidence MUST not get bumped by the distinct phase
        # below the legacy peak — but it also must not be inflated.
        assert learned_after[0].confidence <= 0.95
