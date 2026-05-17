"""Complexity detector — produces ComplexityDecision for an autopilot run.

Resolution order (each later step is only reached if earlier returns None):
    1. Explicit user tier (--tier lite|standard|heavy)
    2. Hard rules (forced HEAVY for security/contract/migration/core)
    3. Strong-lite shortcut (single source + test, all-docs)
    4. Static score (weighted features)
    5. Learned adjustments (instincts hints — placeholder until C5)
    6. Score band → lite (<=25) / heavy (>=70)
    7. Gray zone → LLM classifier if provided
    8. Fallback → STANDARD (per safety: never default to lite when uncertain)

LLM classifier is an injected dependency (Callable) — this module ships with
no LLM wiring. Real model_advisor integration lands in C5+.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from kodawari.autopilot.engine.workflow_policy import ComplexityDecision

_logger = logging.getLogger(__name__)

# Phase-A guards on lane learning. With opposite-direction hints (over -20 +
# under +20 for the same pattern) silently summing to zero, and unbounded
# accumulated deltas able to flip tier on their own, the raw scheme produced
# net-zero or wildly unpredictable adjustments. These bound the surface.
_LANE_LEARNING_DISABLED_ENV = "WORKFLOW_LANE_LEARNING_DISABLED"
_LANE_DELTA_CAP = 40


def _lane_learning_disabled() -> bool:
    return os.environ.get(_LANE_LEARNING_DISABLED_ENV, "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


@dataclass(frozen=True)
class ComplexityInput:
    """Inputs the detector consumes. Built from existing intake artifacts."""

    feature: str
    task_direction: str = ""
    requirements_text: str = ""
    source_of_truth_files: tuple[str, ...] = ()
    task_card_files: tuple[str, ...] = ()
    changed_files: tuple[str, ...] = ()
    path_type: str = ""
    layers: tuple[str, ...] = ()
    learned_hints: tuple[dict[str, Any], ...] = ()

    def all_files(self) -> tuple[str, ...]:
        seen: set[str] = set()
        result: list[str] = []
        for source in (self.source_of_truth_files, self.task_card_files, self.changed_files):
            for path in source:
                if path and path not in seen:
                    seen.add(path)
                    result.append(path)
        return tuple(result)


LLMClassifier = Callable[[ComplexityInput, int, tuple[str, ...]], dict[str, Any]]
"""Signature: (input, current_score, reasons) -> {tier, confidence, risk_flags, reason}"""


# ---------------------------------------------------------------------------
# Hard rules — force HEAVY for safety-critical paths/keywords
# ---------------------------------------------------------------------------

_HARD_HEAVY_PATH_TOKENS: tuple[str, ...] = (
    "/migration",
    "migration_sql/",
    "alembic/",
    "/auth_",
    "/credential",
    "/secret",
    "/permission",
    "/payment",
    "/billing",
    "/contract/",
    "/schema/",
    "/executor_backend",
    "/release_runtime",
    "/release_flow",
    "autopilot/gate",
    "autopilot/execution_",
)

_TASK_CARD_HARD_HEAVY_PATH_TOKENS: tuple[str, ...] = (
    "/api/v1/routes/",
)

_HARD_HEAVY_FILE_SUFFIXES: tuple[str, ...] = (
    ".sql",
)

_HARD_HEAVY_KEYWORDS: tuple[str, ...] = (
    "breaking change",
    "breaking_change",
    "redesign",
    "rewrite",
    "schema migration",
    "data migration",
    "public api",
    "public contract",
    "contract change",
    "schema change",
    "auth bypass",
    "permission model",
)


def _hard_path_hit(files: tuple[str, ...], extra_tokens: tuple[str, ...] = ()) -> str:
    for path in files:
        path_l = path.lower()
        for token in _HARD_HEAVY_PATH_TOKENS + extra_tokens:
            if token in path_l:
                return f"hard:path_token={token}"
        for suffix in _HARD_HEAVY_FILE_SUFFIXES:
            if path_l.endswith(suffix):
                return f"hard:file_suffix={suffix}"
    return ""


def _hard_keyword_hit(text: str) -> str:
    text_l = text.lower()
    for keyword in _HARD_HEAVY_KEYWORDS:
        if keyword in text_l:
            return f"hard:keyword={keyword}"
    return ""


def _apply_hard_rules(inp: ComplexityInput) -> ComplexityDecision | None:
    """Forced HEAVY for any safety-critical signal. Returns None when clean.

    Note: path_type is NOT used as a hard-rule source. The real contract
    only emits read/write/both, which do not map to a specific heavy-risk
    category. Contract/schema risk is covered by file-path hard-rules
    (e.g. /schema/, /contract/), active task-card route scopes
    (e.g. /api/v1/routes/), and keyword hard-rules (e.g. "schema change",
    "contract change").
    """
    path_hit = _hard_path_hit(inp.all_files())
    if not path_hit:
        # Route hard-rule fires on the *authoritative* scope (task card or
        # source-of-truth, both signals of architectural intent). It does
        # NOT fire on ``changed_files`` alone — a small route patch with its
        # test can stay STANDARD; only when the route is explicitly the
        # task target should we force HEAVY.
        authoritative_scope = tuple(inp.source_of_truth_files) + tuple(inp.task_card_files)
        path_hit = _hard_path_hit(authoritative_scope, _TASK_CARD_HARD_HEAVY_PATH_TOKENS)
    if path_hit:
        risk_flag = "contract" if "routes" in path_hit or "schema" in path_hit else (
            "security" if "auth" in path_hit or "credential" in path_hit or "permission" in path_hit
            else "migration" if "migration" in path_hit
            else "core"
        )
        return _make_decision(
            tier="heavy", source="hard_rule", static_score=100,
            hard_rule=path_hit, reasons=(path_hit,), risk_flags=(risk_flag,),
        )

    keyword_hit = _hard_keyword_hit(inp.task_direction + " " + inp.requirements_text)
    if keyword_hit:
        return _make_decision(
            tier="heavy", source="hard_rule", static_score=100,
            hard_rule=keyword_hit, reasons=(keyword_hit,), risk_flags=("ambiguous_scope",),
        )
    return None


# ---------------------------------------------------------------------------
# Strong-lite shortcut — obvious lite without going through scoring
# ---------------------------------------------------------------------------


def _is_test_path(path: str) -> bool:
    """True if path looks like a test file (not naming-heuristic only)."""
    p = path.replace("\\", "/").lower()
    if "/tests/" in p or "/test/" in p:
        return True
    name = p.rsplit("/", 1)[-1]
    return name.startswith("test_") or name.endswith("_test.py")


def _check_strong_lite(inp: ComplexityInput) -> ComplexityDecision | None:
    files = inp.all_files()
    if not files:
        return None

    if all(p.lower().endswith(".md") for p in files):
        return _make_decision(
            tier="lite", source="strong_lite", static_score=0,
            reasons=("all_docs",),
        )

    if len(files) == 2:
        srcs = [p for p in files if not _is_test_path(p)]
        tests = [p for p in files if _is_test_path(p)]
        if len(srcs) == 1 and len(tests) == 1 and not _touches_api_behavior(tuple(srcs)):
            return _make_decision(
                tier="lite", source="strong_lite", static_score=0,
                reasons=("single_source_and_test_pair",),
            )
    return None


# ---------------------------------------------------------------------------
# Static scoring — weighted features
# ---------------------------------------------------------------------------

_REFACTOR_KEYWORDS = ("refactor", "重构", "migrate", "迁移", "rewrite", "redesign", "重设计")
_PERFORMANCE_KEYWORDS = ("performance", "性能", "concurrency", "并发", "cache consistency", "throughput")
_BUG_HELPER_KEYWORDS = ("bug ", "fix ", "fix:", "helper", "typo", "rename", "format ", "test ", "tests ", "doc ", "docs ", "补", "添加")


def _file_count_score(n: int) -> tuple[int, str]:
    if n <= 2:
        return -20, "files<=2"
    if n <= 5:
        return 10, "files=3-5"
    if n <= 10:
        return 35, "files=6-10"
    return 60, "files>10"


def _layer_score(layers: tuple[str, ...]) -> list[tuple[int, str]]:
    points: list[tuple[int, str]] = []
    n = len(layers)
    if n >= 3:
        points.append((25, "layers>=3"))
    has_frontend = any("frontend" in l.lower() or "mobile" in l.lower() or "web" in l.lower() for l in layers)
    has_backend = any("backend" in l.lower() or "service" in l.lower() or "api" in l.lower() for l in layers)
    if has_frontend and has_backend:
        points.append((30, "frontend+backend"))
    return points


def _path_type_score(files: tuple[str, ...]) -> list[tuple[int, str]]:
    points: list[tuple[int, str]] = []
    for f in files:
        f_l = f.lower()
        if "/routes/" in f_l or "/controller" in f_l:
            points.append((40, "touches_route_or_controller"))
            break
    for f in files:
        f_l = f.lower()
        if "/service" in f_l or "/repository" in f_l or "/cache" in f_l:
            points.append((10, "touches_service_or_repo_or_cache"))
            break
    return points


def _touches_api_behavior(files: tuple[str, ...]) -> bool:
    """True when one of the files is an actual route/controller/handler.

    The directory marker ``/api/`` alone is not enough — projects use it as a
    versioning convention (``/api/v1/services/...``) and matching it would
    flag every service as API surface. Only fire when the path or filename
    contains an explicit route/controller/handler token.
    """
    for path in files:
        path_l = path.replace("\\", "/").lower()
        if "/routes/" in path_l or "/controllers/" in path_l or "/handlers/" in path_l or "/endpoints/" in path_l:
            return True
        name = path_l.rsplit("/", 1)[-1]
        if any(token in name for token in ("route", "router", "controller", "handler", "endpoint")):
            return True
    return False


def _keyword_score(text: str) -> list[tuple[int, str]]:
    points: list[tuple[int, str]] = []
    text_l = (text or "").lower()
    if any(kw in text_l for kw in _REFACTOR_KEYWORDS):
        points.append((40, "refactor_keyword"))
    if any(kw in text_l for kw in _PERFORMANCE_KEYWORDS):
        points.append((25, "performance_keyword"))
    if any(kw in text_l for kw in _BUG_HELPER_KEYWORDS):
        points.append((-20, "bug_or_helper_keyword"))
    return points


def _compute_static_score(inp: ComplexityInput) -> tuple[int, tuple[str, ...]]:
    score = 0
    reasons: list[str] = []

    file_score, file_reason = _file_count_score(len(inp.all_files()))
    score += file_score
    reasons.append(f"{file_reason}:{file_score:+d}")

    for delta, reason in _layer_score(inp.layers):
        score += delta
        reasons.append(f"{reason}:{delta:+d}")

    for delta, reason in _path_type_score(inp.all_files()):
        score += delta
        reasons.append(f"{reason}:{delta:+d}")

    for delta, reason in _keyword_score(inp.task_direction + " " + inp.requirements_text):
        score += delta
        reasons.append(f"{reason}:{delta:+d}")

    return score, tuple(reasons)


# ---------------------------------------------------------------------------
# Learned adjustments (C5 will populate these from instincts)
# ---------------------------------------------------------------------------


def _hint_matches_input(inp: ComplexityInput, hint: dict[str, Any]) -> bool:
    """Return True when a learned hint exactly matches current feature.

    Lane-learning hints use `pattern=<feature-name>`. Matching must be strict
    feature equality (case-insensitive), not substring/glob heuristics.
    """
    pattern = str(hint.get("pattern") or "").strip()
    if not pattern:
        return False
    feature = str(inp.feature or "").strip()
    if not feature:
        return False
    return pattern.casefold() == feature.casefold()


def _apply_learned_adjustments(
    inp: ComplexityInput,
    score: int,
    hints: tuple[dict[str, Any], ...],
) -> tuple[int, tuple[str, ...]]:
    """Apply learned weight adjustments with per-pattern dedup + total cap.

    Per-pattern dedup: when two hints share a pattern (typical case is
    over -20 + under +20 both promoted from the same feature), pick the
    one with the newest ``last_seen``. Tied last_seen → higher confidence
    wins; still tied → first encountered. Summing them silently produced
    net-zero adjustments, which is why lane learning had no observable
    effect in production.

    Total delta cap: |sum of applied deltas| <= _LANE_DELTA_CAP. Without
    this a couple of high-confidence hints could swing tier on their own
    and undermine detector predictability.

    Kill switch: env WORKFLOW_LANE_LEARNING_DISABLED=1 short-circuits to
    (score, ()), to allow operators to disable learning while we evolve
    the matching scheme.
    """
    if not hints or _lane_learning_disabled():
        return score, ()

    chosen: dict[str, dict[str, Any]] = {}
    for hint in hints:
        if not _hint_matches_input(inp, hint):
            continue
        delta = int(hint.get("score_delta") or 0)
        if not delta:
            continue
        key = str(hint.get("pattern") or "").casefold()
        existing = chosen.get(key)
        if existing is None:
            chosen[key] = hint
            continue
        new_ls = str(hint.get("last_seen") or "")
        old_ls = str(existing.get("last_seen") or "")
        if new_ls > old_ls:
            chosen[key] = hint
        elif new_ls == old_ls:
            new_conf = float(hint.get("confidence") or 0.0)
            old_conf = float(existing.get("confidence") or 0.0)
            if new_conf > old_conf:
                chosen[key] = hint

    applied: list[str] = []
    total_delta = 0
    for hint in chosen.values():
        delta = int(hint.get("score_delta") or 0)
        remaining = _LANE_DELTA_CAP - abs(total_delta)
        if remaining <= 0:
            break
        clamped = max(-remaining, min(remaining, delta))
        if clamped == 0:
            continue
        total_delta += clamped
        applied.append(f"learned:{hint.get('pattern','?')}:{clamped:+d}")

    return score + total_delta, tuple(applied)


# ---------------------------------------------------------------------------
# LLM gray-zone classifier (optional dependency)
# ---------------------------------------------------------------------------


def _classify_with_llm(
    inp: ComplexityInput,
    current_score: int,
    static_reasons: tuple[str, ...],
    learned: tuple[str, ...],
    classifier: LLMClassifier,
) -> ComplexityDecision:
    """Call the injected LLM classifier. On failure, return STANDARD fallback."""
    try:
        result = classifier(inp, current_score, static_reasons)
    except Exception as exc:  # pragma: no cover — LLM-side failure
        _logger.warning("LLM gray-zone classifier failed: %s — fallback to standard", exc)
        return _make_decision(
            tier="standard", source="fallback_llm_failed",
            static_score=current_score,
            reasons=static_reasons + (f"llm_failed:{type(exc).__name__}",),
            learned_adjustments=learned,
        )

    tier = str(result.get("tier") or "standard").lower()
    if tier not in {"lite", "standard", "heavy"}:
        tier = "standard"

    return _make_decision(
        tier=tier,  # type: ignore[arg-type]
        source="llm_gray_zone",
        static_score=current_score,
        confidence=float(result.get("confidence") or 0.0),
        reasons=static_reasons + (f"llm:{result.get('reason','')}",),
        risk_flags=tuple(result.get("risk_flags") or ()),
        llm_used=True,
        learned_adjustments=learned,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_VALID_TIERS = {"lite", "standard", "heavy"}


def detect_complexity(
    inp: ComplexityInput,
    *,
    requested_tier: str = "auto",
    llm_classifier: Optional[LLMClassifier] = None,
) -> ComplexityDecision:
    """Decide tier for this run. See module docstring for resolution order."""
    requested = (requested_tier or "auto").strip().lower()

    if requested in _VALID_TIERS:
        return _make_decision(
            tier=requested,  # type: ignore[arg-type]
            source="explicit",
            static_score=0,
            reasons=(f"user_explicit:{requested}",),
            confidence=1.0,
        )

    # Safety fallback: when the detector has zero real signals we must NOT
    # default to lite — lite is the "known safe / trivial" lane, not the
    # "information insufficient" lane. `inp.feature` is excluded because it
    # is always truthy from the CLI path; `learned_hints` is excluded because
    # a historical hint alone should not keep us out of the standard lane.
    if not (
        inp.task_direction
        or inp.requirements_text
        or inp.changed_files
        or inp.source_of_truth_files
        or inp.task_card_files
        or inp.path_type
        or inp.layers
    ):
        return _make_decision(
            tier="standard",
            source="empty_input_fallback",
            static_score=0,
            reasons=("no_input_signals",),
        )

    hard = _apply_hard_rules(inp)
    if hard is not None:
        return hard

    strong_lite = _check_strong_lite(inp)
    if strong_lite is not None:
        return strong_lite

    score, reasons = _compute_static_score(inp)
    score, learned = _apply_learned_adjustments(inp, score, inp.learned_hints)

    if _touches_api_behavior(inp.all_files()) and score <= 25:
        return _make_decision(
            tier="standard",
            source="api_behavior_floor",
            static_score=score,
            reasons=reasons + ("api_behavior_floor:minimum_standard",),
            learned_adjustments=learned,
        )
    if score <= 25:
        return _make_decision(
            tier="lite", source="static_score", static_score=score,
            reasons=reasons, learned_adjustments=learned,
        )
    if score >= 70:
        return _make_decision(
            tier="heavy", source="static_score", static_score=score,
            reasons=reasons, learned_adjustments=learned,
        )

    if llm_classifier is not None:
        return _classify_with_llm(inp, score, reasons, learned, llm_classifier)

    return _make_decision(
        tier="standard",
        source="fallback_gray_zone_no_llm",
        static_score=score,
        reasons=reasons + ("gray_zone_no_llm:default_standard",),
        learned_adjustments=learned,
    )


def _make_decision(
    *,
    tier: str,
    source: str,
    static_score: int,
    hard_rule: str = "",
    reasons: tuple[str, ...] = (),
    risk_flags: tuple[str, ...] = (),
    llm_used: bool = False,
    learned_adjustments: tuple[str, ...] = (),
    confidence: float = 0.8,
) -> ComplexityDecision:
    return ComplexityDecision(
        tier=tier,  # type: ignore[arg-type]
        confidence=confidence,
        source=source,
        static_score=static_score,
        hard_rule=hard_rule,
        reasons=reasons,
        risk_flags=risk_flags,
        llm_used=llm_used,
        learned_adjustments=learned_adjustments,
    )


def model_advisor_tier_classifier(
    inp: ComplexityInput,
    current_score: int,
    static_reasons: tuple[str, ...],
) -> dict[str, Any]:
    """Bridge complexity_detector LLMClassifier signature to model_advisor.suggest_tier.

    When the advisor is not enabled, raises RuntimeError so detect_complexity
    falls back to STANDARD. This keeps the zero-LLM path identical to C2.
    """
    from kodawari.autopilot import model_advisor as _advisor

    result = _advisor.suggest_tier(
        task_direction=inp.task_direction,
        files=list(inp.all_files()),
        static_score=current_score,
        reasons=list(static_reasons),
    )
    if result is None:
        raise RuntimeError("model_advisor_disabled_or_failed")
    return result


__all__ = [
    "ComplexityInput",
    "LLMClassifier",
    "detect_complexity",
    "model_advisor_tier_classifier",
]

