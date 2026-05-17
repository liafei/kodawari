"""Compatibility wrapper for recovered SPEC commands."""

from __future__ import annotations

import argparse

from kodawari.cli import spec as legacy_spec


def run_spec_generate_command(args: argparse.Namespace) -> int:
    legacy_args = argparse.Namespace(
        prd=list(args.prd),
        output=args.output,
        priority=args.priority,
    )
    return int(legacy_spec.command_generate(legacy_args))


def run_spec_validate_command(args: argparse.Namespace) -> int:
    legacy_args = argparse.Namespace(
        spec_dir=args.spec_dir,
        report=args.report,
    )
    return int(legacy_spec.command_validate(legacy_args))


def run_spec_coverage_command(args: argparse.Namespace) -> int:
    legacy_args = argparse.Namespace(
        prd=list(args.prd),
        spec_dir=args.spec_dir,
        output=args.output,
        format=args.format,
    )
    return int(legacy_spec.command_coverage(legacy_args))


def run_spec_materialize_command(args: argparse.Namespace) -> int:
    legacy_args = argparse.Namespace(
        spec_dir=args.spec_dir,
        output=args.output,
    )
    return int(legacy_spec.command_materialize(legacy_args))


def register_spec_subcommands(spec_parser: argparse.ArgumentParser) -> None:
    spec_sub = spec_parser.add_subparsers(dest="spec_command", required=True)

    generate = spec_sub.add_parser("generate", help="Generate SPEC files from PRD")
    generate.add_argument("--prd", action="append", required=True)
    generate.add_argument("--output", required=True)
    generate.add_argument("--priority", default="P0")
    generate.set_defaults(handler=run_spec_generate_command)

    validate = spec_sub.add_parser("validate", help="Validate generated SPEC JSON files")
    validate.add_argument("--spec-dir", required=True)
    validate.add_argument("--report", required=True)
    validate.set_defaults(handler=run_spec_validate_command)

    coverage = spec_sub.add_parser("coverage", help="Generate PRD/SPEC coverage report")
    coverage.add_argument("--prd", action="append", required=True)
    coverage.add_argument("--spec-dir", required=True)
    coverage.add_argument("--output", required=True)
    coverage.add_argument("--format", choices=["json", "markdown"], default="json")
    coverage.set_defaults(handler=run_spec_coverage_command)

    materialize = spec_sub.add_parser("materialize", help="Materialize SPEC docs")
    materialize.add_argument("--spec-dir", required=True)
    materialize.add_argument("--output", required=True)
    materialize.set_defaults(handler=run_spec_materialize_command)
