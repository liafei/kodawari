"""Generate automation stability reports from autopilot artifacts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from kodawari.cli.provenance import build_stability_report_provenance
from kodawari.cli.status.stability_report_parser import (
    build_command_output_payload,
    build_report_options,
    load_run_summaries,
    load_run_summary,
    resolve_cli_selection,
    resolve_planning_dirs,
    resolve_report_output_path,
)
from kodawari.cli.status.stability_report_renderer import (
    build_report_data,
    render_stability_markdown,
)


_resolve_cli_selection = resolve_cli_selection
_build_report_options = build_report_options
_resolve_report_output_path = resolve_report_output_path
_load_run_summaries = load_run_summaries
_load_run_summary = load_run_summary
_build_report_data = build_report_data
_render_markdown_report = render_stability_markdown
_command_output_payload = build_command_output_payload


def run_stability_report_command(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root).resolve()
    selection = resolve_cli_selection(args)
    planning_dirs = resolve_planning_dirs(
        project_root=project_root,
        run_ids=selection["run_ids"],
        planning_dirs=selection["explicit_planning_dirs"],
        scan_all=selection["all_runs"],
        updated_since=selection["updated_since"],
        updated_until=selection["updated_until"],
    )
    if not planning_dirs:
        raise ValueError("stability-report requires --run-id/--planning-dir, or use --all-runs")
    runs, warnings = load_run_summaries(planning_dirs)
    provenance = build_stability_report_provenance(project_root, planning_dirs, module_file=Path(__file__))
    report_options = build_report_options(
        args,
        warnings,
        project_root=project_root,
        planning_dirs=planning_dirs,
    )
    report_markdown = render_stability_markdown(runs, report_options=report_options)
    report_data = build_report_data(runs, report_options=report_options)
    output_path = resolve_report_output_path(project_root, getattr(args, "output", None))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report_markdown, encoding="utf-8")
    payload = build_command_output_payload(
        project_root=project_root,
        runs=runs,
        warnings=warnings,
        output_path=output_path,
        selection=selection,
        planning_dirs=planning_dirs,
        report_data=report_data,
        provenance=provenance,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0

