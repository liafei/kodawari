"""Project-local learned prompt lessons.

Prompt lessons are deliberately separate from ``Instinct`` / ``LearnedInstinct``:
those objects describe risky file patterns and are consumed by verify targeting.
This module stores operational prompt guidance as structured template ids so
historical logs never become executable prompt text.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any


PROMPT_LESSON_RELATIVE_PATH = Path(".workflow") / "prompt_lessons.json"
PROMPT_LESSON_THRESHOLD = 2
_SEEN_RUN_IDS_LIMIT = 50

_ROLES: frozenset[str] = frozenset({"planner", "executor", "reviewer", "self_review"})
_CATEGORIES: frozenset[str] = frozenset(
    {
        "planner_shape",
        "planner_revision",
        "planner_task_graph",
        "planner_verify",
        "planner_scope",
        "executor_scope",
        "executor_progress",
        "reviewer_noise",
        "self_review_gap",
    }
)

_TEMPLATES: dict[str, dict[str, str]] = {
    "planner.limit_invariants": {
        "role": "planner",
        "category": "planner_shape",
        "text": "Keep each task's invariants to 5 or fewer high-value items; extra items have needed deterministic truncation before.",
    },
    "planner.change_log_known_task_ref": {
        "role": "planner",
        "category": "planner_revision",
        "text": "When revising a plan, make each change_log target a real task_id and explain only the changed fields.",
    },
    "planner.serialize_parallel_file_conflicts": {
        "role": "planner",
        "category": "planner_task_graph",
        "text": "If multiple tasks write the same file, serialize them with depends_on instead of leaving them parallel.",
    },
    "planner.dedupe_verify_recipes": {
        "role": "planner",
        "category": "planner_verify",
        "text": "Deduplicate verify_recipes before returning the plan.",
    },
    "planner.filter_missing_verify_recipe_roots": {
        "role": "planner",
        "category": "planner_verify",
        "text": "Only include verify_recipes roots that exist in the active workspace.",
    },
    "planner.scope_read_only_files": {
        "role": "planner",
        "category": "planner_scope",
        "text": "For route, handler, schema, or integration changes, include related_existing_tests and read_only_files up front.",
    },
    "executor.read_scope_widening": {
        "role": "executor",
        "category": "executor_scope",
        "text": "When read scope is widened by the runtime, continue within the updated allowed files and avoid repeatedly requesting blocked paths.",
    },
    "executor.no_write_stall": {
        "role": "executor",
        "category": "executor_progress",
        "text": "After identifying the exact old_text for an allowed file, perform the write before continuing broad reads.",
    },
    "planner.stale_contract_tests": {
        "role": "planner",
        "category": "planner_scope",
        "text": "When a behavior or HTTP contract change can stale existing assertions, list every affected legacy test in related_existing_tests and allowed_test_mutations before execution.",
    },
    "executor.stale_contract_assertions": {
        "role": "executor",
        "category": "executor_progress",
        "text": "When verify analysis reports stale literal assertions and the task or recovery card makes those files writable, patch the exact stale assertions before broad rereads.",
    },
    "self_repair.executor_fix_validated": {
        "role": "executor",
        "category": "executor_progress",
        "text": (
            "A kodawari self-repair fix for executor-side runtime behavior was validated end-to-end "
            "(SDK tests + target rerun advanced past the original stop). Trust the corresponding "
            "deterministic recovery path; do not re-introduce broad reread cycles or no-write loops."
        ),
    },
    "self_repair.planner_fix_validated": {
        "role": "planner",
        "category": "planner_shape",
        "text": (
            "A kodawari self-repair fix for planner-side shape was validated end-to-end "
            "(SDK tests + target rerun advanced past the original stop). Continue declaring requires "
            "honestly; the readiness gate now responds correctly to dependency-aware schema mutation."
        ),
    },
    "self_repair.recovery_fix_validated": {
        "role": "executor",
        "category": "executor_progress",
        "text": (
            "A kodawari self-repair fix for the recovery layer was validated end-to-end. Trust "
            "deterministic recovery to converge within recovery_attempts_for_signature; do not assume "
            "the LLM synthesizer will rescue a stalled session."
        ),
    },
    "executor.recovery_no_write_after_scope": {
        "role": "executor",
        "category": "executor_progress",
        "text": "After recovery expands write scope, make the narrow test or contract edit first; do not spend another stall window rereading files already in scope.",
    },
    "executor.tool_call_limit_consolidate": {
        "role": "executor",
        "category": "executor_progress",
        "text": "When a same-path tool-call limit has happened before, read the target once and make one consolidated scoped edit instead of many small retries.",
    },
    "executor.fragmented_read_loop": {
        "role": "executor",
        "category": "executor_progress",
        "text": "When partial reads on one file have stalled before, stop tiny sliding-window reads; use one larger read or patch the known old_text.",
    },
    "executor.pytest_collection_nameerror": {
        "role": "executor",
        "category": "executor_progress",
        "text": "When pytest collection fails on an in-scope NameError, fix the missing symbol or import first, then run the original scoped verify command.",
    },
    "executor.gate_complexity_refactor": {
        "role": "executor",
        "category": "executor_progress",
        "text": "When the rules gate blocks only on function complexity, extract small helpers without changing public behavior or tests.",
    },
    "executor.fix_round_unproductive": {
        "role": "executor",
        "category": "executor_progress",
        "text": (
            "When consecutive peer-review fix rounds make no new file changes, treat it as a real loop "
            "and stop for deterministic repair instead of repeating another no-write round."
        ),
    },
}

_REPAIR_RULE_TO_TEMPLATE: dict[str, str] = {
    "truncate_invariants": "planner.limit_invariants",
    "change_log_known_task_ref": "planner.change_log_known_task_ref",
    "serialize_parallel_file_conflicts": "planner.serialize_parallel_file_conflicts",
    "dedupe_verify_recipes": "planner.dedupe_verify_recipes",
    "filter_missing_verify_recipe_roots": "planner.filter_missing_verify_recipe_roots",
}

_DETERMINISTIC_RECOVERY_ACTION_TO_TEMPLATE: dict[str, str] = {
    "executor_no_write_stall_retry": "executor.no_write_stall",
    "executor_tool_call_limit_retry": "executor.tool_call_limit_consolidate",
    "pytest_collection_nameerror_fix": "executor.pytest_collection_nameerror",
    "gate_complexity_refactor": "executor.gate_complexity_refactor",
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_text(value: Any, *, default: str = "") -> str:
    text = str(value or "").strip()
    return text if text else default


def _to_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _to_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _sanitize_key(value: Any, *, default: str = "") -> str:
    text = _clean_text(value, default=default).lower()
    return "".join(ch if ch.isalnum() or ch in {"_", ".", "-"} else "_" for ch in text)[:80]


def _sanitize_variables(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, Any] = {}
    for raw_key, raw_value in value.items():
        key = _sanitize_key(raw_key)
        if not key:
            continue
        if isinstance(raw_value, (int, float, bool)):
            out[key] = raw_value
        elif isinstance(raw_value, str):
            out[key] = raw_value.replace("\r", " ").replace("\n", " ")[:160]
        elif isinstance(raw_value, list):
            out[key] = [str(item).replace("\r", " ").replace("\n", " ")[:120] for item in raw_value[:5]]
    return out


@dataclass
class PromptLessonCandidate:
    id: str
    signature: str
    role: str
    category: str
    family: str
    template_id: str
    variables: dict[str, Any] = field(default_factory=dict)
    count: int = 0
    distinct_run_count: int = 0
    seen_run_ids: list[str] = field(default_factory=list)
    first_seen: str = field(default_factory=_utc_now_iso)
    last_seen: str = field(default_factory=_utc_now_iso)
    promoted: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "PromptLessonCandidate":
        now = _utc_now_iso()
        return cls(
            id=_clean_text(payload.get("id")),
            signature=_clean_text(payload.get("signature")),
            role=_sanitize_key(payload.get("role"), default="planner"),
            category=_sanitize_key(payload.get("category"), default="planner_shape"),
            family=_sanitize_key(payload.get("family"), default="default"),
            template_id=_sanitize_key(payload.get("template_id")),
            variables=_sanitize_variables(payload.get("variables")),
            count=max(0, _to_int(payload.get("count"), 0)),
            distinct_run_count=max(0, _to_int(payload.get("distinct_run_count"), 0)),
            seen_run_ids=_to_str_list(payload.get("seen_run_ids")),
            first_seen=_clean_text(payload.get("first_seen"), default=now),
            last_seen=_clean_text(payload.get("last_seen"), default=now),
            promoted=_to_bool(payload.get("promoted"), False),
            metadata=_sanitize_variables(payload.get("metadata")),
        )


@dataclass
class LearnedPromptLesson:
    id: str
    signature: str
    role: str
    category: str
    family: str
    template_id: str
    variables: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.75
    count: int = 0
    source: str = "prompt_lesson_learning"
    first_seen: str = field(default_factory=_utc_now_iso)
    last_seen: str = field(default_factory=_utc_now_iso)
    archived: bool = False
    post_promotion_hits: int = 0
    post_promotion_misses: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "LearnedPromptLesson":
        now = _utc_now_iso()
        return cls(
            id=_clean_text(payload.get("id")),
            signature=_clean_text(payload.get("signature")),
            role=_sanitize_key(payload.get("role"), default="planner"),
            category=_sanitize_key(payload.get("category"), default="planner_shape"),
            family=_sanitize_key(payload.get("family"), default="default"),
            template_id=_sanitize_key(payload.get("template_id")),
            variables=_sanitize_variables(payload.get("variables")),
            confidence=_to_float(payload.get("confidence"), 0.75),
            count=max(0, _to_int(payload.get("count"), 0)),
            source=_clean_text(payload.get("source"), default="prompt_lesson_learning"),
            first_seen=_clean_text(payload.get("first_seen"), default=now),
            last_seen=_clean_text(payload.get("last_seen"), default=now),
            archived=_to_bool(payload.get("archived"), False),
            post_promotion_hits=max(0, _to_int(payload.get("post_promotion_hits"), 0)),
            post_promotion_misses=max(0, _to_int(payload.get("post_promotion_misses"), 0)),
            metadata=_sanitize_variables(payload.get("metadata")),
        )


@dataclass
class PromptLessonStoreData:
    schema_version: str = "prompt_lessons.v1"
    prompt_lesson_candidates: list[PromptLessonCandidate] = field(default_factory=list)
    learned_prompt_lessons: list[LearnedPromptLesson] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "prompt_lesson_candidates": [item.to_dict() for item in self.prompt_lesson_candidates],
            "learned_prompt_lessons": [item.to_dict() for item in self.learned_prompt_lessons],
        }


class PromptLessonStore:
    def __init__(self, project_root: Path) -> None:
        self.project_root = Path(project_root).resolve()
        self.path = self.project_root / PROMPT_LESSON_RELATIVE_PATH

    def exists(self) -> bool:
        return self.path.exists()

    def load(self) -> PromptLessonStoreData:
        if not self.path.exists():
            return PromptLessonStoreData()
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("prompt lessons payload must be a JSON object")
        return PromptLessonStoreData(
            schema_version=_clean_text(payload.get("schema_version"), default="prompt_lessons.v1"),
            prompt_lesson_candidates=self._parse_candidates(payload.get("prompt_lesson_candidates")),
            learned_prompt_lessons=self._parse_learned(payload.get("learned_prompt_lessons")),
        )

    def save(self, payload: PromptLessonStoreData) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(payload.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return self.path

    def _parse_candidates(self, rows: Any) -> list[PromptLessonCandidate]:
        if not isinstance(rows, list):
            return []
        items: list[PromptLessonCandidate] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            item = PromptLessonCandidate.from_dict(row)
            if item.id and item.signature and item.template_id in _TEMPLATES:
                items.append(item)
        return items

    def _parse_learned(self, rows: Any) -> list[LearnedPromptLesson]:
        if not isinstance(rows, list):
            return []
        items: list[LearnedPromptLesson] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            item = LearnedPromptLesson.from_dict(row)
            if item.id and item.signature and item.template_id in _TEMPLATES:
                items.append(item)
        return items


def _signature_hash(signature: str) -> str:
    return hashlib.sha1(signature.encode("utf-8", errors="ignore")).hexdigest()[:12]


def _candidate_id(signature: str) -> str:
    return f"prompt-candidate-{_signature_hash(signature)}"


def _learned_id(signature: str) -> str:
    return f"prompt-learned-{_signature_hash(signature)}"


def _confidence_from_count(count: int, threshold: int) -> float:
    if count < threshold:
        return 0.6
    return min(0.95, 0.75 + (max(0, count - threshold) * 0.03))


def _promotion_count(candidate: PromptLessonCandidate) -> int:
    if candidate.distinct_run_count > 0 or candidate.seen_run_ids:
        return int(candidate.distinct_run_count)
    return int(candidate.count)


def _find_candidate(payload: PromptLessonStoreData, candidate_id: str) -> PromptLessonCandidate | None:
    for item in payload.prompt_lesson_candidates:
        if item.id == candidate_id:
            return item
    return None


def _find_learned(payload: PromptLessonStoreData, learned_id: str) -> LearnedPromptLesson | None:
    for item in payload.learned_prompt_lessons:
        if item.id == learned_id:
            return item
    return None


def ingest_prompt_lesson_event(
    project_root: Path,
    event: dict[str, Any],
    *,
    threshold: int = PROMPT_LESSON_THRESHOLD,
) -> dict[str, Any]:
    template_id = _sanitize_key(event.get("template_id"))
    template = _TEMPLATES.get(template_id)
    if template is None:
        return {"updated": False, "reason": "unknown_template"}
    role = _sanitize_key(event.get("role"), default=template["role"])
    category = _sanitize_key(event.get("category"), default=template["category"])
    if role not in _ROLES or category not in _CATEGORIES:
        return {"updated": False, "reason": "unsupported_scope"}
    family = _sanitize_key(event.get("family"), default="default")
    timestamp = _clean_text(event.get("timestamp"), default=_utc_now_iso())
    variables = _sanitize_variables(event.get("variables"))
    metadata = _sanitize_variables(event.get("metadata"))
    signature = f"{role}:{category}:{family}:{template_id}"

    store = PromptLessonStore(project_root)
    payload = store.load()
    candidate_id = _candidate_id(signature)
    candidate = _find_candidate(payload, candidate_id)
    if candidate is None:
        candidate = PromptLessonCandidate(
            id=candidate_id,
            signature=signature,
            role=role,
            category=category,
            family=family,
            template_id=template_id,
            variables=variables,
            first_seen=timestamp,
            last_seen=timestamp,
            metadata=metadata,
        )
        payload.prompt_lesson_candidates.append(candidate)

    candidate.count = int(candidate.count) + 1
    candidate.last_seen = timestamp
    if variables:
        candidate.variables.update(variables)
    if metadata:
        candidate.metadata.update(metadata)
    run_id = _clean_text(event.get("run_id"))
    if run_id and run_id not in candidate.seen_run_ids:
        if len(candidate.seen_run_ids) < _SEEN_RUN_IDS_LIMIT:
            candidate.seen_run_ids.append(run_id)
        candidate.distinct_run_count = int(candidate.distinct_run_count) + 1

    resolved_threshold = max(2, int(threshold))
    promotion_count = _promotion_count(candidate)
    learned_id = _learned_id(signature)
    learned = _find_learned(payload, learned_id)
    promoted = False
    if promotion_count >= resolved_threshold:
        candidate.promoted = True
        confidence = _confidence_from_count(promotion_count, resolved_threshold)
        if learned is None:
            learned = LearnedPromptLesson(
                id=learned_id,
                signature=signature,
                role=role,
                category=category,
                family=family,
                template_id=template_id,
                variables=dict(candidate.variables),
                confidence=confidence,
                count=promotion_count,
                first_seen=candidate.first_seen,
                last_seen=timestamp,
                archived=False,
                metadata=dict(candidate.metadata),
            )
            payload.learned_prompt_lessons.append(learned)
            promoted = True
        else:
            learned.count = promotion_count
            learned.last_seen = timestamp
            learned.confidence = max(float(learned.confidence), confidence)
            learned.archived = False
            if candidate.variables:
                learned.variables.update(candidate.variables)
            if candidate.metadata:
                learned.metadata.update(candidate.metadata)

    store_path = store.save(payload)
    return {
        "updated": True,
        "store_path": str(store_path),
        "candidate_id": candidate.id,
        "candidate_count": int(candidate.count),
        "candidate_distinct_run_count": int(candidate.distinct_run_count),
        "promotion_count": promotion_count,
        "threshold": resolved_threshold,
        "promoted": promoted,
        "learned_prompt_lesson_id": learned.id if learned is not None else "",
    }


def ingest_deterministic_repair_prompt_lessons(
    project_root: Path,
    repairs: list[dict[str, Any]],
    *,
    family: str = "default",
    run_id: str = "",
    final_status: str = "",
    threshold: int = PROMPT_LESSON_THRESHOLD,
) -> dict[str, Any]:
    status = _clean_text(final_status).lower()
    if status not in {"approved", "auto_skipped"}:
        return {"processed": 0, "promoted": 0, "skipped": "final_status_not_successful"}
    processed = 0
    promoted = 0
    for repair in list(repairs or []):
        if not isinstance(repair, dict):
            continue
        rule = _clean_text(repair.get("rule"))
        template_id = _REPAIR_RULE_TO_TEMPLATE.get(rule)
        if not template_id:
            continue
        outcome = ingest_prompt_lesson_event(
            project_root,
            {
                "role": "planner",
                "family": family,
                "template_id": template_id,
                "run_id": run_id,
                "variables": {"rule": rule},
                "metadata": {
                    "source": "deterministic_repair",
                    "rule": rule,
                    "final_status": status,
                },
            },
            threshold=threshold,
        )
        if not bool(outcome.get("updated")):
            continue
        processed += 1
        if bool(outcome.get("promoted")):
            promoted += 1
    return {"processed": processed, "promoted": promoted}


def _analysis_has_tier_a_stale_assertion(analysis: list[dict[str, Any]]) -> bool:
    for row in list(analysis or []):
        if not isinstance(row, dict):
            continue
        if row.get("tier") == "A" and row.get("classification") == "stale_assertion_candidate":
            return True
    return False


def _analysis_has_unauthorized_tier_a(analysis: list[dict[str, Any]]) -> bool:
    for row in list(analysis or []):
        if not isinstance(row, dict):
            continue
        if row.get("tier") == "A" and not bool(row.get("authorized_mutation")):
            return True
    return False


def ingest_verify_failure_prompt_lessons(
    project_root: Path,
    analysis: list[dict[str, Any]],
    *,
    executor_family: str = "default",
    run_id: str = "",
    threshold: int = PROMPT_LESSON_THRESHOLD,
) -> dict[str, Any]:
    """Learn from verify analyzer Tier-A stale assertion signals.

    Unauthorized Tier-A failures teach the planner to enumerate stale contract
    tests up front. Any Tier-A stale assertion teaches the executor to patch
    writable stale assertions promptly once the task or recovery card allows it.
    """
    if not _analysis_has_tier_a_stale_assertion(analysis):
        return {"processed": 0, "promoted": 0, "skipped": "no_tier_a_stale_assertion"}
    processed = 0
    promoted = 0
    if _analysis_has_unauthorized_tier_a(analysis):
        planner_outcome = ingest_prompt_lesson_event(
            project_root,
            {
                "role": "planner",
                "family": "default",
                "template_id": "planner.stale_contract_tests",
                "run_id": run_id,
                "metadata": {
                    "source": "verify_failure_analyzer",
                    "tier": "A",
                    "authorized_mutation": False,
                },
            },
            threshold=threshold,
        )
        if bool(planner_outcome.get("updated")):
            processed += 1
            if bool(planner_outcome.get("promoted")):
                promoted += 1
    executor_outcome = ingest_prompt_lesson_event(
        project_root,
        {
            "role": "executor",
            "family": executor_family,
            "template_id": "executor.stale_contract_assertions",
            "run_id": run_id,
            "metadata": {
                "source": "verify_failure_analyzer",
                "tier": "A",
            },
        },
        threshold=threshold,
    )
    if bool(executor_outcome.get("updated")):
        processed += 1
        if bool(executor_outcome.get("promoted")):
            promoted += 1
    return {"processed": processed, "promoted": promoted}


def _stall_error_code(stall_report: dict[str, Any] | None) -> str:
    if not isinstance(stall_report, dict):
        return ""
    return _clean_text(stall_report.get("error_code") or stall_report.get("reason")).upper()


def _stall_no_write_count(stall_report: dict[str, Any] | None) -> int:
    if not isinstance(stall_report, dict):
        return 0
    counters = stall_report.get("counters")
    if not isinstance(counters, dict):
        return 0
    return max(0, _to_int(counters.get("no_write_iterations"), 0))


def ingest_executor_stall_prompt_lessons(
    project_root: Path,
    stall_report: dict[str, Any] | None,
    *,
    executor_family: str = "default",
    run_id: str = "",
    recovery_decision: dict[str, Any] | None = None,
    threshold: int = PROMPT_LESSON_THRESHOLD,
) -> dict[str, Any]:
    """Learn from executor no-write stalls after recovery/scope work."""
    code = _stall_error_code(stall_report)
    if code == "EXECUTOR_STALLED_FRAGMENTED_READS":
        outcome = ingest_prompt_lesson_event(
            project_root,
            {
                "role": "executor",
                "family": executor_family,
                "template_id": "executor.fragmented_read_loop",
                "run_id": run_id,
                "metadata": {
                    "source": "executor_stall_report",
                    "error_code": code,
                },
            },
            threshold=threshold,
        )
        if not bool(outcome.get("updated")):
            return {"processed": 0, "promoted": 0, "skipped": str(outcome.get("reason") or "not_updated")}
        return {"processed": 1, "promoted": 1 if bool(outcome.get("promoted")) else 0}
    if code != "EXECUTOR_STALLED_NO_WRITE_PROGRESS":
        return {"processed": 0, "promoted": 0, "skipped": "not_supported_stall"}
    if _stall_no_write_count(stall_report) <= 0:
        return {"processed": 0, "promoted": 0, "skipped": "no_no_write_counter"}
    decision = recovery_decision if isinstance(recovery_decision, dict) else {}
    outcome = ingest_prompt_lesson_event(
        project_root,
        {
            "role": "executor",
            "family": executor_family,
            "template_id": "executor.recovery_no_write_after_scope",
            "run_id": run_id,
            "metadata": {
                "source": "executor_stall_report",
                "error_code": code,
                "recovery_action": _clean_text(decision.get("action")),
            },
        },
        threshold=threshold,
    )
    if not bool(outcome.get("updated")):
        return {"processed": 0, "promoted": 0, "skipped": str(outcome.get("reason") or "not_updated")}
    return {"processed": 1, "promoted": 1 if bool(outcome.get("promoted")) else 0}


def ingest_deterministic_recovery_prompt_lessons(
    project_root: Path,
    decisions: list[dict[str, Any]],
    *,
    executor_family: str = "default",
    run_id: str = "",
    threshold: int = PROMPT_LESSON_THRESHOLD,
) -> dict[str, Any]:
    """Learn from deterministic recovery decisions after the run passes."""
    processed = 0
    promoted = 0
    events: list[dict[str, Any]] = []
    for raw in list(decisions or []):
        if not isinstance(raw, dict):
            continue
        if str(raw.get("role") or "") != "deterministic_recovery":
            continue
        action = _sanitize_key(raw.get("action"))
        template_id = _DETERMINISTIC_RECOVERY_ACTION_TO_TEMPLATE.get(action)
        if not template_id:
            continue
        outcome = ingest_prompt_lesson_event(
            project_root,
            {
                "role": "executor",
                "family": executor_family,
                "template_id": template_id,
                "run_id": run_id,
                "metadata": {
                    "source": "deterministic_recovery_success",
                    "recovery_action": action,
                    "detector_name": _clean_text(raw.get("detector_name")),
                },
            },
            threshold=threshold,
        )
        if bool(outcome.get("updated")):
            processed += 1
            if bool(outcome.get("promoted")):
                promoted += 1
        events.append(outcome)
    if not events:
        return {"processed": 0, "promoted": 0, "skipped": "no_supported_deterministic_recovery"}
    return {"processed": processed, "promoted": promoted, "events": events}


def select_prompt_lessons(
    project_root: Path,
    *,
    role: str,
    family_candidates: list[str],
    limit: int = 5,
    min_confidence: float = 0.75,
) -> list[dict[str, Any]]:
    store = PromptLessonStore(project_root)
    payload = store.load()
    wanted_role = _sanitize_key(role)
    families = [family for family in [_sanitize_key(item) for item in family_candidates] if family]
    if "default" not in families:
        families.append("default")
    family_rank = {family: index for index, family in enumerate(families)}
    rows: list[LearnedPromptLesson] = []
    for item in payload.learned_prompt_lessons:
        if item.archived or item.role != wanted_role:
            continue
        if item.family not in family_rank:
            continue
        if float(item.confidence) < float(min_confidence):
            continue
        rows.append(item)
    rows.sort(
        key=lambda item: (
            family_rank.get(item.family, 999),
            -float(item.confidence),
            -int(item.count),
            item.template_id,
        )
    )
    return [item.to_dict() for item in rows[: max(0, int(limit))]]


def _template_text(template_id: str) -> str:
    template = _TEMPLATES.get(template_id)
    if not template:
        return ""
    return template["text"]


def render_prompt_lessons_for_prompt(
    project_root: Path,
    *,
    role: str,
    family_candidates: list[str],
    limit: int = 5,
) -> str:
    lessons = select_prompt_lessons(
        project_root,
        role=role,
        family_candidates=family_candidates,
        limit=limit,
        min_confidence=0.75,
    )
    if not lessons:
        return ""
    lines = [
        f"Learned workflow lessons ({role}, advisory only):",
        "- These lessons are structured historical guidance; task_card, allowed files, and validators remain authoritative.",
    ]
    for item in lessons:
        text = _template_text(str(item.get("template_id") or ""))
        if not text:
            continue
        lines.append(
            "- "
            + text
            + f" (source=prompt_lesson; family={item.get('family')}; "
            + f"confidence={float(item.get('confidence') or 0):.2f}; "
            + f"seen={int(item.get('count') or 0)} distinct run(s); "
            + f"last={_clean_text(item.get('last_seen'))})"
        )
    return "\n".join(lines)[:2200]


def learned_prompt_lesson_nudge_policy(
    project_root: Path,
    *,
    family_candidates: list[str],
) -> dict[str, int]:
    lessons = select_prompt_lessons(
        project_root,
        role="executor",
        family_candidates=family_candidates,
        limit=5,
        min_confidence=0.80,
    )
    if not any(item.get("template_id") == "executor.no_write_stall" for item in lessons):
        return {}
    return {"no_write_after_iter": 3}


__all__ = [
    "PROMPT_LESSON_RELATIVE_PATH",
    "PROMPT_LESSON_THRESHOLD",
    "LearnedPromptLesson",
    "PromptLessonCandidate",
    "PromptLessonStore",
    "PromptLessonStoreData",
    "ingest_deterministic_repair_prompt_lessons",
    "ingest_deterministic_recovery_prompt_lessons",
    "ingest_executor_stall_prompt_lessons",
    "ingest_prompt_lesson_event",
    "ingest_verify_failure_prompt_lessons",
    "learned_prompt_lesson_nudge_policy",
    "render_prompt_lessons_for_prompt",
    "select_prompt_lessons",
]
