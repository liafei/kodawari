"""Recovered kodawari-compatible CLI entrypoint."""

from __future__ import annotations

import io
import json
from pathlib import Path
import sys

from kodawari.cli.autopilot_cmd import run_autopilot_command
from kodawari.cli.changed_files_truth import resolve_task_delta_changed_files
from kodawari.cli.dotenv_loader import load_dotenv
from kodawari.cli.logging_setup import configure_cli_logging
from kodawari.cli.main_support import (
    ARTIFACT_SEMANTICS,
    DEFAULT_GATE_REDLINE,
    MERGED_CONTRACT_VERSION,
    REQUIRED_PLANNING_ARTIFACTS,
    _mismatched_module_repo,
    _repo_mismatch_guard_payload,
    _warn_if_repo_resolution_mismatch,
)
from kodawari.cli.parser_registry import build_parser
from kodawari.cli.provenance import find_kodawari_repo_root, resolved_wrapper_repo_root
from kodawari.cli.status_cmd import _git_changed_files
from kodawari.gate import GateEngine

# Compatibility sentinel for bootstrap checks: "loaded CLI code is from"
_REPO_MISMATCH_WARNING_SENTINEL = "loaded CLI code is from"


def _ensure_utf8_streams() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is not None and hasattr(stream, "buffer") and getattr(stream, "encoding", "utf-8").lower() not in {"utf-8", "utf8"}:
            setattr(sys, stream_name, io.TextIOWrapper(stream.buffer, encoding="utf-8", errors="replace"))


def main(argv: list[str] | None = None) -> int:
    _ensure_utf8_streams()
    # Load project-local .env before any command code so reviewer / executor
    # API keys can be provisioned without re-exporting in every shell.
    # Shell-exported values still win — load_dotenv calls setdefault, never override.
    load_dotenv()
    _warn_if_repo_resolution_mismatch()
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    help_all = "--help-all" in raw_argv
    if help_all:
        raw_argv = [item for item in raw_argv if item != "--help-all"]
    parser = build_parser(help_all=help_all)
    if help_all and not raw_argv:
        print(parser.format_help())
        return 0
    args = parser.parse_args(raw_argv)
    project_root = getattr(args, "project_root", None)
    if project_root:
        load_dotenv(Path(project_root))
        # Fill argparse None-valued CLI defaults from .claude/workflow/defaults.yaml
        # (the project-level "settings page"). Keys with explicit user values are
        # left alone; keys with built-in defaults (e.g. --gate-profile="advisory")
        # are also left alone because argparse already filled them. This only
        # affects the handful of args declared with default=None — see
        # workflow_defaults.BUILTIN_DEFAULTS for the list.
        from kodawari.cli.runtime.workflow_defaults import apply_workflow_defaults
        apply_workflow_defaults(args, Path(project_root))
    configure_cli_logging(int(getattr(args, "verbose", 0) or 0))
    guard_payload = _repo_mismatch_guard_payload(str(getattr(args, "command", "")))
    if guard_payload is not None:
        print(json.dumps(guard_payload, ensure_ascii=False, indent=2))
        return 2
    return int(args.handler(args))


__all__ = [
    "ARTIFACT_SEMANTICS",
    "DEFAULT_GATE_REDLINE",
    "GateEngine",
    "MERGED_CONTRACT_VERSION",
    "REQUIRED_PLANNING_ARTIFACTS",
    "_git_changed_files",
    "_mismatched_module_repo",
    "build_parser",
    "find_kodawari_repo_root",
    "main",
    "resolve_task_delta_changed_files",
    "resolved_wrapper_repo_root",
    "run_autopilot_command",
]


if __name__ == "__main__":
    raise SystemExit(main())
