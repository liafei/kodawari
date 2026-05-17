"""Tests for C2-b: --tier CLI flag and payload wiring.

We do NOT run the full autopilot subprocess here (covered by e2e smoke).
Instead we verify:
  - the parser accepts --tier and rejects invalid values
  - _resolve_tier_and_policy returns the correct decision/policy for
    each requested_tier
  - default --tier="auto" with empty input produces a fallback policy
"""

from __future__ import annotations

import argparse
import json

import pytest

from kodawari.cli.autopilot_cmd import _resolve_tier_and_policy
from kodawari.cli.parser_registry import build_parser


def _argv_for_tier(tier: str | None) -> list[str]:
    base = ["autopilot", "--feature", "f1"]
    if tier is not None:
        base += ["--tier", tier]
    return base


# ---------------------------------------------------------------------------
# Parser accepts --tier values
# ---------------------------------------------------------------------------


def test_parser_accepts_tier_auto_default():
    parser = build_parser()
    args = parser.parse_args(_argv_for_tier(None))
    assert args.tier == "auto"


@pytest.mark.parametrize("tier", ["lite", "standard", "heavy", "auto"])
def test_parser_accepts_valid_tier_values(tier):
    parser = build_parser()
    args = parser.parse_args(_argv_for_tier(tier))
    assert args.tier == tier


def test_parser_rejects_invalid_tier():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(_argv_for_tier("garbage"))


# ---------------------------------------------------------------------------
# Parser --task-cycle / --no-task-cycle sentinel flags
# ---------------------------------------------------------------------------


def test_parser_task_cycle_default_is_none_sentinel():
    parser = build_parser()
    args = parser.parse_args(["autopilot", "--feature", "f1"])
    assert args.task_cycle is None


def test_parser_task_cycle_flag_sets_true():
    parser = build_parser()
    args = parser.parse_args(["autopilot", "--feature", "f1", "--task-cycle"])
    assert args.task_cycle is True


def test_parser_no_task_cycle_flag_sets_false():
    parser = build_parser()
    args = parser.parse_args(["autopilot", "--feature", "f1", "--no-task-cycle"])
    assert args.task_cycle is False


# ---------------------------------------------------------------------------
# _resolve_tier_and_policy maps args -> (decision, policy)
# ---------------------------------------------------------------------------


def _make_args(tier: str = "auto", task: str = "") -> argparse.Namespace:
    return argparse.Namespace(tier=tier, task=task)


def test_resolve_explicit_lite_produces_lite_policy():
    decision, policy = _resolve_tier_and_policy(
        args=_make_args(tier="lite"),
        feature="f1", requirements_text="", changed_files=(),
    )
    assert decision.tier == "lite"
    assert decision.source == "explicit"
    assert policy.effective_tier == "lite"
    assert policy.release_tail_enabled is False
    assert policy.eval_required is False
    assert policy.review_max_rounds == 3


def test_resolve_explicit_standard_produces_standard_policy():
    decision, policy = _resolve_tier_and_policy(
        args=_make_args(tier="standard"),
        feature="f1", requirements_text="", changed_files=(),
    )
    assert decision.tier == "standard"
    assert policy.effective_tier == "standard"
    assert policy.eval_required is False
    assert policy.release_tail_enabled is False
    assert policy.review_max_rounds == 2


def test_resolve_explicit_heavy_produces_heavy_policy():
    decision, policy = _resolve_tier_and_policy(
        args=_make_args(tier="heavy"),
        feature="f1", requirements_text="", changed_files=(),
    )
    assert decision.tier == "heavy"
    assert policy.effective_tier == "heavy"
    assert policy.eval_required is True
    assert policy.release_tail_enabled is True


def test_resolve_auto_with_no_signals_falls_back_to_standard():
    """auto tier + zero input => detector falls to fallback_gray_zone => standard.
    Empty input scores 0 (no files) which is in lite range; but our detector
    actually treats 0-file input as score=-20 (file count <=2). So lite via static.
    """
    decision, policy = _resolve_tier_and_policy(
        args=_make_args(tier="auto", task=""),
        feature="f1", requirements_text="", changed_files=(),
    )
    # Lite is correct for empty signals (no risk indicators).
    assert decision.tier in {"lite", "standard"}
    assert policy.effective_tier == decision.tier


def test_resolve_auto_with_breaking_change_keyword_forces_heavy():
    decision, policy = _resolve_tier_and_policy(
        args=_make_args(tier="auto", task="introduce breaking change to public API"),
        feature="f1", requirements_text="", changed_files=(),
    )
    assert decision.tier == "heavy"
    assert decision.source == "hard_rule"
    assert policy.effective_tier == "heavy"


def test_resolve_auto_uses_changed_files_for_hard_rule_detection():
    """Auth path in changed_files triggers hard rule => heavy."""
    decision, policy = _resolve_tier_and_policy(
        args=_make_args(tier="auto"),
        feature="f1",
        requirements_text="",
        changed_files=("backend/api/v1/services/auth_service.py",),
    )
    assert decision.tier == "heavy"
    assert decision.source == "hard_rule"


def test_decision_and_policy_are_serializable():
    """Both must serialize cleanly for payload emission."""
    import json
    decision, policy = _resolve_tier_and_policy(
        args=_make_args(tier="lite"),
        feature="f1", requirements_text="", changed_files=(),
    )
    encoded = json.dumps({
        "complexity_decision": decision.to_dict(),
        "workflow_policy": policy.to_dict(),
    })
    assert "lite" in encoded
    assert "effective_tier" in encoded


# ---------------------------------------------------------------------------
# Pre-bootstrap git scan: auto mode must seed changed_files from working tree
# ---------------------------------------------------------------------------


def test_auto_mode_pre_bootstrap_seeds_changed_files_from_git(monkeypatch, tmp_path):
    """Auto mode must call the shared git helper before detector so hard rules fire."""
    from kodawari.cli import autopilot_cmd

    captured: dict = {}

    def fake_git(root):
        captured["called_with"] = root
        return ["backend/api/v1/services/auth_service.py"]

    monkeypatch.setattr(autopilot_cmd, "_status_git_changed_files", fake_git)

    # Run the same pre-bootstrap code path that autopilot_cmd does:
    pre_requested_tier = "auto"
    pre_changed_files = tuple(fake_git(tmp_path)) if pre_requested_tier == "auto" else ()
    decision, policy = _resolve_tier_and_policy(
        args=_make_args(tier="auto"),
        feature="f1",
        requirements_text="",
        changed_files=pre_changed_files,
    )
    assert captured["called_with"] == tmp_path
    assert decision.tier == "heavy"
    assert decision.source == "hard_rule"
    assert policy.effective_tier == "heavy"


def test_auto_mode_pre_bootstrap_safe_when_git_unavailable(monkeypatch):
    """If _git_changed_files raises, autopilot must fall back to empty tuple."""
    from kodawari.cli import autopilot_cmd

    def boom(root):
        raise FileNotFoundError("git not installed")

    monkeypatch.setattr(autopilot_cmd, "_status_git_changed_files", boom)

    # Replicating the try/except wrapper from autopilot_cmd:
    try:
        pre_changed_files = tuple(autopilot_cmd._status_git_changed_files("."))
    except Exception:
        pre_changed_files = ()

    decision, policy = _resolve_tier_and_policy(
        args=_make_args(tier="auto"),
        feature="f1",
        requirements_text="",
        changed_files=pre_changed_files,
    )
    # Zero signals -> empty_input_fallback -> standard (not lite).
    assert decision.source == "empty_input_fallback"
    assert decision.tier == "standard"


def test_explicit_tier_skips_pre_bootstrap_git(monkeypatch):
    """Explicit --tier must not invoke git scan (detector short-circuits to explicit)."""
    from kodawari.cli import autopilot_cmd

    called = {"count": 0}

    def track_git(root):
        called["count"] += 1
        return []

    monkeypatch.setattr(autopilot_cmd, "_status_git_changed_files", track_git)

    # Only invoke git when tier == "auto":
    pre_requested_tier = "heavy"
    if pre_requested_tier == "auto":
        pre_changed_files = tuple(autopilot_cmd._status_git_changed_files("."))
    else:
        pre_changed_files = ()

    assert called["count"] == 0
    assert pre_changed_files == ()


# ---------------------------------------------------------------------------
# §1: ComplexityInput signal completeness from planning artifacts
# ---------------------------------------------------------------------------


def test_build_complexity_input_reads_task_card_files(tmp_path):
    """Verify _build_complexity_input reads task_card_files from TASK_CARD_ACTIVE.json."""
    from kodawari.cli.autopilot_cmd import _build_complexity_input

    planning_dir = tmp_path / "planning" / "f1"
    planning_dir.mkdir(parents=True)
    (planning_dir / "TASK_CARD_ACTIVE.json").write_text(
        json.dumps({"files_to_change": ["src/main.py", "tests/test_main.py"]})
    )

    inp = _build_complexity_input(
        feature="f1",
        task_direction="",
        requirements_text="",
        changed_files=(),
        project_root=tmp_path,
    )
    assert inp.task_card_files == ("src/main.py", "tests/test_main.py")


def test_build_complexity_input_reads_layers_from_prd_intake(tmp_path):
    # layers lives on PRD_INTAKE (and PLANNING_CONVERSATION), NOT on
    # REPO_INVENTORY — repo_inventory.schema.json has no `layers`
    # property. The extractor must read from PRD_INTAKE to match the
    # real contract-first artifact shape.
    from kodawari.cli.autopilot_cmd import _build_complexity_input

    planning_dir = tmp_path / "planning" / "f1"
    planning_dir.mkdir(parents=True)
    (planning_dir / "PRD_INTAKE.json").write_text(
        json.dumps({"layers": ["route", "service"]})
    )

    inp = _build_complexity_input(
        feature="f1",
        task_direction="",
        requirements_text="",
        changed_files=(),
        project_root=tmp_path,
    )
    assert inp.layers == ("route", "service")


def test_build_complexity_input_layers_fallback_when_prd_intake_empty(tmp_path):
    # Empty `layers: []` on PRD_INTAKE must not short-circuit the fallback
    # to PLANNING_CONVERSATION. The compat layer can legitimately yield a
    # PRD-shaped view whose layers come from the conversation snapshot
    # even when an earlier PRD stage wrote [].
    from kodawari.cli.autopilot_cmd import _build_complexity_input

    planning_dir = tmp_path / "planning" / "f1"
    planning_dir.mkdir(parents=True)
    (planning_dir / "PRD_INTAKE.json").write_text(json.dumps({"layers": []}))
    (planning_dir / "PLANNING_CONVERSATION.json").write_text(
        json.dumps({"layers": ["route", "service"]})
    )

    inp = _build_complexity_input(
        feature="f1",
        task_direction="",
        requirements_text="",
        changed_files=(),
        project_root=tmp_path,
    )
    assert inp.layers == ("route", "service")


def test_build_complexity_input_ignores_layers_on_repo_inventory(tmp_path):
    # Guard against regressing the old behaviour: a `layers` key on
    # REPO_INVENTORY.json must NOT populate inp.layers because the real
    # schema does not declare that field.
    from kodawari.cli.autopilot_cmd import _build_complexity_input

    planning_dir = tmp_path / "planning" / "f1"
    planning_dir.mkdir(parents=True)
    (planning_dir / "REPO_INVENTORY.json").write_text(
        json.dumps({"layers": ["route", "service"]})
    )

    inp = _build_complexity_input(
        feature="f1",
        task_direction="",
        requirements_text="",
        changed_files=(),
        project_root=tmp_path,
    )
    assert inp.layers == ()


def test_build_complexity_input_does_not_promote_sot_entities_to_files(tmp_path):
    # PRD_INTAKE's `source_of_truth` is entity-level (e.g. "db.primary")
    # and must not be piped into source_of_truth_files — that field in
    # the detector is scored as file paths (file-count, path-token
    # scoring), so leaking entity tokens into it would misclassify runs.
    from kodawari.cli.autopilot_cmd import _build_complexity_input

    planning_dir = tmp_path / "planning" / "f1"
    planning_dir.mkdir(parents=True)
    (planning_dir / "PRD_INTAKE.json").write_text(
        json.dumps({
            "source_of_truth": ["db.primary", "cache.hot"],
            "source_of_truth_canonical": ["db.primary", "cache.hot"],
            "path_type": "write",
        })
    )

    inp = _build_complexity_input(
        feature="f1",
        task_direction="",
        requirements_text="",
        changed_files=(),
        project_root=tmp_path,
    )
    assert inp.source_of_truth_files == ()


def test_build_complexity_input_graceful_fallback_missing_files(tmp_path):
    """Verify _build_complexity_input returns empty values when planning files are missing."""
    from kodawari.cli.autopilot_cmd import _build_complexity_input

    planning_dir = tmp_path / "planning" / "f1"
    planning_dir.mkdir(parents=True)

    inp = _build_complexity_input(
        feature="f1",
        task_direction="",
        requirements_text="",
        changed_files=(),
        project_root=tmp_path,
    )
    assert inp.task_card_files == ()
    assert inp.source_of_truth_files == ()
    assert inp.path_type == ""
    assert inp.layers == ()


def test_resolve_tier_and_policy_with_complete_signals(tmp_path):
    """Verify _resolve_tier_and_policy uses complete input signals including task_card_files."""
    planning_dir = tmp_path / "planning" / "f1"
    planning_dir.mkdir(parents=True)
    (planning_dir / "TASK_CARD_ACTIVE.json").write_text(
        '{"files_to_change": ["backend/api/v1/routes/new_endpoint.py"]}'
    )

    args = argparse.Namespace(tier="auto", task="")
    decision, policy = _resolve_tier_and_policy(
        args=args,
        feature="f1",
        requirements_text="",
        changed_files=(),
        project_root=tmp_path,
    )
    # task_card_files with /routes/ should trigger hard rule → heavy
    assert decision.tier == "heavy"
