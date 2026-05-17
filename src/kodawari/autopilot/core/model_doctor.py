"""Diagnostics for models.v2 role/transport configuration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import ssl
import threading
import time
from typing import Any, Callable
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

from kodawari.autopilot.core import model_config as mc
from kodawari.autopilot.core.http_safety import RedirectBlocked, SafeRedirectHandler
from kodawari.autopilot.core.openai_chat_client import call_openai_chat
from kodawari.autopilot.core.secret_redactor import redact_jsonable, redact_secrets
from kodawari.autopilot.execution import execution_openai_tool_use as openai_tool_use
from kodawari.autopilot.execution.execution_backend import ExecutionBackendConfig
from kodawari.autopilot.planning.planning_agent import generate_plan, _parse_response as _parse_planner_response
from kodawari.infra.io_atomic import atomic_write_json, path_lock


DOCTOR_REPORT_SCHEMA_VERSION = "doctor.report.v1"
PROBE_VERSION = "openai-tools-v1"
SENTINEL = "doctor_probe"
_MODELS_REL = ".claude/workflow/models.yaml"
_CACHE_REL = ".claude/workflow/.doctor_cache/models.json"

_REQUIRED_INLINE_CAPS = {
    "max_tool_iterations",
    "max_token_budget",
    "max_same_tool_calls_per_path",
    "max_tool_calls_per_response",
    "max_wall_clock_seconds",
    "max_no_progress_iterations",
    "max_verify_retries",
}
_OPENAI_CHAT_API_FORMATS = {"openai", "openai_chat"}
_V1_ENV_RULES = {
    "WORKFLOW_PLANNER_MODEL": ("WARN", "v1 env maps to roles.planner.model under models.v2"),
    "WORKFLOW_REVIEWER_MODEL": ("WARN", "v1 env maps to roles.impl_reviewer.model under models.v2"),
    "WORKFLOW_REVIEWER_BACKEND": ("BLOCKED", "v1 backend env conflicts with models.v2 transport selection"),
    "WORKFLOW_REVIEWER_API_KEY": ("WARN", "use transports.<id>.api_key_env instead of reviewer global env"),
    "WORKFLOW_REVIEWER_BASE_URL": ("WARN", "use transports.<id>.base_url_env instead of reviewer global env"),
}
_LEGACY_OPUS_ENV_PREFIX = "WORKFLOW_OPUS_"
_LOCALHOSTS = {"localhost", "127.0.0.1", "::1"}


@dataclass
class DiagnosticLoadResult:
    config: mc.WorkflowModelConfig
    raw: dict[str, Any]
    model_path: Path
    blockers: list[dict[str, Any]]
    warnings: list[dict[str, Any]]
    fatal: bool = False


def doctor_models(
    *,
    project_root: Path | str,
    offline: bool = True,
    probe_tools: bool = False,
    smoke: str = "",
    no_cache: bool = False,
    cache_ttl_seconds: int | None = None,
    urlopen_fn: Callable[[urlrequest.Request, int], Any] | None = None,
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    loaded = load_model_config_diagnostic(root)
    report = _base_report(root, loaded)
    if loaded.fatal:
        return _finalize_report(report)

    config = loaded.config
    _add_role_and_transport_sections(report, config)
    _run_offline_checks(report, config)
    if probe_tools and not offline:
        _run_probe_checks(
            report,
            config,
            project_root=root,
            no_cache=no_cache,
            cache_ttl_seconds=cache_ttl_seconds,
            urlopen_fn=urlopen_fn,
        )
    smoke_mode = str(smoke).strip().lower()
    if smoke and offline and smoke_mode in {"real", "patch-real"}:
        report["blockers"].append(_issue("smoke_real_requires_online", f"--smoke={smoke_mode} cannot run with --offline"))
    elif smoke:
        _run_smoke_checks(report, config, project_root=root, smoke=smoke)
    return _finalize_report(report)


def doctor_exit_code(report: dict[str, Any]) -> int:
    return 2 if str(report.get("status") or "").upper() == "BLOCKED" else 0


def load_model_config_diagnostic(project_root: Path | str) -> DiagnosticLoadResult:
    root = Path(project_root).resolve()
    model_path = root / _MODELS_REL
    blockers: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    if not model_path.exists():
        blockers.append(_issue("models_yaml_missing", "models.yaml is missing", fatal=True))
        return DiagnosticLoadResult(mc.WorkflowModelConfig(), {}, model_path, blockers, warnings, fatal=True)

    raw, error = _load_raw_yaml(model_path)
    if error:
        blockers.append(_issue("yaml_parse_error", error, fatal=True))
        return DiagnosticLoadResult(mc.WorkflowModelConfig(), {}, model_path, blockers, warnings, fatal=True)
    if not isinstance(raw, dict):
        blockers.append(_issue("models_yaml_not_mapping", "models.yaml must be a mapping", fatal=True))
        return DiagnosticLoadResult(mc.WorkflowModelConfig(), {}, model_path, blockers, warnings, fatal=True)

    schema = str(raw.get("schema_version") or "").strip()
    if not schema:
        blockers.append(_issue("missing_schema_version", "models.yaml is missing schema_version", fatal=True))
        return DiagnosticLoadResult(mc.WorkflowModelConfig(), raw, model_path, blockers, warnings, fatal=True)
    if schema != "models.v2":
        blockers.append(_issue("unsupported_schema_version", f"doctor models requires models.v2, got {schema!r}", fatal=True))
        return DiagnosticLoadResult(mc.WorkflowModelConfig(schema_version=schema), raw, model_path, blockers, warnings, fatal=True)

    if not isinstance(raw.get("transports"), dict):
        blockers.append(_issue("transports_not_mapping", "models.v2 transports must be a mapping", fatal=True))
        return DiagnosticLoadResult(mc.WorkflowModelConfig(schema_version=schema), raw, model_path, blockers, warnings, fatal=True)
    if not isinstance(raw.get("roles"), dict):
        blockers.append(_issue("roles_not_mapping", "models.v2 roles must be a mapping", fatal=True))
        return DiagnosticLoadResult(mc.WorkflowModelConfig(schema_version=schema), raw, model_path, blockers, warnings, fatal=True)
    if not isinstance(raw.get("compatibility"), list):
        blockers.append(_issue("compatibility_not_list", "models.v2 compatibility must be a list", fatal=True))
        return DiagnosticLoadResult(mc.WorkflowModelConfig(schema_version=schema), raw, model_path, blockers, warnings, fatal=True)

    try:
        config = mc.WorkflowModelConfig(
            schema_version="models.v2",
            transports=mc._parse_transports(raw.get("transports")),
            roles=mc._parse_roles(raw.get("roles")),
            compatibility=mc._parse_compatibility(raw.get("compatibility")),
            review_enabled=mc._bool_or_none(raw, "review_enabled"),
        )
        mc._project_v2_legacy_fields(config)
    except Exception as exc:
        blockers.append(_issue("models_v2_parse_error", str(exc), fatal=True))
        return DiagnosticLoadResult(mc.WorkflowModelConfig(schema_version=schema), raw, model_path, blockers, warnings, fatal=True)

    cycle = _role_degrade_to_cycle(config)
    if cycle:
        blockers.append(_issue("cyclic_degrade_to", f"cyclic role degrade_to chain: {' -> '.join(cycle)}", fatal=True))
        return DiagnosticLoadResult(config, raw, model_path, blockers, warnings, fatal=True)

    blockers.extend(_collect_validation_blockers(config))
    return DiagnosticLoadResult(config, raw, model_path, blockers, warnings, fatal=False)


def _load_raw_yaml(path: Path) -> tuple[dict[str, Any] | None, str]:
    try:
        import yaml  # type: ignore[import]
        return yaml.safe_load(path.read_text(encoding="utf-8")), ""
    except Exception as exc:
        return None, str(exc)


def _collect_validation_blockers(config: mc.WorkflowModelConfig) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    for required in ("planner", "executor"):
        if required not in config.roles:
            blockers.append(_issue("required_role_missing", f"models.v2 requires roles.{required}", role=required))
    for rule in config.compatibility:
        for transport_name in rule.transports:
            if transport_name not in config.transports:
                blockers.append(_issue("compat_transport_unknown", f"compatibility references unknown transport {transport_name!r}", transport=transport_name))
    for role in config.roles.values():
        transport = config.transports.get(role.transport)
        if transport is None:
            blockers.append(_issue("role_transport_unknown", f"roles.{role.name}.transport references unknown transport {role.transport!r}", role=role.name, transport=role.transport))
            continue
        if role.degrade_to and role.degrade_to not in config.transports:
            blockers.append(_issue("degrade_to_transport_unknown", f"roles.{role.name}.degrade_to references unknown transport {role.degrade_to!r}", role=role.name, transport=role.degrade_to))
        for label, fn in (
            ("role_requires_missing", lambda: mc._validate_role_requires(role, transport)),
            ("planning_dispatcher_unsupported", lambda: mc._validate_planning_driver_role(role, transport) if role.name in {"planner", "plan_reviewer"} else None),
            ("executor_interface_scope_invalid", lambda: mc._validate_executor_role(role, transport) if role.name == "executor" else None),
            ("model_transport_incompatible", lambda: mc._validate_model_compatibility(config, role, transport) if role.model else None),
        ):
            try:
                fn()
            except mc.WorkflowModelConfigError as exc:
                blockers.append(_issue(_validation_code(label, str(exc)), str(exc), role=role.name, transport=transport.name))
    return blockers


def _validation_code(default: str, message: str) -> str:
    text = message.lower()
    if "interface=chat" in text:
        return "chat_executor_forbidden"
    if "api_format=openai_chat" in text:
        return "tool_use_api_format_invalid"
    if "interface=tool_use" in text or "tool-use executor" in text or "tool_use executor" in text:
        return "tool_use_executor_invalid"
    if "mcp until the guarded execution mcp server" in text:
        return "mcp_executor_runner_missing"
    if "scope_mode" in text:
        return "scope_interface_invalid"
    if "runtime_caps" in text:
        return "runtime_caps_missing"
    return default


def _role_degrade_to_cycle(config: mc.WorkflowModelConfig) -> list[str]:
    # Current schema uses degrade_to as a transport id.  This catches accidental
    # role-id chains so doctor can fail clearly instead of reporting nonsense.
    graph = {name: role.degrade_to for name, role in config.roles.items() if role.degrade_to in config.roles}
    for start in graph:
        seen: list[str] = []
        current = start
        while current in graph:
            if current in seen:
                return seen[seen.index(current):] + [current]
            seen.append(current)
            current = graph[current]
    return []


def _base_report(root: Path, loaded: DiagnosticLoadResult) -> dict[str, Any]:
    return {
        "schema_version": DOCTOR_REPORT_SCHEMA_VERSION,
        "status": "PASS",
        "fatal": bool(loaded.fatal),
        "project_root": str(root),
        "models_path": str(loaded.model_path),
        "models_schema_version": loaded.config.schema_version or str(loaded.raw.get("schema_version") or ""),
        "roles": {},
        "transports": {},
        "active_transports": [],
        "inactive_transports": [],
        "quota_groups": {},
        "warnings": list(loaded.warnings),
        "blockers": list(loaded.blockers),
        "probes": {},
        "smokes": {},
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _add_role_and_transport_sections(report: dict[str, Any], config: mc.WorkflowModelConfig) -> None:
    active = {role.transport for role in config.roles.values() if role.transport}
    report["active_transports"] = sorted(active)
    report["inactive_transports"] = sorted(set(config.transports) - active)
    report["roles"] = {
        name: {
            "transport": role.transport,
            "model": role.model,
            "interface": (config.transports.get(role.transport) or mc.WorkflowTransportConfig(name="")).interface,
            "scope_mode": role.scope_mode,
            "execution_protocol": role.execution_protocol,
            "on_unavailable": role.on_unavailable,
            "degrade_to": role.degrade_to,
            "runtime_caps": dict(role.runtime_caps),
        }
        for name, role in sorted(config.roles.items())
    }
    report["transports"] = {
        name: _transport_report(transport, active=name in active)
        for name, transport in sorted(config.transports.items())
    }
    quota_groups: dict[str, list[str]] = {}
    for role in config.roles.values():
        transport = config.transports.get(role.transport)
        if transport is not None:
            quota_groups.setdefault(transport.quota_group or transport.name, []).append(role.name)
    report["quota_groups"] = {key: sorted(value) for key, value in sorted(quota_groups.items())}


def _transport_report(transport: mc.WorkflowTransportConfig, *, active: bool) -> dict[str, Any]:
    executable = transport.primary_executable()
    found = bool(shutil.which(executable)) if executable and transport.kind == "subprocess" else None
    return {
        "active": bool(active),
        "kind": transport.kind,
        "driver": transport.driver,
        "interface": transport.interface,
        "executable": Path(executable).name if executable else "",
        "executable_found": found,
        "api_format": transport.api_format,
        "base_url_env": transport.base_url_env,
        "base_url_present": bool(transport.base_url or (transport.base_url_env and os.environ.get(transport.base_url_env))),
        "api_key_env": transport.api_key_env,
        "api_key_present": bool(transport.api_key_env and os.environ.get(transport.api_key_env)),
        "quota_group": transport.quota_group or transport.name,
        "mcp_server": transport.mcp_server,
    }


def _run_offline_checks(report: dict[str, Any], config: mc.WorkflowModelConfig) -> None:
    _check_v1_env(report)
    _check_quota_groups(report, config)
    _check_active_transport_availability(report, config)


def _check_v1_env(report: dict[str, Any]) -> None:
    for name, (severity, message) in _V1_ENV_RULES.items():
        if os.environ.get(name) is None:
            continue
        target = report["blockers"] if severity == "BLOCKED" else report["warnings"]
        target.append(_issue(f"v1_env_{name.lower()}", message, env=name))
    for name in sorted(key for key in os.environ if key.startswith(_LEGACY_OPUS_ENV_PREFIX)):
        report["blockers"].append(_issue("legacy_opus_env_blocked", "WORKFLOW_OPUS_* env vars are deprecated under models.v2", env=name))


def _check_quota_groups(report: dict[str, Any], config: mc.WorkflowModelConfig) -> None:
    for group, roles in report.get("quota_groups", {}).items():
        if len(roles) >= 2:
            report["warnings"].append(_issue("quota_group_shared", f"quota_group {group!r} is shared by roles {roles}", quota_group=group, roles=roles))
    for role in config.roles.values():
        if not role.degrade_to:
            continue
        source = config.transports.get(role.transport)
        target = config.transports.get(role.degrade_to)
        if source and target and (source.quota_group or source.name) == (target.quota_group or target.name):
            report["warnings"].append(_issue("degrade_to_same_quota_group", f"roles.{role.name}.degrade_to shares quota_group with primary transport", role=role.name, quota_group=source.quota_group or source.name))


def _check_active_transport_availability(report: dict[str, Any], config: mc.WorkflowModelConfig) -> None:
    for role in config.roles.values():
        transport = config.transports.get(role.transport)
        if transport is None:
            continue
        if transport.kind == "subprocess":
            executable = transport.primary_executable()
            if executable and shutil.which(executable):
                continue
            target = report["blockers"] if role.on_unavailable == "fail" else report["warnings"]
            target.append(_issue("cli_executable_missing", f"roles.{role.name} executable is missing: {Path(executable or '').name or '<empty>'}", role=role.name, transport=transport.name))
        if transport.kind == "http":
            _check_http_transport(report, role, transport)
        if role.name == "self_reviewer" and transport.executor_backend() not in {"external_cli", "manual", "noop_test_only"}:
            report["warnings"].append(_issue("self_reviewer_real_driver_unimplemented", "self_reviewer real model driver is not implemented yet", role=role.name, transport=transport.name))


def _check_http_transport(report: dict[str, Any], role: mc.WorkflowRoleConfig, transport: mc.WorkflowTransportConfig) -> None:
    base_url = _transport_base_url(transport)
    if not base_url:
        _availability_issue(report, role, "api_base_url_missing", f"roles.{role.name} transport {transport.name!r} has no base_url/base_url_env value")
    if not transport.api_key_env or not os.environ.get(transport.api_key_env):
        _availability_issue(report, role, "api_key_missing", f"roles.{role.name} transport {transport.name!r} api key env is missing")
    api_format = str(transport.api_format or "").strip().lower()
    if transport.interface == "tool_use" and api_format not in _OPENAI_CHAT_API_FORMATS:
        report["blockers"].append(_issue("tool_use_api_format_not_openai_chat", "tool_use HTTP transports must set api_format=openai_chat", role=role.name, transport=transport.name))
    if transport.interface != "tool_use" and api_format not in _OPENAI_CHAT_API_FORMATS:
        return
    endpoint, error = normalize_openai_endpoint(base_url, api_format=transport.api_format)
    if error:
        report["blockers"].append(_issue(error, f"invalid OpenAI endpoint for transport {transport.name!r}", role=role.name, transport=transport.name))
    elif endpoint:
        report["transports"].setdefault(transport.name, {})["endpoint_host"] = urlparse.urlsplit(endpoint).hostname or ""
        report["transports"].setdefault(transport.name, {})["endpoint_path"] = urlparse.urlsplit(endpoint).path


def _availability_issue(report: dict[str, Any], role: mc.WorkflowRoleConfig, code: str, message: str) -> None:
    target = report["blockers"] if role.on_unavailable == "fail" else report["warnings"]
    target.append(_issue(code, message, role=role.name, transport=role.transport))


def normalize_openai_endpoint(base_url: str, *, api_format: str) -> tuple[str, str]:
    raw = str(base_url or "").strip()
    if not raw:
        return "", "api_base_url_missing"
    parsed = urlparse.urlsplit(raw)
    if not parsed.scheme or not parsed.netloc:
        return "", "endpoint_malformed"
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" and host not in _LOCALHOSTS:
        return "", "non_https_endpoint"
    path = parsed.path.rstrip("/")
    normalized_format = str(api_format or "").strip().lower()
    if "anthropic" in path.lower() and normalized_format in _OPENAI_CHAT_API_FORMATS:
        return "", "openai_transport_points_to_anthropic_endpoint"
    if normalized_format in {"", "auto"}:
        return "", "tool_use_api_format_auto"
    if normalized_format not in _OPENAI_CHAT_API_FORMATS:
        return "", "api_format_not_chat_completions"
    if path.endswith("/chat/completions"):
        endpoint_path = path
    elif path.endswith("/v1"):
        endpoint_path = f"{path}/chat/completions"
    elif not path:
        endpoint_path = "/v1/chat/completions"
    else:
        endpoint_path = f"{path}/v1/chat/completions"
    return urlparse.urlunsplit((parsed.scheme, parsed.netloc, endpoint_path, "", "")), ""


def _run_probe_checks(
    report: dict[str, Any],
    config: mc.WorkflowModelConfig,
    *,
    project_root: Path,
    no_cache: bool,
    cache_ttl_seconds: int | None,
    urlopen_fn: Callable[[urlrequest.Request, int], Any] | None,
) -> None:
    active = {role.transport for role in config.roles.values()}
    for role in config.roles.values():
        transport = config.transports.get(role.transport)
        if transport is None or transport.name not in active:
            continue
        if transport.kind != "http" or transport.interface not in {"tool_use", "chat"}:
            continue
        if str(transport.api_format or "").strip().lower() not in _OPENAI_CHAT_API_FORMATS:
            report["probes"][role.name] = {"status": "SKIPPED", "reason": "api_format_not_openai_chat"}
            continue
        if transport.interface == "tool_use":
            result = _cached_or_run_probe(
                project_root=project_root,
                role=role,
                transport=transport,
                no_cache=no_cache,
                cache_ttl_seconds=cache_ttl_seconds,
                urlopen_fn=urlopen_fn,
            )
        else:
            result = _cached_or_run_chat_probe(
                project_root=project_root,
                role=role,
                transport=transport,
                no_cache=no_cache,
                cache_ttl_seconds=cache_ttl_seconds,
                urlopen_fn=urlopen_fn,
            )
        report["probes"][role.name] = result
        if result.get("status") != "PASS" and role.on_unavailable == "fail" and transport.interface == "tool_use":
            report["blockers"].append(_issue(str(result.get("reason") or "tool_probe_failed"), "tool_use probe failed", role=role.name, transport=transport.name))
        elif result.get("status") != "PASS" and role.on_unavailable == "fail" and transport.interface == "chat" and role.name in {"planner", "plan_reviewer"}:
            report["blockers"].append(_issue(str(result.get("reason") or "chat_probe_failed"), "HTTP chat planner probe failed", role=role.name, transport=transport.name))
        elif result.get("status") != "PASS":
            report["warnings"].append(_issue(str(result.get("reason") or "probe_failed"), "HTTP probe did not pass", role=role.name, transport=transport.name))
        if result.get("model_warning"):
            report["warnings"].append(dict(result["model_warning"]))


def _run_smoke_checks(
    report: dict[str, Any],
    config: mc.WorkflowModelConfig,
    *,
    project_root: Path,
    smoke: str,
) -> None:
    mode = str(smoke or "").strip().lower()
    if mode in {"1", "true", "yes"}:
        mode = "local"
    if mode not in {"local", "real", "patch-local", "patch-real", "planner"}:
        report["blockers"].append(_issue("smoke_mode_invalid", f"unknown doctor smoke mode: {smoke!r}"))
        return
    if mode == "planner":
        _run_planner_smoke_checks(report, config, project_root=project_root)
        return
    active = {role.transport for role in config.roles.values()}
    for role in config.roles.values():
        transport = config.transports.get(role.transport)
        if role.name != "executor" or transport is None or transport.name not in active:
            continue
        if transport.kind != "http" or transport.interface != "tool_use":
            report["smokes"][role.name] = {"status": "SKIPPED", "reason": "executor_not_http_tool_use"}
            continue
        if str(transport.api_format or "").strip().lower() not in _OPENAI_CHAT_API_FORMATS:
            report["smokes"][role.name] = {"status": "SKIPPED", "reason": "api_format_not_openai_chat"}
            continue
        result = _run_openai_tool_use_smoke(project_root=project_root, role=role, transport=transport, mode=mode)
        report["smokes"][role.name] = result
        if result.get("status") != "PASS":
            target = report["blockers"] if role.on_unavailable == "fail" else report["warnings"]
            target.append(_issue(str(result.get("reason") or "smoke_failed"), "openai_tool_use smoke failed", role=role.name, transport=transport.name, mode=mode))


def _run_planner_smoke_checks(
    report: dict[str, Any],
    config: mc.WorkflowModelConfig,
    *,
    project_root: Path,
) -> None:
    role = config.roles.get("planner")
    transport = config.transports.get(role.transport) if role is not None else None
    if role is None or transport is None:
        report["smokes"]["planner"] = {"status": "SKIPPED", "reason": "planner_role_missing"}
        return
    if transport.kind != "http" or transport.interface != "chat":
        report["smokes"]["planner"] = {"status": "SKIPPED", "reason": "planner_not_http_chat"}
        return
    result = _run_openai_chat_planner_smoke(project_root=project_root, role=role, transport=transport)
    report["smokes"]["planner"] = result
    if result.get("status") != "PASS":
        target = report["blockers"] if role.on_unavailable == "fail" else report["warnings"]
        target.append(_issue(str(result.get("reason") or "planner_smoke_failed"), "openai_chat planner smoke failed", role=role.name, transport=transport.name, mode="planner"))


def _run_openai_chat_planner_smoke(
    *,
    project_root: Path,
    role: mc.WorkflowRoleConfig,
    transport: mc.WorkflowTransportConfig,
) -> dict[str, Any]:
    smoke_root = project_root / ".claude" / "workflow" / ".doctor_smoke" / hashlib.sha256(str(time.time_ns()).encode("utf-8")).hexdigest()[:12]
    sample_project = smoke_root / "project"
    fake_env = "WORKFLOW_DOCTOR_FAKE_OPENAI_KEY"
    fake_server: ThreadingHTTPServer | None = None
    fake_thread: threading.Thread | None = None
    old_env: str | None = None
    try:
        sample_project.mkdir(parents=True, exist_ok=True)
        (sample_project / "README.md").write_text("doctor planner smoke\n", encoding="utf-8")
        fake_server, fake_thread, base_url = _start_fake_planner_smoke_server()
        fake_transport = mc.WorkflowTransportConfig(
            name=f"{transport.name}_doctor_fake",
            kind="http",
            driver=transport.driver or "openai_compatible",
            interface="chat",
            api_format=transport.api_format or "openai_chat",
            base_url=base_url,
            api_key_env=fake_env,
            quota_group=transport.quota_group,
            provides=list(transport.provides),
        )
        old_env = os.environ.get(fake_env)
        os.environ[fake_env] = "doctor-fake-key"
        diagnostics: dict[str, Any] = {}
        plan, error = generate_plan(
            executable="",
            task_direction="Doctor planner smoke: update README.md with a tiny code/test scoped task.",
            context_text="Project files:\n- README.md\n",
            previous_findings=[],
            round_number=1,
            timeout_seconds=5,
            model=role.model,
            transport=fake_transport,
            diagnostics_out=diagnostics,
            project_root=sample_project,
        )
        if plan is None:
            return {"status": "BLOCKED", "reason": "planner_smoke_failed", "error": error, "diagnostics": diagnostics}
        tasks = list(plan.get("tasks") or []) if isinstance(plan, dict) else []
        if not tasks:
            return {"status": "BLOCKED", "reason": "planner_smoke_no_tasks", "diagnostics": diagnostics}
        return {"status": "PASS", "reason": "openai_chat_planner_smoke_passed", "task_count": len(tasks), "diagnostics": diagnostics}
    except Exception as exc:
        return {"status": "BLOCKED", "reason": "planner_smoke_exception", "detail": str(exc)}
    finally:
        if fake_server is not None:
            fake_server.shutdown()
            fake_server.server_close()
        if fake_thread is not None:
            fake_thread.join(timeout=2)
        if old_env is None:
            os.environ.pop(fake_env, None)
        else:
            os.environ[fake_env] = old_env
        shutil.rmtree(smoke_root, ignore_errors=True)


def _run_openai_tool_use_smoke(
    *,
    project_root: Path,
    role: mc.WorkflowRoleConfig,
    transport: mc.WorkflowTransportConfig,
    mode: str,
) -> dict[str, Any]:
    force_patch = mode.startswith("patch-")
    transport_mode = "real" if mode.endswith("real") else "local"
    protocol = openai_tool_use.EXACT_STR_REPLACE_PROTOCOL if force_patch else (role.execution_protocol or openai_tool_use.FULL_FILE_PROTOCOL)
    patch_protocol = protocol == openai_tool_use.EXACT_STR_REPLACE_PROTOCOL
    smoke_root = project_root / ".claude" / "workflow" / ".doctor_smoke" / hashlib.sha256(str(time.time_ns()).encode("utf-8")).hexdigest()[:12]
    sample_project = smoke_root / "project"
    planning_dir = sample_project / ".claude" / "workflow" / "smoke"
    request_path = planning_dir / ".execution_request.json"
    old_env: str | None = None
    fake_env = "WORKFLOW_DOCTOR_FAKE_OPENAI_KEY"
    fake_server: ThreadingHTTPServer | None = None
    fake_thread: threading.Thread | None = None
    try:
        planning_dir.mkdir(parents=True, exist_ok=True)
        sample_path = sample_project / ("sample.py" if patch_protocol else "sample.txt")
        original_text = "def value():\n    return 'original'\n" if patch_protocol else "original\n"
        expected_text = "def value():\n    return 'updated'\n" if patch_protocol else "updated\n"
        sample_path.write_text(original_text, encoding="utf-8")
        if transport_mode == "local":
            fake_server, fake_thread, base_url = _start_fake_smoke_server(
                protocol=protocol,
                sample_hash=hashlib.sha256(sample_path.read_bytes()).hexdigest(),
            )
        else:
            base_url = _transport_base_url(transport)
        config = ExecutionBackendConfig(
            backend="openai_tool_use",
            command="",
            project_root=sample_project,
            planning_dir=planning_dir,
            feature="doctor-smoke",
            model=role.model,
            base_url=base_url,
            api_key_env=fake_env if transport_mode == "local" else str(transport.api_key_env or ""),
            api_format=transport.api_format,
            transport_name=transport.name,
            execution_protocol=protocol,
            runtime_caps=dict(role.runtime_caps),
        )
        request_payload = _smoke_execution_request(
            project_root=sample_project,
            planning_dir=planning_dir,
            execution_protocol=protocol,
        )
        atomic_write_json(request_path, request_payload)
        if transport_mode == "local":
            old_env = os.environ.get(fake_env)
            os.environ[fake_env] = "doctor-fake-key"
        result = openai_tool_use.materialize_openai_tool_use_result(
            config=config,
            request_path=request_path,
            request_payload=request_payload,
        )
        status = str(result.get("status") or "").upper()
        sample_text = sample_path.read_text(encoding="utf-8", errors="replace")
        if status != "PASS":
            return {"status": "BLOCKED", "reason": str(result.get("error_code") or "smoke_failed"), "result": redact_jsonable(result)}
        if sample_text != expected_text:
            return {"status": "BLOCKED", "reason": "smoke_file_not_committed", "result": redact_jsonable(result)}
        manifest = planning_dir / ".execution_tool_manifest.json"
        if not manifest.exists():
            return {"status": "BLOCKED", "reason": "tool_manifest_missing", "result": redact_jsonable(result)}
        if patch_protocol:
            patch_log = planning_dir / openai_tool_use.PATCH_ATTEMPTS_FILENAME
            if not patch_log.exists():
                return {"status": "BLOCKED", "reason": "patch_attempts_missing", "result": redact_jsonable(result)}
            manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
            tools = list(manifest_payload.get("tools") or []) if isinstance(manifest_payload, dict) else []
            if "str_replace" not in tools:
                return {"status": "BLOCKED", "reason": "patch_manifest_invalid", "result": redact_jsonable(result)}
        return {
            "status": "PASS",
            "reason": "openai_tool_use_patch_smoke_passed" if patch_protocol else "openai_tool_use_smoke_passed",
            "mode": mode,
            "execution_protocol": protocol,
            "changed_files": result.get("changed_files") or [],
        }
    except Exception as exc:
        return {"status": "BLOCKED", "reason": "smoke_exception", "detail": str(exc)}
    finally:
        if fake_server is not None:
            fake_server.shutdown()
            fake_server.server_close()
        if fake_thread is not None:
            fake_thread.join(timeout=2)
        if old_env is None:
            os.environ.pop(fake_env, None)
        else:
            os.environ[fake_env] = old_env
        shutil.rmtree(smoke_root, ignore_errors=True)


def _smoke_execution_request(*, project_root: Path, planning_dir: Path, execution_protocol: str = openai_tool_use.FULL_FILE_PROTOCOL) -> dict[str, Any]:
    patch_protocol = execution_protocol == openai_tool_use.EXACT_STR_REPLACE_PROTOCOL
    sample_file = "sample.py" if patch_protocol else "sample.txt"
    requested_action = (
        "Call get_file_hash for sample.py, then call str_replace exactly once with old_text \"return 'original'\" and new_text \"return 'updated'\". Finish after the patch succeeds."
        if patch_protocol
        else "Replace sample.txt with exactly the word 'updated' followed by one newline. No other text."
    )
    requirements = (
        "Tool contract test: do not call read_file repeatedly. Use get_file_hash, then str_replace with expected_occurrences=1 and the sha256 returned by get_file_hash. The final sample.py bytes must be exactly: def value():\\n    return 'updated'\\n"
        if patch_protocol
        else "The final sample.txt bytes must be exactly: updated\\n"
    )
    verify_cmd = (
        ""
        if patch_protocol
        else "python -c \"from pathlib import Path; assert Path('sample.txt').read_text(encoding='utf-8') == 'updated\\n'\""
    )
    return {
        "schema_version": "execution.request.v1",
        "feature": "doctor-smoke",
        "task": "DOCTOR_SMOKE",
        "backend": "openai_tool_use",
        "execution_protocol": execution_protocol,
        "backend_capabilities": {},
        "backend_capability_truth": {},
        "executor_command": "",
        "guard_decision": {},
        "project_root": str(project_root),
        "planning_dir": str(planning_dir),
        "task_id": "DOCTOR_SMOKE",
        "requested_action": requested_action,
        "review_round": 0,
        "attempt": 1,
        "files_to_change": [sample_file],
        "invariants": [],
        "task_card": {
            "patch_plan": {
                "path": sample_file,
                "old_text": "return 'original'" if patch_protocol else "",
                "new_text": "return 'updated'" if patch_protocol else "",
                "expected_occurrences": 1,
            }
        } if patch_protocol else {},
        "task_scope": "doctor_smoke",
        "task_requirements": requirements,
        "verify_cmd": verify_cmd,
        "archetype": "",
        "capabilities": [],
        "surface": "",
        "must_fix": [],
        "scope_risk_warnings": [],
        "execution_timeout_hint": None,
    }


def _start_fake_smoke_server(*, protocol: str, sample_hash: str) -> tuple[ThreadingHTTPServer, threading.Thread, str]:
    if protocol == openai_tool_use.EXACT_STR_REPLACE_PROTOCOL:
        calls = [
            ("get_file_hash", {"path": "sample.py"}),
            (
                "str_replace",
                {
                    "path": "sample.py",
                    "old_text": "return 'original'",
                    "new_text": "return 'updated'",
                    "expected_occurrences": 1,
                    "precondition_sha256": sample_hash,
                },
            ),
            ("finish_execution", {"summary": "doctor smoke patched sample.py"}),
        ]
    else:
        calls = [
            ("read_file", {"path": "sample.txt"}),
            ("write_new_file", {"path": "sample.txt", "content": "updated\n"}),
            ("finish_execution", {"summary": "doctor smoke updated sample.txt"}),
        ]
    index = {"value": 0}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            if self.path.rstrip("/") != "/v1/chat/completions":
                self.send_error(404)
                return
            with lock:
                i = index["value"]
                if i >= len(calls):
                    i = len(calls) - 1
                index["value"] += 1
            name, args = calls[i]
            payload = {
                "model": "doctor-fake",
                "usage": {"total_tokens": 1},
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": f"call_{i}",
                                    "type": "function",
                                    "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
                                }
                            ]
                        }
                    }
                ],
            }
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = int(server.server_address[1])
    return server, thread, f"http://127.0.0.1:{port}/v1"


def _start_fake_planner_smoke_server() -> tuple[ThreadingHTTPServer, threading.Thread, str]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            if self.path.rstrip("/") != "/v1/chat/completions":
                self.send_error(404)
                return
            plan = {
                "summary": "doctor planner smoke",
                "business_outcome": "doctor planner smoke",
                "out_of_scope": [],
                "source_of_truth": [],
                "source_of_truth_canonical": [],
                "path_type": "write",
                "layers": ["docs"],
                "coverage_hints": ["smoke"],
                "module_boundaries": [{"name": "docs", "surface": "docs", "roots": ["README.md"], "layers": ["docs"]}],
                "verify_recipes": [{"surface": "docs", "command": "python -m pytest -q", "required": False, "roots": []}],
                "approval_points": [],
                "execution_constraints": {},
                "confidence": "high",
                "confidence_issues": [],
                "tasks": [
                    {
                        "task_id": "T1",
                        "task_name": "Doctor planner smoke task",
                        "layer_owner": "docs",
                        "surface": "docs",
                        "files_to_change": ["README.md"],
                        "new_files": [],
                        "coverage_hints": ["smoke"],
                        "approach": "Use README.md as the smoke target.",
                        "invariants": ["scope remains README.md"],
                        "test_plan": "python -m pytest -q",
                        "verify_cmd": "python -m pytest -q",
                        "depends_on": [],
                        "forbidden_changes": [],
                        "provides": [],
                        "requires": [],
                        "api_contracts": [],
                    }
                ],
                "risks": [],
                "change_log": [],
                "self_assessment": {"score": 10.0, "notes": ["smoke"]},
            }
            payload = {
                "model": "doctor-fake",
                "usage": {"total_tokens": 1},
                "choices": [{"message": {"content": json.dumps(plan, ensure_ascii=False)}}],
            }
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = int(server.server_address[1])
    return server, thread, f"http://127.0.0.1:{port}/v1"


def _cached_or_run_probe(
    *,
    project_root: Path,
    role: mc.WorkflowRoleConfig,
    transport: mc.WorkflowTransportConfig,
    no_cache: bool,
    cache_ttl_seconds: int | None,
    urlopen_fn: Callable[[urlrequest.Request, int], Any] | None,
) -> dict[str, Any]:
    endpoint, error = normalize_openai_endpoint(_transport_base_url(transport), api_format=transport.api_format)
    if error:
        return {"status": "BLOCKED", "reason": error}
    api_key = os.environ.get(transport.api_key_env or "", "")
    key_present = bool(api_key)
    key = _probe_cache_key(role=role, transport=transport, endpoint=endpoint, key_present=key_present, key_fingerprint=_secret_fingerprint(api_key))
    cache_path = project_root / _CACHE_REL
    if not no_cache:
        cached = _read_probe_cache(cache_path, key)
        if cached:
            cached["cached"] = True
            return cached
    result = probe_openai_tools(
        endpoint=endpoint,
        model=role.model,
        api_key=api_key,
        timeout_seconds=5,
        urlopen_fn=urlopen_fn,
    )
    result["cached"] = False
    ttl = _ttl_for_probe_result(result, override=cache_ttl_seconds)
    if ttl > 0 and not no_cache:
        _write_probe_cache(cache_path, key, result, ttl_seconds=ttl)
    return result


def _cached_or_run_chat_probe(
    *,
    project_root: Path,
    role: mc.WorkflowRoleConfig,
    transport: mc.WorkflowTransportConfig,
    no_cache: bool,
    cache_ttl_seconds: int | None,
    urlopen_fn: Callable[[urlrequest.Request, int], Any] | None,
) -> dict[str, Any]:
    endpoint, error = normalize_openai_endpoint(_transport_base_url(transport), api_format=transport.api_format)
    if error:
        return {"status": "BLOCKED", "reason": error}
    api_key = os.environ.get(transport.api_key_env or "", "")
    key_present = bool(api_key)
    key = _probe_cache_key(
        role=role,
        transport=transport,
        endpoint=endpoint,
        key_present=key_present,
        key_fingerprint=_secret_fingerprint(api_key),
        probe_kind="chat_plan",
    )
    cache_path = project_root / _CACHE_REL
    if not no_cache:
        cached = _read_probe_cache(cache_path, key)
        if cached:
            cached["cached"] = True
            return cached
    result = probe_openai_chat_plan(
        transport=transport,
        model=role.model,
        timeout_seconds=30,
        urlopen_fn=urlopen_fn,
    )
    result["cached"] = False
    ttl = _ttl_for_probe_result(result, override=cache_ttl_seconds)
    if ttl > 0 and not no_cache:
        _write_probe_cache(cache_path, key, result, ttl_seconds=ttl)
    return result


def probe_openai_chat_plan(
    *,
    transport: mc.WorkflowTransportConfig,
    model: str,
    timeout_seconds: int = 30,
    urlopen_fn: Callable[[urlrequest.Request, int], Any] | None = None,
) -> dict[str, Any]:
    result = call_openai_chat(
        transport=transport,
        model=model,
        system="You are a diagnostic planning probe. Return JSON only.",
        user=(
            "Return a minimal workflow plan JSON for this fake task. "
            "It must include summary and one tasks item with task_id, task_name, "
            "files_to_change, invariants, test_plan, verify_cmd, depends_on, "
            "provides, requires, and api_contracts."
        ),
        timeout_seconds=timeout_seconds,
        urlopen_fn=urlopen_fn,
    )
    payload: dict[str, Any] = {
        "http_status": int(result.http_status or 0),
        "request_bytes": int(result.request_bytes or 0),
        "response_bytes": int(result.response_bytes or 0),
        "wallclock_ms": int(result.wallclock_ms or 0),
    }
    if result.model_warning:
        payload["model_warning"] = _issue(
            "probe_model_substituted",
            "probe response model differs from requested model",
            requested_model=str(result.model_warning.get("requested") or model),
            actual_model=str(result.model_warning.get("actual") or ""),
        )
    if not result.ok:
        return {"status": "BLOCKED", "reason": result.kind or "chat_probe_failed", "detail": result.detail, **payload}
    plan, parse_error = _parse_planner_response(result.raw_text)
    if plan is None:
        return {"status": "BLOCKED", "reason": "invalid_plan_json", "detail": parse_error, **payload}
    tasks = list(plan.get("tasks") or []) if isinstance(plan, dict) else []
    first = dict(tasks[0]) if tasks and isinstance(tasks[0], dict) else {}
    required = {"task_id", "task_name", "files_to_change", "invariants", "test_plan", "verify_cmd", "depends_on", "provides", "requires", "api_contracts"}
    missing = sorted(key for key in required if key not in first)
    if not str(plan.get("summary") or "").strip() or not first or missing:
        return {"status": "BLOCKED", "reason": "invalid_plan_schema", "missing": missing, **payload}
    return {"status": "PASS", "reason": "chat_plan_supported", **payload}


def probe_openai_tools(
    *,
    endpoint: str,
    model: str,
    api_key: str,
    timeout_seconds: int = 5,
    urlopen_fn: Callable[[urlrequest.Request, int], Any] | None = None,
) -> dict[str, Any]:
    if not api_key:
        return {"status": "BLOCKED", "reason": "auth_missing"}
    payload = _probe_payload(model)
    request = urlrequest.Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    opener = urlrequest.build_opener(SafeRedirectHandler)
    open_fn = urlopen_fn or (lambda req, timeout: opener.open(req, timeout=timeout))
    try:
        response = open_fn(request, max(1, int(timeout_seconds)))
        body = response.read().decode("utf-8", errors="replace")
    except RedirectBlocked as exc:
        return {"status": "BLOCKED", "reason": "redirect_blocked", "detail": str(exc)}
    except urlerror.HTTPError as exc:
        body = _safe_http_error_body(exc)
        return _http_probe_failure(exc.code, body)
    except ssl.SSLError as exc:
        return {"status": "BLOCKED", "reason": "ssl_error", "detail": str(exc)}
    except TimeoutError:
        return {"status": "BLOCKED", "reason": "timeout"}
    except urlerror.URLError as exc:
        reason = str(getattr(exc, "reason", exc))
        if "timed out" in reason.lower():
            return {"status": "BLOCKED", "reason": "timeout", "detail": reason}
        return {"status": "BLOCKED", "reason": "http_error", "detail": reason}
    except OSError as exc:
        return {"status": "BLOCKED", "reason": "http_error", "detail": str(exc)}
    return _parse_tool_probe_response(body, requested_model=model)


def _probe_payload(model: str) -> dict[str, Any]:
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a diagnostic probe. Call the requested tool exactly once."},
            {"role": "user", "content": 'Call echo_tool with exactly {"ping":"doctor_probe"}.'},
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "echo_tool",
                    "description": "Echo a diagnostic sentinel.",
                    "parameters": {
                        "type": "object",
                        "properties": {"ping": {"type": "string"}},
                        "required": ["ping"],
                        "additionalProperties": False,
                    },
                },
            }
        ],
        "tool_choice": {"type": "function", "function": {"name": "echo_tool"}},
        "temperature": 0,
        "max_tokens": 64,
        "stream": False,
    }


def _parse_tool_probe_response(body: str, *, requested_model: str) -> dict[str, Any]:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return {"status": "BLOCKED", "reason": "endpoint_malformed"}
    if not isinstance(payload, dict):
        return {"status": "BLOCKED", "reason": "endpoint_malformed"}
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return {"status": "BLOCKED", "reason": "no_tool_calls"}
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return {"status": "BLOCKED", "reason": "no_tool_calls"}
    calls = message.get("tool_calls")
    if not isinstance(calls, list) or not calls:
        content = str(message.get("content") or "")
        reason = "tools_silently_dropped" if SENTINEL in content else "no_tool_calls"
        return {"status": "BLOCKED", "reason": reason}
    first = calls[0]
    if not isinstance(first, dict) or first.get("type") != "function":
        return {"status": "BLOCKED", "reason": "invalid_tool_args"}
    function = first.get("function")
    if not isinstance(function, dict) or function.get("name") != "echo_tool":
        return {"status": "BLOCKED", "reason": "invalid_tool_args"}
    arguments = function.get("arguments")
    if not isinstance(arguments, str) or not arguments.strip():
        return {"status": "BLOCKED", "reason": "invalid_tool_args"}
    try:
        args = json.loads(arguments)
    except json.JSONDecodeError:
        return {"status": "BLOCKED", "reason": "invalid_tool_args"}
    if not isinstance(args, dict) or args.get("ping") != SENTINEL:
        return {"status": "BLOCKED", "reason": "invalid_tool_args"}
    result: dict[str, Any] = {"status": "PASS", "reason": "tool_calls_supported"}
    actual_model = str(payload.get("model") or "").strip()
    if actual_model and actual_model != requested_model:
        result["model_warning"] = _issue(
            "probe_model_substituted",
            "probe response model differs from requested model",
            requested_model=requested_model,
            actual_model=actual_model,
        )
    return result


def _safe_http_error_body(exc: urlerror.HTTPError) -> str:
    try:
        return exc.read().decode("utf-8", errors="replace")
    except Exception:
        return ""


def _http_probe_failure(status_code: int, body: str) -> dict[str, Any]:
    lowered = body.lower()
    if status_code in {401, 403}:
        return {"status": "BLOCKED", "reason": "auth_invalid", "http_status": status_code}
    if "stream" in lowered and status_code in {400, 501}:
        return {"status": "BLOCKED", "reason": "streaming_required", "http_status": status_code}
    if "tool" in lowered or "function" in lowered:
        return {"status": "BLOCKED", "reason": "tools_rejected", "http_status": status_code}
    if status_code >= 500:
        return {"status": "BLOCKED", "reason": "http_5xx", "http_status": status_code}
    return {"status": "BLOCKED", "reason": "http_error", "http_status": status_code}


def _transport_base_url(transport: mc.WorkflowTransportConfig) -> str:
    if transport.base_url:
        return transport.base_url
    if transport.base_url_env:
        return os.environ.get(transport.base_url_env, "")
    return ""


def _probe_cache_key(
    *,
    role: mc.WorkflowRoleConfig,
    transport: mc.WorkflowTransportConfig,
    endpoint: str,
    key_present: bool,
    key_fingerprint: str,
    probe_kind: str = "tools",
) -> str:
    material = {
        "probe_version": PROBE_VERSION,
        "mode": "probe",
        "probe_kind": str(probe_kind or "tools"),
        "role": role.name,
        "transport": transport.name,
        "interface": transport.interface,
        "model": role.model,
        "api_format": transport.api_format,
        "endpoint": endpoint,
        "key_present": bool(key_present),
        "key_fingerprint": key_fingerprint,
        "https_proxy_hash": _env_hash("HTTPS_PROXY"),
        "http_proxy_hash": _env_hash("HTTP_PROXY"),
        "no_proxy_hash": _env_hash("NO_PROXY"),
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode("utf-8")).hexdigest()


def _secret_fingerprint(value: str) -> str:
    if not value:
        return ""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _env_hash(name: str) -> str:
    value = os.environ.get(name, "")
    return hashlib.sha256(value.encode("utf-8")).hexdigest() if value else ""


def _read_probe_cache(path: Path, key: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    entry = dict(dict(payload.get("entries") or {}).get(key) or {})
    if not entry:
        return None
    if time.time() >= float(entry.get("expires_at", 0)):
        return None
    result = entry.get("result")
    return dict(result) if isinstance(result, dict) else None


def _write_probe_cache(path: Path, key: str, result: dict[str, Any], *, ttl_seconds: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path_lock(path):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            payload = {"schema_version": "doctor.cache.v1", "entries": {}}
        entries = dict(payload.get("entries") or {})
        entries[key] = {
            "expires_at": time.time() + max(1, int(ttl_seconds)),
            "result": redact_secrets(dict(result)),
        }
        payload["entries"] = entries
        atomic_write_json(path, redact_secrets(payload), use_lock=False)


def _ttl_for_probe_result(result: dict[str, Any], *, override: int | None) -> int:
    if override is not None:
        return max(0, int(override))
    reason = str(result.get("reason") or "").strip()
    if reason in {"auth_missing", "auth_invalid"}:
        return 0
    if reason in {"http_5xx", "timeout", "ssl_error"}:
        return 600
    if reason in {"tools_rejected", "tools_silently_dropped", "streaming_required", "no_tool_calls", "invalid_tool_args"}:
        return 3600
    if result.get("status") == "PASS":
        return 86400
    return 600


def _finalize_report(report: dict[str, Any]) -> dict[str, Any]:
    report["blockers"] = _dedupe_issues(report.get("blockers", []))
    report["warnings"] = _dedupe_issues(report.get("warnings", []))
    if report["blockers"]:
        report["status"] = "BLOCKED"
    elif report["warnings"]:
        report["status"] = "WARN"
    else:
        report["status"] = "PASS"
    return redact_secrets(report)


def _dedupe_issues(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in issues:
        key = json.dumps(item, sort_keys=True, ensure_ascii=False)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _issue(code: str, message: str, **details: Any) -> dict[str, Any]:
    payload = {"code": code, "message": message}
    if details:
        payload.update(details)
    return payload


__all__ = [
    "DOCTOR_REPORT_SCHEMA_VERSION",
    "doctor_exit_code",
    "doctor_models",
    "load_model_config_diagnostic",
    "normalize_openai_endpoint",
    "probe_openai_chat_plan",
    "probe_openai_tools",
    "redact_secrets",
]
