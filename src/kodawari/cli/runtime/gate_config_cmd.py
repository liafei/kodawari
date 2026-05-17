"""CLI command for configuring code gate thresholds.

Provides commands to:
- show: display current effective thresholds
- set: override a specific threshold
- apply-profile: load a named preset configuration
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


def run_gate_config_command(args: argparse.Namespace) -> int:
    """Route gate-config subcommand."""
    subcommand = getattr(args, "gate_config_command", None)
    if not subcommand:
        logger.error("Subcommand required: show, set, apply-profile")
        return 1

    # argparse converts dash to underscore
    subcommand_normalized = subcommand.replace("-", "_")

    if subcommand_normalized == "show":
        return _cmd_show(args)
    elif subcommand_normalized == "set":
        return _cmd_set(args)
    elif subcommand_normalized == "apply_profile":
        return _cmd_apply_profile(args)
    else:
        logger.error(f"Unknown subcommand: {subcommand}")
        return 1


def _get_planning_dir(args: argparse.Namespace) -> Path | None:
    """Extract planning directory from args."""
    planning_dir = getattr(args, "planning_dir", None)
    if not planning_dir:
        logger.error("--planning-dir is required")
        return None
    planning_dir = Path(planning_dir).resolve()
    planning_dir.mkdir(parents=True, exist_ok=True)
    return planning_dir


def _load_policy_yaml(planning_dir: Path) -> dict[str, Any]:
    """Load existing gate_policy.yaml or return defaults."""
    policy_file = planning_dir / "gate_policy.yaml"
    if policy_file.exists():
        try:
            with open(policy_file) as f:
                data = yaml.safe_load(f) or {}
                return data
        except Exception as e:
            logger.warning(f"Failed to read gate_policy.yaml: {e}, using defaults")
            return {}
    return {}


def _write_policy_yaml(planning_dir: Path, data: dict[str, Any]) -> None:
    """Write gate_policy.yaml with proper defaults+rules schema."""
    policy_file = planning_dir / "gate_policy.yaml"
    with open(policy_file, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)
    logger.info(f"Wrote gate_policy.yaml to {policy_file}")


def _cmd_show(args: argparse.Namespace) -> int:
    """Show current effective gate thresholds."""
    planning_dir = _get_planning_dir(args)
    if not planning_dir:
        return 1

    try:
        from kodawari.gate.policy_loader import load_gate_policy
    except ImportError:
        logger.error("Failed to import load_gate_policy")
        return 1

    try:
        policy = load_gate_policy(planning_dir)
        if not policy:
            logger.error("No gate_policy.yaml found; using defaults")
            from kodawari.gate.profiles import DEFAULT_THRESHOLDS
            thresholds = DEFAULT_THRESHOLDS.to_dict()
        else:
            thresholds_obj = policy.effective_thresholds(".")
            thresholds = thresholds_obj.__dict__
        print("Current Gate Configuration:")
        print(json.dumps(thresholds, indent=2, default=str))
        return 0
    except Exception as e:
        logger.error(f"Failed to load policy: {e}")
        return 1


def _cmd_set(args: argparse.Namespace) -> int:
    """Set a specific gate threshold."""
    planning_dir = _get_planning_dir(args)
    if not planning_dir:
        return 1

    key_value = getattr(args, "key_value", None)
    if not key_value or "=" not in key_value:
        logger.error("Expected --key-value=KEY=VALUE format")
        return 1

    key, value_str = key_value.split("=", 1)
    key = key.strip()
    value_str = value_str.strip()

    try:
        # Try to parse as int first
        try:
            value = int(value_str)
        except ValueError:
            # Fall back to string
            value = value_str
    except Exception as e:
        logger.error(f"Failed to parse value: {e}")
        return 1

    # Load existing policy
    data = _load_policy_yaml(planning_dir)
    if "defaults" not in data:
        data["defaults"] = {}
    if "rules" not in data:
        data["rules"] = []

    # Update defaults
    data["defaults"][key] = value

    # Write back
    _write_policy_yaml(planning_dir, data)
    print(f"Set {key} = {value}")
    return 0


def _cmd_apply_profile(args: argparse.Namespace) -> int:
    """Apply a named gate configuration profile."""
    planning_dir = _get_planning_dir(args)
    if not planning_dir:
        return 1

    profile_name = getattr(args, "profile_name", None)
    if not profile_name:
        logger.error("Profile name required")
        return 1

    try:
        from kodawari.gate.profiles import PROFILES
    except ImportError:
        logger.error("Failed to import gate profiles")
        return 1

    if profile_name not in PROFILES:
        logger.error(f"Unknown profile: {profile_name}. Available: {list(PROFILES.keys())}")
        return 1

    profile = PROFILES[profile_name]

    # Convert profile thresholds to gate_policy.yaml format
    # GateProfile has thresholds.to_dict()
    thresholds_dict = profile.thresholds.to_dict()

    data = {
        "defaults": thresholds_dict,
        "rules": [],
    }

    _write_policy_yaml(planning_dir, data)
    print(f"Applied profile: {profile_name}")
    return 0


__all__ = ["run_gate_config_command"]
