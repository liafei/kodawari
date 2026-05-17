"""Compatibility tiers for kodawari commands."""

from __future__ import annotations

import argparse


USER_COMMANDS = frozenset(
    {
        "setup",
        "plan",
        "work",
        "work-all",
        "autopilot",
        "status",
        "serve",
        "gate",
        "review",
        "release",
    }
)
OPERATOR_COMMANDS = frozenset(
    {
        "approve",
        "doctor",
        "stability-report",
        "lane-history-fetch",
        "lane-triage",
        "lane-trend",
        "lane-trend-report",
        "review-evidence",
        "execution-evidence",
        "verify",
        "qa",
        "ship-readiness",
        "telemetry",
        "field-report",
        "field-report-update",
        "eval-report",
        "migrate-artifacts",
        "self-repair",
        "self-repair-execute",
        "self-repair-learn",
        "replay-gate",
        "canary-gate",
        "incident-ingest",
    }
)


def command_tier(command: str) -> str:
    if command in USER_COMMANDS:
        return "user"
    if command in OPERATOR_COMMANDS:
        return "operator"
    return "debug"


def apply_command_tiers(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
    *,
    help_all: bool,
) -> None:
    for name, subparser in sub.choices.items():
        tier = command_tier(str(name))
        subparser.set_defaults(command_tier=tier)

    for choice_action in sub._choices_actions:
        tier = command_tier(str(choice_action.dest))
        if not help_all and tier != "user":
            choice_action.help = argparse.SUPPRESS
    if not help_all:
        visible_actions = [
            choice_action
            for choice_action in sub._choices_actions
            if command_tier(str(choice_action.dest)) == "user"
        ]
        sub._choices_actions = visible_actions
        sub.metavar = "{" + ",".join(str(action.dest) for action in visible_actions) + "}"


__all__ = ["OPERATOR_COMMANDS", "USER_COMMANDS", "apply_command_tiers", "command_tier"]
