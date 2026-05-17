"""Optional prompt profile overlays for planner and executor roles."""

from __future__ import annotations

import logging
from pathlib import Path
import re
from typing import Any

logger = logging.getLogger(__name__)

_PROMPTS_YAML_REL = Path(".claude") / "workflow" / "prompts.yaml"


def _dedupe(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = str(raw or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _model_key(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^0-9a-z._+-]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text


def _configured_model_families(profiles: dict[str, Any], keys: list[str]) -> list[str]:
    raw = profiles.get("model_families")
    if not isinstance(raw, dict):
        return []
    out: list[str] = []
    for key in keys:
        value = raw.get(key)
        if isinstance(value, dict):
            value = value.get("families") or value.get("candidates") or value.get("aliases")
        if isinstance(value, str):
            out.append(value)
        elif isinstance(value, list):
            out.extend(str(item) for item in value)
    return _dedupe(out)


def _detected_family_candidates(*, model: str = "", transport_name: str = "", driver: str = "") -> list[str]:
    text = " ".join(str(item or "").lower() for item in (model, transport_name, driver))
    if "mimo" in text:
        return ["mimo", "fast_planner"]
    if "kimi" in text or "moonshot" in text:
        return ["kimi", "fast_planner"]
    if "glm" in text:
        return ["glm", "fast_planner"]
    if "deepseek" in text:
        return ["deepseek", "strict_reasoner"]
    if "gemini" in text:
        return ["gemini", "strict_reasoner"]
    if "gpt-5" in text or "gpt5" in text:
        return ["gpt-5.x", "gpt", "strict_reasoner"]
    if "claude" in text or "sonnet" in text or "opus" in text:
        return ["claude", "code_agent"]
    if "codex" in text:
        return ["codex", "code_agent", "gpt"]
    if "qwen" in text:
        return ["qwen", "code_agent"]
    if "gpt" in text:
        return ["gpt", "strict_reasoner"]
    return ["default"]


def model_family(*, model: str = "", transport_name: str = "", driver: str = "") -> str:
    for candidate in _detected_family_candidates(
        model=model,
        transport_name=transport_name,
        driver=driver,
    ):
        if candidate != "default":
            return candidate
    return "default"


def model_family_candidates(
    *,
    model: str = "",
    transport_name: str = "",
    driver: str = "",
    profiles: dict[str, Any] | None = None,
) -> list[str]:
    exact_keys = [_model_key(model)]
    exact_keys = [key for key in exact_keys if key]
    detected = _detected_family_candidates(
        model=model,
        transport_name=transport_name,
        driver=driver,
    )
    configured = _configured_model_families(dict(profiles or {}), [*exact_keys, *detected])
    return _dedupe([*exact_keys, *configured, *detected, "default"])


def _safe_load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        import yaml  # type: ignore[import]
    except ImportError:
        logger.debug("pyyaml not installed; cannot read prompts.yaml")
        return {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("prompts.yaml parse failed: %s", path, exc_info=True)
        return {}
    return dict(raw) if isinstance(raw, dict) else {}


def load_prompt_profiles(project_root: Path | str | None) -> dict[str, Any]:
    if project_root is None:
        return {}
    return _safe_load_yaml(Path(project_root) / _PROMPTS_YAML_REL)


def _profiles_section(raw: dict[str, Any]) -> dict[str, Any]:
    profiles = raw.get("profiles")
    return dict(profiles) if isinstance(profiles, dict) else dict(raw)


def _resolve_profile_file(project_root: Path, raw_path: Any) -> Path:
    path = Path(str(raw_path or ""))
    if path.is_absolute():
        return path
    workflow_dir = project_root / ".claude" / "workflow"
    workflow_candidate = workflow_dir / path
    if workflow_candidate.exists():
        return workflow_candidate
    return project_root / path


def _entry_text(project_root: Path, entry: Any) -> str:
    if isinstance(entry, str):
        return entry.strip()
    if not isinstance(entry, dict):
        return ""
    text = str(entry.get("text") or "").strip()
    if text:
        return text
    file_value = str(entry.get("file") or "").strip()
    if not file_value:
        return ""
    path = _resolve_profile_file(project_root, file_value)
    try:
        if path.is_file():
            return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return ""


def _role_section(profiles: dict[str, Any], role: str, key: str) -> Any:
    direct = profiles.get(f"{role}_{key}")
    if direct is not None:
        return direct
    role_payload = profiles.get(role)
    if isinstance(role_payload, dict):
        return role_payload.get(key)
    return None


def _overlay_entry(overlays: Any, family: str) -> Any:
    if not isinstance(overlays, dict):
        return None
    for candidate in [family, "default"]:
        if candidate in overlays:
            return overlays.get(candidate)
    return None


def _first_overlay_entry(overlays: Any, candidates: list[str]) -> tuple[str, Any]:
    if not isinstance(overlays, dict):
        return "", None
    for candidate in candidates:
        if candidate in overlays:
            return candidate, overlays.get(candidate)
    return "", None


def render_prompt_profile_text(
    *,
    project_root: Path | str | None,
    role: str,
    model: str = "",
    transport_name: str = "",
    driver: str = "",
) -> str:
    if project_root is None:
        return ""
    root = Path(project_root)
    raw = load_prompt_profiles(root)
    profiles = _profiles_section(raw)
    if not profiles:
        return ""
    candidates = model_family_candidates(
        model=model,
        transport_name=transport_name,
        driver=driver,
        profiles=profiles,
    )
    family, overlay = _first_overlay_entry(_role_section(profiles, role, "overlays"), candidates)
    if not family:
        family = model_family(model=model, transport_name=transport_name, driver=driver)
    chunks = [
        _entry_text(root, _role_section(profiles, role, "kernel")),
        _entry_text(root, overlay),
    ]
    rendered = [chunk for chunk in chunks if chunk]
    if not rendered:
        return ""
    return f"Prompt profile directives ({role}/{family}):\n" + "\n\n".join(rendered)


def render_learned_prompt_lesson_text(
    *,
    project_root: Path | str | None,
    role: str,
    model: str = "",
    transport_name: str = "",
    driver: str = "",
    limit: int = 5,
) -> str:
    if project_root is None:
        return ""
    raw = load_prompt_profiles(project_root)
    profiles = _profiles_section(raw)
    candidates = model_family_candidates(
        model=model,
        transport_name=transport_name,
        driver=driver,
        profiles=profiles,
    )
    try:
        from kodawari.instincts import render_prompt_lessons_for_prompt
    except Exception:
        logger.debug("prompt lesson renderer unavailable", exc_info=True)
        return ""
    try:
        return render_prompt_lessons_for_prompt(
            Path(project_root),
            role=role,
            family_candidates=candidates,
            limit=limit,
        )
    except Exception:
        logger.warning("learned prompt lesson rendering failed", exc_info=True)
        return ""


def nudge_policy_for_model(
    *,
    project_root: Path | str | None,
    model: str = "",
    transport_name: str = "",
    driver: str = "",
) -> dict[str, int]:
    if project_root is None:
        return {}
    raw = load_prompt_profiles(project_root)
    profiles = _profiles_section(raw)
    policies = profiles.get("nudge_policies")
    candidates = model_family_candidates(
        model=model,
        transport_name=transport_name,
        driver=driver,
        profiles=profiles,
    )
    parsed: dict[str, int] = {}
    if isinstance(policies, dict):
        for candidate in reversed(candidates):
            selected = policies.get(candidate)
            if not isinstance(selected, dict):
                continue
            for raw_key, raw_value in selected.items():
                key = str(raw_key or "").strip()
                if not key:
                    continue
                try:
                    value = int(raw_value)
                except (TypeError, ValueError):
                    continue
                if value > 0:
                    parsed[key] = value
    try:
        from kodawari.instincts import learned_prompt_lesson_nudge_policy
    except Exception:
        return parsed
    try:
        parsed.update(
            learned_prompt_lesson_nudge_policy(
                Path(project_root),
                family_candidates=candidates,
            )
        )
    except Exception:
        logger.warning("learned prompt lesson nudge policy failed", exc_info=True)
    return parsed


__all__ = [
    "load_prompt_profiles",
    "model_family",
    "model_family_candidates",
    "nudge_policy_for_model",
    "render_learned_prompt_lesson_text",
    "render_prompt_profile_text",
]
