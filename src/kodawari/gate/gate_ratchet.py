"""Baseline ratchet comparisons for code health snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RatchetRegression:
    metric: str
    baseline: float
    current: float

    @property
    def delta(self) -> float:
        return float(self.current - self.baseline)

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "baseline": self.baseline,
            "current": self.current,
            "delta": self.delta,
        }


@dataclass(frozen=True)
class RatchetResult:
    status: str
    compared_metrics: list[str]
    regressions: list[RatchetRegression]
    improvements: list[RatchetRegression]
    skipped_metrics: list[dict[str, str]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "compared_metrics": list(self.compared_metrics),
            "regressions": [item.to_dict() for item in self.regressions],
            "improvements": [item.to_dict() for item in self.improvements],
            "skipped_metrics": [dict(item) for item in self.skipped_metrics],
            "regression_count": len(self.regressions),
            "improvement_count": len(self.improvements),
        }


def _metric_map(payload: dict[str, Any]) -> dict[str, Any]:
    metrics = payload.get("metrics")
    if isinstance(metrics, dict):
        return dict(metrics)
    return dict(payload)


def _numeric(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def compare_against_baseline(current: dict[str, Any], baseline: dict[str, Any]) -> RatchetResult:
    current_metrics = _metric_map(current)
    baseline_metrics = _metric_map(baseline)
    compared: list[str] = []
    regressions: list[RatchetRegression] = []
    improvements: list[RatchetRegression] = []
    skipped: list[dict[str, str]] = []

    for metric in sorted(set(current_metrics) | set(baseline_metrics)):
        current_value = _numeric(current_metrics.get(metric))
        baseline_value = _numeric(baseline_metrics.get(metric))
        if current_value is None or baseline_value is None:
            skipped.append({"metric": metric, "reason": "non_numeric_or_missing"})
            continue
        compared.append(metric)
        if current_value > baseline_value:
            regressions.append(RatchetRegression(metric=metric, baseline=baseline_value, current=current_value))
        elif current_value < baseline_value:
            improvements.append(RatchetRegression(metric=metric, baseline=baseline_value, current=current_value))

    return RatchetResult(
        status="FAIL" if regressions else "PASS",
        compared_metrics=compared,
        regressions=regressions,
        improvements=improvements,
        skipped_metrics=skipped,
    )


def update_baseline_snapshot(current: dict[str, Any], baseline: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, float]]]:
    updated = dict(baseline)
    updated_metrics = dict(_metric_map(baseline))
    current_metrics = _metric_map(current)
    changes: list[dict[str, float]] = []
    for metric in sorted(set(updated_metrics) | set(current_metrics)):
        current_value = _numeric(current_metrics.get(metric))
        baseline_value = _numeric(updated_metrics.get(metric))
        if current_value is None:
            continue
        if baseline_value is None or current_value < baseline_value:
            updated_metrics[metric] = int(current_value) if current_value.is_integer() else current_value
            changes.append(
                {
                    "metric": metric,
                    "old": baseline_value if baseline_value is not None else -1.0,
                    "new": current_value,
                }
            )
    updated["metrics"] = updated_metrics
    for field in ("generated_at", "source_commit", "tool_versions", "targets", "project_root"):
        if field in current:
            updated[field] = current[field]
    return updated, changes


__all__ = ["RatchetResult", "compare_against_baseline", "update_baseline_snapshot"]
