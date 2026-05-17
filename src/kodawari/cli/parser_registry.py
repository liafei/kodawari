"""Parser registration for kodawari CLI commands."""

from __future__ import annotations

import argparse

from kodawari.cli.delivery.approve_cmd import _cmd_approve
from kodawari.cli.command_tiers import (
    OPERATOR_COMMANDS,
    USER_COMMANDS,
    apply_command_tiers,
    command_tier,
)
from kodawari.cli.evidence.artifact_migration_cmd import run_migrate_artifacts_command
from kodawari.cli.evidence.self_repair_cmd import run_self_repair_command
from kodawari.cli.evidence.self_repair_execute_cmd import run_self_repair_execute_command
from kodawari.cli.evidence.self_repair_learn_cmd import run_self_repair_learn_command
from kodawari.cli.runtime.autopilot_cmd import run_autopilot_command
from kodawari.cli.contract.contract_first_entrypoints import register_contract_first_commands
from kodawari.cli.delivery.delivery_cmds import (
    _cmd_qa,
    _cmd_ship_readiness,
    _cmd_verify,
)
from kodawari.cli.gate.gate_cmd import _cmd_gate
from kodawari.cli.gate.incident_ingest_cmd import run_incident_ingest_command
from kodawari.cli.gate.lane_history_fetch_cmd import run_lane_history_fetch_command
from kodawari.cli.gate.lane_triage_cmd import run_lane_triage_command
from kodawari.cli.gate.lane_trend_cmd import run_lane_trend_command
from kodawari.cli.gate.lane_trend_report_cmd import run_lane_trend_report_command
from kodawari.cli.core.legacy_cmds import _cmd_legacy_compact, _cmd_legacy_runtime
from kodawari.cli.core.main_support import _add_project_root_argument
from kodawari.cli.core.release_gate_cmd import run_canary_gate_command, run_replay_gate_command
from kodawari.cli.evidence.execution_evidence_cmd import run_execution_evidence_command
from kodawari.cli.evidence.review_evidence_cmd import run_review_evidence_command
from kodawari.cli.status.stability_report_cmd import run_stability_report_command
from kodawari.cli.status.status_cmd import _cmd_status
from kodawari.cli.gate.telemetry_field_eval_cmd import (
    run_eval_report_command,
    run_field_report_command,
    run_field_report_update_command,
    run_telemetry_command,
)
from kodawari.cli.runtime.workflow_shell_cmd import (
    run_plan_command,
    run_release_command,
    run_review_command,
    run_setup_command,
    run_work_all_facade_command,
    run_work_command,
)
from kodawari.cli.runtime.doctor_cmd import (
    run_doctor_models_command,
    run_doctor_preflight_command,
)
from kodawari.cli.runtime.init_wizard_cmd import PRESETS, run_init_wizard_command
from kodawari.cli.runtime.decide_cmd import run_decide_command
from kodawari.cli.runtime.gate_config_cmd import run_gate_config_command
from kodawari.cli.parser_shared_args import add_model_and_reviewer_arguments, add_work_runtime_arguments
from kodawari.autopilot.execution.execution_backend import execution_backend_choices, self_review_backend_choices
from kodawari.gate import list_profiles


def _register_autopilot_command(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser("autopilot", help="Run the kodawari autopilot loop")
    _add_project_root_argument(parser)
    parser.add_argument("--feature", required=True)
    parser.add_argument("--task", default="", help="Task direction for model-driven planning")
    parser.add_argument("--prd", help="Path to PRD text/markdown file (primary autopilot input)")
    parser.add_argument("--requirements-file", help="Compatibility alias for --prd")
    parser.add_argument("--profile", default="profiles/generic.yaml")
    parser.add_argument("--verify-cmd", default="pytest -q")
    parser.add_argument("--max-cycles", type=int, default=8)
    parser.add_argument("--parallel-workers", type=int, default=2, help="Worker slots for parallel coordinator assignment")
    parser.add_argument("--token-budget", type=int, default=300000)
    parser.add_argument("--gate-profile", default="advisory", choices=list_profiles())
    parser.add_argument("--simulate", action="store_true", help="Compatibility flag; canonical runtime remains contract-first")
    parser.add_argument(
        "--planner-route",
        default="auto",
        choices=["auto", "model", "generic"],
        help=(
            "Planning route for the contract-first bootstrap inside autopilot: "
            "auto=infer from --task/PLANNING_CONVERSATION.json (legacy default), "
            "model=force PRD-driven model planner, generic=force archetype-based generic planner. "
            "Match the value used with `kodawari plan` to avoid silently switching planners between phases."
        ),
    )
    parser.add_argument(
        "--replan",
        action="store_true",
        help="Regenerate planning artifacts instead of consuming an existing TASK_GRAPH.json",
    )
    parser.add_argument(
        "--executor-backend",
        choices=execution_backend_choices(),
        default="",
        help="Execution backend contract for task-run/autopilot implementation rounds",
    )
    parser.add_argument(
        "--executor-command",
        help="External command used when --executor-backend=external_cli; receives .execution_request.json path",
    )
    parser.add_argument(
        "--self-review-backend",
        choices=self_review_backend_choices(),
        default="",
        help="Self-review backend contract used by autopilot review rounds",
    )
    parser.add_argument(
        "--self-review-command",
        help="External command used when --self-review-backend=external_cli",
    )
    parser.add_argument("--rollback-on-failure", action="store_true", help="Rollback implement changes before verify/gate retries.")
    parser.add_argument("--max-verify-retries", type=int, default=2, help="Maximum verify retry attempts when rollback-on-failure is enabled.")
    parser.add_argument(
        "--max-wall-clock-seconds",
        type=int,
        default=0,
        help=(
            "Whole-loop wall-clock budget (seconds). When exceeded, autopilot writes "
            "ABORT_REPORT.json into planning_dir and exits 124. 0 (default) disables. "
            "This is independent of WORKFLOW_EXECUTOR_TIMEOUT_SECONDS, which is a "
            "per-round budget — wall-clock counts elapsed time across all rounds and "
            "cycles."
        ),
    )
    parser.add_argument(
        "--tier",
        default="auto",
        choices=["auto", "lite", "standard", "heavy"],
        help=(
            "Workflow tier for lightweight pipeline. "
            "'auto' (default) runs the complexity detector. "
            "'lite' / 'standard' / 'heavy' force the corresponding lane. "
            "The resolved lane is applied to planning and runtime behavior."
        ),
    )
    # Tri-state sentinel: default=None means "user did not pass the flag";
    # only explicit --task-cycle / --no-task-cycle wins over tier policy.
    parser.add_argument(
        "--task-cycle",
        dest="task_cycle",
        action="store_true",
        default=None,
        help="Force-enable task_cycle, overriding the tier's policy default.",
    )
    parser.add_argument(
        "--no-task-cycle",
        dest="task_cycle",
        action="store_false",
        help="Force-disable task_cycle, overriding the tier's policy default.",
    )
    add_model_and_reviewer_arguments(parser)
    parser.set_defaults(handler=run_autopilot_command)


def _register_status_command(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser("status", help="Show merged workflow status from planning artifacts")
    _add_project_root_argument(parser)
    parser.add_argument("--feature")
    parser.add_argument("--planning-dir")
    parser.set_defaults(handler=_cmd_status)


def _register_serve_command(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    from kodawari.cli.serve_cmd import DEFAULT_HOST, DEFAULT_PORT, run_serve_command

    parser = sub.add_parser(
        "serve",
        help="Run the kodawari web UI bridge (FastAPI) on a local port",
    )
    parser.add_argument(
        "--root",
        help="Default project root used when client requests omit ?root=. Optional.",
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"Bind host (default: {DEFAULT_HOST})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Bind port (default: {DEFAULT_PORT})")
    parser.add_argument(
        "--log-level",
        default="info",
        choices=["critical", "error", "warning", "info", "debug", "trace"],
        help="uvicorn log level (default: info)",
    )
    parser.set_defaults(handler=run_serve_command)


def _register_doctor_command(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser("doctor", help="Diagnose kodawari runtime configuration")
    nested = parser.add_subparsers(dest="doctor_command", required=True)
    models = nested.add_parser("models", help="Diagnose models.v2 role/transport configuration")
    _add_project_root_argument(models)
    models.add_argument("--offline", action="store_true", help="Validate configuration without network probes")
    models.add_argument("--probe-tools", action="store_true", help="Probe OpenAI-compatible tool calling for active HTTP transports")
    models.add_argument(
        "--smoke",
        nargs="?",
        const="local",
        choices=["local", "real", "patch-local", "patch-real", "planner"],
        default="",
        help="Run an executor or planner smoke test; default value uses a local fake endpoint, --smoke=real uses the configured endpoint; patch-* forces exact_str_replace_v1",
    )
    models.add_argument("--no-cache", action="store_true", help="Bypass doctor probe cache")
    models.add_argument("--cache-ttl-seconds", type=int, help="Override probe cache TTL seconds")
    models.add_argument("--output", help="Optional JSON output path")
    models.set_defaults(handler=run_doctor_models_command)

    preflight = nested.add_parser(
        "preflight",
        help="Static configuration checks before a first autopilot run (no network calls)",
    )
    _add_project_root_argument(preflight)
    preflight.add_argument("--feature", help="Feature slug (used to verify planning_dir writability)")
    preflight.add_argument("--prd", help="Optional PRD path to validate (existence + non-trivial size)")
    preflight.add_argument(
        "--require-real-peer-review",
        action="store_true",
        help="Verify WORKFLOW_REVIEWER_* env vars are set (no network calls)",
    )
    preflight.add_argument("--output", help="Optional JSON output path")
    preflight.set_defaults(handler=run_doctor_preflight_command)


def _register_init_wizard_command(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser(
        "init-wizard",
        help="Interactive first-run config bootstrap (generates models.yaml + .env.example)",
    )
    _add_project_root_argument(parser)
    parser.add_argument(
        "--preset",
        choices=list(PRESETS),
        default="",
        help="Skip the preset prompt and use this preset directly",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Non-interactive mode: use preset defaults without prompts (requires --preset)",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing .claude/workflow/models.yaml and .env.example without backup",
    )
    parser.add_argument("--output", help="Optional JSON output path for the wizard result")
    parser.set_defaults(handler=run_init_wizard_command)


def _register_gate_command(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser("gate", help="Run static quality gate checks against repo or planning scope")
    _add_project_root_argument(parser)
    parser.add_argument("--feature")
    parser.add_argument("--planning-dir")
    parser.add_argument("--path", action="append", help="Explicit path(s) to scan; overrides --scope")
    parser.add_argument(
        "--scope",
        default="auto",
        choices=["auto", "changed", "full"],
        help=(
            "Target selection when --path is not given. "
            "'auto' (default): use changed_files from .execution_result.json if present, "
            "else fall back to full project. "
            "'changed': require .execution_result.json; fail if absent. "
            "'full': scan the entire project (pre-release audit)."
        ),
    )
    parser.add_argument("--profile", default="advisory", choices=list_profiles())
    parser.add_argument("--ratchet", action="store_true", help="Compare current code health against a baseline snapshot")
    parser.add_argument("--baseline", help="Path to code health baseline json used by --ratchet")
    parser.add_argument("--output", help="Optional JSON output path")
    parser.add_argument("--fail-on-block", action="store_true", help="Return non-zero when gate total_status=BLOCKED")
    parser.set_defaults(handler=_cmd_gate)


def _register_decide_command(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser("decide", help="Handle escalation decisions (planning / executor / gate)")
    _add_project_root_argument(parser)
    parser.add_argument("--planning-dir", required=True, help="Planning directory containing decision_request.json files")
    parser.add_argument("--abort", action="store_true", help="Abort any pending decision request(s)")
    parser.add_argument("--status", action="store_true", help="List pending decision requests and their counts")
    parser.set_defaults(handler=run_decide_command)


def _register_gate_config_command(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser("gate-config", help="Configure code gate thresholds")
    _add_project_root_argument(parser)
    nested = parser.add_subparsers(dest="gate_config_command", required=True)

    # gate-config show
    show_parser = nested.add_parser("show", help="Show current gate configuration")
    show_parser.add_argument("--planning-dir", required=True, help="Planning directory")
    show_parser.set_defaults(handler=run_gate_config_command)

    # gate-config set
    set_parser = nested.add_parser("set", help="Set a specific threshold")
    set_parser.add_argument("--planning-dir", required=True, help="Planning directory")
    set_parser.add_argument("--key-value", required=True, help="KEY=VALUE pair (e.g., complexity_block=12)")
    set_parser.set_defaults(handler=run_gate_config_command)

    # gate-config apply-profile
    profile_parser = nested.add_parser("apply-profile", help="Apply a named configuration profile")
    profile_parser.add_argument("--planning-dir", required=True, help="Planning directory")
    profile_parser.add_argument("--profile-name", required=True, choices=list_profiles(), help="Profile name")
    profile_parser.set_defaults(handler=run_gate_config_command)


def _register_stability_report_command(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser("stability-report", help="Aggregate workflow runs into stability and recovery metrics")
    _add_project_root_argument(parser)
    parser.add_argument("--run-id", action="append", help="Planning run id to include (repeatable)")
    parser.add_argument("--planning-dir", action="append", help="Explicit planning dir to include (repeatable)")
    parser.add_argument("--all-runs", action="store_true", help="Scan planning/* for run summaries")
    parser.add_argument("--status", action="append", help="Filter to run summaries with matching status (repeatable)")
    parser.add_argument("--updated-since", help="Filter to runs updated at/after the given ISO date or datetime")
    parser.add_argument("--updated-until", help="Filter to runs updated at/before the given ISO date or datetime")
    parser.add_argument("--max-history-days", type=int, help="Drop runs older than N days based on summary timestamp")
    parser.add_argument("--task-max-cycles", type=int, help="Optional planning threshold used only in markdown/report summaries")
    parser.add_argument("--task-auto-runs", type=int, help="Optional planning threshold used only in markdown/report summaries")
    parser.add_argument("--timeout-per-round", type=int, help="Optional planning threshold used only in markdown/report summaries")
    parser.add_argument("--token-budget-target", type=int, help="Optional token budget target used only in markdown/report summaries")
    parser.add_argument("--format", default="json", choices=["json", "markdown"])
    parser.add_argument("--output", help="Optional output path")
    parser.add_argument("--fail-on-block", action="store_true", help="Return non-zero when aggregate status is BLOCKED")
    parser.set_defaults(handler=run_stability_report_command)


def _register_lane_trend_command(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser("lane-trend", help="Aggregate lane triage artifacts into weekly standing-proof trends")
    _add_project_root_argument(parser)
    parser.add_argument("--artifacts-root", help="Recursive root containing lane_triage_*.json artifacts")
    parser.add_argument("--lane", action="append", help="Optional lane filter (repeatable)")
    parser.add_argument("--max-history-days", type=int, default=7, help="Keep artifacts captured within the most recent N days")
    parser.add_argument("--required-pass-streak", type=int, default=3, help="Required consecutive stable passes to mark a lane as stable")
    parser.add_argument("--json-output", help="Optional JSON output path")
    parser.add_argument("--markdown-output", help="Optional markdown output path")
    parser.add_argument("--fail-on-block", action="store_true", help="Return non-zero when any selected lane is not yet stable")
    parser.set_defaults(handler=run_lane_trend_command)


def _register_lane_history_fetch_command(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser("lane-history-fetch", help="Download recent lane artifacts from GitHub Actions for standing-proof trend runs")
    _add_project_root_argument(parser)
    parser.add_argument("--repo", help="GitHub repo in owner/name form; defaults to GITHUB_REPOSITORY")
    parser.add_argument("--token-env", default="GITHUB_TOKEN", help="Environment variable containing the GitHub token")
    parser.add_argument("--artifact-prefix", action="append", help="Artifact name prefix to include (repeatable)")
    parser.add_argument("--output-dir", help="Directory where downloaded artifacts are extracted")
    parser.add_argument("--json-output", help="Optional JSON manifest output path")
    parser.add_argument("--max-history-days", type=int, default=7, help="Only download artifacts created within the most recent N days")
    parser.add_argument("--per-page", type=int, default=100, help="GitHub artifact page size")
    parser.add_argument("--max-pages", type=int, default=10, help="Maximum GitHub artifact pages to scan")
    parser.add_argument("--fail-on-empty", action="store_true", help="Return non-zero when no matching artifacts were downloaded")
    parser.set_defaults(handler=run_lane_history_fetch_command)


def _register_lane_triage_command(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser("lane-triage", help="Generate lane triage guidance from lane stability summaries")
    _add_project_root_argument(parser)
    parser.add_argument("--lane", default="always-on", help="Lane name used for default summary/artifact resolution")
    parser.add_argument("--summary", help="Lane stability summary JSON path")
    parser.add_argument("--output", help="Optional JSON output path")
    parser.add_argument("--markdown-output", help="Optional markdown output path")
    parser.add_argument("--fail-on-block", action="store_true", help="Return non-zero when triage status is not PASS")
    parser.set_defaults(handler=run_lane_triage_command)


def _register_review_command(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser("review", help="Run review bundle + verify surfaces facade")
    _add_project_root_argument(parser)
    parser.add_argument("--feature")
    parser.add_argument("--planning-dir")
    parser.add_argument("--base-branch", default="main")
    parser.add_argument("--changed-file", action="append", help="Override changed files for scoped review targeting")
    parser.add_argument("--scope-allow", action="append", help="Additional allowed path prefixes for scoped review")
    parser.add_argument("--command-file", help="Optional verify command file override for the facade verify step")
    parser.add_argument("--command", help="Optional verify command override for the facade verify step")
    parser.add_argument("--output", help="Optional JSON output path")
    parser.add_argument("--fail-on-block", action="store_true", help="Return non-zero when review result is BLOCKED")
    parser.set_defaults(handler=run_review_command)


def _register_setup_command(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser("setup", help="Step 1/5: bootstrap inventory and setup checks for canonical workflow")
    _add_project_root_argument(parser)
    parser.add_argument("--feature", default="bootstrap")
    parser.add_argument("--planning-dir")
    parser.add_argument("--prd", help="Optional PRD text/markdown path used by architecture-plan")
    parser.add_argument("--requirements-file", help="Compatibility alias for --prd")
    parser.add_argument("--intake", help="Optional PRD_INTAKE.json path override")
    parser.add_argument("--output", help="Optional ARCHITECTURE_PLAN.json output path")
    parser.add_argument("--emit-md", action="store_true", help="Also emit architecture markdown mirrors")
    parser.add_argument("--mode", default="existing", choices=["existing", "greenfield", "auto"])
    parser.add_argument("--archetype", default="auto")
    parser.add_argument("--capability", action="append", help="Repeatable capability id")
    parser.add_argument("--run-init", action="store_true", help="Also run init using the generated ARCHITECTURE_PLAN")
    parser.set_defaults(handler=run_setup_command)


def _register_plan_command(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser("plan", help="Step 2/5: materialize contract-first planning truth and Plans.md")
    _add_project_root_argument(parser)
    parser.add_argument("--feature", required=True)
    parser.add_argument("--planning-dir")
    parser.add_argument("--task", default="", help="Task direction for model-driven planning")
    parser.add_argument("--prd", help="Path to PRD text/markdown file")
    parser.add_argument("--requirements-file", help="Compatibility alias for --prd")
    parser.add_argument(
        "--planner-route",
        default="auto",
        choices=["auto", "model", "generic"],
        help=(
            "Planning route: auto=infer from --task/PLANNING_CONVERSATION.json, "
            "model=force PRD-driven model planner, generic=force archetype-based generic planner"
        ),
    )
    parser.add_argument(
        "--replan",
        action="store_true",
        help="Regenerate planning artifacts instead of consuming an existing TASK_GRAPH.json",
    )
    parser.set_defaults(handler=run_plan_command)


def _register_work_command(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser("work", help="Step 3/5: run canonical execution loop or work-all entrypoint")
    add_work_runtime_arguments(parser)
    parser.set_defaults(handler=run_work_command)
    nested = parser.add_subparsers(dest="work_command", required=False)
    all_parser = nested.add_parser("all", help="Run the full plan -> work -> release pipeline")
    add_work_runtime_arguments(all_parser)
    all_parser.set_defaults(handler=run_work_all_facade_command)


def _register_work_all_alias_command(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser("work-all", help="Alias for `kodawari work all`")
    add_work_runtime_arguments(parser)
    parser.set_defaults(handler=run_work_all_facade_command)


def _register_release_command(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser("release", help="Step 5/5: run qa + gate + ship-readiness facade")
    _add_project_root_argument(parser)
    parser.add_argument("--feature")
    parser.add_argument("--planning-dir")
    parser.add_argument("--eval-report-path", help="Optional eval report JSON path override")
    parser.add_argument("--auto-eval", action="store_true", help="Auto-generate eval report when missing")
    parser.add_argument("--risk-profile", choices=["low", "medium", "high"], default="medium")
    parser.add_argument("--gate-profile", default="strict", choices=list_profiles())
    parser.add_argument("--gate-path", action="append", help="Optional path(s) to scan during release gate")
    parser.add_argument("--output", help="Optional JSON output path")
    parser.add_argument("--fail-on-block", action="store_true", help="Return non-zero when release result is BLOCKED")
    parser.set_defaults(handler=run_release_command)


def _register_claude_shell_aliases(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    setup_parser = sub.add_parser("wf-setup", help="Facade alias for canonical `kodawari setup` (recommended shell entry)")
    _add_project_root_argument(setup_parser)
    setup_parser.add_argument("--feature", default="bootstrap")
    setup_parser.add_argument("--planning-dir")
    setup_parser.add_argument("--prd", help="Optional PRD text/markdown path used by architecture-plan")
    setup_parser.add_argument("--requirements-file", help="Compatibility alias for --prd")
    setup_parser.add_argument("--intake", help="Optional PRD_INTAKE.json path override")
    setup_parser.add_argument("--output", help="Optional ARCHITECTURE_PLAN.json output path")
    setup_parser.add_argument("--emit-md", action="store_true", help="Also emit architecture markdown mirrors")
    setup_parser.add_argument("--mode", default="existing", choices=["existing", "greenfield", "auto"])
    setup_parser.add_argument("--archetype", default="auto")
    setup_parser.add_argument("--capability", action="append", help="Repeatable capability id")
    setup_parser.add_argument("--run-init", action="store_true", help="Also run init using the generated ARCHITECTURE_PLAN")
    setup_parser.set_defaults(handler=run_setup_command)

    plan_parser = sub.add_parser("wf-plan", help="Facade alias for canonical `kodawari plan`")
    _add_project_root_argument(plan_parser)
    plan_parser.add_argument("--feature", required=True)
    plan_parser.add_argument("--planning-dir")
    plan_parser.add_argument("--task", default="", help="Task direction for model-driven planning")
    plan_parser.add_argument("--prd", help="Path to PRD text/markdown file")
    plan_parser.add_argument("--requirements-file", help="Compatibility alias for --prd")
    plan_parser.add_argument(
        "--planner-route",
        default="auto",
        choices=["auto", "model", "generic"],
        help="Planning route: auto|model|generic",
    )
    plan_parser.add_argument(
        "--replan",
        action="store_true",
        help="Regenerate planning artifacts instead of consuming an existing TASK_GRAPH.json",
    )
    plan_parser.set_defaults(handler=run_plan_command)

    work_parser = sub.add_parser("wf-work", help="Facade alias for canonical `kodawari work`")
    add_work_runtime_arguments(work_parser)
    work_parser.set_defaults(handler=run_work_command)

    work_all_parser = sub.add_parser("wf-work-all", help="Facade alias for canonical `kodawari work all`")
    add_work_runtime_arguments(work_all_parser)
    work_all_parser.set_defaults(handler=run_work_all_facade_command)

    review_parser = sub.add_parser("wf-review", help="Facade alias for canonical `kodawari review`")
    _add_project_root_argument(review_parser)
    review_parser.add_argument("--feature")
    review_parser.add_argument("--planning-dir")
    review_parser.add_argument("--base-branch", default="main")
    review_parser.add_argument("--changed-file", action="append", help="Override changed files for scoped review targeting")
    review_parser.add_argument("--scope-allow", action="append", help="Additional allowed path prefixes for scoped review")
    review_parser.add_argument("--command-file", help="Optional verify command file override for the facade verify step")
    review_parser.add_argument("--command", help="Optional verify command override for the facade verify step")
    review_parser.add_argument("--output", help="Optional JSON output path")
    review_parser.add_argument("--fail-on-block", action="store_true", help="Return non-zero when review result is BLOCKED")
    review_parser.set_defaults(handler=run_review_command)

    release_parser = sub.add_parser("wf-release", help="Facade alias for canonical `kodawari release`")
    _add_project_root_argument(release_parser)
    release_parser.add_argument("--feature")
    release_parser.add_argument("--planning-dir")
    release_parser.add_argument("--eval-report-path", help="Optional eval report JSON path override")
    release_parser.add_argument("--auto-eval", action="store_true", help="Auto-generate eval report when missing")
    release_parser.add_argument("--risk-profile", choices=["low", "medium", "high"], default="medium")
    release_parser.add_argument("--gate-profile", default="strict", choices=list_profiles())
    release_parser.add_argument("--gate-path", action="append", help="Optional path(s) to scan during release gate")
    release_parser.add_argument("--output", help="Optional JSON output path")
    release_parser.add_argument("--fail-on-block", action="store_true", help="Return non-zero when release result is BLOCKED")
    release_parser.set_defaults(handler=run_release_command)

    status_parser = sub.add_parser("wf-status", help="Facade alias for canonical `kodawari status`")
    _add_project_root_argument(status_parser)
    status_parser.add_argument("--feature")
    status_parser.add_argument("--planning-dir")
    status_parser.set_defaults(handler=_cmd_status)


def _register_lane_trend_report_command(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser("lane-trend-report", help="Aggregate lane stability artifacts into trend reports")
    _add_project_root_argument(parser)
    parser.add_argument("--artifacts-dir", action="append", help="Directory containing lane_stability_*.json artifacts (repeatable)")
    parser.add_argument("--summary-path", action="append", help="Explicit lane stability summary JSON path (repeatable)")
    parser.add_argument("--lane", action="append", help="Optional lane filter (repeatable)")
    parser.add_argument("--updated-since", help="Filter to records captured at/after the given ISO date or datetime")
    parser.add_argument("--updated-until", help="Filter to records captured at/before the given ISO date or datetime")
    parser.add_argument("--max-history-days", type=int, default=7, help="Keep records captured within the most recent N days")
    parser.add_argument("--json-output", help="Optional JSON output path")
    parser.add_argument("--output", help="Optional markdown output path")
    parser.add_argument("--fail-on-block", action="store_true", help="Return non-zero when any selected lane is blocked")
    parser.set_defaults(handler=run_lane_trend_report_command)


def _register_verify_command(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser("verify", help="Materialize canonical verify report artifact from current workspace state")
    _add_project_root_argument(parser)
    parser.add_argument("--feature")
    parser.add_argument("--planning-dir")
    parser.add_argument("--base-branch", default="main")
    parser.add_argument("--changed-file", action="append", help="Override changed files for scoped verify targeting")
    parser.add_argument("--command-file", help="Preferred verify command script path (repo-local, repeatable behavior on Windows)")
    parser.add_argument("--command", help="Optional explicit verify command override")
    parser.add_argument("--output", help="Optional JSON output path")
    parser.add_argument("--fail-on-block", action="store_true", help="Return non-zero when verify result is BLOCKED")
    parser.set_defaults(handler=_cmd_verify)


def _register_review_evidence_command(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser("review-evidence", help="Write canonical dual-review evidence artifact from explicit input JSON")
    _add_project_root_argument(parser)
    parser.add_argument("--feature")
    parser.add_argument("--planning-dir")
    parser.add_argument("--input", required=True, help="Canonical review-evidence input JSON path")
    parser.set_defaults(handler=run_review_evidence_command)


def _register_execution_evidence_command(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser("execution-evidence", help="Write canonical manual execution artifact from explicit CLI evidence")
    _add_project_root_argument(parser)
    parser.add_argument("--feature")
    parser.add_argument("--planning-dir")
    parser.add_argument("--backend", required=True, choices=["manual"])
    parser.add_argument("--changed-file", action="append", required=True, help="Repeatable changed file path for the canonical execution artifact")
    parser.add_argument("--status", choices=["PASS", "BLOCKED", "FAIL"], default="PASS")
    parser.add_argument("--summary", help="Optional short execution summary")
    parser.add_argument("--stdout-file", help="Optional path to a text file whose content is copied into stdout_excerpt")
    parser.add_argument("--stderr-file", help="Optional path to a text file whose content is copied into stderr_excerpt")
    parser.add_argument("--returncode", type=int, help="Optional manual return code to persist in the canonical artifact")
    parser.add_argument("--artifact", action="append", help="Repeatable artifact path recorded in .execution_result.json")
    parser.set_defaults(handler=run_execution_evidence_command)


def _register_qa_command(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser("qa", help="Generate QA report artifact from planning runtime evidence")
    _add_project_root_argument(parser)
    parser.add_argument("--feature")
    parser.add_argument("--planning-dir")
    parser.add_argument("--output", help="Optional JSON output path")
    parser.add_argument("--fail-on-block", action="store_true", help="Return non-zero when QA result is BLOCKED")
    parser.set_defaults(handler=_cmd_qa)


def _register_ship_readiness_command(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser("ship-readiness", help="Generate ship readiness checklist and RELEASE artifact")
    _add_project_root_argument(parser)
    parser.add_argument("--feature")
    parser.add_argument("--planning-dir")
    parser.add_argument("--eval-report-path", help="Optional eval report JSON path override")
    parser.add_argument("--auto-eval", action="store_true", help="Auto-generate eval report when missing")
    parser.add_argument("--risk-profile", choices=["low", "medium", "high"], default="medium")
    parser.add_argument("--output", help="Optional JSON output path")
    parser.add_argument("--fail-on-block", action="store_true", help="Return non-zero when readiness is BLOCKED")
    parser.set_defaults(handler=_cmd_ship_readiness)


def _register_telemetry_command(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser("telemetry", help="Materialize telemetry snapshot from planning runtime artifacts")
    _add_project_root_argument(parser)
    parser.add_argument("--feature")
    parser.add_argument("--planning-dir")
    parser.add_argument("--append-history", dest="append_history", action="store_true", default=True)
    parser.add_argument("--no-append-history", dest="append_history", action="store_false")
    parser.add_argument("--max-history-days", type=int, help="Optional retention window for telemetry history reads")
    parser.add_argument("--output", help="Optional JSON output path")
    parser.set_defaults(handler=run_telemetry_command)


def _register_field_report_command(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser("field-report", help="Record a field incident report and materialize FIELD_REPORT artifacts")
    _add_project_root_argument(parser)
    parser.add_argument("--feature")
    parser.add_argument("--planning-dir")
    parser.add_argument("--report-id", help="Optional explicit report id; duplicates are blocked")
    parser.add_argument("--severity", choices=["low", "medium", "high", "critical"], default="medium")
    parser.add_argument("--title", required=True)
    parser.add_argument("--summary", required=True)
    parser.add_argument("--component", default="")
    parser.add_argument("--impact", default="")
    parser.add_argument("--owner", default="")
    parser.add_argument("--status", dest="report_status", default="open")
    parser.add_argument("--tag", action="append", help="Repeatable report tags")
    parser.add_argument("--evidence", action="append", help="Repeatable evidence file paths")
    parser.add_argument("--output", help="Optional JSON output path")
    parser.set_defaults(handler=run_field_report_command)


def _register_field_report_update_command(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser("field-report-update", help="Update field report status with state-machine validation")
    _add_project_root_argument(parser)
    parser.add_argument("--feature")
    parser.add_argument("--planning-dir")
    parser.add_argument("--report-id", required=True)
    parser.add_argument("--status", required=True, choices=["open", "in_progress", "resolved"])
    parser.add_argument("--allow-reopen", action="store_true", help="Allow resolved -> open/in_progress transitions")
    parser.add_argument("--output", help="Optional JSON output path")
    parser.set_defaults(handler=run_field_report_update_command)


def _register_eval_report_command(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser("eval-report", help="Aggregate telemetry+field reports into an eval decision")
    _add_project_root_argument(parser)
    parser.add_argument("--run-id", action="append", help="Run id under planning/ (repeatable)")
    parser.add_argument("--planning-dir", action="append", help="Explicit planning dir (repeatable)")
    parser.add_argument("--all-runs", action="store_true", help="Scan planning/* for telemetry-evaluable runs")
    parser.add_argument("--max-history-days", type=int, help="Filter telemetry snapshots to recent N days")
    parser.add_argument("--min-pass-rate", type=float, default=0.8)
    parser.add_argument("--max-blocked-rate", type=float, default=0.2)
    parser.add_argument("--max-critical-field-reports", type=int, default=0)
    parser.add_argument(
        "--emit-input-lock",
        nargs="?",
        const="",
        help="Emit eval input lock JSON (optional path, default AUTOMATION_EVAL_INPUT_LOCK.json)",
    )
    parser.add_argument("--input-lock", help="Replay eval using a previously emitted input lock JSON")
    parser.add_argument("--output", help="Output markdown path")
    parser.add_argument("--json-output", help="Output JSON path")
    parser.add_argument("--fail-on-block", action="store_true", help="Return non-zero when eval status is BLOCKED")
    parser.set_defaults(handler=run_eval_report_command)


def _register_migrate_artifacts_command(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser("migrate-artifacts", help="Dry-run or apply schema migrations for machine artifacts")
    _add_project_root_argument(parser)
    parser.add_argument("--feature")
    parser.add_argument("--run-id", action="append", help="Planning run id to migrate (repeatable)")
    parser.add_argument("--planning-dir", action="append", help="Explicit planning dir to migrate (repeatable)")
    parser.add_argument("--all-runs", action="store_true", help="Scan planning/* for migrations")
    parser.add_argument("--write", action="store_true", help="Apply migrations in place and create backups")
    parser.set_defaults(handler=run_migrate_artifacts_command)


def _register_self_repair_command(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser("self-repair", help="Generate a kodawari self-repair task from run artifacts")
    _add_project_root_argument(parser)
    parser.add_argument("--feature", help="Feature/run id under planning/")
    parser.add_argument("--planning-dir", help="Explicit planning directory")
    parser.add_argument("--write", action="store_true", help="Write .workflow_self_repair.json into the planning dir")
    parser.add_argument("--markdown", action="store_true", help="Also write SELF_REPAIR.md when --write is used")
    parser.add_argument("--output", help="Optional JSON output path")
    parser.set_defaults(handler=run_self_repair_command)


def _register_self_repair_execute_command(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser(
        "self-repair-execute",
        help="Phase 3: spawn a kodawari autopilot run from a self-repair proposal (env-gated)",
    )
    _add_project_root_argument(parser)
    parser.add_argument("--feature", help="Feature/run id under planning/")
    parser.add_argument("--planning-dir", help="Explicit planning directory holding .workflow_self_repair.json")
    parser.add_argument("--proposal", help="Path to a .workflow_self_repair.json (overrides --feature/--planning-dir)")
    parser.add_argument("--sdk-root", help="Override kodawari root used for path containment + spawn cwd")
    parser.add_argument(
        "--confidence-min",
        type=float,
        default=None,
        help="Override the minimum confidence threshold (default 0.85, env WORKFLOW_SELF_REPAIR_CONFIDENCE_MIN)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Run gates only; do not spawn the SDK autopilot")
    parser.add_argument("--write", action="store_true", help="Write .workflow_self_repair_execution.json into the planning dir")
    parser.set_defaults(handler=run_self_repair_execute_command)


def _register_self_repair_learn_command(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser(
        "self-repair-learn",
        help="Phase 4: emit prompt_lessons + journal entries for validated self-repair outcomes",
    )
    _add_project_root_argument(parser)
    parser.add_argument("--feature", help="Feature/run id under planning/")
    parser.add_argument("--planning-dir", help="Explicit planning directory holding .workflow_self_repair_execution.json")
    parser.add_argument("--execution-record", help="Path to a .workflow_self_repair_execution.json (overrides --feature)")
    parser.add_argument(
        "--target-after",
        help="Planning dir of the original target run AFTER the SDK fix landed (enables Level-2 evaluation)",
    )
    parser.add_argument("--sdk-root", help="Override kodawari root used for the journal write")
    parser.add_argument(
        "--lesson-project-root",
        help="Project root that owns the prompt_lessons store (default: SDK root)",
    )
    parser.set_defaults(handler=run_self_repair_learn_command)


def _register_replay_gate_command(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser("replay-gate", help="Evaluate replay gate using a frozen replay input artifact")
    _add_project_root_argument(parser)
    parser.add_argument("--input", help="Replay gate input JSON path")
    parser.add_argument("--output", help="Replay gate result JSON path")
    parser.add_argument("--fail-on-block", action="store_true", help="Return non-zero when replay gate is BLOCKED")
    parser.set_defaults(handler=run_replay_gate_command)


def _register_canary_gate_command(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser("canary-gate", help="Evaluate canary gate using a frozen canary input artifact")
    _add_project_root_argument(parser)
    parser.add_argument("--input", help="Canary gate input JSON path")
    parser.add_argument("--output", help="Canary gate result JSON path")
    parser.add_argument("--max-failed", type=int, help="Override allowed failed canary samples")
    parser.add_argument("--fail-on-block", action="store_true", help="Return non-zero when canary gate is BLOCKED")
    parser.set_defaults(handler=run_canary_gate_command)


def _register_incident_ingest_command(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser("incident-ingest", help="Ingest an incident into the field-report state machine")
    _add_project_root_argument(parser)
    parser.add_argument("--feature")
    parser.add_argument("--planning-dir")
    parser.add_argument("--incident-id", help="Optional explicit incident/report id")
    parser.add_argument("--source", default="production")
    parser.add_argument("--severity", choices=["low", "medium", "high", "critical"], default="high")
    parser.add_argument("--title", required=True)
    parser.add_argument("--summary", required=True)
    parser.add_argument("--component", default="")
    parser.add_argument("--impact", default="")
    parser.add_argument("--owner", default="")
    parser.add_argument("--tag", action="append", help="Repeatable incident tags")
    parser.add_argument("--evidence", action="append", help="Repeatable evidence file paths")
    parser.set_defaults(handler=run_incident_ingest_command)


def _register_approve_command(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser("approve", help="Write a decision response for a pending autopilot decision request")
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--feature", help="Feature name used to locate planning/<feature>")
    parser.add_argument("--planning-dir", help="Explicit planning dir (overrides --feature)")
    parser.add_argument("--option", default="", help="Option id to select; defaults to recommended_option from the request")
    parser.add_argument("--rationale", default="", help="Optional human rationale for the decision")
    parser.add_argument("--force", action="store_true", help="Overwrite an existing response or bypass option validation")
    parser.add_argument("--output", help="Optional JSON output path")
    parser.set_defaults(handler=_cmd_approve)


def _register_compact_command(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser("compact", help="Historical compact shell compatibility shim")
    _add_project_root_argument(parser)
    parser.add_argument("--feature", required=True)
    parser.add_argument("--planning-dir")
    parser.add_argument("--include-instincts", dest="include_instincts", action="store_true", default=True)
    parser.add_argument("--no-include-instincts", dest="include_instincts", action="store_false")
    parser.add_argument("--log-tail-lines", type=int, default=20)
    parser.add_argument("--output", help="Optional JSON output path")
    parser.set_defaults(handler=_cmd_legacy_compact)


def _register_legacy_runtime_command(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
    *,
    name: str,
    help_text: str,
    default_max_cycles: int,
) -> None:
    parser = sub.add_parser(name, help=help_text)
    _add_project_root_argument(parser)
    parser.add_argument("--feature", required=True)
    parser.add_argument("--planning-dir")
    parser.add_argument("--requirements-file")
    parser.add_argument("--profile", default="profiles/generic.yaml")
    parser.add_argument("--verify-cmd", default="pytest -q")
    parser.add_argument("--max-cycles", type=int, default=default_max_cycles)
    parser.add_argument("--token-budget", type=int, default=300000)
    parser.add_argument("--gate-profile", default="advisory", choices=list_profiles())
    parser.set_defaults(handler=_cmd_legacy_runtime)


def _register_legacy_shell_commands(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    _register_compact_command(sub)
    _register_legacy_runtime_command(
        sub,
        name="research",
        help_text="Historical research shell routed to canonical kodawari runtime",
        default_max_cycles=8,
    )
    _register_legacy_runtime_command(
        sub,
        name="develop",
        help_text="Historical develop shell routed to canonical kodawari runtime",
        default_max_cycles=8,
    )
    _register_legacy_runtime_command(
        sub,
        name="quick-develop",
        help_text="Historical quick-develop shell routed to canonical kodawari runtime",
        default_max_cycles=3,
    )
    _register_legacy_runtime_command(
        sub,
        name="optimize-existing-develop",
        help_text="Historical optimize-existing-develop shell routed to canonical kodawari runtime",
        default_max_cycles=8,
    )


def build_parser(*, help_all: bool = True) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kodawari", description="kodawari CLI")
    parser.add_argument("-v", "--verbose", action="count", default=0, help="Increase CLI logging verbosity")
    parser.add_argument("--help-all", action="store_true", help="Show operator and debug commands in top-level help")
    sub = parser.add_subparsers(dest="command", required=True)
    _register_setup_command(sub)
    _register_plan_command(sub)
    _register_work_command(sub)
    _register_work_all_alias_command(sub)
    _register_autopilot_command(sub)
    _register_approve_command(sub)
    _register_status_command(sub)
    _register_serve_command(sub)
    _register_doctor_command(sub)
    _register_init_wizard_command(sub)
    _register_gate_command(sub)
    _register_decide_command(sub)
    _register_gate_config_command(sub)
    _register_stability_report_command(sub)
    _register_lane_history_fetch_command(sub)
    _register_lane_triage_command(sub)
    _register_lane_trend_command(sub)
    _register_lane_trend_report_command(sub)
    _register_review_command(sub)
    _register_review_evidence_command(sub)
    _register_execution_evidence_command(sub)
    _register_verify_command(sub)
    _register_qa_command(sub)
    _register_ship_readiness_command(sub)
    _register_release_command(sub)
    _register_claude_shell_aliases(sub)
    _register_telemetry_command(sub)
    _register_field_report_command(sub)
    _register_field_report_update_command(sub)
    _register_eval_report_command(sub)
    _register_migrate_artifacts_command(sub)
    _register_self_repair_command(sub)
    _register_self_repair_execute_command(sub)
    _register_self_repair_learn_command(sub)
    _register_replay_gate_command(sub)
    _register_canary_gate_command(sub)
    _register_incident_ingest_command(sub)
    register_contract_first_commands(sub)
    _register_legacy_shell_commands(sub)
    apply_command_tiers(sub, help_all=help_all)
    return parser


__all__ = ["OPERATOR_COMMANDS", "USER_COMMANDS", "build_parser", "command_tier"]
