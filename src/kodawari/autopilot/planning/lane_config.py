"""Lane preset configurations for tier-based workflow policies.

A lane is a user-facing product concept that maps to a set of preset values
fed into WorkflowPolicy. Three lanes:
  - LITE     : trivial change (single helper, small bug, doc edit)
  - STANDARD : middle of the road; does NOT include eval/release approval
               (those belong to HEAVY only — STANDARD must stay light)
  - HEAVY    : architectural / contract / safety / security; full pipeline
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True)
class LaneConfig:
    """A lane preset. Inputs to WorkflowPolicy resolver."""

    name: str

    review_max_rounds: int
    review_blocking_threshold: frozenset[str]
    require_self_review: bool

    task_cycle_enabled: bool
    parallel_runtime_enabled: bool

    release_tail_enabled: bool
    release_approval_required: bool

    telemetry_enabled: bool
    eval_required: bool

    artifact_profile: str
    suppressed_artifacts: frozenset[str]

    compact_context_enabled: bool

    decision_policy: str


_LITE_SUPPRESSED: Final[frozenset[str]] = frozenset({
    "DESIGN.md",
    "QA_REPORT.md",
    "RELEASE.md",
    "STATUS.md",
    "COMPACT_CONTEXT.md",
    "semantic_compact.json",
    "semantic_compact.md",
    ".qa_report.json",
    ".ship_readiness.json",
    ".status_snapshot.json",
})

_STANDARD_SUPPRESSED: Final[frozenset[str]] = frozenset({
    "QA_REPORT.md",
    "RELEASE.md",
    ".ship_readiness.json",
    "COMPACT_CONTEXT.md",
    "semantic_compact.json",
})


LITE_LANE: Final[LaneConfig] = LaneConfig(
    name="lite",
    # Lite stays cheap, but a single semantic review must-fix often needs more
    # than one fix attempt because reviewer feedback is rarely complete on the
    # first pass. The 5-run stability harness on 2026-05-10 showed one run cut
    # off at the round limit; bumping from 2 to 3 gives the executor one extra
    # fix opportunity while preserving the monotonic ``lite ≤ standard ≤ heavy``
    # invariant (standard=2, heavy=3 elsewhere — lite stays at-or-below).
    # ``loop_runner._reviewer_drift_result`` bounds downside: if the reviewer
    # raises a *different* must_fix signature for 2 consecutive rounds
    # (Jaccard < 0.5 — substantial topic shift), we terminate as
    # REVIEWER_DRIFT_DETECTED before exhausting the round budget.
    review_max_rounds=3,
    review_blocking_threshold=frozenset({"blocking"}),
    require_self_review=False,
    task_cycle_enabled=False,
    parallel_runtime_enabled=False,
    release_tail_enabled=False,
    release_approval_required=False,
    telemetry_enabled=False,
    eval_required=False,
    artifact_profile="minimal",
    suppressed_artifacts=_LITE_SUPPRESSED,
    compact_context_enabled=False,
    decision_policy="auto-skip",
)

STANDARD_LANE: Final[LaneConfig] = LaneConfig(
    name="standard",
    review_max_rounds=2,
    review_blocking_threshold=frozenset({"blocking", "critical"}),
    require_self_review=True,
    task_cycle_enabled=False,
    parallel_runtime_enabled=False,
    release_tail_enabled=False,
    release_approval_required=False,
    telemetry_enabled=False,
    eval_required=False,
    artifact_profile="standard",
    suppressed_artifacts=_STANDARD_SUPPRESSED,
    compact_context_enabled=False,
    decision_policy="soft-gate",
)

HEAVY_LANE: Final[LaneConfig] = LaneConfig(
    name="heavy",
    review_max_rounds=3,
    review_blocking_threshold=frozenset({"blocking", "critical", "high"}),
    require_self_review=True,
    task_cycle_enabled=True,
    parallel_runtime_enabled=True,
    release_tail_enabled=True,
    release_approval_required=True,
    telemetry_enabled=True,
    eval_required=True,
    artifact_profile="full",
    suppressed_artifacts=frozenset(),
    compact_context_enabled=True,
    decision_policy="approval-required",
)


_LANES_BY_NAME: Final[dict[str, LaneConfig]] = {
    "lite": LITE_LANE,
    "standard": STANDARD_LANE,
    "heavy": HEAVY_LANE,
}


def lane_for(name: str | None) -> LaneConfig:
    """Resolve a lane by name. Falls back to HEAVY for unknown/None.

    HEAVY is the safe fallback so back-compat callers (no --tier) keep
    pre-tier behavior.
    """
    key = (name or "").strip().lower()
    return _LANES_BY_NAME.get(key, HEAVY_LANE)


__all__ = [
    "LaneConfig",
    "LITE_LANE",
    "STANDARD_LANE",
    "HEAVY_LANE",
    "lane_for",
]
