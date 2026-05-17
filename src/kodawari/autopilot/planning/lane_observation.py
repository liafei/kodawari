"""Lane observation — post-run evidence for tier self-calibration.

After every autopilot run we capture:
  - what tier the detector predicted
  - what tier the run actually behaved like (from diff size, rounds used,
    blocking findings, gate status, etc.)

Mismatches feed the instincts learning system so future detector runs can
adjust weights for the patterns that mispredicted.

This module ships:
  - LaneObservation dataclass
  - infer_actual_tier(actual_signals) -> tier
  - build_lane_observation(predicted_decision, actual_signals) -> LaneObservation
  - write_lane_observation(planning_dir, observation) -> Path
  - to_learning_event(observation) -> dict suitable for instincts ingestion

Wiring into autopilot_cmd happens in this same commit. Wiring into the
instincts engine (learning_candidate creation) is deferred to a follow-up
commit so we can validate observation collection in isolation first.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kodawari.autopilot.engine.workflow_policy import ComplexityDecision, TierName

LANE_OBSERVATION_FILENAME = ".lane_observation.json"
LANE_OBSERVATION_SCHEMA_VERSION = "lane.observation.v2"


@dataclass(frozen=True)
class ActualRunSignals:
    """Post-run measurements used to infer what tier the work was."""

    diff_loc: int = 0
    files_changed: int = 0
    rounds_used: int = 0
    planning_rounds: int = 0
    execution_rounds: int = 0
    review_rounds: int = 0
    executor_attempts: int = 0
    deterministic_recovery_hits: int = 0
    synthesizer_calls: int = 0
    recovery_pressure: int = 0
    review_must_fix_max: int = 0
    blocking_findings: int = 0
    gate_status: str = ""
    verify_status: str = ""
    escalated: bool = False
    release_decision_required: bool = False
    contract_or_schema_touched: bool = False
    precondition_blocked: bool = False
    tasks_split_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LaneObservation:
    """A single observation pairing predicted tier vs. actual run signals."""

    schema_version: str
    feature: str
    generated_at: str
    predicted_tier: TierName
    classification_source: str
    static_score: int
    llm_used: bool
    risk_flags: tuple[str, ...]
    actual_tier: TierName
    actual_signals: ActualRunSignals
    underclassified: bool
    overclassified: bool
    mismatch: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "feature": self.feature,
            "generated_at": self.generated_at,
            "predicted_tier": self.predicted_tier,
            "classification_source": self.classification_source,
            "static_score": self.static_score,
            "llm_used": self.llm_used,
            "risk_flags": list(self.risk_flags),
            "actual_tier": self.actual_tier,
            "actual_signals": self.actual_signals.to_dict(),
            "underclassified": self.underclassified,
            "overclassified": self.overclassified,
            "mismatch": self.mismatch,
        }


_TIER_ORDER: dict[str, int] = {"lite": 0, "standard": 1, "heavy": 2}


def infer_actual_tier(signals: ActualRunSignals) -> TierName:
    """Infer the tier the run actually behaved like, post-hoc.

    Heavy markers (any one is enough):
      - escalated through reviewer
      - effective planning+review pressure is high
      - diff_loc > 300 OR files_changed > 6
      - >= 3 blocking findings
      - gate failed
      - touched contract/schema/security paths
      - release decision required

    Standard markers (any one):
      - diff_loc > 80 OR files_changed > 3
      - planning/review needed a normal second pass
      - 1-2 blocking findings

    Otherwise: lite.
    """
    has_split_rounds = bool(signals.planning_rounds or signals.execution_rounds or signals.review_rounds)
    effective_rounds = signals.planning_rounds + signals.review_rounds
    if effective_rounds <= 0:
        effective_rounds = signals.rounds_used
    recovery_pressure = signals.recovery_pressure or (
        signals.deterministic_recovery_hits + signals.synthesizer_calls
    )
    if signals.precondition_blocked:
        return "heavy"
    if signals.escalated:
        return "heavy"
    if not has_split_rounds and signals.rounds_used >= 3:
        return "heavy"
    if recovery_pressure >= 3:
        return "heavy"
    if signals.review_rounds > 2 or signals.review_must_fix_max > 3:
        return "heavy"
    if effective_rounds >= 4:
        return "heavy"
    if signals.diff_loc > 300 or signals.files_changed > 6:
        return "heavy"
    if signals.blocking_findings >= 3:
        return "heavy"
    if signals.gate_status.upper() in {"FAIL", "BLOCK", "BLOCKED"}:
        return "heavy"
    if signals.contract_or_schema_touched:
        return "heavy"
    if signals.release_decision_required:
        return "heavy"

    if signals.diff_loc > 80 or signals.files_changed > 3:
        return "standard"
    if effective_rounds >= 2:
        return "standard"
    if recovery_pressure >= 1:
        return "standard"
    if signals.blocking_findings >= 1:
        return "standard"

    return "lite"


def build_lane_observation(
    *,
    feature: str,
    predicted: ComplexityDecision,
    signals: ActualRunSignals,
    timestamp: datetime | None = None,
) -> LaneObservation:
    actual = infer_actual_tier(signals)
    pred_rank = _TIER_ORDER.get(predicted.tier, 0)
    actual_rank = _TIER_ORDER.get(actual, 0)
    return LaneObservation(
        schema_version=LANE_OBSERVATION_SCHEMA_VERSION,
        feature=str(feature or ""),
        generated_at=(timestamp or datetime.now(timezone.utc)).isoformat(),
        predicted_tier=predicted.tier,
        classification_source=predicted.source,
        static_score=predicted.static_score,
        llm_used=predicted.llm_used,
        risk_flags=tuple(predicted.risk_flags),
        actual_tier=actual,
        actual_signals=signals,
        underclassified=actual_rank > pred_rank,
        overclassified=actual_rank < pred_rank,
        mismatch=actual_rank != pred_rank,
    )


def write_lane_observation(
    planning_dir: Path,
    observation: LaneObservation,
    *,
    filename: str = LANE_OBSERVATION_FILENAME,
) -> Path:
    """Persist an observation to planning/<feature>/.lane_observation.json."""
    path = (Path(planning_dir) / filename).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(observation.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def to_learning_event(observation: LaneObservation) -> dict[str, Any]:
    """Map an observation to a learning-event dict suitable for instincts ingestion.

    The actual instincts.engine.ingest_error_event call lands in a follow-up
    commit; this function is the contract today.
    """
    direction = (
        "underclassified" if observation.underclassified
        else "overclassified" if observation.overclassified
        else "match"
    )
    return {
        "category": "lane",
        "phase": "COMPLEXITY_DETECTION",
        "action": direction,
        "message": (
            f"predicted={observation.predicted_tier} "
            f"actual={observation.actual_tier} "
            f"diff_loc={observation.actual_signals.diff_loc} "
            f"files={observation.actual_signals.files_changed} "
            f"blockers={observation.actual_signals.blocking_findings}"
        ),
        "metadata": observation.to_dict(),
    }


__all__ = [
    "ActualRunSignals",
    "LaneObservation",
    "LANE_OBSERVATION_FILENAME",
    "LANE_OBSERVATION_SCHEMA_VERSION",
    "infer_actual_tier",
    "build_lane_observation",
    "write_lane_observation",
    "to_learning_event",
]

