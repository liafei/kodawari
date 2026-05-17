"""Shared source-of-truth semantics used by planning and gate checks."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable
from typing import Any

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    yaml = None


_ROOT_ALIASES = {
    "db": {"db", "db.primary", "db.default", "db.main"},
    "cache": {"cache", "cache.primary", "cache.default"},
}
_NAMESPACE_PREFIXES = ("db", "cache", "queue", "event", "storage", "kv")


def _normalize_text(value: object) -> str:
    text = str(value or "").strip().replace("\\", "/").lower()
    while text.startswith("./"):
        text = text[2:]
    return text


def canonical_source_entity(value: object) -> str:
    text = _normalize_text(value)
    if not text:
        return ""
    if text.endswith(".*"):
        text = text[:-2]
    for prefix in _NAMESPACE_PREFIXES:
        marker = f"{prefix}."
        if not text.startswith(marker):
            continue
        remainder = text[len(marker):]
        parts = [part for part in remainder.split(".") if part]
        if not parts:
            return prefix
        # Preserve root aliases like db.primary as-is.
        if prefix in _ROOT_ALIASES and f"{prefix}.{parts[0]}" in _ROOT_ALIASES[prefix]:
            return f"{prefix}.{parts[0]}"
        return f"{prefix}.{parts[0]}"
    if "." in text:
        first = text.split(".", 1)[0]
        return f"db.{first}"
    return f"db.{text}"


def canonicalize_source_of_truth(values: Iterable[object]) -> list[str]:
    canonical: list[str] = []
    seen: set[str] = set()
    for item in values:
        normalized = canonical_source_entity(item)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        canonical.append(normalized)
    return canonical


def _normalize_semantic_label(value: object) -> str:
    text = _normalize_text(value)
    return " ".join(text.split())


def _load_ownership_payload(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return {}

    stripped = text.strip()
    if not stripped:
        return {}

    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        if yaml is None:
            return {}
        try:
            payload = yaml.safe_load(stripped)
        except Exception:
            return {}
    if not isinstance(payload, dict):
        return {}
    return payload


def _iter_ownership_modules(payload: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    modules = payload.get("modules")
    if isinstance(modules, dict):
        items: list[tuple[str, dict[str, Any]]] = []
        for module_name, module_payload in modules.items():
            if isinstance(module_payload, dict):
                items.append((str(module_name).strip(), dict(module_payload)))
        return items
    if isinstance(modules, list):
        items = []
        for module_payload in modules:
            if not isinstance(module_payload, dict):
                continue
            module_name = str(
                module_payload.get("name")
                or module_payload.get("module")
                or module_payload.get("path")
                or ""
            ).strip()
            if module_name:
                items.append((module_name, dict(module_payload)))
        return items
    return []


def _module_name_from_payload(module_name: str, module_payload: dict[str, Any]) -> str:
    name = str(module_name or "").strip()
    if name:
        return name
    path = str(module_payload.get("path") or "").strip().replace("\\", "/")
    if not path:
        return ""
    return Path(path).stem


def _canonical_for_values(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, dict):
        for key in ("semantic", "name", "value", "label"):
            candidate = raw.get(key)
            if str(candidate or "").strip():
                return [str(candidate)]
        return []
    if not isinstance(raw, list):
        return [str(raw)]

    values: list[str] = []
    for item in raw:
        if isinstance(item, dict):
            for key in ("semantic", "name", "value", "label"):
                candidate = item.get(key)
                if str(candidate or "").strip():
                    values.append(str(candidate))
                    break
            continue
        text = str(item).strip()
        if text:
            values.append(text)
    return values


def load_domain_source_of_truth(ownership_path: Path) -> dict[str, str]:
    payload = _load_ownership_payload(ownership_path)
    if not payload:
        return {}

    mapping: dict[str, str] = {}
    for module_name, module_payload in _iter_ownership_modules(payload):
        resolved_module = _module_name_from_payload(module_name, module_payload)
        if not resolved_module:
            continue
        for semantic in _canonical_for_values(module_payload.get("canonical_for")):
            normalized = _normalize_semantic_label(semantic)
            if not normalized or normalized in mapping:
                continue
            mapping[normalized] = resolved_module
    return mapping


def build_contract_coverage_hints(
    *,
    layers: Iterable[str],
    path_type: str,
    source_of_truth_canonical: Iterable[str],
) -> list[str]:
    hints: list[str] = []
    for raw in [f"path:{_normalize_text(path_type)}", *[f"layer:{_normalize_text(layer)}" for layer in layers]]:
        text = _normalize_text(raw)
        if text and text not in hints:
            hints.append(text)
    for item in source_of_truth_canonical:
        text = _normalize_text(item)
        if text:
            hint = f"sot:{text}"
            if hint not in hints:
                hints.append(hint)
    return hints


def source_of_truth_allows_target(
    *,
    target: str,
    declared_raw: Iterable[object],
    declared_canonical: Iterable[object] | None = None,
) -> bool:
    normalized_target = _normalize_text(target)
    if not normalized_target:
        return False

    raw_set = {_normalize_text(item) for item in declared_raw if _normalize_text(item)}
    canonical_set = {
        _normalize_text(item)
        for item in (declared_canonical or canonicalize_source_of_truth(declared_raw))
        if _normalize_text(item)
    }
    declared = raw_set | canonical_set

    if normalized_target in declared:
        return True
    for root, aliases in _ROOT_ALIASES.items():
        if normalized_target.startswith(f"{root}.") and declared.intersection(aliases):
            return True
    for item in declared:
        if item.endswith(".*") and normalized_target.startswith(item[:-1]):
            return True
    return False
