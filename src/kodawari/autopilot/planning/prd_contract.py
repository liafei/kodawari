"""Contract-first PRD intake helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any

from kodawari.source_of_truth import (
    build_contract_coverage_hints,
    canonicalize_source_of_truth,
)


PRD_INTAKE_SCHEMA_VERSION = "contract_first.prd_intake.v1"
PATH_TYPES = {"read", "write", "both"}
LAYER_ORDER = ("schema", "repository", "service", "route", "frontend", "model", "util")
DEFAULT_SOURCE_OF_TRUTH = ["db.primary"]
DEFAULT_LAYERS = ["service", "repository", "route"]
LAYER_ALIASES = {
    "schema": "schema",
    "db": "schema",
    "database": "schema",
    "repository": "repository",
    "repo": "repository",
    "data layer": "repository",
    "service": "service",
    "route": "route",
    "router": "route",
    "api": "route",
    "frontend": "frontend",
    "ui": "frontend",
    "model": "model",
    "util": "util",
}
SECTION_SYNONYMS = {
    "business_outcome": (
        "business outcome",
        "goal",
        "goals",
        "业务结果",
        "目标",
    ),
    "source_of_truth": (
        "source of truth",
        "sot",
        "真实数据源",
        "真值",
    ),
    "flow_type": (
        "flow type",
        "flow",
        "流程类型",
        "流程",
    ),
    "layers": (
        "layer ownership",
        "layers",
        "layer",
        "层级归属",
        "层",
    ),
    "out_of_scope": (
        "out of scope",
        "non-goal",
        "non-goals",
        "不做什么",
        "不做",
        "非目标",
    ),
}
NEGATIVE_OUTCOME_PREFIXES = (
    "do not",
    "don't",
    "must not",
    "should not",
    "no ",
    "not ",
    "不要",
    "不做",
    "不允许",
    "不能",
    "不可",
    "无需",
)
NEGATIVE_LAYER_TOKENS = (
    "不做",
    "不需要",
    "无需",
    "not needed",
    "not required",
    "no change",
    "本轮不做",
    "不改",
)
POSITIVE_LAYER_TOKENS = (
    "需要",
    "需",
    "新增",
    "补",
    "暴露",
    "添加",
    "更新",
    "改",
    "复用",
    "implement",
    "change",
    "update",
    "add",
    "reuse",
    "expose",
)
STRONG_POSITIVE_LAYER_TOKENS = (
    "新增",
    "补",
    "暴露",
    "添加",
    "更新",
    "implement",
    "update",
    "add",
    "reuse",
    "expose",
)
SOT_IDENTIFIER_RE = re.compile(
    r"\b(?:db|cache|kv|queue|event|storage)\.[A-Za-z0-9_./-]+\b"
    r"|\b[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_=/:-]+)+\b"
)


@dataclass(frozen=True)
class ValidationIssue:
    field: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"field": self.field, "message": self.message}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_text(value: Any, default: str = "") -> str:
    text = str(value or "").replace("\ufeff", "").strip()
    return text if text else default


def _string_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    normalized: list[str] = []
    for item in values:
        text = _clean_text(item)
        if text:
            normalized.append(text)
    return normalized


def _normalized_heading_text(line: str) -> str:
    text = _clean_text(line)
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text)
    text = re.sub(r"^\d+\s*[.)、．]?\s*", "", text)
    text = re.sub(r"[（(].*?[）)]", "", text)
    return text.strip(" :-：").lower()


def _section_key(line: str) -> str | None:
    normalized = _normalized_heading_text(line)
    if not normalized:
        return None
    for key, aliases in SECTION_SYNONYMS.items():
        for alias in aliases:
            if not normalized.startswith(alias):
                continue
            suffix = normalized[len(alias):]
            if not suffix or suffix[0] in {" ", ":", "：", "/", "-", "|"}:
                return key
    return None


def _strip_list_marker(line: str) -> str:
    text = _clean_text(line)
    text = re.sub(r"^\s*[-*]\s*", "", text)
    return text.strip()


def _extract_sections(prd_text: str) -> dict[str, list[str]]:
    sections = {key: [] for key in SECTION_SYNONYMS}
    current: str | None = None
    for raw in prd_text.splitlines():
        line = _clean_text(raw)
        if not line:
            continue
        key = _section_key(line)
        if key is not None:
            current = key
            inline_parts = re.split(r":|：", line, maxsplit=1)
            if len(inline_parts) == 2:
                detail = _strip_list_marker(inline_parts[1])
                if detail:
                    sections[key].append(detail)
            continue
        if current is None:
            continue
        sections[current].append(_strip_list_marker(line))
    return sections


def _is_section_header_line(line: str) -> bool:
    return _section_key(line) is not None


def _content_lines(prd_text: str) -> list[str]:
    lines: list[str] = []
    for raw in prd_text.splitlines():
        line = _clean_text(raw)
        if not line:
            continue
        if _is_section_header_line(line):
            continue
        lowered = line.lower()
        if lowered.startswith("source of truth") or lowered.startswith("sot:"):
            continue
        lines.append(_strip_list_marker(line))
    return lines


def _contains_any(text: str, tokens: tuple[str, ...]) -> bool:
    lowered = str(text or "").strip().lower()
    return any(token in lowered for token in tokens)


def _looks_negative_outcome(text: str) -> bool:
    lowered = str(text or "").strip().lower()
    return any(lowered.startswith(token) for token in NEGATIVE_OUTCOME_PREFIXES)


def _business_outcome(prd_text: str, sections: dict[str, list[str]]) -> str:
    preferred_lines = [
        line
        for line in sections.get("business_outcome", []) + sections.get("flow_type", [])
        if line and not _looks_negative_outcome(line)
    ]
    if preferred_lines:
        selected = preferred_lines[0]
        return selected if len(selected) <= 180 else selected[:177] + "..."

    content_lines = _content_lines(prd_text)
    preferred = [
        line
        for line in content_lines
        if not _looks_negative_outcome(line)
        and any(
            token in line.lower()
            for token in ("allow", "support", "enable", "return", "provide", "实现", "支持", "提供", "允许")
        )
    ]
    selected = preferred[0] if preferred else (content_lines[0] if content_lines else "")
    if not selected:
        return "Deliver the requested feature with clear source-of-truth and layered ownership."
    return selected if len(selected) <= 180 else selected[:177] + "..."


def _identifier_candidates(text: str) -> list[str]:
    matches = [str(item).strip(" ,.;。；、") for item in SOT_IDENTIFIER_RE.findall(text)]
    deduped: list[str] = []
    for item in matches:
        if item and item not in deduped:
            deduped.append(item)
    return deduped


def _source_of_truth(prd_text: str, sections: dict[str, list[str]]) -> list[str]:
    section_lines = sections.get("source_of_truth", [])
    matches: list[str] = []
    for line in section_lines:
        for item in _identifier_candidates(line):
            if item not in matches:
                matches.append(item)
    if matches:
        return matches

    body_matches: list[str] = []
    for item in _identifier_candidates(prd_text):
        if item not in body_matches:
            body_matches.append(item)
    if body_matches:
        return body_matches
    return list(DEFAULT_SOURCE_OF_TRUTH)


def _path_type(prd_text: str, sections: dict[str, list[str]]) -> str:
    text = "\n".join(sections.get("flow_type", [])) or prd_text
    lowered = text.lower()
    read_markers = ("read path", "read", "query", "list", "fetch", "display", "读取", "查询", "展示")
    write_markers = ("write path", "write", "create", "update", "delete", "save", "写入", "新增", "更新", "删除")
    has_read = any(token in lowered for token in read_markers)
    has_write = any(token in lowered for token in write_markers)
    if has_read and has_write:
        return "both"
    if has_write:
        return "write"
    return "read"


def _layers_from_section(section_lines: list[str]) -> list[str]:
    found: list[str] = []
    for line in section_lines:
        lowered = line.lower()
        negative = _contains_any(lowered, NEGATIVE_LAYER_TOKENS)
        strong_positive = _contains_any(lowered, STRONG_POSITIVE_LAYER_TOKENS)
        for token, normalized in LAYER_ALIASES.items():
            if token not in lowered:
                continue
            if negative and not strong_positive:
                continue
            if normalized not in found:
                found.append(normalized)
    return found


def _layers(prd_text: str, sections: dict[str, list[str]]) -> tuple[list[str], bool]:
    found = _layers_from_section(sections.get("layers", []))
    if not found:
        for raw in prd_text.splitlines():
            line = _clean_text(raw)
            lowered = line.lower()
            has_context = any(marker in lowered for marker in ("layer", "layers", "scope", "ownership", "层"))
            positive = _contains_any(lowered, POSITIVE_LAYER_TOKENS)
            negative = _contains_any(lowered, NEGATIVE_LAYER_TOKENS)
            strong_positive = _contains_any(lowered, STRONG_POSITIVE_LAYER_TOKENS)
            for token, normalized in LAYER_ALIASES.items():
                if token not in lowered:
                    continue
                if negative and not strong_positive:
                    continue
                if not has_context and not positive:
                    continue
                if normalized not in found:
                    found.append(normalized)
    used_default = False
    if not found:
        found = list(DEFAULT_LAYERS)
        used_default = True
    ordered: list[str] = []
    for layer in LAYER_ORDER:
        if layer in found:
            ordered.append(layer)
    return ordered, used_default


def _out_of_scope(prd_text: str, sections: dict[str, list[str]]) -> list[str]:
    values = [line for line in sections.get("out_of_scope", []) if line]
    if not values:
        lines = prd_text.splitlines()
        collecting = False
        for raw in lines:
            line = _clean_text(raw)
            if not line:
                if collecting:
                    collecting = False
                continue
            lowered = line.lower()
            marker = any(marker in lowered for marker in SECTION_SYNONYMS["out_of_scope"])
            if marker:
                collecting = True
                inline = re.split(r":|：", line, maxsplit=1)
                detail = _clean_text(inline[1]) if len(inline) == 2 else ""
                if detail:
                    values.append(detail)
                continue
            if collecting and not _is_section_header_line(line):
                detail = _strip_list_marker(line)
                if detail:
                    values.append(detail)
                continue
            if collecting and _is_section_header_line(line):
                collecting = False
    deduped: list[str] = []
    for item in values:
        if item not in deduped:
            deduped.append(item)
    return deduped


def _confidence_issues(*, business_outcome: str, source_of_truth: list[str], used_default_layers: bool) -> list[str]:
    issues: list[str] = []
    if source_of_truth == DEFAULT_SOURCE_OF_TRUTH:
        issues.append("source_of_truth fell back to default value db.primary.")
    if used_default_layers:
        issues.append("layers fell back to default service/repository/route set.")
    if _looks_negative_outcome(business_outcome):
        issues.append("business_outcome looks like a non-goal or negative invariant.")
    return issues


def build_prd_intake(prd_text: str, *, feature: str = "") -> dict[str, Any]:
    sections = _extract_sections(prd_text)
    business_outcome = _business_outcome(prd_text, sections)
    source_of_truth = _source_of_truth(prd_text, sections)
    source_of_truth_canonical = canonicalize_source_of_truth(source_of_truth)
    layers, used_default_layers = _layers(prd_text, sections)
    path_type = _path_type(prd_text, sections)
    slices = extract_prd_slices(prd_text)
    payload = {
        "schema_version": PRD_INTAKE_SCHEMA_VERSION,
        "generated_at": _utc_now_iso(),
        "feature": _clean_text(feature),
        "business_outcome": business_outcome,
        "source_of_truth": source_of_truth,
        "source_of_truth_canonical": source_of_truth_canonical,
        "path_type": path_type,
        "layers": layers,
        "coverage_hints": build_contract_coverage_hints(
            layers=layers,
            path_type=path_type,
            source_of_truth_canonical=source_of_truth_canonical,
        ),
        "out_of_scope": _out_of_scope(prd_text, sections),
        "slices": slices,
    }
    issues = _confidence_issues(
        business_outcome=business_outcome,
        source_of_truth=source_of_truth,
        used_default_layers=used_default_layers,
    )
    payload["confidence"] = "low" if issues else "high"
    payload["confidence_issues"] = issues
    return payload


# ---------------------------------------------------------------------------
# Multi-slice PRD detection (Stage E1: epic replan)
# ---------------------------------------------------------------------------

# Matches H2 headings of the form "## Slice 1: title" / "## 切片 1: title" /
# "## Phase 1: title" / "## Part 1: title". The slice index must be numeric
# and the colon (full-width or ASCII) is required so we don't mis-detect
# section headings like "## Slice options" or "## 切片说明".
_SLICE_HEADING_RE = re.compile(
    r"^##\s+(?:Slice|Phase|Part|切片|阶段|部分)\s*(\d+)\s*[:：]\s*(.+?)\s*$",
    re.MULTILINE,
)


def extract_prd_slices(prd_text: str) -> list[dict[str, Any]]:
    """Extract multi-slice markers from a PRD.

    A multi-slice PRD declares discrete shipping units via H2 headings:
    ``## Slice 1: <title>`` or ``## 切片 1: <title>``. When present, kodawari
    work-all iterates the slices in order — running plan + work for each one
    independently — instead of treating the whole PRD as a single unit.

    Returns ``[]`` when the PRD has zero or one slice marker (single-slice
    mode, the historical default). Slice indices declared in the PRD are
    preserved as-is, but the returned list is sorted by appearance order so
    a misnumbered PRD (e.g. "Slice 2" before "Slice 1") still gets the
    document's intent.
    """
    matches = list(_SLICE_HEADING_RE.finditer(prd_text))
    if len(matches) < 2:
        return []
    slices: list[dict[str, Any]] = []
    for position, match in enumerate(matches):
        start = match.end()
        end = matches[position + 1].start() if position + 1 < len(matches) else len(prd_text)
        body = prd_text[start:end].strip("\n")
        declared_index = int(match.group(1))
        title = _clean_text(match.group(2))
        slices.append({
            "position": position,
            "declared_index": declared_index,
            "title": title,
            "content": body,
        })
    return slices


def validate_prd_intake(payload: dict[str, Any]) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    outcome = _clean_text(payload.get("business_outcome"))
    if not outcome:
        issues.append(ValidationIssue(field="business_outcome", message="business_outcome is required"))
    path_type = _clean_text(payload.get("path_type")).lower()
    if path_type not in PATH_TYPES:
        issues.append(ValidationIssue(field="path_type", message=f"path_type must be one of {sorted(PATH_TYPES)}"))
    sot = _string_list(payload.get("source_of_truth"))
    if not sot:
        issues.append(ValidationIssue(field="source_of_truth", message="source_of_truth must contain at least one item"))
    canonical_sot = _string_list(payload.get("source_of_truth_canonical"))
    if not canonical_sot:
        issues.append(ValidationIssue(field="source_of_truth_canonical", message="source_of_truth_canonical must contain at least one item"))
    layers = _string_list(payload.get("layers"))
    if not layers:
        issues.append(ValidationIssue(field="layers", message="layers must contain at least one item"))
    coverage_hints = _string_list(payload.get("coverage_hints"))
    if not coverage_hints:
        issues.append(ValidationIssue(field="coverage_hints", message="coverage_hints must contain at least one item"))
    confidence = _clean_text(payload.get("confidence"), default="high").lower()
    if confidence not in {"high", "low"}:
        issues.append(ValidationIssue(field="confidence", message="confidence must be high or low"))
    return issues


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected object JSON in {path}")
    return payload


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _normalize_hint(value: Any) -> str:
    return _clean_text(value).lower()


def _task_coverage_hints(task: dict[str, Any]) -> set[str]:
    hints = {_normalize_hint(item) for item in _string_list(task.get("coverage_hints")) if _normalize_hint(item)}
    layer = _clean_text(task.get("layer_owner")).lower()
    if layer:
        hints.add(f"layer:{layer}")
    return hints


def _task_invariant_corpus(task: dict[str, Any]) -> str:
    parts = [
        _clean_text(task.get("task_name")),
        _clean_text(task.get("layer_owner")),
        _clean_text(task.get("test_proof")),
        " ".join(_string_list(task.get("invariants"))),
    ]
    return " ".join(part for part in parts if part).lower()


def _task_has_non_test_source(task: dict[str, Any]) -> bool:
    for raw in _string_list(task.get("core_files")):
        normalized = raw.replace("\\", "/").lower()
        name = Path(normalized).name
        if normalized.startswith("tests/") or "/tests/" in normalized or name.startswith("test_"):
            continue
        return True
    return False


def _lexical_coverage_fallback(tasks: list[dict[str, Any]], business_outcome: str) -> dict[str, Any]:
    text = " ".join(_task_invariant_corpus(task) for task in tasks if isinstance(task, dict))
    tokens = [item for item in re.split(r"[^a-z0-9_]+", business_outcome.lower()) if len(item) > 2]
    if not tokens:
        return {"status": "WARN", "details": "business_outcome has no analyzable tokens and structured coverage signals are unavailable."}
    matched = [token for token in tokens if token in text]
    if matched:
        return {"status": "PASS", "details": f"Structured coverage unavailable; lexical fallback matched outcome tokens: {matched[:6]}"}
    return {"status": "WARN", "details": "Structured coverage unavailable and lexical overlap with business_outcome is weak."}


def _canonical_sot_for_prd(prd_intake: dict[str, Any]) -> list[str]:
    return _string_list(prd_intake.get("source_of_truth_canonical")) or canonicalize_source_of_truth(
        _string_list(prd_intake.get("source_of_truth"))
    )


def _task_layer_set(tasks: list[dict[str, Any]]) -> set[str]:
    return {
        _clean_text(task.get("layer_owner")).lower()
        for task in tasks
        if isinstance(task, dict) and _clean_text(task.get("layer_owner"))
    }


def _has_non_test_source_any(tasks: list[dict[str, Any]]) -> bool:
    return any(_task_has_non_test_source(task) for task in tasks if isinstance(task, dict))


def _task_hints_corpus(tasks: list[dict[str, Any]]) -> tuple[set[str], str]:
    hints: set[str] = set()
    corpus = ""
    for task in tasks:
        if not isinstance(task, dict):
            continue
        hints.update(_task_coverage_hints(task))
        corpus = f"{corpus} {_task_invariant_corpus(task)}".strip()
    return hints, corpus


def _find_missing_entities(
    canonical_sot: list[str], task_hints: set[str], invariant_corpus: str
) -> list[str]:
    missing: list[str] = []
    for entity in canonical_sot:
        normalized = _normalize_hint(entity)
        stripped = normalized.removeprefix("db.")
        if f"sot:{normalized}" in task_hints or normalized in invariant_corpus or stripped in invariant_corpus:
            continue
        missing.append(entity)
    return missing


def _prd_coverage_issues(
    path_type: str,
    missing_layers: list[str],
    has_non_test_source: bool,
    missing_entities: list[str],
) -> list[str]:
    issues: list[str] = []
    if missing_layers:
        issues.append(f"missing layer coverage for {missing_layers}")
    if path_type in {"write", "both"} and not has_non_test_source:
        issues.append("write-path PRD has no non-test core source file coverage")
    if missing_entities:
        issues.append(f"missing source_of_truth coverage for {missing_entities}")
    return issues


def _has_structured_axes(declared_layers: list[str], canonical_sot: list[str], path_type: str) -> bool:
    return bool(declared_layers or canonical_sot or path_type)


def _prd_coverage_pass_details(
    task_layers: set[str], declared_layers: list[str], path_type: str, canonical_sot: list[str]
) -> str:
    covered_layers = sorted(task_layers.intersection(set(declared_layers))) if declared_layers else sorted(task_layers)
    parts = [f"layers_covered={covered_layers}", f"path_type={path_type}"]
    if canonical_sot:
        parts.append(f"source_of_truth_covered={canonical_sot}")
    return "; ".join(parts)


def prd_coverage_check(*, tasks: list[dict[str, Any]], prd_intake: dict[str, Any]) -> dict[str, Any]:
    business_outcome = _clean_text(prd_intake.get("business_outcome"))
    if not business_outcome:
        return {"status": "FAIL", "details": "Missing business_outcome in PRD intake."}
    if not tasks:
        return {"status": "FAIL", "details": "Task graph is empty."}

    declared_layers = [item.lower() for item in _string_list(prd_intake.get("layers"))]
    canonical_sot = _canonical_sot_for_prd(prd_intake)
    path_type = _clean_text(prd_intake.get("path_type"), default="read").lower()

    task_layers = _task_layer_set(tasks)
    missing_layers = [layer for layer in declared_layers if layer not in task_layers]
    has_non_test_source = _has_non_test_source_any(tasks)
    task_hints, invariant_corpus = _task_hints_corpus(tasks)
    missing_entities = _find_missing_entities(canonical_sot, task_hints, invariant_corpus)

    if not _has_structured_axes(declared_layers, canonical_sot, path_type):
        return _lexical_coverage_fallback(tasks, business_outcome)

    issues = _prd_coverage_issues(path_type, missing_layers, has_non_test_source, missing_entities)
    if issues:
        return {"status": "FAIL", "details": "; ".join(issues)}

    return {"status": "PASS", "details": _prd_coverage_pass_details(task_layers, declared_layers, path_type, canonical_sot)}


def render_prd_intake_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# PRD Intake",
        "",
        f"- schema_version: {_clean_text(payload.get('schema_version'))}",
        f"- feature: {_clean_text(payload.get('feature'))}",
        f"- generated_at: {_clean_text(payload.get('generated_at'))}",
        f"- confidence: {_clean_text(payload.get('confidence'), default='high')}",
        "",
        "## Business Outcome",
        f"- {_clean_text(payload.get('business_outcome'))}",
        "",
        "## Source Of Truth",
    ]
    sot = _string_list(payload.get("source_of_truth"))
    if sot:
        lines.extend(f"- {item}" for item in sot)
    else:
        lines.append("- (none)")
    lines.extend(["", "## Canonical Source Of Truth"])
    canonical_sot = _string_list(payload.get("source_of_truth_canonical"))
    if canonical_sot:
        lines.extend(f"- {item}" for item in canonical_sot)
    else:
        lines.append("- (none)")
    lines.extend(["", "## Flow", f"- path_type: {_clean_text(payload.get('path_type'))}", "", "## Layers"])
    layers = _string_list(payload.get("layers"))
    if layers:
        lines.extend(f"- {item}" for item in layers)
    else:
        lines.append("- (none)")
    lines.extend(["", "## Coverage Hints"])
    coverage_hints = _string_list(payload.get("coverage_hints"))
    if coverage_hints:
        lines.extend(f"- {item}" for item in coverage_hints)
    else:
        lines.append("- (none)")
    lines.extend(["", "## Confidence Issues"])
    confidence_issues = _string_list(payload.get("confidence_issues"))
    if confidence_issues:
        lines.extend(f"- {item}" for item in confidence_issues)
    else:
        lines.append("- (none)")
    lines.extend(["", "## Out Of Scope"])
    out_of_scope = _string_list(payload.get("out_of_scope"))
    if out_of_scope:
        lines.extend(f"- {item}" for item in out_of_scope)
    else:
        lines.append("- (none)")
    return "\n".join(lines) + "\n"
