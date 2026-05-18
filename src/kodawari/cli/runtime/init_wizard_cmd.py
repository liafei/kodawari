"""`kodawari init-wizard` — interactive first-run config bootstrap.

Generates ``.claude/workflow/models.yaml`` and ``.env.example`` from one of
three opinionated presets so a new user can go from cloned repo to runnable
autopilot in a single command. Non-interactive mode via ``--preset <name>
--yes`` keeps it CI-friendly.

Out of scope: no API calls, no model probing, no remote downloads. Pure
template instantiation + filesystem writes. This is the static-config
counterpart to ``doctor preflight``.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from kodawari.cli.runtime.workflow_defaults import render_default_defaults_yaml
from kodawari.infra.io_atomic import atomic_write_text


PRESETS = ("claude-subscription", "openai-compatible", "multi-provider")


@dataclass(frozen=True)
class WizardAnswer:
    preset: str
    project_root: Path
    api_key_env: str
    base_url: str
    model: str
    planner_model: str
    reviewer_model: str
    executor_model: str
    planner_key_env: str
    reviewer_key_env: str
    executor_key_env: str
    planner_base_url: str
    reviewer_base_url: str
    executor_base_url: str


def run_init_wizard_command(args: argparse.Namespace) -> int:
    project_root = Path(str(getattr(args, "project_root", ".") or ".")).resolve()
    preset = str(getattr(args, "preset", "") or "").strip()
    non_interactive = bool(getattr(args, "yes", False))
    overwrite = bool(getattr(args, "overwrite", False))
    output_path = str(getattr(args, "output", "") or "").strip()

    if preset and preset not in PRESETS:
        print(json.dumps({
            "_rc": 2,
            "status": "FAIL",
            "error": f"unknown preset {preset!r}; expected one of {list(PRESETS)}",
        }, ensure_ascii=False, indent=2))
        return 2

    if not preset:
        if non_interactive:
            print(json.dumps({
                "_rc": 2,
                "status": "FAIL",
                "error": "non-interactive mode (--yes) requires --preset to be explicit",
            }, ensure_ascii=False, indent=2))
            return 2
        preset = _prompt_preset()

    answer = _collect_answers(
        preset=preset,
        project_root=project_root,
        non_interactive=non_interactive,
    )

    models_yaml = _render_models_yaml(answer)
    env_example = _render_env_example(answer)
    defaults_yaml = render_default_defaults_yaml()

    written = _write_artifacts(
        project_root=project_root,
        models_yaml=models_yaml,
        env_example=env_example,
        defaults_yaml=defaults_yaml,
        overwrite=overwrite,
    )

    payload = {
        "_rc": 0,
        "status": "PASS",
        "entrypoint": "kodawari init-wizard",
        "preset": preset,
        "project_root": str(project_root),
        "written": [str(p) for p in written],
        "next_steps": _next_steps(answer),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    if output_path:
        atomic_write_text(Path(output_path), json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------


def _prompt_preset(*, reader: Callable[[str], str] | None = None) -> str:
    reader = reader or input
    print("\nChoose a configuration preset:")
    print("  1) claude-subscription  — Claude Code CLI (no API key, uses subscription auth)")
    print("  2) openai-compatible    — Single OpenAI-compatible HTTP endpoint")
    print("  3) multi-provider       — Different models per role (planner/reviewer/executor)")
    while True:
        choice = reader("Enter 1, 2, or 3: ").strip()
        if choice in {"1", "claude-subscription"}:
            return "claude-subscription"
        if choice in {"2", "openai-compatible"}:
            return "openai-compatible"
        if choice in {"3", "multi-provider"}:
            return "multi-provider"
        print(f"Unrecognized choice {choice!r}; please enter 1, 2, or 3.")


def _collect_answers(
    *,
    preset: str,
    project_root: Path,
    non_interactive: bool,
    reader: Callable[[str], str] | None = None,
) -> WizardAnswer:
    reader = reader or input
    if preset == "claude-subscription":
        return _claude_subscription_defaults(project_root=project_root)
    if preset == "openai-compatible":
        if non_interactive:
            return _openai_compatible_defaults(project_root=project_root)
        return _prompt_openai_compatible(project_root=project_root, reader=reader)
    if preset == "multi-provider":
        if non_interactive:
            return _multi_provider_defaults(project_root=project_root)
        return _prompt_multi_provider(project_root=project_root, reader=reader)
    raise ValueError(f"unexpected preset {preset!r}")


def _claude_subscription_defaults(*, project_root: Path) -> WizardAnswer:
    return WizardAnswer(
        preset="claude-subscription",
        project_root=project_root,
        api_key_env="",
        base_url="",
        model="claude-opus-4-7",
        planner_model="claude-opus-4-7",
        reviewer_model="claude-opus-4-7",
        executor_model="claude-opus-4-7",
        planner_key_env="",
        reviewer_key_env="",
        executor_key_env="",
        planner_base_url="",
        reviewer_base_url="",
        executor_base_url="",
    )


def _openai_compatible_defaults(*, project_root: Path) -> WizardAnswer:
    return WizardAnswer(
        preset="openai-compatible",
        project_root=project_root,
        api_key_env="WORKFLOW_API_KEY",
        base_url="https://api.openai.com/v1",
        model="gpt-4o",
        planner_model="gpt-4o",
        reviewer_model="gpt-4o",
        executor_model="gpt-4o",
        planner_key_env="WORKFLOW_API_KEY",
        reviewer_key_env="WORKFLOW_API_KEY",
        executor_key_env="WORKFLOW_API_KEY",
        planner_base_url="https://api.openai.com/v1",
        reviewer_base_url="https://api.openai.com/v1",
        executor_base_url="https://api.openai.com/v1",
    )


def _multi_provider_defaults(*, project_root: Path) -> WizardAnswer:
    return WizardAnswer(
        preset="multi-provider",
        project_root=project_root,
        api_key_env="",
        base_url="",
        model="",
        planner_model="gpt-4o",
        reviewer_model="claude-opus-4-7",
        executor_model="gpt-4o",
        planner_key_env="WORKFLOW_PLANNER_API_KEY",
        reviewer_key_env="WORKFLOW_REVIEWER_API_KEY",
        executor_key_env="WORKFLOW_EXECUTOR_API_KEY",
        planner_base_url="https://api.openai.com/v1",
        reviewer_base_url="https://api.anthropic.com/v1",
        executor_base_url="https://api.openai.com/v1",
    )


def _prompt_openai_compatible(
    *,
    project_root: Path,
    reader: Callable[[str], str],
) -> WizardAnswer:
    defaults = _openai_compatible_defaults(project_root=project_root)
    base_url = reader(f"API base URL [{defaults.base_url}]: ").strip() or defaults.base_url
    api_key_env = reader(f"Env var name for API key [{defaults.api_key_env}]: ").strip() or defaults.api_key_env
    model = reader(f"Model name [{defaults.model}]: ").strip() or defaults.model
    return WizardAnswer(
        preset="openai-compatible",
        project_root=project_root,
        api_key_env=api_key_env,
        base_url=base_url,
        model=model,
        planner_model=model,
        reviewer_model=model,
        executor_model=model,
        planner_key_env=api_key_env,
        reviewer_key_env=api_key_env,
        executor_key_env=api_key_env,
        planner_base_url=base_url,
        reviewer_base_url=base_url,
        executor_base_url=base_url,
    )


def _prompt_multi_provider(
    *,
    project_root: Path,
    reader: Callable[[str], str],
) -> WizardAnswer:
    defaults = _multi_provider_defaults(project_root=project_root)
    print("\nConfigure each role (press Enter to accept the default):\n")
    planner_base_url = reader(f"Planner base URL [{defaults.planner_base_url}]: ").strip() or defaults.planner_base_url
    planner_key_env = reader(f"Planner key env [{defaults.planner_key_env}]: ").strip() or defaults.planner_key_env
    planner_model = reader(f"Planner model [{defaults.planner_model}]: ").strip() or defaults.planner_model
    reviewer_base_url = reader(f"Reviewer base URL [{defaults.reviewer_base_url}]: ").strip() or defaults.reviewer_base_url
    reviewer_key_env = reader(f"Reviewer key env [{defaults.reviewer_key_env}]: ").strip() or defaults.reviewer_key_env
    reviewer_model = reader(f"Reviewer model [{defaults.reviewer_model}]: ").strip() or defaults.reviewer_model
    executor_base_url = reader(f"Executor base URL [{defaults.executor_base_url}]: ").strip() or defaults.executor_base_url
    executor_key_env = reader(f"Executor key env [{defaults.executor_key_env}]: ").strip() or defaults.executor_key_env
    executor_model = reader(f"Executor model [{defaults.executor_model}]: ").strip() or defaults.executor_model
    return WizardAnswer(
        preset="multi-provider",
        project_root=project_root,
        api_key_env="",
        base_url="",
        model="",
        planner_model=planner_model,
        reviewer_model=reviewer_model,
        executor_model=executor_model,
        planner_key_env=planner_key_env,
        reviewer_key_env=reviewer_key_env,
        executor_key_env=executor_key_env,
        planner_base_url=planner_base_url,
        reviewer_base_url=reviewer_base_url,
        executor_base_url=executor_base_url,
    )


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def _render_models_yaml(answer: WizardAnswer) -> str:
    if answer.preset == "claude-subscription":
        return _render_claude_subscription_yaml(answer)
    return _render_http_roles_yaml(answer)


def _render_claude_subscription_yaml(answer: WizardAnswer) -> str:
    return (
        'schema_version: "models.v2"\n'
        "\n"
        "# Preset: claude-subscription — uses the Claude Code CLI subprocess. No API\n"
        "# key required; relies on `claude auth login` having been run.\n"
        "\n"
        "transports:\n"
        "  claude_subscription:\n"
        "    kind: subprocess\n"
        "    driver: claude_code\n"
        "    interface: agent\n"
        "    executable: claude\n"
        "    provides: [interface.agent, repo.read_file, repo.grep, repo.glob, repo.write_file]\n"
        "\n"
        "compatibility:\n"
        f"  - {{models: [{answer.model}], transports: [claude_subscription], interfaces: [agent]}}\n"
        "\n"
        "roles:\n"
        "  planner:\n"
        "    transport: claude_subscription\n"
        f"    model: {answer.planner_model}\n"
        "    requires: [interface.agent]\n"
        "    on_unavailable: fail\n"
        "\n"
        "  plan_reviewer:\n"
        "    transport: claude_subscription\n"
        f"    model: {answer.reviewer_model}\n"
        "    requires: [interface.agent]\n"
        "    on_unavailable: fail\n"
        "\n"
        "  impl_reviewer:\n"
        "    transport: claude_subscription\n"
        f"    model: {answer.reviewer_model}\n"
        "    requires: [interface.agent]\n"
        "    on_unavailable: fail\n"
        "\n"
        "  executor:\n"
        "    transport: claude_subscription\n"
        f"    model: {answer.executor_model}\n"
        "    requires: [interface.agent, repo.read_file, repo.write_file]\n"
        "    on_unavailable: fail\n"
    )


def _render_http_roles_yaml(answer: WizardAnswer) -> str:
    """Render an HTTP-based models.yaml. Used by both ``openai-compatible``
    (one transport reused by all roles) and ``multi-provider`` (three
    independent transports)."""
    transports: list[tuple[str, str, str]] = []  # (name, base_url, key_env)
    if answer.preset == "openai-compatible":
        transports.append(("primary_tool_use", answer.base_url, answer.api_key_env))
    else:
        seen: dict[tuple[str, str], str] = {}
        for role, base_url, key_env in (
            ("planner", answer.planner_base_url, answer.planner_key_env),
            ("reviewer", answer.reviewer_base_url, answer.reviewer_key_env),
            ("executor", answer.executor_base_url, answer.executor_key_env),
        ):
            handle = (base_url, key_env)
            if handle not in seen:
                seen[handle] = f"{role}_tool_use"
                transports.append((seen[handle], base_url, key_env))

    transport_block = "\n".join(
        f"  {name}:\n"
        "    kind: http\n"
        "    driver: openai_compatible\n"
        "    interface: tool_use\n"
        "    api_format: openai_chat\n"
        f"    base_url: {base_url}\n"
        f"    api_key_env: {key_env}\n"
        "    provides: [interface.tool_use, repo.read_file, repo.write_file]\n"
        for name, base_url, key_env in transports
    )

    if answer.preset == "openai-compatible":
        planner_transport = reviewer_transport = executor_transport = "primary_tool_use"
    else:
        # Map back from (base_url, key_env) → transport name
        rev_lookup = {(t[1], t[2]): t[0] for t in transports}
        planner_transport = rev_lookup[(answer.planner_base_url, answer.planner_key_env)]
        reviewer_transport = rev_lookup[(answer.reviewer_base_url, answer.reviewer_key_env)]
        executor_transport = rev_lookup[(answer.executor_base_url, answer.executor_key_env)]

    models_used = sorted({answer.planner_model, answer.reviewer_model, answer.executor_model})
    compat_lines = []
    for model_name in models_used:
        # Find the role(s) that use this model and emit a row per transport.
        rows: set[str] = set()
        for role_model, role_transport in (
            (answer.planner_model, planner_transport),
            (answer.reviewer_model, reviewer_transport),
            (answer.executor_model, executor_transport),
        ):
            if role_model == model_name:
                rows.add(role_transport)
        for transport_name in sorted(rows):
            compat_lines.append(
                f"  - {{models: [{model_name}], transports: [{transport_name}], "
                "interfaces: [tool_use], api_formats: [openai_chat]}"
            )

    return (
        'schema_version: "models.v2"\n'
        "\n"
        f"# Preset: {answer.preset} — generated by `kodawari init-wizard`.\n"
        "# Set the env vars listed below (see .env.example) before running.\n"
        "\n"
        "transports:\n"
        f"{transport_block}\n"
        "compatibility:\n"
        + "\n".join(compat_lines) + "\n\n"
        "roles:\n"
        "  planner:\n"
        f"    transport: {planner_transport}\n"
        f"    model: {answer.planner_model}\n"
        "    requires: [interface.tool_use]\n"
        "    on_unavailable: fail\n"
        "\n"
        "  plan_reviewer:\n"
        f"    transport: {reviewer_transport}\n"
        f"    model: {answer.reviewer_model}\n"
        "    requires: [interface.tool_use]\n"
        "    on_unavailable: fail\n"
        "\n"
        "  impl_reviewer:\n"
        f"    transport: {reviewer_transport}\n"
        f"    model: {answer.reviewer_model}\n"
        "    requires: [interface.tool_use]\n"
        "    on_unavailable: fail\n"
        "\n"
        "  executor:\n"
        f"    transport: {executor_transport}\n"
        f"    model: {answer.executor_model}\n"
        "    requires: [interface.tool_use, repo.read_file, repo.write_file]\n"
        "    on_unavailable: fail\n"
    )


def _render_env_example(answer: WizardAnswer) -> str:
    if answer.preset == "claude-subscription":
        return (
            "# claude-subscription preset uses the Claude Code CLI subprocess auth.\n"
            "# No API keys are needed here. Run `claude auth login` first.\n"
            "#\n"
            "# Optional knobs:\n"
            "# WORKFLOW_REVIEWER_TIMEOUT=300\n"
            "# WORKFLOW_EXECUTOR_TIMEOUT_SECONDS=600\n"
        )

    keys = {
        answer.planner_key_env,
        answer.reviewer_key_env,
        answer.executor_key_env,
        answer.api_key_env,
    }
    keys.discard("")
    lines = [f"# Generated by `kodawari init-wizard` (preset: {answer.preset})\n#"]
    lines.append("# Set each key below before running. None of these are checked into git.")
    lines.append("# The wizard generated .env.example as a template — copy to .env and fill in.")
    lines.append("")
    for key in sorted(keys):
        lines.append(f"{key}=<paste-your-key-here>")
    lines.append("")
    lines.append("# Optional knobs:")
    lines.append("# WORKFLOW_REVIEW_ENABLED=1  # enable real peer review")
    lines.append("# WORKFLOW_REVIEWER_TIMEOUT=300")
    lines.append("# WORKFLOW_EXECUTOR_TIMEOUT_SECONDS=600")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------


def _write_artifacts(
    *,
    project_root: Path,
    models_yaml: str,
    env_example: str,
    defaults_yaml: str,
    overwrite: bool,
) -> list[Path]:
    config_dir = project_root / ".claude" / "workflow"
    config_dir.mkdir(parents=True, exist_ok=True)
    models_path = config_dir / "models.yaml"
    defaults_path = config_dir / "defaults.yaml"
    env_path = project_root / ".env.example"

    written: list[Path] = []
    if models_path.exists() and not overwrite:
        backup = models_path.with_suffix(".yaml.bak.before_wizard")
        if not backup.exists():
            backup.write_text(models_path.read_text(encoding="utf-8"), encoding="utf-8")
    atomic_write_text(models_path, models_yaml)
    written.append(models_path)

    # defaults.yaml is greenfield-only: only write if missing, never overwrite
    # an existing one (it's intentionally user-editable; the wizard shouldn't
    # clobber tuned values on a re-run).
    if not defaults_path.exists():
        atomic_write_text(defaults_path, defaults_yaml)
        written.append(defaults_path)

    if env_path.exists() and not overwrite:
        backup = env_path.with_suffix(".example.bak.before_wizard")
        if not backup.exists():
            backup.write_text(env_path.read_text(encoding="utf-8"), encoding="utf-8")
    atomic_write_text(env_path, env_example)
    written.append(env_path)

    return written


# ---------------------------------------------------------------------------
# Next-step hints
# ---------------------------------------------------------------------------


def _next_steps(answer: WizardAnswer) -> list[str]:
    steps: list[str] = []
    if answer.preset == "claude-subscription":
        steps.append("Run `claude auth login` if you have not already.")
    else:
        steps.append("Copy .env.example to .env and fill in your API keys.")
        steps.append("Or export the env vars in your shell (PowerShell / bash).")
    steps.append("Run `kodawari doctor preflight --feature <feature> --prd <path>` to sanity-check.")
    steps.append("Run `kodawari work-all --feature <feature> --prd <path> --planner-route model` to start a real run.")
    return steps


__all__ = [
    "PRESETS",
    "WizardAnswer",
    "run_init_wizard_command",
]
