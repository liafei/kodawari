"""Canonical review-evidence artifact helpers."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from kodawari.infra.artifact_versions import load_versioned_artifact, validate_schema_version
from kodawari.infra.io_atomic import atomic_write_json


REVIEW_EVIDENCE_SCHEMA_VERSION = "review.evidence.v1"
REVIEW_EVIDENCE_FILENAME = ".review_evidence.json"

_SCHEMA_CACHE: dict[str, dict[str, Any]] = {}


class ReviewEvidenceSchemaValidationError(ValueError):
    """Raised when a review evidence payload does not satisfy its schema."""

    def __init__(self, errors: list[dict[str, str]]) -> None:
        super().__init__("review evidence schema validation failed")
        self.errors = errors


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _schema_path() -> Path:
    return Path(__file__).resolve().parents[1] / "schemas" / "observability" / "review_evidence.schema.json"


def _load_schema() -> dict[str, Any]:
    cached = _SCHEMA_CACHE.get("review_evidence")
    if cached is not None:
        return cached
    payload = json.loads(_schema_path().read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"invalid review evidence schema: {_schema_path()}")
    _SCHEMA_CACHE["review_evidence"] = payload
    return payload


def _safe_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _safe_confidence(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _normalize_evidence_item(item: Any) -> dict[str, Any] | None:
    if isinstance(item, dict):
        hit = str(item.get("hit") or "").strip()
        if not hit:
            return None
        payload: dict[str, Any] = {
            "file": str(item.get("file") or "<runtime>").strip() or "<runtime>",
            "rule": str(item.get("rule") or "review_evidence.evidence").strip() or "review_evidence.evidence",
            "hit": hit,
            "confidence": _safe_confidence(item.get("confidence", 1.0)),
        }
        try:
            if item.get("line") is not None and str(item.get("line")).strip():
                payload["line"] = int(item.get("line"))
        except (TypeError, ValueError):
            pass
        metadata = item.get("metadata")
        if isinstance(metadata, dict) and metadata:
            payload["metadata"] = dict(metadata)
        return payload
    text = str(item or "").strip()
    if not text:
        return None
    return {
        "file": "<runtime>",
        "rule": "review_evidence.note",
        "hit": text,
        "confidence": 0.5,
    }


def normalize_review_evidence_payload(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload or {})
    checks = dict(normalized.get("checks") or {})
    evidence = [
        item
        for item in (_normalize_evidence_item(raw) for raw in list(normalized.get("evidence") or []))
        if item is not None
    ]
    blocking_reason = str(normalized.get("blocking_reason") or normalized.get("details") or "").strip()
    details = str(normalized.get("details") or blocking_reason).strip()
    result = {
        **normalized,
        "status": str(normalized.get("status") or "UNKNOWN").strip().upper() or "UNKNOWN",
        "blocking_reason": blocking_reason,
        "checks": {
            **checks,
            "self_review_count": _safe_int(checks.get("self_review_count")),
            "peer_review_count": _safe_int(checks.get("peer_review_count")),
            "must_fix_remaining": _safe_int(checks.get("must_fix_remaining")),
        },
        "issues": [str(item).strip() for item in list(normalized.get("issues") or []) if str(item).strip()],
        "evidence": evidence,
    }
    if details:
        result["details"] = details
    return result


def coerce_review_evidence_payload(
    payload: dict[str, Any] | None,
    *,
    source: str,
    explicit: bool,
) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    normalized = normalize_review_evidence_payload(payload)
    normalized["source"] = str(source).strip()
    normalized["explicit"] = bool(explicit)
    return normalized


def extract_review_evidence_from_compliance_report(
    report: dict[str, Any] | None,
    *,
    source: str,
    explicit: bool,
) -> dict[str, Any] | None:
    if not isinstance(report, dict):
        return None
    checks = report.get("checks")
    if not isinstance(checks, list):
        return None
    for item in checks:
        if not isinstance(item, dict):
            continue
        if str(item.get("check_name") or "").strip() != "review_evidence":
            continue
        payload = {
            "status": str(item.get("status") or "").strip(),
            "blocking_reason": str(item.get("details") or "").strip(),
            "details": str(item.get("details") or "").strip(),
            "issues": [str(value) for value in list(item.get("issues") or []) if str(value).strip()],
            "checks": dict(item.get("checks") or {}),
            "evidence": list(item.get("evidence") or []),
        }
        return coerce_review_evidence_payload(payload, source=source, explicit=explicit)
    return None


def validate_review_evidence_payload(payload: dict[str, Any]) -> dict[str, Any]:
    schema = _load_schema()
    validator = Draft202012Validator(schema)
    errors: list[dict[str, str]] = []
    for error in sorted(validator.iter_errors(payload), key=lambda item: list(item.path)):
        field = ".".join(str(part) for part in error.path) or "<root>"
        errors.append({"field": field, "message": error.message})
    if errors:
        raise ReviewEvidenceSchemaValidationError(errors=errors)
    return payload


def build_review_evidence_artifact(
    *,
    feature: str,
    planning_dir: Path,
    review_evidence: dict[str, Any],
    entrypoint: str,
) -> dict[str, Any]:
    normalized = normalize_review_evidence_payload(review_evidence)
    normalized.pop("explicit", None)
    normalized.pop("source", None)
    artifact = {
        "schema_version": REVIEW_EVIDENCE_SCHEMA_VERSION,
        "generated_at": _utc_now_iso(),
        "feature": str(feature).strip(),
        "planning_dir": str(planning_dir.resolve()),
        "entrypoint": str(entrypoint).strip() or "kodawari review-evidence",
        **normalized,
    }
    validate_review_evidence_payload(artifact)
    return artifact


def load_review_evidence_artifact(path: Path) -> dict[str, Any]:
    payload = load_versioned_artifact(path)
    validate_review_evidence_payload(payload)
    return payload


def write_review_evidence_artifact(path: Path, payload: dict[str, Any]) -> None:
    validate_schema_version(path, payload)
    validate_review_evidence_payload(payload)
    atomic_write_json(path, payload)
