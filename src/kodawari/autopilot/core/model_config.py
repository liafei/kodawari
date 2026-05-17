"""Workflow model configuration loader.

Reads .claude/workflow/models.yaml from the project root and surfaces model
selections for workflow roles.

Two schema generations are supported:

models.v1
  Legacy flat fields: planner_model, executor_model, executor_models,
  reviewer_model, plan_reviewer_model, reviewer_backend, review_enabled.

models.v2
  Transport pool + roles + compatibility matrix.  v1 fields are ignored in
  v2 files; projected legacy attributes are derived from roles so existing
  runtime entrypoints can migrate incrementally.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import fnmatch
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_MODELS_YAML_REL = ".claude/workflow/models.yaml"
_SCHEMA_V1 = "models.v1"
_SCHEMA_V2 = "models.v2"
_VALID_BACKENDS = {"api", "cli", "mcp", "codex"}
_VALID_ROLE_NAMES = {
    "planner",
    "reviewer",
    "plan_reviewer",
    "self_reviewer",
    "impl_reviewer",
    "gate_reviewer",
    "executor_recovery",
    "executor",
}
_VALID_ON_UNAVAILABLE = {"fail", "degrade_to_simulate", "skip", "degrade_to"}
_VALID_RUNTIME_CAP_KEYS = {
    "max_tool_iterations",
    "max_token_budget",
    "max_hard_token_budget",
    "max_same_tool_calls_per_path",
    "max_tool_calls_per_response",
    "max_wall_clock_seconds",
    "max_no_progress_iterations",
    "max_no_write_iterations",
    "max_no_write_iterations_under_budget_pressure",
    "max_redundant_read_count",
    "max_repeated_search_count",
    "max_patch_apply_failures",
    "max_context_overflow_retries",
    "max_recovery_attempts",
    "max_unproductive_fix_rounds",
    "recovery_timeout_seconds",
    "max_verify_retries",
    "max_http_retries",
    "http_timeout_seconds",
    "max_waf_retries",
    "verify_timeout_seconds",
    "max_full_read_tool_results",
    "max_full_read_tool_result_bytes",
}
_VALID_EXECUTION_PROTOCOLS = {"", "full_file_v1", "exact_str_replace_v1"}
_OPENAI_CHAT_API_FORMATS = {"openai", "openai_chat"}
_ANTHROPIC_API_FORMATS = {"anthropic", "anthropic_messages"}
_SUPPORTED_HTTP_API_FORMATS = _OPENAI_CHAT_API_FORMATS | _ANTHROPIC_API_FORMATS
_V1_FIELDS = {
    "planner_model",
    "executor_model",
    "executor_models",
    "reviewer_model",
    "plan_reviewer_model",
    "reviewer_backend",
}
_V2_FIELDS = {"transports", "roles", "compatibility"}


class WorkflowModelConfigError(ValueError):
    """Raised when models.v2 contains an invalid role/transport/model tuple."""


class Capability(str, Enum):
    REPO_READ_FILE = "repo.read_file"
    REPO_GREP = "repo.grep"
    REPO_GLOB = "repo.glob"
    REPO_WRITE_FILE = "repo.write_file"
    SHELL_EXEC = "shell.exec"
    NET_FETCH = "net.fetch"
    PATCH_EMIT = "patch.emit"
    TOOL_USE = "interface.tool_use"
    AGENT_LOOP = "interface.agent"
    CHAT = "interface.chat"
    MCP = "interface.mcp"


_CAPABILITY_VALUES = {item.value for item in Capability}
_TOOL_CAPABILITY_ALIASES = {
    "read": Capability.REPO_READ_FILE.value,
    "read_file": Capability.REPO_READ_FILE.value,
    "grep": Capability.REPO_GREP.value,
    "glob": Capability.REPO_GLOB.value,
    "write": Capability.REPO_WRITE_FILE.value,
    "write_file": Capability.REPO_WRITE_FILE.value,
    "edit": Capability.REPO_WRITE_FILE.value,
    "bash": Capability.SHELL_EXEC.value,
    "shell": Capability.SHELL_EXEC.value,
}
_LEGACY_CAPABILITY_ALIASES = {
    "repo_read": [
        Capability.REPO_READ_FILE.value,
        Capability.REPO_GREP.value,
        Capability.REPO_GLOB.value,
    ],
    "repo_write": [Capability.REPO_WRITE_FILE.value],
    "tool_use": [Capability.TOOL_USE.value],
    "agent": [Capability.AGENT_LOOP.value],
}


@dataclass
class WorkflowTransportConfig:
    name: str
    kind: str = ""
    driver: str = ""
    interface: str = ""
    executable: str = ""
    host_executable: str = ""
    api_format: str = ""
    base_url: str = ""
    base_url_env: str = ""
    api_key_env: str = ""
    mcp_server: str = ""
    quota_group: str = ""
    provides: list[str] = field(default_factory=list)

    def primary_executable(self) -> str:
        return self.host_executable or self.executable

    def legacy_reviewer_backend(self) -> str:
        driver = _normalize_key(self.driver)
        interface = _normalize_key(self.interface)
        kind = _normalize_key(self.kind)
        if interface == "mcp":
            return "mcp"
        if driver == "codex_cli":
            return "codex"
        if driver in {"claude_cli", "claude_code"}:
            return "cli"
        if kind == "http" or driver in {"openai_compatible", "anthropic_http", "mimo_http"}:
            return "api"
        return ""

    def executor_backend(self) -> str:
        driver = _normalize_key(self.driver)
        kind = _normalize_key(self.kind)
        interface = _normalize_key(self.interface)
        if driver == "codex_cli":
            return "codex_cli"
        if driver in {"claude_code", "claude_cli"}:
            return "claude_code"
        if kind == "http" and interface == "tool_use":
            return "openai_tool_use"
        if driver == "external_cli" or kind == "external":
            return "external_cli"
        if driver == "manual" or kind == "manual":
            return "manual"
        if driver == "noop_test_only" or kind == "test":
            return "noop_test_only"
        return ""


@dataclass
class WorkflowRoleConfig:
    name: str
    transport: str = ""
    model: str = ""
    requires: list[str] = field(default_factory=list)
    scope_mode: str = ""
    execution_protocol: str = ""
    force_compat: bool = False
    on_unavailable: str = ""
    degrade_to: str = ""
    runtime_caps: dict[str, int] = field(default_factory=dict)


@dataclass
class WorkflowCompatibilityRule:
    model: str = ""
    models: list[str] = field(default_factory=list)
    transports: list[str] = field(default_factory=list)
    interfaces: list[str] = field(default_factory=list)
    api_formats: list[str] = field(default_factory=list)
    roles: list[str] = field(default_factory=list)

    def matches(
        self,
        *,
        role: WorkflowRoleConfig,
        transport: WorkflowTransportConfig,
    ) -> bool:
        model_matches = False
        if self.models and role.model in self.models:
            model_matches = True
        elif self.model:
            model_matches = fnmatch.fnmatchcase(role.model, self.model)
        if not model_matches:
            return False
        if self.roles and _normalize_key(role.name) not in self.roles:
            return False
        if self.transports and _normalize_key(transport.name) not in self.transports:
            return False
        if self.interfaces and _normalize_key(transport.interface) not in self.interfaces:
            return False
        if self.api_formats:
            api_format = _normalize_key(transport.api_format)
            if api_format not in self.api_formats:
                return False
        return True


@dataclass
class WorkflowModelConfig:
    schema_version: str = ""
    planner_model: str = ""
    executor_model: str = ""
    executor_models: dict[str, str] = field(default_factory=dict)
    reviewer_model: str = ""
    plan_reviewer_model: str = ""
    reviewer_backend: str = ""
    review_enabled: bool | None = None
    transports: dict[str, WorkflowTransportConfig] = field(default_factory=dict)
    roles: dict[str, WorkflowRoleConfig] = field(default_factory=dict)
    compatibility: list[WorkflowCompatibilityRule] = field(default_factory=list)

    def resolve_executor_model(self, backend: str) -> str:
        """Return the executor model best matching *backend*.

        v1 resolution: executor_models[backend] > executor_model > "".
        v2 resolution uses projected executor_models derived from roles.
        """
        key = _normalize_key(backend)
        if key:
            per_backend = self.executor_models.get(key)
            if per_backend:
                return per_backend
        return self.executor_model

    def get_role(self, role_name: str, *, fallback: bool = True) -> WorkflowRoleConfig | None:
        key = _normalize_key(role_name)
        role = self.roles.get(key)
        if role is not None:
            return role
        if fallback and key in {"plan_reviewer", "impl_reviewer"}:
            return self.roles.get("reviewer")
        return None

    def transport_for_role(self, role_name: str, *, fallback: bool = True) -> WorkflowTransportConfig | None:
        role = self.get_role(role_name, fallback=fallback)
        if role is None or not role.transport:
            return None
        return self.transports.get(_normalize_key(role.transport))

    def role_driver(self, role_name: str, *, fallback: bool = True) -> str:
        transport = self.transport_for_role(role_name, fallback=fallback)
        return transport.driver if transport else ""

    def role_interface(self, role_name: str, *, fallback: bool = True) -> str:
        transport = self.transport_for_role(role_name, fallback=fallback)
        return transport.interface if transport else ""

    def role_executable(self, role_name: str, *, fallback: bool = True) -> str:
        transport = self.transport_for_role(role_name, fallback=fallback)
        if transport is None:
            return ""
        return transport.primary_executable()

    def executor_backend_for_role(self) -> str:
        transport = self.transport_for_role("executor")
        return transport.executor_backend() if transport else ""

    def reviewer_backend_for_role(self, role_name: str = "impl_reviewer") -> str:
        transport = self.transport_for_role(role_name)
        return transport.legacy_reviewer_backend() if transport else ""

    def self_review_backend_for_role(self) -> str:
        transport = self.transport_for_role("self_reviewer")
        if transport is None:
            return ""
        backend = transport.executor_backend()
        return backend if backend in {"external_cli", "manual", "noop_test_only"} else ""


def load_model_config(project_root: Path | str) -> WorkflowModelConfig:
    """Load models.yaml from *project_root*/.claude/workflow/models.yaml."""
    path = Path(project_root) / _MODELS_YAML_REL
    if not path.exists():
        return WorkflowModelConfig()

    raw = _safe_load_yaml(path)
    if raw is None:
        return WorkflowModelConfig()

    schema = str(raw.get("schema_version") or "").strip()
    if schema == _SCHEMA_V1:
        if any(field in raw for field in _V2_FIELDS):
            logger.warning("models.v1 ignores v2 fields: %s", sorted(field for field in _V2_FIELDS if field in raw))
        return _load_v1(raw)
    if schema == _SCHEMA_V2:
        if any(field in raw for field in _V1_FIELDS):
            logger.warning("models.v2 ignores v1 fields: %s", sorted(field for field in _V1_FIELDS if field in raw))
        return _load_v2(raw)
    if schema:
        logger.warning(
            "models.yaml schema_version %r not supported (expected %r or %r); ignoring",
            schema,
            _SCHEMA_V1,
            _SCHEMA_V2,
        )
    return WorkflowModelConfig()


def migrate_v1_to_v2(raw: dict[str, Any]) -> dict[str, Any]:
    """Return a best-effort models.v2 dictionary for a models.v1 dictionary.

    The migration preserves role intent but does not guess secrets. API reviewer
    credentials remain env-var based and can be filled in by the operator.
    """
    if str(raw.get("schema_version") or "").strip() != _SCHEMA_V1:
        raise WorkflowModelConfigError("migrate_v1_to_v2 expects schema_version='models.v1'")

    planner_model = _clean_text(raw.get("planner_model"))
    executor_model = _clean_text(raw.get("executor_model"))
    executor_models = _executor_models_map(raw)
    reviewer_model = _clean_text(raw.get("reviewer_model"))
    plan_reviewer_model = _clean_text(raw.get("plan_reviewer_model")) or reviewer_model
    reviewer_backend = _validated_backend(raw) or "api"

    transports: dict[str, dict[str, Any]] = {
        "claude_local": {
            "kind": "subprocess",
            "driver": "claude_cli",
            "interface": "tool_use",
            "executable": "claude",
            "provides": [
                Capability.REPO_READ_FILE.value,
                Capability.REPO_GREP.value,
                Capability.REPO_GLOB.value,
            ],
        },
        "codex_local": {
            "kind": "subprocess",
            "driver": "codex_cli",
            "interface": "agent",
            "executable": "codex",
            "provides": [
                Capability.REPO_READ_FILE.value,
                Capability.REPO_GREP.value,
                Capability.REPO_GLOB.value,
                Capability.REPO_WRITE_FILE.value,
                Capability.SHELL_EXEC.value,
            ],
        },
        "claude_code_local": {
            "kind": "subprocess",
            "driver": "claude_code",
            "interface": "agent",
            "executable": "claude",
            "provides": [
                Capability.REPO_READ_FILE.value,
                Capability.REPO_GREP.value,
                Capability.REPO_GLOB.value,
                Capability.REPO_WRITE_FILE.value,
                Capability.SHELL_EXEC.value,
            ],
        },
        "reviewer_api": {
            "kind": "http",
            "driver": "http",
            "interface": "chat",
            "api_format": "auto",
            "base_url_env": "WORKFLOW_REVIEWER_BASE_URL",
            "api_key_env": "WORKFLOW_REVIEWER_API_KEY",
            "provides": [],
        },
    }
    if reviewer_backend == "mcp":
        transports["claude_mcp_review"] = {
            "kind": "subprocess",
            "driver": "claude_cli",
            "interface": "mcp",
            "host_executable": "claude",
            "mcp_server": "kodawari.autopilot.review.mcp_review_server",
            "provides": [Capability.MCP.value],
        }

    roles: dict[str, dict[str, Any]] = {}
    if planner_model:
        roles["planner"] = {
            "transport": "claude_local",
            "model": planner_model,
            "on_unavailable": "fail",
        }
    if plan_reviewer_model:
        roles["plan_reviewer"] = {
            "transport": "codex_local",
            "model": plan_reviewer_model,
            "on_unavailable": "fail",
        }
    if reviewer_model:
        roles["impl_reviewer"] = {
            "transport": _reviewer_backend_transport_id(reviewer_backend),
            "model": reviewer_model,
            "on_unavailable": "fail",
        }
    executor_transport = _migrated_executor_transport(executor_models)
    migrated_executor_model = executor_models.get("codex_cli") or executor_models.get("claude_code") or executor_model
    if migrated_executor_model:
        roles["executor"] = {
            "transport": executor_transport,
            "model": migrated_executor_model,
            "scope_mode": "post_diff",
            "on_unavailable": "fail",
        }

    compatibility = _compatibility_from_roles(roles)
    return {
        "schema_version": _SCHEMA_V2,
        "transports": transports,
        "compatibility": compatibility,
        "roles": roles,
        "review_enabled": raw.get("review_enabled"),
    }


def _load_v1(raw: dict[str, Any]) -> WorkflowModelConfig:
    return WorkflowModelConfig(
        schema_version=_SCHEMA_V1,
        planner_model=_str(raw, "planner_model"),
        executor_model=_str(raw, "executor_model"),
        executor_models=_executor_models_map(raw),
        reviewer_model=_str(raw, "reviewer_model"),
        plan_reviewer_model=_str(raw, "plan_reviewer_model"),
        reviewer_backend=_validated_backend(raw),
        review_enabled=_bool_or_none(raw, "review_enabled"),
    )


def _reviewer_backend_transport_id(backend: str) -> str:
    if backend == "codex":
        return "codex_local"
    if backend == "cli":
        return "claude_local"
    if backend == "mcp":
        return "claude_mcp_review"
    return "reviewer_api"


def _migrated_executor_transport(executor_models: dict[str, str]) -> str:
    if "claude_code" in executor_models and "codex_cli" not in executor_models:
        return "claude_code_local"
    return "codex_local"


def _compatibility_from_roles(roles: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[str]] = {}
    for role in roles.values():
        transport = _normalize_key(role.get("transport"))
        model = _clean_text(role.get("model"))
        if not transport or not model:
            continue
        interface = {
            "codex_local": "agent",
            "claude_local": "tool_use",
            "claude_code_local": "agent",
            "claude_mcp_review": "mcp",
            "reviewer_api": "chat",
        }.get(transport, "")
        grouped.setdefault((transport, interface), []).append(model)
    return [
        {
            "models": sorted(set(models)),
            "transports": [transport],
            "interfaces": [interface] if interface else [],
        }
        for (transport, interface), models in sorted(grouped.items())
    ]


def _load_v2(raw: dict[str, Any]) -> WorkflowModelConfig:
    config = WorkflowModelConfig(
        schema_version=_SCHEMA_V2,
        transports=_parse_transports(raw.get("transports")),
        roles=_parse_roles(raw.get("roles")),
        compatibility=_parse_compatibility(raw.get("compatibility")),
        review_enabled=_bool_or_none(raw, "review_enabled"),
    )
    _validate_v2(config)
    _project_v2_legacy_fields(config)
    return config


def _parse_transports(value: Any) -> dict[str, WorkflowTransportConfig]:
    if not isinstance(value, dict) or not value:
        raise WorkflowModelConfigError("models.v2 requires a non-empty transports mapping")
    transports: dict[str, WorkflowTransportConfig] = {}
    for raw_name, raw_spec in value.items():
        name = _normalize_key(raw_name)
        if not name:
            raise WorkflowModelConfigError("models.v2 transport names cannot be empty")
        if not isinstance(raw_spec, dict):
            raise WorkflowModelConfigError(f"transports.{name} must be a mapping")
        if "api_key" in raw_spec:
            raise WorkflowModelConfigError(f"transports.{name}.api_key is not allowed; use api_key_env")
        backend_alias = _normalize_key(raw_spec.get("backend"))
        kind = _normalize_kind(raw_spec.get("kind") or backend_alias)
        executable = _clean_text(raw_spec.get("executable"))
        host_executable = _clean_text(raw_spec.get("host_executable"))
        api_format = _normalize_key(raw_spec.get("api_format"))
        mcp_server = _clean_text(raw_spec.get("mcp_server"))
        driver = _normalize_key(raw_spec.get("driver")) or _infer_driver(
            kind=kind,
            backend_alias=backend_alias,
            executable=host_executable or executable,
            api_format=api_format,
            mcp_server=mcp_server,
        )
        interface = _normalize_key(raw_spec.get("interface")) or _infer_interface(
            kind=kind,
            driver=driver,
            mcp_server=mcp_server,
        )
        provides = _capability_list(raw_spec.get("provides"))
        legacy_tools = _string_list(raw_spec.get("default_tools"), lower=True)
        if legacy_tools:
            logger.warning("models.v2 transports.%s.default_tools is deprecated; use provides", name)
            provides.extend(_capabilities_from_tools(legacy_tools))
        interface_capability = _interface_capability(interface)
        if interface_capability:
            provides.append(interface_capability)
        transports[name] = WorkflowTransportConfig(
            name=name,
            kind=kind,
            driver=driver,
            interface=interface,
            executable=executable,
            host_executable=host_executable,
            api_format=api_format,
            base_url=_clean_text(raw_spec.get("base_url")),
            base_url_env=_clean_text(raw_spec.get("base_url_env")),
            api_key_env=_clean_text(raw_spec.get("api_key_env")),
            mcp_server=mcp_server,
            quota_group=_clean_text(raw_spec.get("quota_group")) or name,
            provides=_dedupe([item for item in provides if item]),
        )
    return transports


def _parse_roles(value: Any) -> dict[str, WorkflowRoleConfig]:
    if not isinstance(value, dict) or not value:
        raise WorkflowModelConfigError("models.v2 requires a non-empty roles mapping")
    roles: dict[str, WorkflowRoleConfig] = {}
    for raw_name, raw_spec in value.items():
        name = _normalize_key(raw_name)
        if name not in _VALID_ROLE_NAMES:
            raise WorkflowModelConfigError(f"unsupported models.v2 role {name!r}")
        if not isinstance(raw_spec, dict):
            raise WorkflowModelConfigError(f"roles.{name} must be a mapping")
        transport = _normalize_key(raw_spec.get("transport"))
        if not transport:
            raise WorkflowModelConfigError(f"roles.{name}.transport is required")
        on_unavailable = _normalize_key(raw_spec.get("on_unavailable"))
        if not on_unavailable:
            raise WorkflowModelConfigError(f"roles.{name}.on_unavailable is required")
        if on_unavailable not in _VALID_ON_UNAVAILABLE:
            raise WorkflowModelConfigError(
                f"roles.{name}.on_unavailable {on_unavailable!r} is unsupported "
                f"(expected one of {sorted(_VALID_ON_UNAVAILABLE)})"
            )
        degrade_to = _normalize_key(raw_spec.get("degrade_to"))
        if on_unavailable == "degrade_to" and not degrade_to:
            raise WorkflowModelConfigError(f"roles.{name}.degrade_to is required when on_unavailable=degrade_to")
        requires = _capability_list(raw_spec.get("requires"))
        legacy_tools = _string_list(raw_spec.get("tools"), lower=True)
        if legacy_tools:
            logger.warning("models.v2 roles.%s.tools is deprecated; use requires", name)
            requires.extend(_capabilities_from_tools(legacy_tools))
        legacy_required = _string_list(raw_spec.get("required_capabilities"), lower=True)
        if legacy_required:
            logger.warning("models.v2 roles.%s.required_capabilities is deprecated; use requires", name)
            requires.extend(_capabilities_from_legacy(legacy_required))
        roles[name] = WorkflowRoleConfig(
            name=name,
            transport=transport,
            model=_clean_text(raw_spec.get("model")),
            requires=_dedupe(requires),
            scope_mode=_normalize_key(raw_spec.get("scope_mode")),
            execution_protocol=_normalize_execution_protocol(raw_spec.get("execution_protocol"), role_name=name),
            force_compat=_bool_value(raw_spec.get("force_compat")),
            on_unavailable=on_unavailable,
            degrade_to=degrade_to,
            runtime_caps=_parse_runtime_caps(raw_spec.get("runtime_caps"), role_name=name),
        )
    return roles


def _parse_compatibility(value: Any) -> list[WorkflowCompatibilityRule]:
    if not isinstance(value, list) or not value:
        raise WorkflowModelConfigError("models.v2 requires a non-empty compatibility list")
    rules: list[WorkflowCompatibilityRule] = []
    for index, raw_rule in enumerate(value):
        if not isinstance(raw_rule, dict):
            raise WorkflowModelConfigError(f"compatibility[{index}] must be a mapping")
        model = _clean_text(raw_rule.get("model"))
        models = _string_list(raw_rule.get("models"))
        if not model and not models:
            raise WorkflowModelConfigError(f"compatibility[{index}] requires model glob or models exact list")
        rules.append(
            WorkflowCompatibilityRule(
                model=model,
                models=models,
                transports=_string_list(raw_rule.get("transports"), lower=True),
                interfaces=_string_list(raw_rule.get("interfaces"), lower=True),
                api_formats=_string_list(raw_rule.get("api_formats"), lower=True),
                roles=_string_list(raw_rule.get("roles"), lower=True),
            )
        )
    return rules


def _parse_runtime_caps(value: Any, *, role_name: str) -> dict[str, int]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise WorkflowModelConfigError(f"roles.{role_name}.runtime_caps must be a mapping")
    caps: dict[str, int] = {}
    for raw_key, raw_value in value.items():
        key = _normalize_key(raw_key)
        if key not in _VALID_RUNTIME_CAP_KEYS:
            raise WorkflowModelConfigError(
                f"roles.{role_name}.runtime_caps.{key} is unsupported "
                f"(expected one of {sorted(_VALID_RUNTIME_CAP_KEYS)})"
            )
        try:
            parsed = int(raw_value)
        except (TypeError, ValueError):
            raise WorkflowModelConfigError(f"roles.{role_name}.runtime_caps.{key} must be an integer") from None
        if parsed <= 0:
            raise WorkflowModelConfigError(f"roles.{role_name}.runtime_caps.{key} must be positive")
        caps[key] = parsed
    return caps


def _validate_v2(config: WorkflowModelConfig) -> None:
    for rule in config.compatibility:
        for transport_name in rule.transports:
            if transport_name not in config.transports:
                raise WorkflowModelConfigError(
                    f"compatibility rule for {rule.model!r} references unknown transport {transport_name!r}"
                )
    for role in config.roles.values():
        transport = config.transports.get(role.transport)
        if transport is None:
            raise WorkflowModelConfigError(f"roles.{role.name}.transport references unknown transport {role.transport!r}")
        if role.degrade_to and role.degrade_to not in config.transports:
            raise WorkflowModelConfigError(f"roles.{role.name}.degrade_to references unknown transport {role.degrade_to!r}")
        _validate_role_requires(role, transport)
        if role.name in {"planner", "plan_reviewer"}:
            _validate_planning_driver_role(role, transport)
        if role.name == "executor":
            _validate_executor_role(role, transport)
        if role.model:
            _validate_model_compatibility(config, role, transport)


def _validate_role_requires(role: WorkflowRoleConfig, transport: WorkflowTransportConfig) -> None:
    if not role.requires:
        return
    supported = set(transport.provides)
    missing = [cap for cap in role.requires if cap not in supported]
    if missing:
        raise WorkflowModelConfigError(
            f"roles.{role.name}.requires {missing}, but transport {transport.name!r} "
            f"provides {sorted(supported)}"
        )


def _validate_executor_role(role: WorkflowRoleConfig, transport: WorkflowTransportConfig) -> None:
    interface = _normalize_key(transport.interface)
    driver = _normalize_key(transport.driver)
    backend = transport.executor_backend()
    execution_protocol = _effective_execution_protocol(role)
    if backend in {"manual", "external_cli", "noop_test_only"}:
        scope_mode = role.scope_mode or "none"
        if scope_mode not in {"none", "post_diff"}:
            raise WorkflowModelConfigError(
                f"roles.executor.scope_mode {scope_mode!r} is unsupported for {backend} executor; expected 'none'"
            )
        if role.execution_protocol:
            raise WorkflowModelConfigError(
                f"roles.executor.execution_protocol {role.execution_protocol!r} is unsupported for {backend} executor"
            )
        return
    if interface == "chat":
        raise WorkflowModelConfigError(
            "roles.executor cannot use interface=chat in models.v2; patch-protocol execution is a v3 feature"
        )
    if interface == "tool_use":
        scope_mode = role.scope_mode or "inline_guard"
        if scope_mode != "inline_guard":
            raise WorkflowModelConfigError(
                f"roles.executor.scope_mode {scope_mode!r} is unsupported for tool_use executor; expected 'inline_guard'"
            )
        if execution_protocol not in {"full_file_v1", "exact_str_replace_v1"}:
            raise WorkflowModelConfigError(
                f"roles.executor.execution_protocol {execution_protocol!r} is unsupported for tool_use executor"
            )
        _validate_executor_runtime_caps(role)
        api_format = _normalize_key(transport.api_format)
        if _normalize_key(transport.kind) != "http" or api_format not in _SUPPORTED_HTTP_API_FORMATS:
            raise WorkflowModelConfigError(
                "roles.executor interface=tool_use requires an HTTP transport with "
                "api_format in {openai_chat, anthropic_messages}"
            )
        return
    if interface == "mcp":
        scope_mode = role.scope_mode or "inline_guard"
        if scope_mode != "inline_guard":
            raise WorkflowModelConfigError(
                f"roles.executor.scope_mode {scope_mode!r} is unsupported for mcp executor; expected 'inline_guard'"
            )
        if execution_protocol != "full_file_v1":
            raise WorkflowModelConfigError(
                f"roles.executor.execution_protocol {execution_protocol!r} is unsupported for mcp executor"
            )
        _validate_executor_runtime_caps(role)
        raise WorkflowModelConfigError(
            "roles.executor cannot use interface=mcp until the guarded execution MCP server is implemented"
        )
    if interface == "agent" and driver in {"codex_cli", "claude_code", "claude_cli"}:
        scope_mode = role.scope_mode or "post_diff"
        if scope_mode != "post_diff":
            raise WorkflowModelConfigError(
                f"roles.executor.scope_mode {scope_mode!r} is unsupported for agent executor; expected 'post_diff'"
            )
        if execution_protocol != "full_file_v1":
            raise WorkflowModelConfigError(
                f"roles.executor.execution_protocol {execution_protocol!r} is unsupported for agent executor"
            )
        return
    raise WorkflowModelConfigError(
        f"roles.executor transport {transport.name!r} is not a supported v2 executor "
        f"(driver={driver!r}, interface={interface!r})"
    )


def _validate_executor_runtime_caps(role: WorkflowRoleConfig) -> None:
    required = {
        "max_tool_iterations",
        "max_token_budget",
        "max_same_tool_calls_per_path",
        "max_tool_calls_per_response",
        "max_wall_clock_seconds",
        "max_no_progress_iterations",
        "max_verify_retries",
    }
    missing = sorted(required - set(role.runtime_caps))
    if missing:
        raise WorkflowModelConfigError(
            f"roles.executor.runtime_caps missing required keys for inline_guard executor: {missing}"
        )


def _effective_execution_protocol(role: WorkflowRoleConfig) -> str:
    return _normalize_key(role.execution_protocol) or "full_file_v1"


def _validate_planning_driver_role(role: WorkflowRoleConfig, transport: WorkflowTransportConfig) -> None:
    driver = _normalize_key(transport.driver)
    interface = _normalize_key(transport.interface)
    if driver == "noop":
        return
    if driver == "codex_cli" and interface == "agent":
        return
    if driver == "claude_cli" and interface == "tool_use":
        return
    if _normalize_key(transport.kind) == "http" and interface in {"chat", "tool_use"} and driver in {"openai_compatible", "mimo_http", "http"}:
        api_format = _normalize_key(transport.api_format)
        if _normalize_key(transport.kind) == "http" and api_format in _SUPPORTED_HTTP_API_FORMATS:
            _validate_http_chat_transport(role, transport)
            return
    raise WorkflowModelConfigError(
        f"roles.{role.name} transport {transport.name!r} is not supported by the v2 planning dispatcher "
        f"(driver={driver!r}, interface={interface!r})"
    )


def _validate_http_chat_transport(role: WorkflowRoleConfig, transport: WorkflowTransportConfig) -> None:
    if not (transport.base_url or transport.base_url_env):
        raise WorkflowModelConfigError(
            f"roles.{role.name} HTTP chat transport {transport.name!r} requires base_url or base_url_env"
        )
    if not transport.api_key_env:
        raise WorkflowModelConfigError(
            f"roles.{role.name} HTTP chat transport {transport.name!r} requires api_key_env"
        )


def _validate_model_compatibility(
    config: WorkflowModelConfig,
    role: WorkflowRoleConfig,
    transport: WorkflowTransportConfig,
) -> None:
    if role.force_compat:
        logger.warning(
            "models.v2 roles.%s uses force_compat=true; skipping compatibility matrix for model=%r transport=%r",
            role.name,
            role.model,
            transport.name,
        )
        return
    for rule in config.compatibility:
        if rule.matches(role=role, transport=transport):
            return
    raise WorkflowModelConfigError(
        f"roles.{role.name} model/transport/interface tuple is not compatible: "
        f"model={role.model!r}, transport={transport.name!r}, interface={transport.interface!r}"
    )


def _project_v2_legacy_fields(config: WorkflowModelConfig) -> None:
    planner = config.get_role("planner", fallback=False)
    reviewer = config.get_role("reviewer", fallback=False)
    plan_reviewer = config.get_role("plan_reviewer") or reviewer
    impl_reviewer = config.get_role("impl_reviewer") or reviewer
    executor = config.get_role("executor", fallback=False)
    config.planner_model = planner.model if planner else ""
    config.plan_reviewer_model = plan_reviewer.model if plan_reviewer else ""
    config.reviewer_model = impl_reviewer.model if impl_reviewer else ""
    config.executor_model = executor.model if executor else ""
    executor_backend = config.executor_backend_for_role()
    if executor_backend and config.executor_model:
        config.executor_models[executor_backend] = config.executor_model
    config.reviewer_backend = config.reviewer_backend_for_role("impl_reviewer")


def _capabilities_for_role(role: WorkflowRoleConfig, transport: WorkflowTransportConfig) -> set[str]:
    del role
    return set(transport.provides)


def _executor_models_map(raw: dict[str, Any]) -> dict[str, str]:
    value = raw.get("executor_models")
    if value is None:
        return {}
    if not isinstance(value, dict):
        logger.warning("models.yaml executor_models must be a mapping; ignoring (got %r)", type(value).__name__)
        return {}
    cleaned: dict[str, str] = {}
    for key, item in value.items():
        key_str = _normalize_key(key)
        item_str = _clean_text(item)
        if not key_str or not item_str:
            continue
        cleaned[key_str] = item_str
    return cleaned


def _safe_load_yaml(path: Path) -> dict[str, Any] | None:
    try:
        import yaml  # type: ignore[import]
    except ImportError:
        logger.debug("pyyaml not installed; cannot read models.yaml")
        return None
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("models.yaml parse failed: %s", path, exc_info=True)
        return None
    return raw if isinstance(raw, dict) else None


def _str(raw: dict[str, Any], key: str) -> str:
    return _clean_text(raw.get(key))


def _bool_or_none(raw: dict[str, Any], key: str) -> bool | None:
    val = raw.get(key)
    if val is None:
        return None
    if isinstance(val, bool):
        return val
    s = _normalize_key(val)
    if s in {"1", "true", "yes", "on"}:
        return True
    if s in {"0", "false", "no", "off"}:
        return False
    return None


def _validated_backend(raw: dict[str, Any]) -> str:
    val = _normalize_key(raw.get("reviewer_backend"))
    if not val:
        return ""
    if val not in _VALID_BACKENDS:
        logger.warning(
            "models.yaml reviewer_backend %r not recognised (expected one of %s); ignoring",
            val,
            sorted(_VALID_BACKENDS),
        )
        return ""
    return val


def _string_list(value: Any, *, lower: bool = False) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, list):
        items = value
    else:
        return []
    out: list[str] = []
    for item in items:
        text = _clean_text(item)
        if lower:
            text = text.lower()
        if text:
            out.append(text)
    return out


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _normalize_key(value: Any) -> str:
    return _clean_text(value).lower().replace("-", "_")


def _normalize_execution_protocol(value: Any, *, role_name: str) -> str:
    protocol = _normalize_key(value)
    if protocol not in _VALID_EXECUTION_PROTOCOLS:
        raise WorkflowModelConfigError(
            f"roles.{role_name}.execution_protocol {protocol!r} is unsupported "
            f"(expected one of {sorted(item for item in _VALID_EXECUTION_PROTOCOLS if item)})"
        )
    return protocol


def _normalize_kind(value: Any) -> str:
    raw = _normalize_key(value)
    aliases = {
        "": "",
        "cli": "subprocess",
        "api": "http",
        "http": "http",
        "subprocess": "subprocess",
        "stdio": "stdio",
        "sdk": "sdk",
        "in_process": "in_process",
        "external_cli": "external",
        "external": "external",
        "manual": "manual",
        "noop_test_only": "test",
        "test": "test",
    }
    kind = aliases.get(raw)
    if kind is None:
        raise WorkflowModelConfigError(f"unsupported transport kind/backend {raw!r}")
    return kind


def _infer_driver(
    *,
    kind: str,
    backend_alias: str,
    executable: str,
    api_format: str,
    mcp_server: str,
) -> str:
    if backend_alias in {"manual", "external_cli", "noop_test_only"}:
        return backend_alias
    if kind == "manual":
        return "manual"
    if kind == "external":
        return "external_cli"
    if kind == "test":
        return "noop_test_only"
    if kind == "http":
        if api_format in _ANTHROPIC_API_FORMATS:
            # anthropic_messages is dispatched through the openai_compatible
            # tool-use executor, with format conversion happening in
            # tool_use_transport (request body & response normalization).
            # We keep this as "openai_compatible" so existing dispatch routing
            # (LocalCodexAdapter.execution_backend selection) still finds it.
            return "openai_compatible"
        if api_format in _OPENAI_CHAT_API_FORMATS:
            return "openai_compatible"
        return "http"
    if kind == "in_process":
        return "noop"
    exe = Path(executable).stem.lower()
    if exe.startswith("codex"):
        return "codex_cli"
    if exe.startswith("claude"):
        return "claude_cli"
    if mcp_server:
        return "claude_cli"
    return backend_alias or kind


def _infer_interface(*, kind: str, driver: str, mcp_server: str) -> str:
    if mcp_server:
        return "mcp"
    if kind == "http":
        return "chat"
    if driver == "noop":
        return "chat"
    if driver == "codex_cli":
        return "agent"
    if driver in {"external_cli", "manual", "noop_test_only"}:
        return driver
    if driver in {"claude_cli", "claude_code"}:
        return "tool_use"
    return ""


def _interface_capability(interface: str) -> str:
    mapping = {
        "chat": Capability.CHAT.value,
        "tool_use": Capability.TOOL_USE.value,
        "agent": Capability.AGENT_LOOP.value,
        "mcp": Capability.MCP.value,
    }
    return mapping.get(_normalize_key(interface), "")


def _capability_list(value: Any) -> list[str]:
    values = _string_list(value)
    out: list[str] = []
    for raw in values:
        normalized = _normalize_capability(raw)
        if normalized:
            out.append(normalized)
    return _dedupe(out)


def _normalize_capability(value: str) -> str:
    text = _clean_text(value).lower().replace("-", "_")
    if not text:
        return ""
    text = _TOOL_CAPABILITY_ALIASES.get(text.replace(".", "_"), text)
    if text in _CAPABILITY_VALUES:
        return text
    raise WorkflowModelConfigError(
        f"unsupported capability {value!r}; expected one of {sorted(_CAPABILITY_VALUES)}"
    )


def _capabilities_from_tools(tools: list[str]) -> list[str]:
    out: list[str] = []
    for tool in tools:
        capability = _TOOL_CAPABILITY_ALIASES.get(_normalize_key(tool))
        if capability:
            out.append(capability)
    return _dedupe(out)


def _capabilities_from_legacy(values: list[str]) -> list[str]:
    out: list[str] = []
    for value in values:
        alias = _LEGACY_CAPABILITY_ALIASES.get(_normalize_key(value))
        if alias is not None:
            out.extend(alias)
        else:
            out.append(_normalize_capability(value))
    return _dedupe(out)


def _dedupe(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _clean_text(value)
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _bool_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return _normalize_key(value) in {"1", "true", "yes", "on"}
