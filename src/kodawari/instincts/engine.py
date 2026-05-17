"""Minimal instinct learning functions for WS-116 absorption and P0 error-learning."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
import re
from typing import Any

import logging

from kodawari.autopilot.model_advisor import suggest_instinct_pattern as _model_suggest
from kodawari.autopilot.review.precheck import is_test_file
from kodawari.instincts.models import (
    Instinct,
    LearnedInstinct,
    LearningCandidate,
)
from kodawari.instincts.storage import InstinctStore

logger = logging.getLogger(__name__)

ERROR_LEARNING_THRESHOLD = 3
_ERROR_LEARNING_CATEGORIES = {
    "setup", "implement", "review", "verify", "gate", "runtime", "external_gateway",
    "lane",  # C7: tier misclassification events from lane_observation
}
# Cap on how many run_ids we keep verbatim per candidate. Beyond this we still
# count distinct runs in ``distinct_run_count`` but stop growing the list, to
# keep the on-disk store small for chronic patterns.
_SEEN_RUN_IDS_LIMIT = 50


# Threshold above which a project-level LearnedInstinct is considered stable
# enough to share with other projects via the global store. Tuned so that a
# pattern needs to survive several distinct runs (PR2.5 distinct-run
# semantics) before we publish it cross-project. With the current
# _confidence_from_count formula:
#   distinct_run_count == threshold (3) → confidence 0.75
#   distinct_run_count == threshold + 4  → confidence 0.87
# 0.85 lands at distinct_run_count = threshold + 3, i.e. 6 distinct runs.
GLOBAL_PROMOTION_CONFIDENCE_THRESHOLD = 0.85

# Categories that describe portable, machine-/account-level failure modes
# rather than repo-specific test or layout problems. Only learned instincts
# whose category is in here can ever reach the global store.
_PORTABLE_CATEGORIES: frozenset[str] = frozenset({
    "runtime",
    "external_gateway",
    "setup",  # e.g. CODEX_CLI_MISSING / CLAUDE_CODE_HOME_INACCESSIBLE
})

# error_code suffixes that signal portable failure shapes. Anything matching
# these is publishable; anything else stays project-local even if confidence
# is high (a high-confidence "tests/test_ranking.py keeps failing" is
# intentionally NOT portable — it is a repo-specific signal).
_PORTABLE_ERROR_CODE_SUFFIXES: tuple[str, ...] = (
    "_TIMEOUT",
    "_MISSING",
    "_START_FAILED",
    "_HOME_INACCESSIBLE",
    "_AUTH_FAILED",
    "_QUOTA_EXCEEDED",
    "_GATEWAY_BLOCKED",
    "_RATE_LIMITED",
)

# Keys in candidate.metadata that we carry forward onto LearnedInstinct.
# error_code is the load-bearing one for portable judgment; backend lets
# downstream UI explain "where this came from".
_LEARNED_METADATA_KEYS: tuple[str, ...] = ("error_code", "backend")


def _carry_learning_metadata(candidate_metadata: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(candidate_metadata, dict):
        return {}
    out: dict[str, Any] = {}
    for key in _LEARNED_METADATA_KEYS:
        value = candidate_metadata.get(key)
        if value not in (None, ""):
            out[key] = value
    return out


def is_portable_learned_instinct(item: LearnedInstinct) -> bool:
    """Return True when ``item`` should be eligible for the global store.

    Decision is rooted in the structured ``error_code`` carried in metadata
    (PR3) — repo-specific test/file glob patterns can NOT pass this check
    even at high confidence.
    """
    if item.category not in _PORTABLE_CATEGORIES:
        return False
    error_code = str(item.metadata.get("error_code") or "").strip().upper()
    if not error_code:
        return False
    return any(error_code.endswith(suffix) for suffix in _PORTABLE_ERROR_CODE_SUFFIXES)


def _maybe_promote_to_global(learned: LearnedInstinct) -> None:
    """Publish ``learned`` into the cross-project store when it qualifies.

    Failures here NEVER bubble up — global promotion is best-effort and must
    not block local learning under any circumstance.
    """
    try:
        if float(learned.confidence) < GLOBAL_PROMOTION_CONFIDENCE_THRESHOLD:
            return
        if not is_portable_learned_instinct(learned):
            return
        from kodawari.instincts.global_store import GlobalInstinctStore
        GlobalInstinctStore().upsert_learned(learned)
    except Exception:  # noqa: BLE001 - never crash project ingest on global problems
        logger.warning("global promotion skipped due to error", exc_info=True)


def _promotion_count(candidate: LearningCandidate) -> int:
    """Return the count that drives promotion / confidence decisions.

    Once any event has carried a real ``run_id`` (so ``distinct_run_count`` or
    ``seen_run_ids`` is non-empty), we switch to distinct-run semantics — a
    single noisy run firing 10 events stays at distinct_run_count=1. Purely
    legacy candidates that pre-date PR2.5 (no run_id history at all) keep the
    older event-count semantics so existing on-disk state still progresses.
    """
    if candidate.distinct_run_count > 0 or candidate.seen_run_ids:
        return int(candidate.distinct_run_count)
    return int(candidate.count)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_patterns(patterns: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in patterns:
        value = str(raw).strip().replace("\\", "/")
        if not value or value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    return normalized


def _next_instinct_id(items: list[Instinct]) -> str:
    max_suffix = 0
    for item in items:
        if not item.id.startswith("instinct-"):
            continue
        suffix = item.id.removeprefix("instinct-")
        if suffix.isdigit():
            max_suffix = max(max_suffix, int(suffix))
    return f"instinct-{max_suffix + 1}"


def _normalize_error_signature(message: str) -> str:
    text = str(message or "").lower().strip()
    if not text:
        return "(empty-error)"
    patterns = [
        (r"\d{4}-\d{2}-\d{2}t\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:z|[+-]\d{2}:\d{2})?", "<ts>"),
        (r"attempt\s+\d+\s*/\s*\d+", "attempt <n>/<n>"),
        (r"attempt\s+\d+", "attempt <n>"),
        (r"line\s+\d+", "line <n>"),
        (r"cycle\s+\d+", "cycle <n>"),
        (r"\b0x[0-9a-f]+\b", "<hex>"),
        (r"\b\d+\b", "<n>"),
    ]
    normalized = text
    for pattern, replacement in patterns:
        normalized = re.sub(pattern, replacement, normalized)
    return re.sub(r"\s+", " ", normalized).strip()[:300]


def _signature_hash(signature: str, category: str) -> str:
    raw = f"{category}:{signature}".encode("utf-8", errors="ignore")
    return hashlib.sha1(raw).hexdigest()[:12]


def _candidate_id(signature: str, category: str) -> str:
    return f"candidate-{_signature_hash(signature, category)}"


def _learned_id(signature: str, category: str) -> str:
    return f"learned-{_signature_hash(signature, category)}"


def _normalize_error_category(raw: Any) -> str:
    category = str(raw or "").strip().lower()
    return category if category in _ERROR_LEARNING_CATEGORIES else "runtime"


def _normalize_event_text(raw: Any) -> str:
    return str(raw or "").strip()


def _first_test_path(message: str) -> str:
    candidates = re.findall(r"[\w./\\-]+\.py", message)
    for raw in candidates:
        normalized = str(raw).replace("\\", "/").strip("./")
        if is_test_file(normalized):
            return normalized
    return ""


def _suggest_pattern(*, category: str, phase: str, message: str) -> str:
    test_path = _first_test_path(message)
    if test_path:
        return test_path
    lowered = message.lower()
    if category in {"setup", "verify"}:
        return "tests/test_*.py"
    if category == "review":
        return "planning/*"
    if category == "gate":
        if "migration" in lowered or "schema" in lowered:
            return "tests/test_*migration*.py"
        return "tests/test_*.py"
    if "api" in lowered or "endpoint" in lowered:
        return "tests/test_*api*.py"
    if "ranking" in lowered or "score" in lowered:
        return "tests/test_*ranking*.py"
    if "migration" in lowered or "schema" in lowered:
        return "tests/test_*migration*.py"
    if "plan" in phase.lower():
        return "planning/*"
    return "src/**/*.py"


def _confidence_from_count(count: int, threshold: int) -> float:
    if count < threshold:
        return 0.6
    offset = max(0, count - threshold)
    return min(0.95, 0.75 + (offset * 0.03))


def _upsert_instinct(payload: Any, *, pattern: str, category: str, confidence: float) -> Instinct:
    for item in payload.instincts:
        if item.pattern != pattern:
            continue
        item.archived = False
        item.category = category or item.category
        item.confidence = max(float(item.confidence), float(confidence))
        return item
    instinct = Instinct(
        id=_next_instinct_id(payload.instincts),
        pattern=pattern,
        category=category or "recovery",
        confidence=float(confidence),
        archived=False,
    )
    payload.instincts.append(instinct)
    return instinct


def learn_from_globs(project_root: Path, patterns: list[str]) -> dict[str, Any]:
    store = InstinctStore(project_root)
    payload = store.load()
    normalized = _normalize_patterns(patterns)

    by_pattern = {item.pattern: item for item in payload.instincts}
    inserted = 0
    updated = 0

    for pattern in normalized:
        current = by_pattern.get(pattern)
        if current is None:
            instinct = Instinct(
                id=_next_instinct_id(payload.instincts),
                pattern=pattern,
                category="recovery",
                confidence=0.6,
                archived=False,
            )
            payload.instincts.append(instinct)
            by_pattern[pattern] = instinct
            inserted += 1
            continue

        current.archived = False
        current.confidence = max(float(current.confidence), 0.6)
        updated += 1

    store_path = store.save(payload)
    return {
        "project_root": str(Path(project_root).resolve()),
        "patterns": normalized,
        "inserted": inserted,
        "updated": updated,
        "store_path": str(store_path),
    }


def _hints_payload(
    payload: Any,
    *,
    min_confidence: float,
    include_archived: bool,
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}

    def merge_item(item: dict[str, Any]) -> None:
        pattern = str(item.get("pattern") or "").strip()
        if not pattern:
            return
        if bool(item.get("archived")) and not include_archived:
            return
        if float(item.get("confidence", 0.0) or 0.0) < min_confidence:
            return
        existing = merged.get(pattern)
        # On tie, the later-processed item wins. Iteration order is
        # manual instincts -> learned_instincts, so a LearnedInstinct with
        # equal confidence will replace a bare manual entry — preserving
        # signature / source / explanation that the merger downstream
        # needs for global-vs-project conflict detection.
        if existing is None or float(item.get("confidence", 0.0) or 0.0) >= float(existing.get("confidence", 0.0) or 0.0):
            merged[pattern] = item

    for item in payload.instincts:
        merge_item(
            {
                "id": item.id,
                "pattern": item.pattern,
                "category": item.category,
                "confidence": float(item.confidence),
                "archived": bool(item.archived),
                "source": "manual",
            }
        )
    for item in payload.learned_instincts:
        merge_item(
            {
                "id": item.id,
                "pattern": item.pattern,
                "category": item.category,
                "confidence": float(item.confidence),
                "archived": bool(item.archived),
                "source": str(item.source or "error_learning"),
                "signature": item.signature,
                "count": int(item.count),
                "explanation": item.explanation,
            }
        )
    hints = list(merged.values())
    hints.sort(key=lambda row: (-float(row.get("confidence", 0.0) or 0.0), str(row.get("id") or "")))
    return hints


def _global_hint_rows(
    *,
    min_confidence: float,
    include_archived: bool,
) -> list[dict[str, Any]]:
    """Return hint rows sourced from the cross-project global store.

    Failures (missing file, lock contention, corrupt JSON) are silently
    swallowed: a degraded global path must never break the project read.
    """
    try:
        from kodawari.instincts.global_store import GlobalInstinctStore
        items = GlobalInstinctStore().load_learned()
    except Exception:  # noqa: BLE001
        logger.warning("global instincts read skipped due to error", exc_info=True)
        return []
    rows: list[dict[str, Any]] = []
    for item in items:
        if not include_archived and item.archived:
            continue
        if float(item.confidence) < float(min_confidence):
            continue
        rows.append(
            {
                "id": item.id,
                "pattern": item.pattern,
                "category": item.category,
                "confidence": float(item.confidence),
                "archived": bool(item.archived),
                "source": str(item.source or "error_learning"),
                "signature": item.signature,
                "count": int(item.count),
                "explanation": item.explanation,
                "scope": "global",
            }
        )
    return rows


def list_instincts(project_root: Path, min_confidence: float = 0.0, include_archived: bool = False) -> dict[str, Any]:
    payload = InstinctStore(project_root).load()
    threshold = float(min_confidence)
    project_items = _hints_payload(
        payload,
        min_confidence=threshold,
        include_archived=bool(include_archived),
    )
    # Tag project items so the merger can tell them apart from global ones.
    for row in project_items:
        row.setdefault("scope", "project")
    # Collect dedup keys directly from the raw store instead of the merged
    # hints, because _hints_payload deduplicates by pattern and a manual
    # ``Instinct`` (no signature) can shadow a ``LearnedInstinct`` with the
    # same pattern. Without reading the raw payload we'd lose project
    # signatures from the conflict set and global rows would leak through.
    project_signatures: set[str] = {
        str(item.signature).strip()
        for item in payload.learned_instincts
        if str(item.signature).strip()
    }
    project_patterns: set[str] = {
        str(item.pattern).strip()
        for item in payload.learned_instincts
        if str(item.pattern).strip()
    } | {
        str(item.pattern).strip()
        for item in payload.instincts
        if str(item.pattern).strip()
    }
    global_items = _global_hint_rows(
        min_confidence=threshold,
        include_archived=bool(include_archived),
    )
    merged = list(project_items)
    for row in global_items:
        sig = str(row.get("signature") or "").strip()
        pat = str(row.get("pattern") or "").strip()
        if sig and sig in project_signatures:
            continue
        if pat and pat in project_patterns:
            continue
        merged.append(row)
        if sig:
            project_signatures.add(sig)
        if pat:
            project_patterns.add(pat)
    merged.sort(key=lambda row: (-float(row.get("confidence", 0.0) or 0.0), str(row.get("id") or "")))
    return {
        "project_root": str(Path(project_root).resolve()),
        "count": len(merged),
        "min_confidence": threshold,
        "include_archived": include_archived,
        "items": merged,
    }


def select_instinct_hints(project_root: Path, *, limit: int = 5, min_confidence: float = 0.5) -> list[dict[str, Any]]:
    payload = list_instincts(
        project_root,
        min_confidence=min_confidence,
        include_archived=False,
    )
    size = max(0, int(limit))
    return list(payload["items"])[:size]


def _find_candidate(payload: Any, *, candidate_id: str) -> LearningCandidate | None:
    for candidate in payload.learning_candidates:
        if candidate.id == candidate_id:
            return candidate
    return None


def _find_learned(payload: Any, *, learned_id: str) -> LearnedInstinct | None:
    for item in payload.learned_instincts:
        if item.id == learned_id:
            return item
    return None


def ingest_error_event(
    project_root: Path,
    event: dict[str, Any],
    *,
    threshold: int = ERROR_LEARNING_THRESHOLD,
) -> dict[str, Any]:
    store = InstinctStore(project_root)
    payload = store.load()
    message = _normalize_event_text(event.get("message"))
    category = _normalize_error_category(event.get("category"))
    phase = _normalize_event_text(event.get("phase")).upper() or "RUNTIME"
    action = _normalize_event_text(event.get("action"))
    timestamp = _normalize_event_text(event.get("timestamp"))
    if not timestamp:
        timestamp = _utc_now_iso()
    if not message:
        return {
            "project_root": str(Path(project_root).resolve()),
            "updated": False,
            "reason": "empty_message",
        }

    signature = _normalize_error_signature(message)
    candidate_id = _candidate_id(signature, category)
    pattern = _suggest_pattern(category=category, phase=phase, message=message)
    raw_metadata = event.get("metadata")
    event_metadata = dict(raw_metadata) if isinstance(raw_metadata, dict) else {}
    # Capture top-level error_code into candidate metadata so the portable
    # judgment in is_portable_learned_instinct() can read it back without
    # parsing the message — this is the structured handle PR3 / PR4 rely on.
    event_error_code = _normalize_event_text(event.get("error_code"))
    if event_error_code:
        event_metadata.setdefault("error_code", event_error_code)
    candidate = _find_candidate(payload, candidate_id=candidate_id)
    if candidate is None:
        candidate = LearningCandidate(
            id=candidate_id,
            signature=signature,
            category=category,
            phase=phase,
            action=action,
            example_message=message,
            suggested_pattern=pattern,
            count=0,
            first_seen=timestamp,
            last_seen=timestamp,
            promoted=False,
            metadata=dict(event_metadata),
        )
        payload.learning_candidates.append(candidate)

    candidate.count = int(candidate.count) + 1
    candidate.last_seen = timestamp
    # Track distinct run_ids so a noisy single run can never promote on its
    # own. Empty run_id is dropped: we cannot tell whether it is the same
    # session or a brand-new one, so it must not bump distinct_run_count.
    event_run_id = _normalize_event_text(event.get("run_id"))
    if event_run_id and event_run_id not in candidate.seen_run_ids:
        if len(candidate.seen_run_ids) < _SEEN_RUN_IDS_LIMIT:
            candidate.seen_run_ids.append(event_run_id)
        candidate.distinct_run_count = int(candidate.distinct_run_count) + 1
    if event_metadata:
        candidate.metadata.update(event_metadata)
    if not candidate.example_message:
        candidate.example_message = message
    if not candidate.suggested_pattern:
        candidate.suggested_pattern = pattern
    if not candidate.action and action:
        candidate.action = action
    if not candidate.phase and phase:
        candidate.phase = phase

    resolved_threshold = max(2, int(threshold))
    learned_id = _learned_id(signature, category)
    promoted = False
    learned = _find_learned(payload, learned_id=learned_id)
    promotion_count = _promotion_count(candidate)
    if promotion_count >= resolved_threshold:
        candidate.promoted = True
        resolved_pattern = candidate.suggested_pattern or pattern
        confidence = _confidence_from_count(promotion_count, resolved_threshold)
        if learned is None:
            # At first promotion, try model advisor for a higher-quality pattern.
            # Falls back to heuristic silently if advisor is not configured or fails.
            advised_pattern = _model_suggest(
                message=message, category=category, phase=phase
            )
            if advised_pattern:
                logger.debug(
                    "instinct model_advisor: using advised pattern %r (was %r)",
                    advised_pattern,
                    resolved_pattern,
                )
                resolved_pattern = advised_pattern
            learned = LearnedInstinct(
                id=learned_id,
                signature=signature,
                pattern=resolved_pattern,
                category=category,
                confidence=confidence,
                count=promotion_count,
                source="model_advised" if advised_pattern else "error_learning",
                explanation=(
                    f"Model-advised pattern for repeated {category} failures"
                    if advised_pattern
                    else f"Promoted after repeated {category} failures"
                ),
                first_seen=candidate.first_seen,
                last_seen=timestamp,
                archived=False,
                metadata=_carry_learning_metadata(candidate.metadata),
            )
            payload.learned_instincts.append(learned)
            promoted = True
        else:
            learned.count = promotion_count
            learned.last_seen = timestamp
            learned.confidence = max(float(learned.confidence), confidence)
            learned.archived = False
            if resolved_pattern and not learned.pattern:
                learned.pattern = resolved_pattern
            # Carry forward newly-observed structured fields (e.g. an
            # error_code that only became available after first promotion).
            for key, value in _carry_learning_metadata(candidate.metadata).items():
                learned.metadata.setdefault(key, value)
        _upsert_instinct(
            payload,
            pattern=resolved_pattern,
            category=category,
            confidence=confidence,
        )
        _maybe_promote_to_global(learned)

    store_path = store.save(payload)
    return {
        "project_root": str(Path(project_root).resolve()),
        "updated": True,
        "store_path": str(store_path),
        "candidate_id": candidate.id,
        "candidate_signature": signature,
        "candidate_count": int(candidate.count),
        "candidate_distinct_run_count": int(candidate.distinct_run_count),
        "promotion_count": promotion_count,
        "threshold": resolved_threshold,
        "promoted": promoted,
        "learned_instinct_id": learned.id if learned is not None else "",
        "learned_pattern": learned.pattern if learned is not None else "",
    }


def ingest_error_events(
    project_root: Path,
    events: list[dict[str, Any]],
    *,
    threshold: int = ERROR_LEARNING_THRESHOLD,
) -> dict[str, Any]:
    processed = 0
    promoted = 0
    last_store_path = ""
    for event in events:
        if not isinstance(event, dict):
            continue
        outcome = ingest_error_event(project_root, event, threshold=threshold)
        if not bool(outcome.get("updated")):
            continue
        processed += 1
        if bool(outcome.get("promoted")):
            promoted += 1
        last_store_path = str(outcome.get("store_path") or last_store_path)
    return {
        "project_root": str(Path(project_root).resolve()),
        "processed": processed,
        "promoted": promoted,
        "store_path": last_store_path,
    }


LANE_LEARNING_THRESHOLD = 2
_LANE_SCORE_DELTA_OVER = -20
_LANE_SCORE_DELTA_UNDER = +20


def _score_delta_for_lane_event(event: dict[str, Any]) -> int:
    metadata = event.get("metadata") or {}
    if bool(metadata.get("overclassified")):
        return _LANE_SCORE_DELTA_OVER
    if bool(metadata.get("underclassified")):
        return _LANE_SCORE_DELTA_UNDER
    return 0


SUPPORTED_LANE_SCHEMA_PREFIX = "lane.observation.v2"


def _lane_event_schema_version(event: dict[str, Any]) -> str:
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    return _normalize_event_text(metadata.get("schema_version"))


def _lane_event_schema_supported(event: dict[str, Any]) -> bool:
    """Return True when the event carries a supported lane observation schema.

    Empty schema_version is allowed for backward compat with un-stamped events
    emitted before lane observation grew a version field. Anything else must
    match the current lane.observation.v2 family — older v1 payloads are kept
    in the store for audit but never re-promoted.
    """

    version = _lane_event_schema_version(event)
    if not version:
        return True
    return version.startswith(SUPPORTED_LANE_SCHEMA_PREFIX)


def ingest_lane_event(
    project_root: Path,
    event: dict[str, Any],
    *,
    threshold: int = LANE_LEARNING_THRESHOLD,
) -> dict[str, Any]:
    """Ingest a lane observation event; promotes to LearnedInstinct with score_delta in metadata."""
    if not _lane_event_schema_supported(event):
        return {
            "project_root": str(Path(project_root).resolve()),
            "updated": False,
            "reason": "unsupported_schema_version",
            "schema_version": _lane_event_schema_version(event),
        }
    score_delta = _score_delta_for_lane_event(event)
    if score_delta == 0:
        return {"project_root": str(Path(project_root).resolve()), "updated": False, "reason": "no_mismatch"}

    message = _normalize_event_text(event.get("message"))
    if not message:
        return {"project_root": str(Path(project_root).resolve()), "updated": False, "reason": "empty_message"}

    store = InstinctStore(project_root)
    payload = store.load()
    timestamp = _normalize_event_text(event.get("timestamp")) or _utc_now_iso()
    category = "lane"
    event_metadata = dict(event.get("metadata") or {})
    feature_name = str(event_metadata.get("feature") or "").strip()
    mismatch_dir = "under" if score_delta > 0 else "over"
    # Candidate key is (feature, mismatch_direction) so different features never
    # merge even when their mismatch shapes are identical.
    lane_key = f"{feature_name}:{mismatch_dir}" if feature_name else f"{_normalize_error_signature(message)}:{mismatch_dir}"
    signature = lane_key
    candidate_id = _candidate_id(lane_key, category)
    pattern = feature_name or lane_key
    merged_metadata = {**event_metadata, "score_delta": score_delta}

    candidate = _find_candidate(payload, candidate_id=candidate_id)
    if candidate is None:
        candidate = LearningCandidate(
            id=candidate_id,
            signature=signature,
            category=category,
            phase="COMPLEXITY_DETECTION",
            count=0,
            example_message=message,
            suggested_pattern=pattern,
            first_seen=timestamp,
            last_seen=timestamp,
            promoted=False,
            metadata=merged_metadata,
        )
        payload.learning_candidates.append(candidate)

    candidate.count = int(candidate.count) + 1
    candidate.last_seen = timestamp
    candidate.metadata.update(merged_metadata)
    # PR2.5 distinct-run semantics, applied to lane mismatches: a single
    # session that re-runs a feature 3 times must NOT promote on its own.
    # Empty run_id is the legacy / un-stamped fallback — promotion still
    # works via raw event_count for those, mirroring _promotion_count().
    event_run_id = _normalize_event_text(event.get("run_id"))
    if event_run_id and event_run_id not in candidate.seen_run_ids:
        if len(candidate.seen_run_ids) < _SEEN_RUN_IDS_LIMIT:
            candidate.seen_run_ids.append(event_run_id)
        candidate.distinct_run_count = int(candidate.distinct_run_count) + 1

    resolved_threshold = max(1, int(threshold))
    learned_id = _learned_id(signature, category)
    promoted = False
    learned = _find_learned(payload, learned_id=learned_id)
    promotion_count = _promotion_count(candidate)
    if promotion_count >= resolved_threshold:
        candidate.promoted = True
        confidence = _confidence_from_count(promotion_count, resolved_threshold)
        if learned is None:
            learned = LearnedInstinct(
                id=learned_id,
                signature=signature,
                pattern=pattern,
                category=category,
                confidence=confidence,
                count=promotion_count,
                source="lane_learning",
                explanation=f"Lane mismatch: score_delta={score_delta}",
                first_seen=candidate.first_seen,
                last_seen=timestamp,
                archived=False,
                metadata={"score_delta": score_delta},
            )
            payload.learned_instincts.append(learned)
            promoted = True
        else:
            learned.count = promotion_count
            learned.last_seen = timestamp
            learned.confidence = max(float(learned.confidence), confidence)
            learned.archived = False
            learned.metadata["score_delta"] = score_delta

    store_path = store.save(payload)
    return {
        "project_root": str(Path(project_root).resolve()),
        "updated": True,
        "store_path": str(store_path),
        "candidate_id": candidate.id,
        "candidate_count": int(candidate.count),
        "candidate_distinct_run_count": int(candidate.distinct_run_count),
        "promotion_count": promotion_count,
        "threshold": resolved_threshold,
        "promoted": promoted,
        "score_delta": score_delta,
        "learned_instinct_id": learned.id if learned is not None else "",
    }


def load_lane_hints(project_root: Path, *, min_confidence: float = 0.5) -> list[dict[str, Any]]:
    """Load promoted lane hints (with score_delta) for use in complexity detector."""
    store = InstinctStore(project_root)
    payload = store.load()
    hints: list[dict[str, Any]] = []
    for item in payload.learned_instincts:
        if item.category != "lane" or bool(item.archived):
            continue
        if float(item.confidence) < min_confidence:
            continue
        try:
            delta = int(item.metadata.get("score_delta") or 0)
        except (TypeError, ValueError):
            continue
        if delta == 0:
            continue
        hints.append({
            "id": item.id,
            "pattern": item.pattern,
            "category": item.category,
            "confidence": float(item.confidence),
            "score_delta": delta,
            "source": "lane_learning",
            # last_seen lets the detector dedupe opposite-direction hints
            # that share a pattern (over+under both promoted) — without it
            # _apply_learned_adjustments has no way to break the tie and
            # silently sums to net zero.
            "last_seen": str(item.last_seen or ""),
        })
    return hints
