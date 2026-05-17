"""Canonical repo-inventory artifact helpers for generic planning."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kodawari.project_model import build_repo_inventory_payload


SCHEMA_VERSION = "contract_first.repo_inventory.v1"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_repo_inventory(
    *,
    project_root: Path,
    archetype: str = "auto",
    capabilities: list[str] | None = None,
    mode: str = "existing",
) -> dict[str, Any]:
    payload = build_repo_inventory_payload(
        project_root=project_root,
        archetype=archetype,
        capabilities=capabilities,
        mode=mode,
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now_iso(),
        **payload,
    }


def render_repo_inventory_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Repo Inventory",
        "",
        f"- archetype: {payload.get('archetype', '')}",
        f"- capabilities: {', '.join(payload.get('capabilities') or [])}",
        f"- package_managers: {', '.join(payload.get('package_managers') or [])}",
        "",
        "## Surfaces",
    ]
    surfaces = [dict(item) for item in list(payload.get("surfaces") or []) if isinstance(item, dict)]
    if surfaces:
        for item in surfaces:
            lines.append(
                f"- {item.get('name', '')}: roots={', '.join(item.get('roots') or [])}; "
                f"verify={item.get('verify_command', '') or 'missing'}"
            )
    else:
        lines.append("- (none)")
    return "\n".join(lines) + "\n"
