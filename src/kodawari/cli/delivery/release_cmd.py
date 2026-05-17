"""Release facade built on qa + gate + ship-readiness."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from kodawari.cli.delivery.delivery_cmds import _cmd_qa, _cmd_ship_readiness
from kodawari.cli.gate.gate_cmd import _cmd_gate
from kodawari.cli.io_atomic import atomic_write_text
from kodawari.cli.core.legacy_runtime_invocation import invoke_cli_handler, legacy_step_result
from kodawari.cli.main_support import _build_cli_provenance, _resolve_feature_planning_dir, _write_json_output


def _emit(payload: dict[str, Any], *, output: str | None) -> int:
    _write_json_output(output, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return int(payload.get("_rc", 0) or 0)


def _combined_status(*payloads: dict[str, Any]) -> str:
    statuses = [str(item.get("status") or item.get("total_status") or "").upper() for item in payloads if isinstance(item, dict)]
    if any(status == "FAIL" for status in statuses):
        return "FAIL"
    if any(status == "BLOCKED" for status in statuses):
        return "BLOCKED"
    return "PASS"


def _normalize_gate_status(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    total_status = str(payload.get("total_status") or payload.get("status") or "").upper()
    normalized["status"] = "PASS" if total_status == "PASS" else "BLOCKED"
    normalized["total_status"] = total_status or normalized["status"]
    return normalized


def _write_changelog(
    *,
    planning_dir: Path,
    feature: str,
    review_payload: dict[str, Any] | None,
    ship_payload: dict[str, Any] | None,
) -> Path:
    review_changed = dict((review_payload or {}).get("changed_files") or {})
    changed_files = [str(item) for item in list(review_changed.get("items") or []) if str(item).strip()]
    lines = [
        f"# CHANGELOG ({feature})",
        "",
        f"- review_status: {str((review_payload or {}).get('status') or '')}",
        f"- ship_status: {str((ship_payload or {}).get('status') or '')}",
        f"- ship_summary: {str((ship_payload or {}).get('summary') or '')}",
        "",
        "## Changed Files",
    ]
    if changed_files:
        lines.extend(f"- {item}" for item in changed_files)
    else:
        lines.append("- (none)")
    changelog_path = planning_dir / "CHANGELOG.md"
    atomic_write_text(changelog_path, "\n".join(lines) + "\n")
    return changelog_path


def run_release_command(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root).resolve()
    feature = str(getattr(args, "feature", "") or "").strip()
    planning_dir = _resolve_feature_planning_dir(
        project_root=project_root,
        feature=feature,
        planning_dir=getattr(args, "planning_dir", None),
    )
    qa_rc, qa_payload = invoke_cli_handler(
        _cmd_qa,
        argparse.Namespace(
            project_root=str(project_root),
            feature=feature,
            planning_dir=str(planning_dir),
            output=None,
            fail_on_block=False,
        ),
    )
    gate_rc, gate_payload_raw = invoke_cli_handler(
        _cmd_gate,
        argparse.Namespace(
            project_root=str(project_root),
            feature=feature,
            planning_dir=str(planning_dir),
            path=list(getattr(args, "gate_path", []) or ["src"]),
            profile=str(getattr(args, "gate_profile", "strict") or "strict"),
            output=None,
            fail_on_block=False,
        ),
    )
    gate_payload = _normalize_gate_status(gate_payload_raw)
    ship_rc, ship_payload = invoke_cli_handler(
        _cmd_ship_readiness,
        argparse.Namespace(
            project_root=str(project_root),
            feature=feature,
            planning_dir=str(planning_dir),
            eval_report_path=getattr(args, "eval_report_path", None),
            auto_eval=bool(getattr(args, "auto_eval", False)),
            risk_profile=str(getattr(args, "risk_profile", "medium") or "medium"),
            output=None,
            fail_on_block=False,
        ),
    )
    changelog_path = _write_changelog(
        planning_dir=planning_dir,
        feature=feature,
        review_payload=None,
        ship_payload=ship_payload,
    )
    status = _combined_status(qa_payload, gate_payload, ship_payload)
    payload = dict(ship_payload)
    payload.update(
        {
            "_rc": 0 if qa_rc == 0 and gate_rc == 0 and ship_rc == 0 and status == "PASS" else int(ship_rc or gate_rc or qa_rc or 2),
            "status": status,
            "entrypoint": "kodawari release",
            "canonical_command": "kodawari qa + kodawari gate + kodawari ship-readiness",
            "feature": feature,
            "planning_dir": str(planning_dir),
            "qa": dict(qa_payload),
            "gate": dict(gate_payload),
            "artifacts": {
                **dict(ship_payload.get("artifacts") or {}),
                "CHANGELOG.md": str(changelog_path),
            },
            "steps": [
                legacy_step_result(name="qa", rc=qa_rc, payload=qa_payload),
                legacy_step_result(name="gate", rc=gate_rc, payload=gate_payload),
                legacy_step_result(name="ship-readiness", rc=ship_rc, payload=ship_payload),
            ],
            "provenance": _build_cli_provenance(
                command="release",
                project_root=project_root,
                planning_dir=planning_dir,
            ),
        }
    )
    if status != "PASS" and not str(payload.get("blocking_reason") or "").strip():
        payload["blocking_reason"] = str(
            ship_payload.get("blocking_reason")
            or gate_payload.get("blocking_reason")
            or qa_payload.get("blocking_reason")
            or ship_payload.get("summary")
            or gate_payload.get("summary")
            or qa_payload.get("summary")
            or "release facade blocked"
        )
    return _emit(payload, output=getattr(args, "output", None))


__all__ = ["run_release_command"]

