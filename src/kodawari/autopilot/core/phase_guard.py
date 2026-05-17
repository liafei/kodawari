"""Phase capability guards for contract-first execution."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


VALID_PHASE_MODE = {"analyze", "implement"}
VALID_CONTRACT_MODE = {"off", "warn", "strict"}


def _clean_text(value: Any, default: str = "") -> str:
    text = str(value or "").strip()
    return text if text else default


def _normalized_mode(value: Any, *, valid: set[str], default: str) -> str:
    resolved = _clean_text(value, default=default).lower()
    return resolved if resolved in valid else default


def normalize_contract_mode(value: Any) -> str:
    return _normalized_mode(value, valid=VALID_CONTRACT_MODE, default="off")


def normalize_phase_mode(value: Any) -> str:
    return _normalized_mode(value, valid=VALID_PHASE_MODE, default="implement")


def _string_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    return [str(item).strip() for item in values if str(item).strip()]


@dataclass
class PhaseGuardResult:
    blocked: bool
    status: str
    reason: str
    warnings: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "blocked": bool(self.blocked),
            "status": str(self.status),
            "reason": str(self.reason),
            "warnings": list(self.warnings),
        }


def _task_card_files(task_card: dict[str, Any] | None) -> list[str]:
    if not isinstance(task_card, dict):
        return []
    files = _string_list(task_card.get("files_to_change"))
    return [item.replace("\\", "/") for item in files]


def _path_allowed(path: str, allowed_files: list[str]) -> bool:
    normalized = str(path or "").strip().replace("\\", "/")
    if not normalized:
        return True
    for allowed in allowed_files:
        scope = str(allowed or "").strip().replace("\\", "/")
        if not scope:
            continue
        if normalized == scope:
            return True
        if normalized.startswith(scope + "/"):
            return True
    if normalized.startswith("tests/"):
        return True
    return False


def pre_implement_guard(
    *,
    phase_mode: str,
    contract_mode: str,
    task_card: dict[str, Any] | None,
) -> PhaseGuardResult:
    phase = normalize_phase_mode(phase_mode)
    mode = normalize_contract_mode(contract_mode)
    warnings: list[str] = []
    if phase == "analyze":
        reason = "phase_mode=analyze does not allow code changes."
        return PhaseGuardResult(blocked=True, status="FAIL", reason=reason, warnings=warnings)
    if phase == "implement" and mode in {"warn", "strict"} and not isinstance(task_card, dict):
        reason = "phase_mode=implement requires an active task card in contract-first mode."
        if mode == "strict":
            return PhaseGuardResult(blocked=True, status="FAIL", reason=reason, warnings=warnings)
        warnings.append(reason)
        return PhaseGuardResult(blocked=False, status="WARN", reason=reason, warnings=warnings)
    return PhaseGuardResult(blocked=False, status="PASS", reason="", warnings=warnings)


def scope_guard(
    *,
    changed_files: list[str],
    task_card: dict[str, Any] | None,
    strict_scope: bool,
    contract_mode: str,
) -> PhaseGuardResult:
    mode = normalize_contract_mode(contract_mode)
    warnings: list[str] = []
    allowed_files = _task_card_files(task_card)
    if not allowed_files:
        return PhaseGuardResult(blocked=False, status="PASS", reason="", warnings=warnings)
    out_of_scope = [path for path in changed_files if not _path_allowed(path, allowed_files)]
    if not out_of_scope:
        return PhaseGuardResult(blocked=False, status="PASS", reason="", warnings=warnings)
    reason = f"scope drift detected: {out_of_scope}"
    blocked = bool(strict_scope or mode == "strict")
    if not blocked:
        warnings.append(reason)
    return PhaseGuardResult(
        blocked=blocked,
        status="FAIL" if blocked else "WARN",
        reason=reason,
        warnings=warnings,
    )


def dirty_core_guard(
    *,
    core_dirty_files: list[str],
    contract_mode: str,
) -> PhaseGuardResult:
    mode = normalize_contract_mode(contract_mode)
    warnings: list[str] = []
    dirty = [str(item).strip().replace("\\", "/") for item in core_dirty_files if str(item).strip()]
    if not dirty:
        return PhaseGuardResult(blocked=False, status="PASS", reason="", warnings=warnings)
    reason = f"pre-existing dirty core files detected: {dirty}"
    blocked = mode == "strict"
    if not blocked:
        warnings.append(reason)
    return PhaseGuardResult(
        blocked=blocked,
        status="FAIL" if blocked else "WARN",
        reason=reason,
        warnings=warnings,
    )


def load_task_card(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    if not path.exists():
        return None
    import json

    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def guard_pre_implement(
    *,
    phase_mode: str,
    contract_mode: str | None = None,
    contract_first_mode: str | None = None,
    task_card: dict[str, Any] | None = None,
    task_card_path: Path | None = None,
    task_card_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    resolved_task_card = task_card_payload if isinstance(task_card_payload, dict) else task_card
    if resolved_task_card is None and task_card_path is not None:
        resolved_task_card = load_task_card(task_card_path)
    return pre_implement_guard(
        phase_mode=phase_mode,
        contract_mode=str(contract_mode or contract_first_mode or "off"),
        task_card=resolved_task_card,
    ).to_dict()
