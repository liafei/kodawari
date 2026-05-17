"""Planning source fingerprints for contract-first task graph reuse."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from typing import Any


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def sha256_text(value: str) -> str:
    return sha256(str(value or "").encode("utf-8", errors="replace")).hexdigest()


def sha256_file(path: Path | None) -> str:
    if path is None or not path.exists() or not path.is_file():
        return ""
    try:
        return sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def planning_source_contract(
    *,
    feature: str,
    prd_path: Path | None,
    task_direction: str,
) -> dict[str, Any]:
    prd_resolved = prd_path.resolve() if prd_path is not None and prd_path.exists() else None
    task_text = _clean_text(task_direction)
    payload = {
        "schema_version": "planning.source.v1",
        "feature": _clean_text(feature),
        "prd_path": str(prd_resolved) if prd_resolved is not None else "",
        "prd_sha256": sha256_file(prd_resolved),
        "task_direction_sha256": sha256_text(task_text),
        "task_direction": task_text,
        "has_task_direction": bool(task_text),
    }
    payload["source_fingerprint"] = "sha256:" + sha256_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
    return payload


def attach_planning_source(task_graph: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    payload = dict(task_graph)
    payload["planning_source"] = dict(source)
    return payload


def planning_source_status(
    task_graph: dict[str, Any],
    current_source: dict[str, Any],
) -> tuple[bool, str, dict[str, str]]:
    source = task_graph.get("planning_source")
    if not isinstance(source, dict) or not source:
        return True, "legacy_unknown", {}
    mismatches: dict[str, str] = {}
    current_prd_hash = _clean_text(current_source.get("prd_sha256"))
    stored_prd_hash = _clean_text(source.get("prd_sha256"))
    if current_prd_hash and stored_prd_hash and current_prd_hash != stored_prd_hash:
        mismatches["prd_sha256"] = "changed"
    current_task_hash = _clean_text(current_source.get("task_direction_sha256"))
    stored_task_hash = _clean_text(source.get("task_direction_sha256"))
    if bool(current_source.get("has_task_direction")) and stored_task_hash and current_task_hash != stored_task_hash:
        mismatches["task_direction_sha256"] = "changed"
    current_feature = _clean_text(current_source.get("feature"))
    stored_feature = _clean_text(source.get("feature"))
    if current_feature and stored_feature and current_feature != stored_feature:
        mismatches["feature"] = "changed"
    if mismatches:
        return False, "stale", mismatches
    return True, "fresh", {}


def task_direction_from_prd(prd_path: Path | None) -> str:
    if prd_path is None or not prd_path.exists():
        return ""
    try:
        text = prd_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            stripped = stripped.lstrip("#").strip()
        if stripped:
            return stripped
    return ""


__all__ = [
    "attach_planning_source",
    "planning_source_contract",
    "planning_source_status",
    "task_direction_from_prd",
]
