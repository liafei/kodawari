"""Shared helpers extracted from the autopilot engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from kodawari.utils.glob_match import glob_match
import logging
from pathlib import Path
import subprocess
from typing import Any

from kodawari.autopilot.core.collaboration import ArchitectureDecision, CollaborationContext

logger = logging.getLogger(__name__)


class ExecutionPhase(str, Enum):
    PLAN_REVIEW = "PLAN_REVIEW"
    IMPLEMENT = "IMPLEMENT"
    VERIFY = "VERIFY"
    GATE = "GATE"


@dataclass
class ExecutionPlan:
    stages: list[dict[str, str]]
    estimated_cycles: int
    estimated_tokens: int


@dataclass
class AutopilotConfig:
    project_root: Path
    feature: str
    task_direction: str = ""
    requirements_file: Path | None = None
    contract_first_mode: str = "off"
    phase_mode: str = "implement"
    task_card_path: Path | None = None
    executor_backend: str = ""
    executor_command: str = ""
    self_review_backend: str = ""
    self_review_command: str = ""
    strict_scope: bool = False
    profile: str = "profiles/generic.yaml"
    verify_cmd: str = "pytest -q"
    max_cycles: int = 8
    token_budget: int = 300000
    allowed_paths: list[str] = field(default_factory=list)
    initial_changed_files: list[str] = field(default_factory=list)
    dry_run: bool = False
    resume: bool = False
    non_interactive: bool = True
    collaboration_max_rounds: int = 6
    hook_events_enabled: bool = True
    real_peer_review: bool = False
    require_real_peer_review: bool = False
    opus_reviewer_backend: str = ""
    executor_model: str = ""
    reviewer_backend: str = ""
    reviewer_model: str = ""
    reviewer_api_format: str = ""
    reviewer_base_url: str = ""
    enforce_dual_review: bool = False
    peer_review_max_tokens: int = 4096
    verify_setup_recovery_max_attempts: int = 1
    verify_setup_cleanup_strategy: str = "conservative"
    verify_setup_recovery_retry_interval_seconds: int = 2
    verify_setup_recovery_fallback_strategy: bool = True
    protected_files_check_enabled: bool = True
    protected_files: list[str] = field(default_factory=list)
    protected_files_critical: list[str] = field(
        default_factory=lambda: [
            "tests/conftest.py",
            "profiles/*.yaml",
            ".github/workflows/*.yml",
            ".claude/workflow/*.yaml",
        ]
    )
    protected_files_warning: list[str] = field(
        default_factory=lambda: [
            "README.md",
            "CHANGELOG.md",
            "docs/ARCHITECTURE.md",
            ".env",
            ".env.*",
        ]
    )
    rollback_on_failure: bool = False
    max_verify_retries: int = 2


@dataclass
class _FallbackPatternSuggestion:
    pattern_id: str
    title: str
    rationale: str
    confidence: float
    checklist: list[str] = field(default_factory=list)
    verify_hints: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "pattern_id": self.pattern_id,
            "title": self.title,
            "rationale": self.rationale,
            "confidence": self.confidence,
            "checklist": list(self.checklist),
            "verify_hints": list(self.verify_hints),
        }


class _FallbackPatternRegistry:
    """Minimal registry used before kodawari.patterns is available."""

    _PATTERN_CONFIGS: tuple[dict[str, Any], ...] = (
        {
            "tokens": ("ranking", "score", "weight", "sort"),
            "pattern_id": "ranking-rules",
            "title": "Ranking Rules",
            "rationale": "Task text indicates ranking or weighted scoring behavior.",
            "confidence": 0.92,
            "checklist": [
                "Normalize scores before sorting.",
                "Define deterministic tie-breakers.",
            ],
            "verify_hints": ["test_*ranking*.py"],
        },
        {
            "tokens": ("migration", "schema", "alter table", "column"),
            "pattern_id": "schema-migration",
            "title": "Schema Migration",
            "rationale": "Task text indicates schema or migration work.",
            "confidence": 0.9,
            "checklist": [
                "Create backward-compatible migration steps.",
                "Document rollback expectations.",
            ],
            "verify_hints": ["test_*migration*.py"],
        },
        {
            "tokens": ("api", "endpoint", "route", "rest"),
            "pattern_id": "api-endpoint",
            "title": "API Endpoint",
            "rationale": "Task text indicates API endpoint implementation.",
            "confidence": 0.75,
            "checklist": ["Validate request/response contract."],
            "verify_hints": ["test_*api*.py"],
        },
        {
            "tokens": ("crud", "create", "read", "update", "delete"),
            "pattern_id": "crud",
            "title": "CRUD",
            "rationale": "Task text indicates create/read/update/delete operations.",
            "confidence": 0.7,
            "checklist": ["Cover create/read/update/delete flows in tests."],
            "verify_hints": ["test_*crud*.py"],
        },
    )

    def _collect_text(
        self,
        *,
        task_label: str,
        task_scope: str | None,
        requirements: str | None,
    ) -> str:
        return "\n".join(
            part.strip()
            for part in [str(task_label or ""), str(task_scope or ""), str(requirements or "")]
            if part and str(part).strip()
        ).lower()

    def _contains_any(self, text: str, tokens: tuple[str, ...]) -> bool:
        return any(token in text for token in tokens)

    def _build_suggestion(self, config: dict[str, Any]) -> _FallbackPatternSuggestion:
        return _FallbackPatternSuggestion(
            pattern_id=str(config["pattern_id"]),
            title=str(config["title"]),
            rationale=str(config["rationale"]),
            confidence=float(config["confidence"]),
            checklist=list(config.get("checklist", [])),
            verify_hints=list(config.get("verify_hints", [])),
        )

    def analyze(
        self,
        *,
        task_id: str,
        task_label: str,
        task_scope: str | None = None,
        requirements: str | None = None,
    ) -> list[_FallbackPatternSuggestion]:
        del task_id
        text = self._collect_text(
            task_label=task_label,
            task_scope=task_scope,
            requirements=requirements,
        )
        suggestions = [
            self._build_suggestion(config)
            for config in self._PATTERN_CONFIGS
            if self._contains_any(text, tuple(str(item) for item in config.get("tokens", ())))
        ]
        return sorted(suggestions, key=lambda item: item.confidence, reverse=True)


class _FallbackAdapter:
    """Fallback adapter used if local adapter import fails unexpectedly."""

    def check_health(self) -> tuple[bool, str]:
        return False, "fallback-adapter-unavailable"

    def implement(self, task: str, context: dict[str, Any]) -> dict[str, Any]:
        del task, context
        return {
            "status": "blocked",
            "reason": "FALLBACK_ADAPTER_FORBIDDEN",
            "blocking_reason": "fallback adapter cannot perform real implementation work",
            "mode": "fallback",
        }


@dataclass
class _LoopRuntime:
    task_label: str
    task_scope: str | None
    task_id: str
    context: CollaborationContext
    peer_review_policy: dict[str, Any]
    pre_compact_payload: dict[str, Any]
    peer_review_enabled: bool = True
    semantic_compact_payload: dict[str, Any] | None = None
    round_records: list[dict[str, Any]] = field(default_factory=list)
    hook_events: list[dict[str, Any]] = field(default_factory=list)
    last_changed_files: list[str] = field(default_factory=list)
    codex_self_reviews: list[dict[str, Any]] = field(default_factory=list)
    peer_reviews: list[dict[str, Any]] = field(default_factory=list)
    peer_review_summary: dict[str, Any] = field(default_factory=dict)
    post_execution_qa: dict[str, Any] | None = None
    verify_check: dict[str, Any] | None = None
    gate_check: dict[str, Any] | None = None
    execution_result: dict[str, Any] | None = None
    execution_artifacts: dict[str, str] | None = None
    pending_recovery_card: dict[str, Any] | None = None
    recovery_attempts: int = 0
    recovery_attempt_signature: str = ""
    recovery_attempts_for_signature: int = 0
    recovery_decisions: list[dict[str, Any]] = field(default_factory=list)
    rollback_checkpoint: Any | None = None
    config_override: dict[str, Any] | None = None
    # Cached result of the last _validate_proceed_review_evidence() call.
    # Set by gate_round.run_proceed_round(); reused by _finish_loop() compliance
    # reporting to avoid a redundant second validation pass on the same runtime.
    last_proceed_evidence: dict[str, Any] | None = None


def resolve_requirements_text(
    *,
    requirements_text: str | None,
    requirements_file: Path | None,
) -> str:
    if requirements_text is not None:
        return requirements_text
    if requirements_file and requirements_file.exists():
        return requirements_file.read_text(encoding="utf-8")
    return ""


def build_default_pattern_registry() -> Any:
    try:
        from kodawari.patterns import (
            APIEndpointPattern,
            CRUDPattern,
            PatternRegistry,
            RankingRulesPattern,
            SchemaMigrationPattern,
        )

        registry = PatternRegistry()
        registry.register(CRUDPattern())
        registry.register(APIEndpointPattern())
        registry.register(RankingRulesPattern())
        registry.register(SchemaMigrationPattern())
        return registry
    except Exception:
        logger.warning("falling back to minimal pattern registry", exc_info=True)
        return _FallbackPatternRegistry()


def build_default_adapter(config: AutopilotConfig | None = None) -> Any:
    try:
        from kodawari.autopilot.execution.local_adapter import LocalCodexAdapter, LocalCodexAdapterConfig

        adapter_config = LocalCodexAdapterConfig(
            cwd=getattr(config, "project_root", None),
            executor_backend=str(getattr(config, "executor_backend", "") or ""),
            executor_command=str(getattr(config, "executor_command", "") or ""),
            self_review_backend=str(getattr(config, "self_review_backend", "") or ""),
            self_review_command=str(getattr(config, "self_review_command", "") or ""),
            real_peer_review=bool(getattr(config, "real_peer_review", False)),
            require_real_peer_review=bool(getattr(config, "require_real_peer_review", False)),
            opus_reviewer_backend=str(getattr(config, "opus_reviewer_backend", "") or ""),
            opus_gateway_max_tokens=int(getattr(config, "peer_review_max_tokens", 4096) or 4096),
            executor_model=str(getattr(config, "executor_model", "") or ""),
            reviewer_backend=str(getattr(config, "reviewer_backend", "") or ""),
            reviewer_model=str(getattr(config, "reviewer_model", "") or ""),
            reviewer_api_format=str(getattr(config, "reviewer_api_format", "") or ""),
            reviewer_base_url=str(getattr(config, "reviewer_base_url", "") or ""),
        )
        return LocalCodexAdapter(config=adapter_config)
    except Exception:
        logger.warning("falling back to minimal adapter", exc_info=True)
        return _FallbackAdapter()


def task_id_from_label(task_label: str) -> str:
    raw = str(task_label or "").strip()
    if ":" in raw:
        return raw.split(":", 1)[0].strip().upper()
    compact = "".join(ch for ch in raw.upper() if ch.isalnum())
    return (compact[:8] or "TASK").upper()


def serialize_decision(item: Any) -> dict[str, Any]:
    if hasattr(item, "to_dict"):
        serialized = item.to_dict()
        if isinstance(serialized, dict):
            return serialized
    if isinstance(item, dict):
        return dict(item)
    return {
        "id": str(getattr(item, "decision_id", "")),
        "decision": str(getattr(item, "decision", "")),
        "rationale": str(getattr(item, "rationale", "")),
    }


def pattern_suggestions(
    pattern_registry: Any,
    *,
    task_id: str,
    task_label: str,
    task_scope: str | None,
    requirements: str,
) -> list[Any]:
    try:
        return pattern_registry.analyze(
            task_id=task_id,
            task_label=task_label,
            task_scope=task_scope,
            requirements=requirements,
        )
    except TypeError:
        return pattern_registry.analyze(task_id, task_label, task_scope, requirements)


def to_pattern_hint(item: Any) -> dict[str, Any]:
    if hasattr(item, "to_dict"):
        hint = item.to_dict()
        if isinstance(hint, dict):
            return hint
    if isinstance(item, dict):
        return dict(item)
    return {
        "pattern_id": str(getattr(item, "pattern_id", "unknown")),
        "title": str(getattr(item, "title", "")),
        "rationale": str(getattr(item, "rationale", "")),
        "confidence": float(getattr(item, "confidence", 0.0) or 0.0),
    }


def is_authorized_to_modify(path: str, *, task_label: str = "", task_scope: str = "") -> bool:
    normalized = path.replace("\\", "/").lower()
    if normalized == "tests/conftest.py":
        return False
    text = f"{task_label}\n{task_scope}".lower()
    return normalized in text or Path(normalized).name.lower() in text


def is_never_authorized_protected_file(path: str) -> bool:
    """Return True for protected files that task text must never authorize.

    Policy files under .claude/workflow/ define the runtime control surface
    for autopilot itself. Mentioning them in task text must not turn an
    otherwise critical edit into an authorized one.
    """
    normalized = path.replace("\\", "/")
    return matching_patterns(normalized, [".claude/workflow/*.yaml"])


def matching_patterns(path: str, patterns: list[str]) -> bool:
    normalized = path.replace("\\", "/")
    return any(glob_match(normalized, pattern) for pattern in patterns)


def check_protected_files(
    changed_files: list[str],
    *,
    task_label: str = "",
    task_scope: str = "",
    protected_files_check_enabled: bool,
    protected_files: list[str],
    protected_files_critical: list[str],
    protected_files_warning: list[str],
) -> dict[str, Any]:
    if not protected_files_check_enabled:
        return {"blocked": False, "critical": [], "warning": []}

    critical_patterns = list(protected_files_critical) + list(protected_files)
    warning_patterns = list(protected_files_warning)
    critical_hits: list[str] = []
    warning_hits: list[str] = []

    for path in changed_files:
        if matching_patterns(path, critical_patterns):
            if is_never_authorized_protected_file(path) or not is_authorized_to_modify(
                path,
                task_label=task_label,
                task_scope=task_scope,
            ):
                critical_hits.append(path)
            continue
        if matching_patterns(path, warning_patterns):
            warning_hits.append(path)

    return {
        "blocked": bool(critical_hits),
        "critical": critical_hits,
        "warning": warning_hits,
    }


def looks_like_setup_error(message: str) -> bool:
    text = str(message or "").lower()
    tokens = ["fixture", "scopemismatch", "error at setup", "failed at setup", "setup failed"]
    return any(token in text for token in tokens)


def snapshot_dirty_files(project_root: Path, *, planning_dir: Path | None = None) -> set[str]:
    """Return the set of project-root-relative paths that git considers dirty.

    Uses ``git status --porcelain`` so it covers both modified and untracked
    files.  Falls back to an empty set when git is unavailable (e.g. non-git
    directories) or the command times out, so callers never crash.

    When project_root is a subdirectory of the git root (e.g. running inside a
    monorepo), git returns paths relative to the git root (e.g.
    ``kodawari/src/…``). We detect and strip that prefix so callers always
    receive project-root-relative paths (e.g. ``src/…``).

    When ``planning_dir`` is provided, any file under it is treated as
    kodawari's own scratch (state, rounds log, task cards, compact
    context, compliance reports, parallel worker worktrees, etc.) and
    excluded from the dirty set so scope-drift checks never false-fire on
    these orchestrator-owned artifacts.
    """
    try:
        root = project_root.resolve()
        # Detect the git-scope prefix (e.g. "kodawari/") so we can strip it.
        prefix_proc = subprocess.run(
            ["git", "rev-parse", "--show-prefix"],
            cwd=str(root),
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            timeout=10,
        )
        git_prefix = prefix_proc.stdout.strip().replace("\\", "/").rstrip("/")

        proc = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(root),
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            timeout=10,
        )
        if proc.returncode != 0:
            return set()
        from kodawari.cli.evidence.changed_files_truth import _is_runtime_internal_path

        planning_prefix = ""
        if planning_dir is not None:
            try:
                rel = planning_dir.resolve().relative_to(root)
                planning_prefix = rel.as_posix().rstrip("/") + "/"
            except ValueError:
                planning_prefix = ""
        dirty: set[str] = set()
        for line in proc.stdout.splitlines():
            if len(line) > 3:
                # Handle rename entries ("old -> new") — take the target name
                path_part = line[3:].strip().split(" -> ")[-1].replace("\\", "/")
                # When running inside a git subdirectory, only keep files that
                # live under project_root (i.e. they start with git_prefix).
                # Files in sibling directories (e.g. newsapp/ when we're in
                # kodawari/) are outside project_root and must be excluded.
                if git_prefix:
                    if path_part.startswith(git_prefix + "/"):
                        path_part = path_part[len(git_prefix) + 1:]
                    else:
                        continue  # outside project_root, skip
                # Drop workflow-internal scratch paths (`.parallel_workers/`,
                # `.workflow/`, legacy `.claude/memory/`) so they never pollute
                # scope-drift checks.
                if _is_runtime_internal_path(path_part):
                    continue
                # Drop anything inside the current planning_dir — those are
                # kodawari's own state/artifact files (rounds log,
                # compact context, task cards, compliance reports, ...).
                if planning_prefix and path_part.startswith(planning_prefix):
                    continue
                dirty.add(path_part)
        return dirty
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return set()


__all__ = [
    "AutopilotConfig",
    "ExecutionPhase",
    "ExecutionPlan",
    "_LoopRuntime",
    "build_default_adapter",
    "build_default_pattern_registry",
    "check_protected_files",
    "looks_like_setup_error",
    "pattern_suggestions",
    "resolve_requirements_text",
    "serialize_decision",
    "snapshot_dirty_files",
    "task_id_from_label",
    "to_pattern_hint",
]

