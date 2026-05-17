"""Thin facade routing layer for setup/plan/work/review/release/work-all."""

from __future__ import annotations

import argparse

from kodawari.cli.contract.plan_cmd import run_plan_command as _run_plan_command
from kodawari.cli.delivery.release_cmd import run_release_command as _run_release_command
from kodawari.cli.evidence.review_cmd import run_review_command as _run_review_command
from kodawari.cli.runtime.setup_cmd import run_setup_command as _run_setup_command
from kodawari.cli.runtime.work_cmd import (
    run_work_all_facade_command as _run_work_all_facade_command,
    run_work_command as _run_work_command,
)


def run_setup_command(args: argparse.Namespace) -> int:
    return _run_setup_command(args)


def run_plan_command(args: argparse.Namespace) -> int:
    return _run_plan_command(args)


def run_work_command(args: argparse.Namespace) -> int:
    return _run_work_command(args)


def run_work_all_facade_command(args: argparse.Namespace) -> int:
    return _run_work_all_facade_command(args)


def run_review_command(args: argparse.Namespace) -> int:
    return _run_review_command(args)


def run_release_command(args: argparse.Namespace) -> int:
    return _run_release_command(args)


__all__ = [
    "run_plan_command",
    "run_release_command",
    "run_review_command",
    "run_setup_command",
    "run_work_all_facade_command",
    "run_work_command",
]

