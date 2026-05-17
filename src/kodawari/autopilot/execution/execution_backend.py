"""Execution backend contract, capability matrix, and registry."""

from __future__ import annotations

from dataclasses import dataclass
import copy
from pathlib import Path
from typing import Any, Protocol


NOOP_TEST_ONLY_BACKEND = "noop_test_only"
MANUAL_BACKEND = "manual"
EXTERNAL_CLI_BACKEND = "external_cli"
CODEX_CLI_BACKEND = "codex_cli"
CLAUDE_CODE_BACKEND = "claude_code"
OPENAI_TOOL_USE_BACKEND = "openai_tool_use"

# Repo-wide default verify command; when a task card does not specify a
# scoped verify_cmd the planner falls back to this. Implementations should
# NOT actually run the full suite in the implementation step — the workflow
# runtime handles repo verify post-implementation.
DEFAULT_VERIFY_EXPECTATION = "pytest -q"

_VERIFY_PYTEST_PREFIXES = ("pytest ", "python -m pytest ", "python3 -m pytest ")


def _is_default_full_repo_verify(verify_cmd: str) -> bool:
    normalized = str(verify_cmd or "").strip()
    if not normalized:
        return False
    if normalized == DEFAULT_VERIFY_EXPECTATION:
        return True
    # Matches `pytest -q`, `pytest -q --tb=short`, `python -m pytest -q`, etc.
    # when no path argument is present (full-suite invocation).
    prefixed = normalized + " "
    for prefix in _VERIFY_PYTEST_PREFIXES:
        if prefixed.startswith(prefix):
            tail = normalized[len(prefix) - 1 :].strip().split()
            # All tokens are flags (starting with "-") → no test path argument
            if tail and all(tok.startswith("-") for tok in tail):
                return True
    return False


def verify_expectation_text(verify_cmd: str) -> str:
    """Soften the implementation-step verify instruction when the task card
    asks for a full-repo verify. Implementations should focus on task-local
    evidence; runtime is responsible for running the broader suite afterwards."""
    normalized = str(verify_cmd or "").strip()
    if _is_default_full_repo_verify(normalized):
        return (
            "default repo verify (pytest -q) is handled by workflow runtime after implementation; "
            "focus on task-local evidence instead of running the full suite in this step"
        )
    return normalized


@dataclass(frozen=True)
class ExecutionBackendDescriptor:
    name: str
    maturity: str
    implemented: bool
    executor_selectable: bool = True
    self_review_selectable: bool = False
    requires_command: bool = False
    supports_agent_teams: bool = False
    supports_worktree_isolation: bool = False
    supports_hooks: bool = False
    supports_memory: bool = False
    supports_deterministic_changed_files: bool = False

    def capabilities(self) -> dict[str, Any]:
        return {
            "backend": self.name,
            "maturity": self.maturity,
            "implemented": bool(self.implemented),
            "executor_selectable": bool(self.executor_selectable),
            "self_review_selectable": bool(self.self_review_selectable),
            "requires_command": bool(self.requires_command),
            "supports_agent_teams": bool(self.supports_agent_teams),
            "supports_worktree_isolation": bool(self.supports_worktree_isolation),
            "supports_hooks": bool(self.supports_hooks),
            "supports_memory": bool(self.supports_memory),
            "supports_deterministic_changed_files": bool(self.supports_deterministic_changed_files),
        }


_CAPABILITY_TRUTH_FIELDS = (
    "implemented",
    "supports_deterministic_changed_files",
    "supports_agent_teams",
    "supports_worktree_isolation",
    "supports_hooks",
    "supports_memory",
)

_CAPABILITY_DEFAULT_NOTES = {
    "implemented": "Execution backend runner is registered and can materialize canonical execution artifacts.",
    "supports_deterministic_changed_files": "Backend can derive changed_files deterministically instead of trusting model output.",
        "supports_agent_teams": "Backend can allocate native worker/reviewer team primitives.",
        "supports_worktree_isolation": "Backend can execute in an isolated worktree or equivalent isolated directory.",
        "supports_hooks": "Backend can use native host hooks instead of kernel-side preflight checks only.",
        "supports_memory": "Backend can use native host memory instead of prompt-level compact context injection only.",
}

_BACKEND_CAPABILITY_TRUTH_OVERRIDES: dict[str, dict[str, dict[str, Any]]] = {
    CLAUDE_CODE_BACKEND: {
        "supports_agent_teams": {
            "runtime_state": "planned",
            "evidence": [],
            "note": "Current claude_code execution uses a single `claude -p` subprocess and does not allocate native agent teams.",
        },
        "supports_worktree_isolation": {
            "runtime_state": "verified",
            "evidence": ["execution_claude_code.directory_isolation"],
            "note": "claude_code allocates a directory-isolated execution workspace under planning_dir/.parallel_workers and syncs allowed files back.",
        },
        "supports_hooks": {
            "runtime_state": "kernel_only",
            "evidence": ["execution_claude_code.preflight_guard"],
            "note": "Override commands are checked by a kernel-side preflight guard, but native Claude host hooks are not wired.",
        },
        "supports_memory": {
            "runtime_state": "kernel_only",
            "evidence": ["execution_claude_code.semantic_compact_prompt_injection"],
            "note": "semantic_compact.json is injected into the prompt, but native Claude host memory is not wired.",
        },
    },
    CODEX_CLI_BACKEND: {
        "supports_agent_teams": {
            "runtime_state": "planned",
            "evidence": [],
            "note": "Current codex_cli execution still shells out to the CLI and does not allocate native agent teams.",
        },
        "supports_worktree_isolation": {
            "runtime_state": "default_on",
            "evidence": ["execution_codex_cli.directory_isolation"],
            "note": "codex_cli isolation is ON by default (hardening wave 2026-04-16). Opt-out via WORKFLOW_CODEX_ISOLATION=0 or isolation_workspace=False. Allowed files are synced back on success only; failed runs leave the workspace for debugging.",
        },
        "supports_hooks": {
            "runtime_state": "planned",
            "evidence": [],
            "note": "Current codex_cli execution has no native host hook integration.",
        },
        "supports_memory": {
            "runtime_state": "planned",
            "evidence": [],
            "note": "Current codex_cli execution has no native host memory integration.",
        },
    },
}


_BACKEND_DESCRIPTORS: dict[str, ExecutionBackendDescriptor] = {
    EXTERNAL_CLI_BACKEND: ExecutionBackendDescriptor(
        name=EXTERNAL_CLI_BACKEND,
        maturity="stable",
        implemented=True,
        executor_selectable=True,
        self_review_selectable=True,
        requires_command=True,
    ),
    MANUAL_BACKEND: ExecutionBackendDescriptor(
        name=MANUAL_BACKEND,
        maturity="stable",
        implemented=True,
        executor_selectable=True,
        self_review_selectable=True,
    ),
    NOOP_TEST_ONLY_BACKEND: ExecutionBackendDescriptor(
        name=NOOP_TEST_ONLY_BACKEND,
        maturity="test_only",
        implemented=True,
        executor_selectable=True,
        self_review_selectable=True,
        supports_deterministic_changed_files=True,
    ),
    CODEX_CLI_BACKEND: ExecutionBackendDescriptor(
        name=CODEX_CLI_BACKEND,
        maturity="stable",
        implemented=True,
        executor_selectable=True,
        self_review_selectable=False,
        supports_worktree_isolation=True,
        supports_deterministic_changed_files=True,
    ),
    CLAUDE_CODE_BACKEND: ExecutionBackendDescriptor(
        name=CLAUDE_CODE_BACKEND,
        maturity="beta",
        implemented=True,
        executor_selectable=True,
        self_review_selectable=False,
        supports_agent_teams=False,
        supports_worktree_isolation=True,
        supports_hooks=False,
        supports_memory=False,
        supports_deterministic_changed_files=True,
    ),
    OPENAI_TOOL_USE_BACKEND: ExecutionBackendDescriptor(
        name=OPENAI_TOOL_USE_BACKEND,
        maturity="beta",
        implemented=True,
        executor_selectable=True,
        self_review_selectable=False,
        supports_worktree_isolation=True,
        supports_deterministic_changed_files=True,
    ),
}

_EXECUTION_BACKEND_ORDER = tuple(
    name
    for name, descriptor in _BACKEND_DESCRIPTORS.items()
    if descriptor.executor_selectable
)
_SELF_REVIEW_BACKEND_ORDER = tuple(
    name
    for name, descriptor in _BACKEND_DESCRIPTORS.items()
    if descriptor.self_review_selectable
)

ALLOWED_EXECUTION_BACKENDS = {
    "",
    *_BACKEND_DESCRIPTORS.keys(),
}


@dataclass(frozen=True)
class ExecutionBackendConfig:
    backend: str
    command: str
    project_root: Path
    planning_dir: Path
    feature: str
    executable: str = "codex"
    timeout_seconds: int = 600
    model: str = ""
    base_url: str = ""
    base_url_env: str = ""
    api_key_env: str = ""
    api_format: str = ""
    transport_name: str = ""
    execution_protocol: str = ""
    runtime_caps: dict[str, int] | None = None


@dataclass(frozen=True)
class ExecutionBackendInvocation:
    config: ExecutionBackendConfig
    task: str
    context: dict[str, Any]
    allowed_files: list[str]
    request_path: Path
    result_path: Path
    request_payload: dict[str, Any]


class ExecutionBackendRunner(Protocol):
    def __call__(self, invocation: ExecutionBackendInvocation) -> dict[str, Any]:
        """Materialize canonical execution.result.v1 payload."""


_BACKEND_REGISTRY: dict[str, ExecutionBackendRunner] = {}
_DEFAULT_BACKENDS_REGISTERED = False


def execution_backend_choices() -> list[str]:
    return list(_EXECUTION_BACKEND_ORDER)


def self_review_backend_choices() -> list[str]:
    return list(_SELF_REVIEW_BACKEND_ORDER)


def execution_backend_descriptor(name: str) -> ExecutionBackendDescriptor:
    normalized = str(name or "").strip().lower()
    descriptor = _BACKEND_DESCRIPTORS.get(normalized)
    if descriptor is not None:
        return descriptor
    if not normalized:
        return ExecutionBackendDescriptor(
            name="",
            maturity="unconfigured",
            implemented=False,
            executor_selectable=False,
            self_review_selectable=False,
        )
    return ExecutionBackendDescriptor(
        name=normalized,
        maturity="unknown",
        implemented=False,
        executor_selectable=False,
        self_review_selectable=False,
    )


def execution_backend_capabilities(name: str) -> dict[str, Any]:
    return execution_backend_descriptor(name).capabilities()


def execution_backend_capability_truth(name: str) -> dict[str, Any]:
    descriptor = execution_backend_descriptor(name)
    capabilities = descriptor.capabilities()
    truth: dict[str, Any] = {}
    overrides = _BACKEND_CAPABILITY_TRUTH_OVERRIDES.get(str(descriptor.name or "").strip().lower(), {})
    for field in _CAPABILITY_TRUTH_FIELDS:
        descriptor_value = bool(capabilities.get(field))
        runtime_state = "verified" if descriptor_value else "planned"
        entry = {
            "descriptor_value": descriptor_value,
            "runtime_state": runtime_state,
            "evidence": [],
            "note": _CAPABILITY_DEFAULT_NOTES[field],
        }
        entry.update(copy.deepcopy(overrides.get(field, {})))
        truth[field] = entry
    return truth


def resolve_execution_backend(value: str, *, test_environment: bool) -> str:
    normalized = str(value or "").strip().lower()
    if normalized and normalized not in ALLOWED_EXECUTION_BACKENDS:
        supported = sorted(item for item in ALLOWED_EXECUTION_BACKENDS if item)
        raise ValueError(f"unsupported executor backend '{normalized}'; expected one of {supported}")
    if normalized:
        return normalized
    return NOOP_TEST_ONLY_BACKEND if test_environment else ""


def register_execution_backend(name: str, runner: ExecutionBackendRunner) -> None:
    normalized = str(name or "").strip().lower()
    if not normalized:
        raise ValueError("execution backend name cannot be empty")
    _BACKEND_REGISTRY[normalized] = runner


def ensure_default_execution_backends() -> None:
    global _DEFAULT_BACKENDS_REGISTERED
    if _DEFAULT_BACKENDS_REGISTERED:
        return

    from kodawari.autopilot.execution.execution_claude_code import materialize_claude_code_result
    from kodawari.autopilot.execution.execution_codex_cli import materialize_codex_cli_result
    from kodawari.autopilot.execution.execution_openai_tool_use import materialize_openai_tool_use_result

    def _codex_runner(invocation: ExecutionBackendInvocation) -> dict[str, Any]:
        return materialize_codex_cli_result(
            config=invocation.config,
            request_path=invocation.request_path,
            request_payload=invocation.request_payload,
        )

    def _claude_runner(invocation: ExecutionBackendInvocation) -> dict[str, Any]:
        return materialize_claude_code_result(
            config=invocation.config,
            request_path=invocation.request_path,
            request_payload=invocation.request_payload,
        )

    def _openai_tool_runner(invocation: ExecutionBackendInvocation) -> dict[str, Any]:
        return materialize_openai_tool_use_result(
            config=invocation.config,
            request_path=invocation.request_path,
            request_payload=invocation.request_payload,
        )

    register_execution_backend(CODEX_CLI_BACKEND, _codex_runner)
    register_execution_backend(CLAUDE_CODE_BACKEND, _claude_runner)
    register_execution_backend(OPENAI_TOOL_USE_BACKEND, _openai_tool_runner)
    _DEFAULT_BACKENDS_REGISTERED = True


def registered_execution_backend_runner(name: str) -> ExecutionBackendRunner | None:
    ensure_default_execution_backends()
    normalized = str(name or "").strip().lower()
    if not normalized:
        return None
    return _BACKEND_REGISTRY.get(normalized)


def run_registered_execution_backend(
    name: str,
    *,
    invocation: ExecutionBackendInvocation,
) -> dict[str, Any] | None:
    runner = registered_execution_backend_runner(name)
    if runner is None:
        return None
    return runner(invocation)


__all__ = [
    "ALLOWED_EXECUTION_BACKENDS",
    "CLAUDE_CODE_BACKEND",
    "CODEX_CLI_BACKEND",
    "ExecutionBackendConfig",
    "ExecutionBackendDescriptor",
    "ExecutionBackendInvocation",
    "EXTERNAL_CLI_BACKEND",
    "MANUAL_BACKEND",
    "NOOP_TEST_ONLY_BACKEND",
    "OPENAI_TOOL_USE_BACKEND",
    "DEFAULT_VERIFY_EXPECTATION",
    "ensure_default_execution_backends",
    "execution_backend_capability_truth",
    "execution_backend_capabilities",
    "execution_backend_choices",
    "execution_backend_descriptor",
    "register_execution_backend",
    "registered_execution_backend_runner",
    "resolve_execution_backend",
    "run_registered_execution_backend",
    "self_review_backend_choices",
    "verify_expectation_text",
]

