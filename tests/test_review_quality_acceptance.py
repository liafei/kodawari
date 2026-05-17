"""review_quality acceptance matrix.

Centralizes which review_quality values count as a pass for dev iteration
vs the release phase. Locking the matrix here means gate / release flow /
delivery report / dashboards all branch on the same decision.
"""

from __future__ import annotations

import pytest

from kodawari.autopilot.review_runtime_policy import (
    REVIEW_QUALITY_VALUES,
    review_quality_acceptance,
)


def test_real_quality_accepted_everywhere() -> None:
    dev = review_quality_acceptance("real")
    rel = review_quality_acceptance("real", release_phase=True)
    assert dev == {"accept": True, "block_release": False, "reason": "semantic_review_performed"}
    assert rel == {"accept": True, "block_release": False, "reason": "semantic_review_performed"}


def test_simulated_acceptable_for_dev_blocks_release() -> None:
    dev = review_quality_acceptance("simulated")
    assert dev["accept"] is True
    assert dev["block_release"] is True
    assert dev["reason"] == "simulate_local_dev_ok"

    rel = review_quality_acceptance("simulated", release_phase=True)
    assert rel["accept"] is False
    assert rel["block_release"] is True
    assert rel["reason"] == "simulate_local_only"


def test_degraded_never_acceptable() -> None:
    dev = review_quality_acceptance("degraded")
    rel = review_quality_acceptance("degraded", release_phase=True)
    assert dev["accept"] is False
    assert rel["accept"] is False
    assert dev["block_release"] is True
    assert dev["reason"] == "fallback_used"


def test_unavailable_never_acceptable() -> None:
    res = review_quality_acceptance("unavailable")
    assert res["accept"] is False
    assert res["block_release"] is True
    assert res["reason"] == "real_review_unavailable"


@pytest.mark.parametrize("garbage", ["", "REAL", "", "made-up", None])
def test_unknown_quality_blocks(garbage: str | None) -> None:
    res = review_quality_acceptance(garbage or "")
    if garbage in {"", None}:
        assert res["accept"] is False
        assert res["block_release"] is True
    elif garbage == "REAL":
        # Case-insensitive normalization
        assert res["accept"] is True
    else:
        assert res["accept"] is False
        assert res["reason"] == "unknown_quality"


def test_quality_values_constant_is_complete() -> None:
    """If a new review_quality value is added, the matrix must learn about it."""
    assert REVIEW_QUALITY_VALUES == frozenset({"real", "degraded", "simulated", "unavailable"})


def test_release_block_set_implies_dev_acceptable_only_for_simulated() -> None:
    """Audit invariant: a quality that blocks release must either accept in dev
    (simulated) or also reject in dev. degraded/unavailable do both."""
    for quality in ("simulated", "degraded", "unavailable"):
        rel = review_quality_acceptance(quality, release_phase=True)
        dev = review_quality_acceptance(quality)
        assert rel["block_release"] is True
        if quality == "simulated":
            assert dev["accept"] is True
        else:
            assert dev["accept"] is False
