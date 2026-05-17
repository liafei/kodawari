"""Thin contract-first command registry with generic planning extensions."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from kodawari.autopilot.planning.task_graph import build_task_graph, render_task_graph_markdown, validate_task_graph
from kodawari.autopilot.execution.execution_backend import execution_backend_choices, self_review_backend_choices
from kodawari.cli import contract_first_cmd as legacy_contract_first_cmd
from kodawari.cli.artifact_versions import ArtifactSchemaVersionError
from kodawari.cli.contract.command_contract import build_error_payload, normalize_mutating_payload
from kodawari.cli.contract.contract_first_generic_cmd import (
    resolve_task_plan_context,
    run_architecture_plan_command,
    run_init_command,
)
from kodawari.cli.contract.plans_markdown import load_optional_task_card_active, render_plans_markdown
from kodawari.cli.contract.planning_requirements import PlanningStrictnessError
from kodawari.cli.contract.contract_first_schema import (
    ContractFirstSchemaValidationError,
    validate_contract_first_payload,
    write_contract_first_artifact,
)
from kodawari.cli.io_atomic import CorruptArtifactError, atomic_write_text
from kodawari.cli.provenance import build_cli_provenance


def _emit(payload: dict[str, Any]) -> int:
    normalized = normalize_mutating_payload(payload)
    print(json.dumps(normalized, ensure_ascii=False, indent=2))
    return int(normalized.get("_rc", 0) or 0)


def _planning_dir(project_root: Path, feature: str, explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).resolve()
    return (project_root / "planning" / feature).resolve()


def _provenance(command: str, *, project_root: Path, planning_dir: Path) -> dict[str, Any]:
    return build_cli_provenance(
        command=command,
        project_root=project_root,
        planning_dir=planning_dir,
        module_file=Path(__file__),
    )


def _planning_error_extra_payload(error: PlanningStrictnessError) -> dict[str, Any]:
    payload = {"planning_requirements": dict(error.requirements)}
    optional_keys = ("input_intake_confidence", "input_confidence_issues", "validation_errors")
    for key in optional_keys:
        value = error.requirements.get(key)
        if value in (None, "", []):
            continue
        payload[key] = value
    return payload


def _emit_contract_error(
    *,
    command: str,
    project_root: Path,
    planning_dir: Path,
    feature: str,
    error: Exception,
    remediation: list[str],
) -> int:
    error_code = "contract_first_artifact_invalid"
    validation_errors: list[dict[str, str]] = []
    status = "FAIL"
    next_action = f"Fix the contract-first planning issue, then rerun `kodawari {command}`."
    extra_payload: dict[str, Any] = {}
    if isinstance(error, ArtifactSchemaVersionError):
        error_code = "artifact_schema_version_invalid"
    elif isinstance(error, ContractFirstSchemaValidationError):
        error_code = "artifact_schema_invalid"
        validation_errors = list(error.errors)
    elif isinstance(error, CorruptArtifactError):
        error_code = "artifact_corrupt"
    elif isinstance(error, PlanningStrictnessError):
        error_code = error.error_code
        status = "BLOCKED"
        next_action = "Run `kodawari architecture-plan` before rerunning task-plan."
        extra_payload.update(_planning_error_extra_payload(error))
    payload = build_error_payload(
        command=command,
        project_root=project_root,
        planning_dir=planning_dir,
        module_file=Path(__file__),
        error=str(error),
        error_code=error_code,
        remediation=remediation,
        next_action=next_action,
        extra={
            "_rc": 2,
            "status": status,
            "feature": feature,
            "planning_dir": str(planning_dir),
            **({"validation_errors": validation_errors} if validation_errors else {}),
            **extra_payload,
        },
    )
    return _emit(payload)


def _write_markdown(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, content)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _task_plan_result_payload(
    *,
    feature: str,
    planning_dir: Path,
    output_path: Path,
    plans_path: Path,
    markdown_path: Path | None,
    context: dict[str, Any],
    intake: dict[str, Any],
    errors: list[str],
    project_root: Path,
) -> dict[str, Any]:
    artifacts = {"TASK_GRAPH.json": str(output_path), "REPO_INVENTORY.json": str(context["repo_inventory_path"])}
    if context.get("architecture_plan_path") is not None:
        artifacts["ARCHITECTURE_PLAN.json"] = str(context["architecture_plan_path"])
    artifacts["Plans.md"] = str(plans_path)
    if markdown_path is not None:
        artifacts["TASK_GRAPH.md"] = str(markdown_path)
    return {
        "_rc": 0 if not errors else 2,
        "status": "PASS" if not errors else "FAIL",
        "entrypoint": "kodawari task-plan",
        "feature": feature,
        "planning_dir": str(planning_dir),
        "artifacts": artifacts,
        "planning_mode": context["planning_mode"],
        "planning_requirements": dict(context.get("planning_requirements") or {}),
        "input_intake_confidence": str(intake.get("confidence") or "high"),
        "input_confidence_issues": list(intake.get("confidence_issues") or []),
        "validation_errors": errors,
        "provenance": _provenance("task-plan", project_root=project_root, planning_dir=planning_dir),
    }


def _build_task_graph_artifact(
    *,
    args: argparse.Namespace,
    project_root: Path,
    feature: str,
    planning_dir: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[str], Path]:
    context = resolve_task_plan_context(
        project_root=project_root,
        planning_dir=planning_dir,
        intake_path=getattr(args, "intake", None),
        architecture_plan_path=getattr(args, "architecture_plan", None),
        repo_inventory_path=getattr(args, "repo_inventory", None),
        archetype=str(getattr(args, "archetype", "auto") or getattr(args, "project_profile", "auto") or "auto"),
        capabilities=list(getattr(args, "capability", []) or []),
        mode=str(getattr(args, "mode", "auto") or "auto"),
    )
    intake = dict(context["intake"])
    graph = build_task_graph(
        intake,
        project_root=project_root,
        project_profile=str(getattr(args, "project_profile", "auto")),
        repo_inventory=dict(context["repo_inventory"]),
        architecture_plan=dict(context["architecture_plan"] or {}),
        planning_mode=str(context["planning_mode"]),
    )
    errors = validate_task_graph(graph)
    if str(intake.get("confidence") or "high").lower() == "low":
        errors.extend(f"input PRD intake low confidence: {item}" for item in list(intake.get("confidence_issues") or []) or ["semantic confidence below threshold"])
    validate_contract_first_payload("task_graph", graph)
    output_path = Path(args.output).resolve() if getattr(args, "output", None) else (planning_dir / "TASK_GRAPH.json")
    write_contract_first_artifact(output_path, graph, schema_name="task_graph")
    return context, intake, graph, errors, output_path


def _cmd_task_plan(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root).resolve()
    feature = str(args.feature).strip()
    planning_dir = _planning_dir(project_root, feature, getattr(args, "planning_dir", None))
    try:
        context, intake, graph, errors, output_path = _build_task_graph_artifact(
            args=args,
            project_root=project_root,
            feature=feature,
            planning_dir=planning_dir,
        )
    except (
        FileNotFoundError,
        ArtifactSchemaVersionError,
        ContractFirstSchemaValidationError,
        CorruptArtifactError,
        PlanningStrictnessError,
        ValueError,
    ) as exc:
        return _emit_contract_error(
            command="task-plan",
            project_root=project_root,
            planning_dir=planning_dir,
            feature=feature,
            error=exc,
            remediation=["Generate PRD/architecture/repo-inventory artifacts before rerunning task-plan."],
        )
    markdown_path = output_path.with_suffix(".md") if bool(getattr(args, "emit_md", False)) else None
    plans_path = planning_dir / "Plans.md"
    task_card_active = load_optional_task_card_active(planning_dir / "TASK_CARD_ACTIVE.json")
    _write_markdown(
        plans_path,
        render_plans_markdown(
            graph,
            intake=intake,
            architecture_plan=dict(context.get("architecture_plan") or {}),
            task_card_active=task_card_active,
            generated_at=str(graph.get("generated_at") or ""),
            source_digest=_sha256_file(output_path),
        ),
    )
    if markdown_path is not None:
        _write_markdown(markdown_path, render_task_graph_markdown(graph))
    return _emit(
        _task_plan_result_payload(
            feature=feature,
            planning_dir=planning_dir,
            output_path=output_path,
            plans_path=plans_path,
            markdown_path=markdown_path,
            context=context,
            intake=intake,
            errors=errors,
            project_root=project_root,
        )
    )


def _register_prd_intake(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    prd_intake = sub.add_parser("prd-intake", help="Generate PRD_INTAKE contract artifact")
    prd_intake.add_argument("--project-root", default=".")
    prd_intake.add_argument("--feature", required=True)
    prd_intake.add_argument("--prd", required=True, help="Path to PRD text/markdown file")
    prd_intake.add_argument("--planning-dir")
    prd_intake.add_argument("--output", help="Optional PRD_INTAKE.json output path")
    prd_intake.add_argument("--emit-md", action="store_true", help="Also emit PRD_INTAKE markdown mirror")
    prd_intake.set_defaults(handler=legacy_contract_first_cmd._cmd_prd_intake)


def _register_architecture_plan(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    architecture = sub.add_parser("architecture-plan", help="Build ARCHITECTURE_PLAN and REPO_INVENTORY artifacts")
    architecture.add_argument("--project-root", default=".")
    architecture.add_argument("--feature", required=True)
    architecture.add_argument("--prd", help="Optional PRD text/markdown file path")
    architecture.add_argument("--intake", help="Optional PRD_INTAKE.json path override")
    architecture.add_argument("--planning-dir")
    architecture.add_argument("--output", help="Optional ARCHITECTURE_PLAN.json output path")
    architecture.add_argument("--emit-md", action="store_true", help="Also emit markdown mirrors")
    architecture.add_argument("--mode", default="auto", choices=["auto", "existing", "greenfield"])
    architecture.add_argument("--archetype", default="auto")
    architecture.add_argument("--capability", action="append", help="Repeatable capability id")
    architecture.set_defaults(handler=run_architecture_plan_command)


def _register_init(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    init_cmd = sub.add_parser("init", help="Scaffold a minimal archetype-compatible project skeleton")
    init_cmd.add_argument("--project-root", default=".")
    init_cmd.add_argument("--architecture-plan", help="Optional ARCHITECTURE_PLAN.json input path")
    init_cmd.add_argument("--archetype", default="")
    init_cmd.add_argument("--capability", action="append", help="Repeatable capability id")
    init_cmd.set_defaults(handler=run_init_command)


def _register_task_plan(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    task_plan = sub.add_parser("task-plan", help="Build TASK_GRAPH from contract-first planning truth")
    task_plan.add_argument("--project-root", default=".")
    task_plan.add_argument("--feature", required=True)
    task_plan.add_argument("--intake", help="Path to PRD_INTAKE.json")
    task_plan.add_argument("--architecture-plan", help="Optional ARCHITECTURE_PLAN.json path")
    task_plan.add_argument("--repo-inventory", help="Optional REPO_INVENTORY.json path")
    task_plan.add_argument("--planning-dir")
    task_plan.add_argument("--output", help="Optional TASK_GRAPH.json output path")
    task_plan.add_argument("--emit-md", action="store_true", help="Also emit TASK_GRAPH markdown mirror")
    task_plan.add_argument("--mode", default="auto", choices=["auto", "existing", "greenfield"])
    task_plan.add_argument("--archetype", default="auto")
    task_plan.add_argument("--capability", action="append", help="Repeatable capability id")
    task_plan.add_argument("--project-profile", default="auto", help="Compatibility profile hint (mapped onto archetype-aware planning)")
    task_plan.set_defaults(handler=_cmd_task_plan)


def _register_task_prepare(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    task_prepare = sub.add_parser("task-prepare", help="Build TASK_CARD for one task")
    task_prepare.add_argument("--project-root", default=".")
    task_prepare.add_argument("--feature", required=True)
    task_prepare.add_argument("--graph", required=True, help="Path to TASK_GRAPH.json")
    task_prepare.add_argument("--task", required=True, help="Task id, e.g. T1")
    task_prepare.add_argument("--planning-dir")
    task_prepare.add_argument("--output", help="Optional TASK_CARD output path")
    task_prepare.add_argument("--emit-md", action="store_true", help="Also emit TASK_CARD markdown mirror")
    task_prepare.set_defaults(handler=legacy_contract_first_cmd._cmd_task_prepare)


def _register_task_run(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    task_run = sub.add_parser("task-run", help="Execute one task card with optional strict scope")
    task_run.add_argument("--project-root", default=".")
    task_run.add_argument("--feature", required=True)
    task_run.add_argument("--card", required=True, help="Path to TASK_CARD json")
    task_run.add_argument("--planning-dir")
    task_run.add_argument("--requirements-file")
    task_run.add_argument("--verify-cmd", default="pytest -q")
    task_run.add_argument("--max-cycles", type=int, default=8)
    task_run.add_argument("--token-budget", type=int, default=300000)
    task_run.add_argument("--contract-mode", default="strict", choices=["off", "warn", "strict"])
    task_run.add_argument("--phase-mode", default="implement", choices=["analyze", "implement"])
    task_run.add_argument("--strict-scope", action="store_true")
    task_run.add_argument("--executor-backend", choices=execution_backend_choices())
    task_run.add_argument("--executor-command", help="Command used when --executor-backend=external_cli")
    task_run.add_argument("--self-review-backend", choices=self_review_backend_choices())
    task_run.add_argument("--self-review-command", help="Command used when --self-review-backend=external_cli")
    task_run.add_argument("--real-peer-review", action="store_true")
    task_run.add_argument("--require-real-peer-review", action="store_true")
    task_run.add_argument("--real-opus-review", action="store_true", help="Legacy alias for --real-peer-review")
    task_run.add_argument("--require-real-opus-review", action="store_true", help="Legacy alias for --require-real-peer-review")
    task_run.add_argument(
        "--opus-reviewer-backend",
        default="",
        choices=["", "auto", "api", "cli", "mcp", "codex"],
        help="Deprecated alias for --reviewer-backend; will be removed after 2026-08-01.",
    )
    task_run.add_argument(
        "--reviewer-backend",
        default="",
        choices=["", "auto", "api", "cli", "mcp", "codex"],
        help="Reviewer backend: api (HTTP+key), cli (Claude CLI), mcp (Claude CLI+MCP), codex (Codex CLI).",
    )
    task_run.add_argument("--executor-model", default="", help="Model override for executor.")
    task_run.add_argument("--reviewer-model", default="", help="Model override for reviewer (api backend).")
    task_run.add_argument(
        "--reviewer-api-format",
        default="",
        choices=["", "auto", "anthropic", "openai"],
        help="API format for reviewer (api backend). env: WORKFLOW_REVIEWER_API_FORMAT.",
    )
    task_run.add_argument("--reviewer-base-url", default="", help="Base URL for reviewer (api backend). env: WORKFLOW_REVIEWER_BASE_URL.")
    task_run.add_argument("--peer-review-max-tokens", type=int, default=4096)
    task_run.add_argument("--rollback-on-failure", action="store_true", help="Rollback implement changes before verify/gate retries.")
    task_run.add_argument("--max-verify-retries", type=int, default=2, help="Maximum verify retry attempts when rollback-on-failure is enabled.")
    task_run.add_argument("--no-enable-peer-review", action="store_true")
    task_run.set_defaults(handler=legacy_contract_first_cmd._cmd_task_run)


def _register_compliance(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    compliance = sub.add_parser("compliance-check", help="Generate COMPLIANCE_REPORT from planning artifacts")
    compliance.add_argument("--project-root", default=".")
    compliance.add_argument("--feature", required=True)
    compliance.add_argument("--planning-dir")
    compliance.add_argument("--changed-file", action="append", help="Override changed files (repeatable)")
    compliance.add_argument("--output", help="Optional COMPLIANCE_REPORT output path")
    compliance.set_defaults(handler=legacy_contract_first_cmd._cmd_compliance_check)


def register_contract_first_commands(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    _register_prd_intake(sub)
    _register_architecture_plan(sub)
    _register_init(sub)
    _register_task_plan(sub)
    _register_task_prepare(sub)
    _register_task_run(sub)
    _register_compliance(sub)


