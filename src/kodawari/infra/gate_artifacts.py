"""Gate artifact writer — canonical location for gate result persistence.

Owns ``write_gate_artifacts`` and ``_render_gate_markdown`` so that
``kodawari.autopilot.*`` can persist gate results without transitively
pulling ``kodawari.cli.*`` at import time.

The optional side-effect step (``sync_gate_side_effects``) still lives in
``cli/gate_state_sync.py`` because it reaches into workflow_chain/state
artifacts that are CLI-owned. It is imported lazily — callers that pass
``sync_side_effects=False`` (autopilot engine does) never load the CLI edge.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from kodawari.infra.contract_version import MERGED_CONTRACT_VERSION
from kodawari.infra.io_atomic import atomic_write_json, atomic_write_text


def _render_gate_markdown(payload: dict[str, Any], planning_dir: Path) -> str:
    profile = payload.get("profile") if isinstance(payload.get("profile"), dict) else {}
    rows = [
        f"# GATE ({planning_dir.name})",
        "",
        f"- contract_version: {MERGED_CONTRACT_VERSION}",
        f"- entrypoint: kodawari gate",
        f"- total_status: {payload.get('total_status', 'UNKNOWN')}",
        f"- profile: {profile.get('name', '-')}",
        f"- mode: {profile.get('mode', '-')}",
        f"- scanned_files: {payload.get('scanned_files', 0)}",
        f"- total_violations: {payload.get('total_violations', 0)}",
        f"- blocking_violations: {payload.get('blocking_violations', 0)}",
        "",
        "## Items",
        "",
        "| checker | status | checked_files | violations |",
        "|---|---|---:|---:|",
    ]
    for item in payload.get("items", []) if isinstance(payload.get("items"), list) else []:
        rows.append(
            f"| {item.get('checker', '-')} | {item.get('status', '-')} | {item.get('checked_files', 0)} | {item.get('violation_count', 0)} |"
        )
    ratchet = payload.get("ratchet")
    if isinstance(ratchet, dict):
        rows.extend(
            [
                "",
                "## Ratchet",
                "",
                f"- status: {ratchet.get('status', 'UNKNOWN')}",
                f"- regressions: {ratchet.get('regression_count', 0)}",
                f"- improvements: {ratchet.get('improvement_count', 0)}",
            ]
        )
        regressions = list(ratchet.get("regressions") or [])
        if regressions:
            rows.extend(["", "| metric | baseline | current | delta |", "|---|---:|---:|---:|"])
            for item in regressions:
                rows.append(
                    f"| {item.get('metric', '-')} | {item.get('baseline', 0)} | {item.get('current', 0)} | {item.get('delta', 0)} |"
                )
    return "\n".join(rows).strip() + "\n"


def write_gate_artifacts(
    payload: dict[str, Any],
    planning_dir: Path,
    *,
    sync_side_effects: bool = True,
) -> dict[str, Any]:
    planning_dir.mkdir(parents=True, exist_ok=True)
    gate_json_path = (planning_dir / ".gate_result.json").resolve()
    gate_md_path = (planning_dir / "GATE.md").resolve()
    atomic_write_json(gate_json_path, payload)
    atomic_write_text(gate_md_path, _render_gate_markdown(payload, planning_dir))
    if sync_side_effects:
        # Lazy import: only pulls the cli/ edge when a CLI caller requests it.
        from kodawari.cli.gate_state_sync import sync_gate_side_effects

        sync_gate_side_effects(planning_dir, payload)
    payload["planning_dir"] = str(planning_dir)
    payload["gate_artifacts"] = {
        ".gate_result.json": str(gate_json_path),
        "GATE.md": str(gate_md_path),
    }
    return payload


__all__ = ["write_gate_artifacts", "_render_gate_markdown"]
