"""Local adapter bridge for minimal Codex execution and review simulation."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import os
from pathlib import Path
import shutil
from typing import Any
import warnings

from kodawari.autopilot.execution.execution_backend import (
    CLAUDE_CODE_BACKEND,
    EXTERNAL_CLI_BACKEND,
    MANUAL_BACKEND,
    NOOP_TEST_ONLY_BACKEND,
    ExecutionBackendConfig,
    resolve_execution_backend,
)
from kodawari.autopilot.execution.execution_artifacts import (
    is_test_environment,
    run_execution_backend,
)
from kodawari.autopilot.execution import local_adapter_changes as _changes
from kodawari.autopilot.execution import local_adapter_preflight as _preflight
from kodawari.autopilot.execution import local_adapter_recovery as _recovery
from kodawari.autopilot.execution import local_adapter_review_runtime as _review_runtime
from kodawari.autopilot.execution import local_adapter_self_review as _self_review_runtime
from kodawari.autopilot.review.gateways.cli import (
    CliReviewerConfig,
    cli_reviewer_available,
    request_cli_review,
    request_mcp_review,
)
from kodawari.autopilot.review.gateways.codex import (
    CodexReviewerConfig,
    codex_reviewer_available,
    request_codex_review,
)
from kodawari.autopilot.review.peer_review_gateway import (
    PeerReviewGatewayConfig as OpusGatewayConfig,
    request_peer_review as request_opus_review,
)
from kodawari.autopilot.review.review_precheck import (
    apply_deterministic_review_guard as _apply_deterministic_review_guard,
    compute_deterministic_findings,
)
from kodawari.autopilot.review.review_bundle import REVIEW_BUNDLE_FILENAME, build_review_bundle, write_review_bundle
from kodawari.autopilot.review.review_bridge import run_codex_self_review, run_post_execution_qa
from kodawari.autopilot.review.backend_doctor import (
    ReviewerHealthCache,
    probe_reviewer_backend,
)
from kodawari.infra.io_atomic import atomic_write_canonical_json
from kodawari.autopilot.core.model_config import load_model_config
from kodawari.autopilot.core.task_modes import is_verification_only_task
from kodawari.autopilot.recovery.executor_recovery import (
    RecoverySynthesizerConfig,
    normalize_recovery_decision,
    request_recovery_decision,
)


logger = logging.getLogger(__name__)

from kodawari.autopilot.core._env_helpers import (  # noqa: E402
    clean_text as _clean_text, env_flag_new_or_old as _env_flag_new_or_old, env_int as _env_int,
    env_new_or_old as _env_new_or_old, env_text as _env_text, sanitize_model as _sanitize_model,
    warn_deprecated_env as _warn_deprecated_env,
)


@dataclass
class LocalCodexAdapterConfig:
    executable: str = "codex"
    cwd: Path | None = None
    timeout_seconds: int = 600
    simulate: bool = True
    executor_backend: str = ""
    executor_command: str = ""
    executor_base_url: str = ""
    executor_base_url_env: str = ""
    executor_api_key_env: str = ""
    executor_api_format: str = ""
    executor_transport_name: str = ""
    executor_execution_protocol: str = ""
    executor_runtime_caps: dict[str, int] = field(default_factory=dict)
    self_review_backend: str = ""
    self_review_command: str = ""
    real_peer_review: bool = False
    require_real_peer_review: bool = False
    opus_gateway_base_url: str = ""
    opus_gateway_api_key: str = field(default="", repr=False)
    opus_gateway_model: str = "claude-opus-4.1"
    opus_gateway_api_format: str = "auto"
    opus_gateway_timeout_seconds: int = 45
    opus_gateway_retry_attempts: int = 2
    opus_gateway_max_tokens: int = 4096
    opus_reviewer_backend: str = "auto"
    opus_reviewer_executable: str = ""
    opus_reviewer_timeout_seconds: int = 300
    # Canonical multi-backend fields (new env vars; fallback to old env vars in _resolved_config)
    executor_model: str = ""
    reviewer_backend: str = ""
    reviewer_model: str = ""
    reviewer_api_format: str = ""
    reviewer_base_url: str = ""
    reviewer_api_key: str = field(default="", repr=False)
    reviewer_executable_claude: str = ""
    reviewer_executable_codex: str = ""
    recovery_backend: str = ""
    recovery_model: str = ""
    recovery_api_format: str = ""
    recovery_base_url: str = ""
    recovery_api_key: str = field(default="", repr=False)
    recovery_executable_claude: str = ""
    recovery_executable_codex: str = ""
    recovery_timeout_seconds: int = 60
    recovery_reasoning_effort: str = ""
    default_changed_files: list[str] = field(default_factory=lambda: ["src/app.py"])

    def __post_init__(self) -> None:
        # Sync legacy opus_gateway_* fields to canonical reviewer_* fields and
        # emit DeprecationWarning if any legacy field was explicitly set
        # (non-default). opus_gateway_model / opus_gateway_api_format carry
        # non-empty defaults, so detection compares value against default.
        legacy_pairs = [
            ("opus_gateway_base_url", "reviewer_base_url", ""),
            ("opus_gateway_api_key", "reviewer_api_key", ""),
            ("opus_gateway_model", "reviewer_model", "claude-opus-4.1"),
            ("opus_gateway_api_format", "reviewer_api_format", "auto"),
        ]
        deprecated_used: list[str] = []
        for old_field, new_field, default in legacy_pairs:
            old_val = getattr(self, old_field)
            if old_val != default:
                deprecated_used.append(old_field)
                if old_val and not getattr(self, new_field):
                    object.__setattr__(self, new_field, old_val)
        if deprecated_used:
            warnings.warn(
                f"LocalCodexAdapterConfig: {', '.join(deprecated_used)} are deprecated; "
                f"use the canonical reviewer_* fields instead "
                f"(see docs/contracts/ENV_VAR_MIGRATION.md for migration schedule).",
                DeprecationWarning,
                stacklevel=2,
            )


class LocalCodexAdapter:
    def __init__(self, config: LocalCodexAdapterConfig | None = None) -> None:
        self.config = self._resolved_config(config)
        self._reviewer_health_cache = ReviewerHealthCache(
            ttl_seconds=max(0, _env_int("WORKFLOW_REVIEWER_DOCTOR_TTL", 600))
        )

    def _resolved_config(self, config: LocalCodexAdapterConfig | None) -> LocalCodexAdapterConfig:
        base = config or LocalCodexAdapterConfig()
        project_root = Path(base.cwd or Path.cwd()).resolve()
        models = load_model_config(project_root)
        v2_impl_review_requested = False
        v2_impl_review_required = False
        if models.schema_version == "models.v2":
            executor_role = models.get_role("executor", fallback=False)
            executor_transport = models.transport_for_role("executor", fallback=False)
            executor_backend = models.executor_backend_for_role()
            executor_executable = models.role_executable("executor")
            if (
                executor_backend
                and not os.environ.get("WORKFLOW_EXECUTOR_BACKEND")
                and _clean_text(base.executor_backend) in {"", CLAUDE_CODE_BACKEND}
            ):
                base.executor_backend = executor_backend
            if executor_executable and _clean_text(base.executable) in {"", "codex", "claude"}:
                base.executable = executor_executable
            if executor_role is not None and executor_transport is not None:
                base.executor_transport_name = executor_transport.name
                base.executor_execution_protocol = executor_role.execution_protocol or base.executor_execution_protocol
                base.executor_runtime_caps = dict(executor_role.runtime_caps)
                if executor_transport.base_url and not _clean_text(base.executor_base_url):
                    base.executor_base_url = executor_transport.base_url
                if executor_transport.base_url_env and not _clean_text(base.executor_base_url):
                    base.executor_base_url = _env_text(executor_transport.base_url_env, "")
                base.executor_base_url_env = executor_transport.base_url_env
                base.executor_api_key_env = executor_transport.api_key_env
                base.executor_api_format = executor_transport.api_format
            self_review_backend = models.self_review_backend_for_role()
            if self_review_backend and not _clean_text(base.self_review_backend):
                base.self_review_backend = self_review_backend
            impl_role = models.get_role("impl_reviewer")
            impl_transport = models.transport_for_role("impl_reviewer")
            if impl_role is not None and impl_transport is not None:
                v2_impl_review_requested = True
                v2_impl_review_required = impl_role.on_unavailable == "fail"
                backend = impl_transport.legacy_reviewer_backend()
                if backend and not _clean_text(base.reviewer_backend):
                    base.reviewer_backend = backend
                executable = impl_transport.primary_executable()
                if executable and backend in {"cli", "mcp"} and not _clean_text(base.reviewer_executable_claude):
                    base.reviewer_executable_claude = executable
                if executable and backend == "codex" and not _clean_text(base.reviewer_executable_codex):
                    base.reviewer_executable_codex = executable
                if backend == "api":
                    if impl_transport.base_url and not _clean_text(base.reviewer_base_url):
                        base.reviewer_base_url = impl_transport.base_url
                    if impl_transport.base_url_env and not _clean_text(base.reviewer_base_url):
                        base.reviewer_base_url = _env_text(impl_transport.base_url_env, "")
                    if impl_transport.api_key_env and not _clean_text(base.reviewer_api_key):
                        base.reviewer_api_key = _env_text(impl_transport.api_key_env, "")
                    if impl_transport.api_format and not _clean_text(base.reviewer_api_format):
                        base.reviewer_api_format = impl_transport.api_format
            recovery_role = models.get_role("executor_recovery", fallback=False) or impl_role
            recovery_transport = models.transport_for_role("executor_recovery", fallback=False) or impl_transport
            if recovery_role is not None and recovery_transport is not None:
                recovery_backend = recovery_transport.legacy_reviewer_backend()
                if recovery_backend and not _clean_text(base.recovery_backend):
                    base.recovery_backend = recovery_backend
                if recovery_role.model and not _clean_text(base.recovery_model):
                    base.recovery_model = _sanitize_model(recovery_role.model)
                recovery_executable = recovery_transport.primary_executable()
                if recovery_executable and recovery_backend in {"cli", "mcp"} and not _clean_text(base.recovery_executable_claude):
                    base.recovery_executable_claude = recovery_executable
                if recovery_executable and recovery_backend == "codex" and not _clean_text(base.recovery_executable_codex):
                    base.recovery_executable_codex = recovery_executable
                if recovery_backend == "api":
                    if recovery_transport.base_url and not _clean_text(base.recovery_base_url):
                        base.recovery_base_url = recovery_transport.base_url
                    if recovery_transport.base_url_env and not _clean_text(base.recovery_base_url):
                        base.recovery_base_url = _env_text(recovery_transport.base_url_env, "")
                    if recovery_transport.api_key_env and not _clean_text(base.recovery_api_key):
                        base.recovery_api_key = _env_text(recovery_transport.api_key_env, "")
                    if recovery_transport.api_format and not _clean_text(base.recovery_api_format):
                        base.recovery_api_format = recovery_transport.api_format
                recovery_timeout = int(recovery_role.runtime_caps.get("recovery_timeout_seconds") or 0)
                if recovery_timeout > 0:
                    base.recovery_timeout_seconds = recovery_timeout
        if models.reviewer_model and not _clean_text(base.reviewer_model):
            base.reviewer_model = _sanitize_model(models.reviewer_model)
        if models.reviewer_backend and not _clean_text(base.reviewer_backend):
            base.reviewer_backend = models.reviewer_backend
        review_enabled_from_models = models.review_enabled
        # --- Executor ---
        # Backend must be resolved BEFORE executor_model so per-backend yaml lookup
        # works (`--executor-backend claude_code` picks `executor_models.claude_code`
        # from yaml, not the flat `executor_model` which may target a different CLI).
        base.executor_backend = _env_text("WORKFLOW_EXECUTOR_BACKEND", base.executor_backend)
        base.executor_command = _env_text("WORKFLOW_EXECUTOR_COMMAND", base.executor_command)
        base.executor_execution_protocol = _env_text("WORKFLOW_EXECUTOR_PROTOCOL", base.executor_execution_protocol)
        base.self_review_backend = _env_text("WORKFLOW_SELF_REVIEW_BACKEND", base.self_review_backend)
        base.self_review_command = _env_text("WORKFLOW_SELF_REVIEW_COMMAND", base.self_review_command)
        base.executable = _env_text("WORKFLOW_CODEX_EXECUTABLE", base.executable)
        base.timeout_seconds = _env_int("WORKFLOW_EXECUTOR_TIMEOUT_SECONDS", base.timeout_seconds)
        if not _clean_text(base.executor_model):
            yaml_model = models.resolve_executor_model(base.executor_backend)
            if yaml_model:
                base.executor_model = _sanitize_model(yaml_model)
        base.executor_model = _sanitize_model(_env_text("WORKFLOW_EXECUTOR_MODEL", base.executor_model))
        # Review gate: three-state; new var explicit value > old var; REQUIRED wins over ENABLED=0
        env_required_flag = _env_flag_new_or_old("WORKFLOW_REVIEW_REQUIRED", "WORKFLOW_OPUS_REVIEW_REQUIRED")
        env_required = env_required_flag is True
        env_enabled_flag = _env_flag_new_or_old("WORKFLOW_REVIEW_ENABLED", "WORKFLOW_OPUS_REVIEW_ENABLED")
        env_real_disabled = env_enabled_flag is False
        if env_required and env_enabled_flag is False:
            logger.warning("WORKFLOW_REVIEW_REQUIRED=1 overrides REVIEW_ENABLED=0; real review will be performed")
        # --- Canonical reviewer fields (new vars take priority; fall back to old vars) ---
        base.reviewer_api_key = _env_new_or_old("WORKFLOW_REVIEWER_API_KEY", "WORKFLOW_OPUS_API_KEY", base.reviewer_api_key or base.opus_gateway_api_key)
        base.opus_gateway_api_key = base.reviewer_api_key  # keep legacy in sync
        base.reviewer_base_url = _env_new_or_old("WORKFLOW_REVIEWER_BASE_URL", "WORKFLOW_OPUS_GATEWAY", base.reviewer_base_url or base.opus_gateway_base_url)
        base.opus_gateway_base_url = base.reviewer_base_url
        base.reviewer_model = _sanitize_model(_env_new_or_old("WORKFLOW_REVIEWER_MODEL", "WORKFLOW_OPUS_MODEL", base.reviewer_model))
        base.opus_gateway_model = base.reviewer_model or base.opus_gateway_model
        base.reviewer_api_format = _env_new_or_old("WORKFLOW_REVIEWER_API_FORMAT", "WORKFLOW_OPUS_API_FORMAT", base.reviewer_api_format or base.opus_gateway_api_format)
        base.opus_gateway_api_format = base.reviewer_api_format or base.opus_gateway_api_format
        base.recovery_backend = _env_text("WORKFLOW_RECOVERY_BACKEND", base.recovery_backend or base.reviewer_backend)
        base.recovery_model = _sanitize_model(_env_text("WORKFLOW_RECOVERY_MODEL", base.recovery_model or base.reviewer_model))
        base.recovery_base_url = _env_text("WORKFLOW_RECOVERY_BASE_URL", base.recovery_base_url or base.reviewer_base_url)
        base.recovery_api_key = _env_text("WORKFLOW_RECOVERY_API_KEY", base.recovery_api_key or base.reviewer_api_key)
        base.recovery_api_format = _env_text("WORKFLOW_RECOVERY_API_FORMAT", base.recovery_api_format or base.reviewer_api_format)
        base.recovery_timeout_seconds = _env_int("WORKFLOW_RECOVERY_TIMEOUT", base.recovery_timeout_seconds)
        base.recovery_timeout_seconds = _env_int(
            "WORKFLOW_RECOVERY_SYNTHESIZER_TIMEOUT_SECONDS",
            base.recovery_timeout_seconds,
        )
        base.recovery_reasoning_effort = _env_text("WORKFLOW_RECOVERY_REASONING_EFFORT", base.recovery_reasoning_effort or "low")
        if env_required:
            review_enabled = True
        elif env_enabled_flag is not None:
            review_enabled = env_enabled_flag is True
        elif review_enabled_from_models is not None:
            review_enabled = bool(review_enabled_from_models)
        else:
            review_enabled = bool(base.real_peer_review or v2_impl_review_requested)
        # Auto-enable when API key exists, unless review was explicitly disabled
        # by env or models.yaml.
        auto_enable_real_review = (
            bool(base.reviewer_api_key)
            and not env_real_disabled
            and review_enabled_from_models is not False
        )
        base.real_peer_review = bool(review_enabled or auto_enable_real_review)
        base.require_real_peer_review = bool(base.require_real_peer_review or env_required or v2_impl_review_required)
        base.opus_gateway_timeout_seconds = _env_int("WORKFLOW_OPUS_TIMEOUT_SECONDS", base.opus_gateway_timeout_seconds)
        base.opus_gateway_retry_attempts = _env_int("WORKFLOW_OPUS_RETRY_ATTEMPTS", base.opus_gateway_retry_attempts)
        base.opus_gateway_max_tokens = _env_int("WORKFLOW_OPUS_MAX_TOKENS", base.opus_gateway_max_tokens)
        # --- Canonical reviewer backend (new var takes priority) ---
        _VALID_REVIEWER_BACKENDS = {"api", "cli", "mcp", "codex", "auto", ""}
        raw_backend = _env_new_or_old("WORKFLOW_REVIEWER_BACKEND", "WORKFLOW_OPUS_REVIEWER_BACKEND", base.reviewer_backend or base.opus_reviewer_backend).lower()
        if raw_backend and raw_backend not in _VALID_REVIEWER_BACKENDS:
            logger.error("Unsupported reviewer backend %r; expected one of %s.", raw_backend, sorted(_VALID_REVIEWER_BACKENDS - {""}))
            base.reviewer_backend = f"unsupported:{raw_backend}"
        else:
            base.reviewer_backend = raw_backend
        base.opus_reviewer_backend = base.reviewer_backend
        if not _clean_text(base.recovery_backend):
            base.recovery_backend = base.reviewer_backend
        if _clean_text(base.recovery_backend) == "auto":
            base.recovery_backend = base.reviewer_backend
        # Per-backend reviewer executables: specific > generic > legacy
        generic_exe = _env_text("WORKFLOW_REVIEWER_EXECUTABLE", "")
        legacy_exe = _env_text("WORKFLOW_OPUS_REVIEWER_EXECUTABLE", "")
        if legacy_exe and not generic_exe:
            _warn_deprecated_env("WORKFLOW_OPUS_REVIEWER_EXECUTABLE", "WORKFLOW_REVIEWER_EXECUTABLE")
        fallback_exe = generic_exe or legacy_exe
        base.reviewer_executable_claude = _env_text("WORKFLOW_REVIEWER_CLAUDE_EXECUTABLE", base.reviewer_executable_claude or fallback_exe)
        base.reviewer_executable_codex = _env_text("WORKFLOW_REVIEWER_CODEX_EXECUTABLE", base.reviewer_executable_codex or fallback_exe)
        base.recovery_executable_claude = _env_text("WORKFLOW_RECOVERY_CLAUDE_EXECUTABLE", base.recovery_executable_claude or base.reviewer_executable_claude)
        base.recovery_executable_codex = _env_text("WORKFLOW_RECOVERY_CODEX_EXECUTABLE", base.recovery_executable_codex or base.reviewer_executable_codex)
        base.opus_reviewer_executable = base.reviewer_executable_claude or base.reviewer_executable_codex
        legacy_timeout = _env_text("WORKFLOW_OPUS_REVIEWER_TIMEOUT", "")
        if legacy_timeout and not _env_text("WORKFLOW_REVIEWER_TIMEOUT", ""):
            _warn_deprecated_env("WORKFLOW_OPUS_REVIEWER_TIMEOUT", "WORKFLOW_REVIEWER_TIMEOUT")
        base.opus_reviewer_timeout_seconds = _env_int("WORKFLOW_REVIEWER_TIMEOUT", _env_int("WORKFLOW_OPUS_REVIEWER_TIMEOUT", base.opus_reviewer_timeout_seconds))
        return base

    def check_health(self) -> tuple[bool, str]:
        backend = self._resolved_executor_backend()
        if not backend:
            return False, "executor backend not configured"
        if backend == NOOP_TEST_ONLY_BACKEND:
            if is_test_environment():
                return True, "simulate:no_op_test_only" if self.config.simulate else "noop_test_only"
            return False, "noop_test_only backend is only allowed in tests"
        if backend == MANUAL_BACKEND:
            return True, "manual-execution-backend"
        command = str(self.config.executor_command or "").strip()
        if command:
            return True, "external_cli configured"
        executable = str(self.config.executable or "").strip()
        if executable and Path(executable).exists():
            return True, f"{executable} available"
        resolved = shutil.which(self.config.executable)
        if resolved:
            return True, f"{self.config.executable} available at {resolved}"
        return False, "external_cli backend requires executor command"

    def peer_review_preflight(self, *, task: str, context: dict[str, Any]) -> dict[str, Any]:
        del task
        workspace_root = Path(str(context.get("project_root") or self.config.cwd or Path.cwd())).resolve()
        backend = self._resolved_reviewer_backend()
        _, gateway_error = self._real_peer_review_gateway_config()
        return _preflight.peer_review_preflight(
            context=context,
            deps=_preflight.PeerReviewPreflightDeps(
                real_requested=self._real_peer_review_requested(),
                real_required=self._real_peer_review_required(),
                backend=backend,
                workspace_root=workspace_root,
                doctor_enabled=self._reviewer_doctor_enabled(),
                cli_config=self._cli_reviewer_config(),
                codex_config=self._codex_reviewer_config(),
                reviewer_base_url=_clean_text(self.config.reviewer_base_url) or _clean_text(self.config.opus_gateway_base_url),
                reviewer_api_key=_clean_text(self.config.reviewer_api_key) or _clean_text(self.config.opus_gateway_api_key),
                health_cache=self._reviewer_health_cache,
                gateway_error=gateway_error,
                cli_available_fn=cli_reviewer_available,
                codex_available_fn=codex_reviewer_available,
                probe_backend_fn=probe_reviewer_backend,
                write_health_fn=self._write_reviewer_health_artifact,
                failed_review_fn=self._failed_real_peer_review,
            ),
        )

    def _reviewer_doctor_enabled(self) -> bool:
        mode = _clean_text(_env_text("WORKFLOW_REVIEWER_DOCTOR", "1")).lower()
        return mode not in {"0", "false", "off", "skip"}

    def _write_reviewer_health_artifact(self, *, context: dict[str, Any], report: dict[str, Any]) -> None:
        planning_dir_raw = _clean_text(context.get("planning_dir"))
        if not planning_dir_raw:
            return
        try:
            planning_dir = Path(planning_dir_raw).resolve()
            planning_dir.mkdir(parents=True, exist_ok=True)
            payload = dict(report or {})
            payload["doctor_enabled"] = self._reviewer_doctor_enabled()
            payload.setdefault("probe_source", "same_run")
            run_id = _clean_text(context.get("run_id"))
            if run_id:
                payload["run_id"] = run_id
            atomic_write_canonical_json(planning_dir / "reviewer_health.json", payload)
        except OSError:
            logger.warning("failed to write reviewer_health.json", exc_info=True)

    def _resolved_executor_backend(self) -> str:
        if self.config.simulate and is_test_environment() and not str(self.config.executor_backend or "").strip():
            return NOOP_TEST_ONLY_BACKEND
        return resolve_execution_backend(
            self.config.executor_backend,
            test_environment=is_test_environment(),
        )

    def _resolved_self_review_backend(self) -> str:
        configured = str(self.config.self_review_backend or "").strip()
        if configured:
            return resolve_execution_backend(
                configured,
                test_environment=is_test_environment(),
            )
        return ""

    def _infer_changed_files(self, context: dict[str, Any]) -> list[str]:
        return _changes.infer_changed_files(
            context,
            cwd=self.config.cwd,
            default_changed_files=list(self.config.default_changed_files),
        )

    def _explicit_changed_files(self, context: dict[str, Any]) -> list[str]:
        return _changes.explicit_changed_files(context)

    def _is_fix_action(self, context: dict[str, Any]) -> bool:
        return _changes.is_fix_action(context)

    def _changed_files_from_hints(self, context: dict[str, Any]) -> list[str]:
        return _changes.changed_files_from_hints(context)

    def _changed_files_from_scope_text(self, context: dict[str, Any]) -> list[str]:
        return _changes.changed_files_from_scope_text(context)

    def _extract_path_tokens(self, text: str) -> list[str]:
        return _changes.extract_path_tokens(text)

    def _workspace_default_changed_files(self) -> list[str]:
        return _changes.workspace_default_changed_files(self.config.cwd)

    def implement(self, task: str, context: dict[str, Any]) -> dict[str, Any]:
        if bool(context.get("simulate_failure")):
            return self._error_result(
                message=str(context.get("simulate_failure_message") or "simulated implementation failure"),
                attempt=int(context.get("attempt", 1) or 1),
            )
        planning_dir = Path(str(context.get("planning_dir") or self.config.cwd or Path.cwd())).resolve()
        backend_context = dict(context)
        backend_context.setdefault("project_root", str(Path(str(context.get("project_root") or self.config.cwd or Path.cwd())).resolve()))
        backend_context.setdefault("planning_dir", str(planning_dir))
        backend_context.setdefault("task_id", str(context.get("task_id") or task))
        backend_context.setdefault("requested_action", "implement")
        allowed_files = [str(item) for item in list(context.get("task_card_files") or []) if str(item).strip()]
        task_card = backend_context.get("task_card")
        if not allowed_files and not is_verification_only_task(backend_context, task_card if isinstance(task_card, dict) else None):
            allowed_files = self._infer_changed_files(context)
        backend_result = run_execution_backend(
            config=self._execution_backend_config(task=task, context=context, planning_dir=planning_dir),
            task=task,
            context=backend_context,
            allowed_files=allowed_files,
        )
        backend_result.setdefault("attempt", int(context.get("attempt", 1) or 1))
        return backend_result

    def _execution_backend_config(
        self,
        *,
        task: str,
        context: dict[str, Any],
        planning_dir: Path,
    ) -> ExecutionBackendConfig:
        resolved_backend = self._resolved_executor_backend()
        default_executable = (
            _env_text("WORKFLOW_CLAUDE_EXECUTABLE", str(self.config.executable or "claude"))
            if resolved_backend == CLAUDE_CODE_BACKEND
            else str(self.config.executable or "codex")
        )
        return ExecutionBackendConfig(
            backend=resolved_backend,
            command=str(
                self.config.executor_command
                or context.get("executor_command")
                or context.get("local_command")
                or ""
            ).strip(),
            project_root=Path(str(context.get("project_root") or self.config.cwd or Path.cwd())).resolve(),
            planning_dir=planning_dir,
            feature=str(context.get("feature") or task).strip(),
            executable=default_executable,
            timeout_seconds=max(30, int(self.config.timeout_seconds)),
            model=_clean_text(self.config.executor_model),
            base_url=_clean_text(self.config.executor_base_url),
            base_url_env=_clean_text(self.config.executor_base_url_env),
            api_key_env=_clean_text(self.config.executor_api_key_env),
            api_format=_clean_text(self.config.executor_api_format),
            transport_name=_clean_text(self.config.executor_transport_name),
            execution_protocol=_clean_text(self.config.executor_execution_protocol),
            runtime_caps=dict(self.config.executor_runtime_caps or {}),
        )

    def _success_result(self, context: dict[str, Any], *, mode: str) -> dict[str, Any]:
        return {
            "status": "done",
            "changes": self._infer_changed_files(context),
            "attempt": int(context.get("attempt", 1) or 1),
            "mode": mode,
        }

    def _error_result(self, *, message: str, attempt: int) -> dict[str, Any]:
        return {
            "status": "error",
            "error": message,
            "changes": [],
            "attempt": attempt,
        }

    def review(
        self,
        *,
        task: str,
        context: dict[str, Any],
        changed_files: list[str],
        review_iteration: int = 0,
    ) -> dict[str, Any]:
        real_requested = self._real_peer_review_requested()
        real_required = self._real_peer_review_required()
        resolved, real_error = self._resolve_real_review_path(
            task=task,
            context=context,
            changed_files=changed_files,
            review_iteration=review_iteration,
            real_requested=real_requested,
            real_required=real_required,
        )
        if resolved is not None:
            return resolved
        if real_error:
            logger.warning("real review unavailable; falling back to simulated review: %s", real_error)
        payload = self._simulate_review_payload(context, changed_files, review_iteration)
        return self._with_review_runtime(
            payload,
            mode="simulate_local",
            real_requested=real_requested,
            real_required=real_required,
            fallback_used=bool(real_requested and real_error),
            error=real_error,
        )

    def synthesize_executor_recovery(
        self,
        *,
        task: str,
        context: dict[str, Any],
        must_fix: list[str],
        stall_report: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        config = self._recovery_synthesizer_config()
        return _recovery.synthesize_executor_recovery_result(
            task=task,
            context=context,
            must_fix=must_fix,
            stall_report=stall_report,
            config=config,
            cwd=self.config.cwd,
            request_decision_fn=request_recovery_decision,
            normalize_decision_fn=normalize_recovery_decision,
        )

    def _recovery_synthesizer_config(self) -> RecoverySynthesizerConfig:
        return _recovery.build_recovery_synthesizer_config(
            self.config,
            resolved_reviewer_backend=self._resolved_reviewer_backend(),
        )

    @staticmethod
    def _recovery_synthesizer_unavailable_reason(config: RecoverySynthesizerConfig) -> str:
        return _recovery.recovery_synthesizer_unavailable_reason(config)

    @staticmethod
    def _requested_existing_recovery_files(decision: dict[str, Any], *, allowed_files: list[str]) -> list[str]:
        return _recovery.requested_existing_recovery_files(decision, allowed_files=allowed_files)

    def _resolve_real_review_path(
        self,
        *,
        task: str,
        context: dict[str, Any],
        changed_files: list[str],
        review_iteration: int,
        real_requested: bool,
        real_required: bool,
    ) -> tuple[dict[str, Any] | None, str]:
        if not real_requested:
            return None, ""
        review, error = self._real_peer_review(
            task=task,
            context=context,
            changed_files=changed_files,
            review_iteration=review_iteration,
        )
        if review is not None:
            planning_dir = Path(str(context.get("planning_dir") or self.config.cwd or Path.cwd())).resolve()
            backend = self._resolved_reviewer_backend()
            mode = {"cli": "real_cli_reviewer", "mcp": "real_mcp_reviewer", "codex": "real_codex_reviewer"}.get(backend, "real_peer_review_gateway")
            return (
                self._with_review_runtime(
                    review,
                    mode=mode,
                    real_requested=real_requested,
                    real_required=real_required,
                    bundle_path=str((planning_dir / REVIEW_BUNDLE_FILENAME).resolve()),
                ),
                "",
            )
        detail = _clean_text(error)
        if real_required:
            logger.warning("required real review failed: %s", detail)
            return self._failed_real_peer_review(error=detail, real_required=True), ""
        return None, detail

    def _simulate_review_payload(
        self,
        context: dict[str, Any],
        changed_files: list[str],
        review_iteration: int,
    ) -> dict[str, Any]:
        return _review_runtime.simulate_review_payload(context, changed_files, review_iteration)

    def _review_runtime_payload(
        self,
        *,
        mode: str,
        real_requested: bool,
        real_required: bool,
        fallback_used: bool = False,
        error: str = "",
    ) -> dict[str, Any]:
        return _review_runtime.review_runtime_payload(
            mode=mode,
            real_requested=real_requested,
            real_required=real_required,
            fallback_used=fallback_used,
            error=error,
            gateway=self._review_runtime_gateway(),
        )

    def _normalized_review_mode(self, mode: str) -> str:
        return _review_runtime.normalized_review_mode(mode)

    def _review_runtime_gateway(self) -> dict[str, str]:
        return _review_runtime.review_runtime_gateway(
            backend=self._resolved_reviewer_backend(),
            reviewer_model=_clean_text(self.config.reviewer_model),
            reviewer_base_url=_clean_text(self.config.reviewer_base_url),
            opus_gateway_base_url=_clean_text(self.config.opus_gateway_base_url),
            reviewer_api_format=_clean_text(self.config.reviewer_api_format),
            opus_gateway_model=_clean_text(self.config.opus_gateway_model),
            opus_gateway_api_format=_clean_text(self.config.opus_gateway_api_format),
        )

    def _attach_runtime_error(self, runtime: dict[str, Any], *, error: str) -> None:
        _review_runtime.attach_runtime_error(runtime, error=error)

    def _with_review_runtime(
        self,
        payload: dict[str, Any],
        *,
        mode: str,
        real_requested: bool,
        real_required: bool,
        fallback_used: bool = False,
        error: str = "",
        bundle_path: str = "",
    ) -> dict[str, Any]:
        return _review_runtime.with_review_runtime(
            payload,
            mode=mode,
            real_requested=real_requested,
            real_required=real_required,
            gateway=self._review_runtime_gateway(),
            fallback_used=fallback_used,
            error=error,
            bundle_path=bundle_path,
        )

    def _real_peer_review_requested(self) -> bool:
        return bool(self.config.real_peer_review or self.config.require_real_peer_review)

    def _real_peer_review_required(self) -> bool:
        return bool(self.config.require_real_peer_review)

    def _real_peer_review_gateway_config(self) -> tuple[OpusGatewayConfig | None, str]:
        base_url = _clean_text(self.config.reviewer_base_url) or _clean_text(self.config.opus_gateway_base_url)
        api_key = _clean_text(self.config.reviewer_api_key) or _clean_text(self.config.opus_gateway_api_key)
        if not base_url:
            return None, "WORKFLOW_REVIEWER_BASE_URL (or WORKFLOW_OPUS_GATEWAY) is empty"
        if not api_key:
            return None, "WORKFLOW_REVIEWER_API_KEY (or WORKFLOW_OPUS_API_KEY) is empty"
        model = _clean_text(self.config.reviewer_model) or _clean_text(self.config.opus_gateway_model)
        api_format = _clean_text(self.config.reviewer_api_format) or _clean_text(self.config.opus_gateway_api_format)
        return (
            OpusGatewayConfig(
                base_url=base_url,
                api_key=api_key,
                model=model,
                timeout_seconds=int(self.config.opus_gateway_timeout_seconds),
                api_format=api_format or "auto",
                retry_attempts=int(self.config.opus_gateway_retry_attempts),
                max_tokens=int(self.config.opus_gateway_max_tokens),
            ),
            "",
        )

    def _resolved_reviewer_backend(self) -> str:
        configured = _clean_text(self.config.reviewer_backend).lower() or "auto"
        if configured.startswith("unsupported:"):
            return configured  # propagate; preflight will block
        if configured in {"api", "cli", "mcp", "codex"}:
            return configured
        # "auto" or empty: use api (backward-compatible default)
        return "api"

    def _cli_reviewer_config(self) -> CliReviewerConfig:
        return CliReviewerConfig(
            executable=_clean_text(self.config.reviewer_executable_claude) or "claude",
            timeout_seconds=int(self.config.opus_reviewer_timeout_seconds or 120),
            max_tokens=int(self.config.opus_gateway_max_tokens or 4096),
            retry_attempts=int(self.config.opus_gateway_retry_attempts or 1),
            model=_clean_text(self.config.reviewer_model),
        )

    def _codex_reviewer_config(self) -> CodexReviewerConfig:
        return CodexReviewerConfig(
            executable=_clean_text(self.config.reviewer_executable_codex) or "codex",
            timeout_seconds=int(self.config.opus_reviewer_timeout_seconds or 180),
            retry_attempts=int(self.config.opus_gateway_retry_attempts or 1),
            model=_clean_text(self.config.reviewer_model),
        )

    def _real_peer_review(
        self,
        *,
        task: str,
        context: dict[str, Any],
        changed_files: list[str],
        review_iteration: int,
    ) -> tuple[dict[str, Any] | None, str]:
        backend = self._resolved_reviewer_backend()
        if backend.startswith("unsupported:"):
            return None, f"unsupported reviewer backend: {backend[len('unsupported:'):]}"
        if backend == "api":
            return self._real_peer_review_api(task=task, context=context, changed_files=changed_files, review_iteration=review_iteration)
        return self._real_peer_review_cli(task=task, context=context, changed_files=changed_files, review_iteration=review_iteration, use_mcp=(backend == "mcp"), use_codex=(backend == "codex"))

    def _prepare_review_artifacts(
        self,
        *,
        task: str,
        context: dict[str, Any],
        changed_files: list[str],
        review_iteration: int,
    ) -> tuple[dict[str, Any], dict[str, Any], Path]:
        """Build deterministic findings and review bundle (shared by api/cli)."""
        project_root = Path(str(context.get("project_root") or self.config.cwd or Path.cwd())).resolve()
        planning_dir = Path(str(context.get("planning_dir") or project_root)).resolve()
        deterministic_findings = compute_deterministic_findings(
            planning_dir=planning_dir,
            changed_files=changed_files,
            task_card_files=[str(item) for item in list(context.get("task_card_files") or []) if str(item).strip()],
            invariants=[str(item) for item in list(context.get("task_invariants") or []) if str(item).strip()],
            project_root=project_root,
            runtime_verify_check=dict(context.get("runtime_verify_check") or {}),
        )
        review_bundle = build_review_bundle(
            feature=str(context.get("feature") or task).strip(),
            task=task,
            project_root=project_root,
            planning_dir=planning_dir,
            context=context,
            changed_files=changed_files,
            review_iteration=review_iteration,
            deterministic_findings=deterministic_findings,
        )
        write_review_bundle(planning_dir / REVIEW_BUNDLE_FILENAME, review_bundle)
        return deterministic_findings, review_bundle, planning_dir

    def _real_peer_review_api(self, *, task: str, context: dict[str, Any], changed_files: list[str], review_iteration: int) -> tuple[dict[str, Any] | None, str]:
        """Original HTTP API path (requires API key)."""
        gateway, error = self._real_peer_review_gateway_config()
        if gateway is None:
            return None, error
        deterministic_findings, review_bundle, _ = self._prepare_review_artifacts(task=task, context=context, changed_files=changed_files, review_iteration=review_iteration)
        review, request_error = request_opus_review(gateway, task=task, context=context, changed_files=changed_files, review_iteration=review_iteration, review_bundle=review_bundle)
        if review is None:
            return None, request_error
        return _apply_deterministic_review_guard(review, deterministic_findings=deterministic_findings), ""

    def _real_peer_review_cli(
        self,
        *,
        task: str,
        context: dict[str, Any],
        changed_files: list[str],
        review_iteration: int,
        use_mcp: bool = False,
        use_codex: bool = False,
    ) -> tuple[dict[str, Any] | None, str]:
        """CLI-based review path (Claude or Codex, no API key)."""
        deterministic_findings, review_bundle, _ = self._prepare_review_artifacts(
            task=task, context=context,
            changed_files=changed_files, review_iteration=review_iteration,
        )
        project_root = Path(str(context.get("project_root") or self.config.cwd or Path.cwd())).resolve()
        if use_codex:
            review, request_error = request_codex_review(
                self._codex_reviewer_config(),
                task=task, context=context,
                changed_files=changed_files, review_iteration=review_iteration,
                review_bundle=review_bundle,
                project_root=project_root,
            )
        else:
            cli_config = self._cli_reviewer_config()
            request_fn = request_mcp_review if use_mcp else request_cli_review
            review, request_error = request_fn(
                cli_config,
                task=task, context=context,
                changed_files=changed_files, review_iteration=review_iteration,
                review_bundle=review_bundle,
                project_root=project_root,
            )
        if review is None:
            return None, request_error
        guarded = _apply_deterministic_review_guard(review, deterministic_findings=deterministic_findings)
        backend = self._resolved_reviewer_backend()
        provenance = {"cli": "cli_reviewer", "mcp": "mcp_reviewer", "codex": "codex_reviewer"}.get(backend, "opus_gateway")
        guarded["reviewer"] = provenance
        guarded["source"] = f"kodawari.real_{provenance}"
        return guarded, ""

    def _failed_real_peer_review(self, *, error: str, real_required: bool) -> dict[str, Any]:
        return _review_runtime.failed_real_peer_review(
            error=error,
            real_required=real_required,
            gateway=self._review_runtime_gateway(),
        )

    def _needs_test_updates(self, changed_files: list[str], review_iteration: int) -> bool:
        return _review_runtime.needs_test_updates(changed_files, review_iteration)

    def _task_scope_allows_test_updates(self, context: dict[str, Any]) -> bool:
        return _review_runtime.task_scope_allows_test_updates(context)

    def _static_review(
        self, *, approved: bool, summary: str, must_fix: list[str],
        should_fix: list[str] | None = None, severity: str, score: int,
        gate_recommendation: str,
    ) -> dict[str, Any]:
        return _review_runtime.static_review(
            approved=approved,
            summary=summary,
            must_fix=must_fix,
            should_fix=should_fix,
            severity=severity,
            score=score,
            gate_recommendation=gate_recommendation,
        )

    def _review_no_changes(self) -> dict[str, Any]:
        return _review_runtime.review_no_changes()

    def _review_missing_tests(self) -> dict[str, Any]:
        return _review_runtime.review_missing_tests()

    def _review_test_scope_conflict(self, changed_files: list[str]) -> dict[str, Any]:
        return _review_runtime.review_test_scope_conflict(changed_files)

    def _review_approved(self) -> dict[str, Any]:
        return _review_runtime.review_approved()

    def _self_review_input(self, *, task: str, context: dict[str, Any], changed_files: list[str], review_iteration: int) -> dict[str, Any]:
        return _self_review_runtime.self_review_input(
            task=task,
            context=context,
            changed_files=changed_files,
            review_iteration=review_iteration,
        )

    def _external_self_review(
        self,
        *,
        task: str,
        context: dict[str, Any],
        changed_files: list[str],
        review_iteration: int,
    ) -> dict[str, Any]:
        return _self_review_runtime.external_self_review(
            self,
            task=task,
            context=context,
            changed_files=changed_files,
            review_iteration=review_iteration,
        )

    def self_review(
        self,
        *,
        task: str,
        context: dict[str, Any],
        changed_files: list[str],
        review_iteration: int = 0,
    ) -> dict[str, Any]:
        backend = self._resolved_self_review_backend()
        if backend == EXTERNAL_CLI_BACKEND:
            payload = self._external_self_review(
                task=task,
                context=context,
                changed_files=changed_files,
                review_iteration=review_iteration,
            )
        elif not backend:
            # No-fake-run policy Fix 8: ``run_codex_self_review`` is
            # deterministic — it returns approved=bool(content), no LLM
            # call. Previously this payload silently passed self-review
            # whenever any file changed. Now: in production strict mode,
            # explicitly mark the result as not-a-real-review with
            # approved=False so downstream gates surface the missing
            # backend. Dev / subscription / test runs keep the legacy
            # bool(content) behavior so local iteration without
            # WORKFLOW_SELF_REVIEW_BACKEND set still works.
            from kodawari.autopilot.core.runtime_checks import _no_fake_run_strict
            content = "\n".join(str(item) for item in changed_files if str(item).strip())
            payload = run_codex_self_review(task, content, reviewer="codex")
            payload["source"] = "kodawari.self_review.local_default"
            payload["review_quality"] = "local_default"
            if _no_fake_run_strict():
                payload["approved"] = False
                payload["blocking_reason"] = "LOCAL_DEFAULT_NOT_A_REVIEW"
                payload["summary"] = (
                    "local_default self-review is deterministic file-presence "
                    "only — production strict mode requires a real self-review "
                    "backend (set WORKFLOW_SELF_REVIEW_BACKEND or unset "
                    "WORKFLOW_REVIEW_ENABLED)."
                )
        elif backend == MANUAL_BACKEND:
            payload = {
                "status": "BLOCKED",
                "approved": False,
                "summary": "manual self-review backend cannot complete inside autopilot loop",
                "blocking_reason": "SELF_REVIEW_MANUAL_UNSUPPORTED",
                "reviewer": "codex",
                "source": "kodawari.self_review.manual",
            }
        elif backend == NOOP_TEST_ONLY_BACKEND and is_test_environment():
            content = "\n".join(str(item) for item in changed_files if str(item).strip())
            payload = run_codex_self_review(task, content, reviewer="codex")
            payload["source"] = "kodawari.self_review.noop_test_only"
        elif backend == NOOP_TEST_ONLY_BACKEND:
            # No-fake-run policy Fix 11: the noop_fallback path used to
            # silently pass self-review when WORKFLOW_SELF_REVIEW_BACKEND
            # was set to noop_test_only but the runtime is NOT in a test
            # environment (operator misconfiguration). In production
            # strict mode, mark as not-a-real-review. Outside strict
            # mode, keep the fallback for parity but label the runtime
            # quality so downstream gates can see it.
            from kodawari.autopilot.core.runtime_checks import _no_fake_run_strict
            content = "\n".join(str(item) for item in changed_files if str(item).strip())
            payload = run_codex_self_review(task, content, reviewer="codex")
            payload["source"] = "kodawari.self_review.noop_fallback"
            payload["review_quality"] = "noop_fallback"
            if _no_fake_run_strict():
                payload["approved"] = False
                payload["blocking_reason"] = "NOOP_FALLBACK_NOT_A_REVIEW"
                payload["summary"] = (
                    "noop_fallback self-review is deterministic — "
                    "production strict mode does not accept this path."
                )
        else:
            payload = {
                "status": "BLOCKED",
                "approved": False,
                "summary": "self-review backend is not configured for this environment",
                "blocking_reason": "SELF_REVIEW_BACKEND_MISSING",
                "reviewer": "codex",
                "source": "kodawari.self_review.unconfigured",
            }
        payload["task"] = task
        return payload

    def post_execution_qa(
        self,
        *,
        task: str,
        context: dict[str, Any],
        artifacts: list[str],
    ) -> dict[str, Any]:
        payload = run_post_execution_qa(task, artifacts=list(artifacts or []), context=context)
        payload["task"] = task
        return payload

    def override_review_config(
        self,
        *,
        real_peer_review: bool | None = None,
        require_real_peer_review: bool | None = None,
    ) -> dict[str, bool]:
        return _review_runtime.override_review_config(
            self.config,
            real_peer_review=real_peer_review,
            require_real_peer_review=require_real_peer_review,
        )

    def restore_review_config(self, original: dict[str, bool]) -> None:
        _review_runtime.restore_review_config(self.config, original)

    def on_hook_event(
        self,
        *,
        event: str,
        task: str,
        context: dict[str, Any],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        del context
        details = {
            "event": event,
            "task": task,
            "adapter_mode": "simulate" if self.config.simulate else "local-command",
            "cycle": payload.get("cycle"),
        }
        return {"status": "ok", "details": details}

