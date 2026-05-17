"""Shared argparse argument groups for kodawari commands."""

from __future__ import annotations

import argparse

from kodawari.autopilot.execution.execution_backend import execution_backend_choices, self_review_backend_choices
from kodawari.cli.core.main_support import _add_project_root_argument
from kodawari.gate import list_profiles


def add_model_and_reviewer_arguments(parser: argparse.ArgumentParser) -> None:
    """Add executor/reviewer model and backend arguments shared across parsers."""
    parser.add_argument("--real-peer-review", action="store_true", default=False, help="Enable real peer review")
    parser.add_argument("--require-real-peer-review", action="store_true", default=False, help="Require real peer review (fail if unavailable)")
    parser.add_argument("--real-opus-review", action="store_true", default=False, help="Legacy alias for --real-peer-review")
    parser.add_argument("--require-real-opus-review", action="store_true", default=False, help="Legacy alias for --require-real-peer-review")
    parser.add_argument("--executor-model", default="", help="Model override for the executor backend")
    parser.add_argument("--reviewer-backend", default="", choices=["", "api", "cli", "mcp", "codex", "auto"], help="Reviewer backend: api, cli, mcp, codex, or auto")
    parser.add_argument("--reviewer-model", default="", help="Model override for the reviewer backend")
    parser.add_argument("--reviewer-api-format", default="", choices=["", "openai", "anthropic", "auto"], help="API format for the reviewer (openai, anthropic, or auto)")
    parser.add_argument("--reviewer-base-url", default="", help="Base URL for the reviewer API endpoint")
    parser.add_argument("--opus-reviewer-backend", default="", help="Deprecated alias for --reviewer-backend; will be removed after 2026-08-01")


def add_work_runtime_arguments(parser: argparse.ArgumentParser) -> None:
    _add_project_root_argument(parser)
    parser.add_argument("--feature")
    parser.add_argument("--planning-dir")
    parser.add_argument("--task", default="", help="Task direction for model-driven planning")
    parser.add_argument("--prd", help="Path to PRD text/markdown file (primary work/work all input)")
    parser.add_argument("--requirements-file", help="Compatibility alias for --prd")
    parser.add_argument(
        "--planner-route",
        default="auto",
        choices=["auto", "model", "generic"],
        help=(
            "Planning route used when bootstrapping contract-first planning under work: "
            "auto=infer from --task/PLANNING_CONVERSATION.json (legacy default), "
            "model=force PRD-driven model planner, generic=force archetype-based generic planner."
        ),
    )
    parser.add_argument(
        "--replan",
        action="store_true",
        help="Regenerate planning artifacts instead of consuming an existing TASK_GRAPH.json",
    )
    parser.add_argument("--profile", default="profiles/generic.yaml")
    parser.add_argument("--verify-cmd", default="pytest -q")
    parser.add_argument("--max-cycles", type=int, default=8)
    parser.add_argument("--parallel-workers", type=int, default=2, help="Worker slots for parallel coordinator assignment")
    parser.add_argument("--token-budget", type=int, default=300000)
    parser.add_argument("--gate-profile", default="advisory", choices=list_profiles())
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
        help="Self-review backend contract used by work/work all review rounds",
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
            "Whole-loop wall-clock budget (seconds) forwarded to the nested autopilot step. "
            "0 disables (default). Independent of WORKFLOW_EXECUTOR_TIMEOUT_SECONDS (per-round)."
        ),
    )
    add_model_and_reviewer_arguments(parser)
    parser.add_argument("--base-branch", default="main")
    parser.add_argument("--changed-file", action="append", help="Optional changed file override for downstream review facade")
    parser.add_argument("--scope-allow", action="append", help="Optional extra allowed prefixes for downstream review facade")
    parser.add_argument("--command-file", help="Optional verify command file override for downstream review facade")
    parser.add_argument("--command", help="Optional verify command override for downstream review facade")
    parser.add_argument("--eval-report-path", help="Optional eval report path override for downstream release facade")
    parser.add_argument("--auto-eval", action="store_true", help="Auto-generate eval report during downstream release facade")
    parser.add_argument("--risk-profile", choices=["low", "medium", "high"], default="medium")
    parser.add_argument("--release-gate-profile", default="strict", choices=list_profiles())
    parser.add_argument("--release-gate-path", action="append", help="Optional path(s) to scan during downstream release gate")
    parser.add_argument("--force-rerun", action="store_true", help="Ignore completed PASS steps in .work_all_manifest.json")
    parser.add_argument("--output", help="Optional JSON output path for facade payloads")


__all__ = ["add_model_and_reviewer_arguments", "add_work_runtime_arguments"]
