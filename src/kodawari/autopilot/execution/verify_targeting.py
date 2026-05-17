"""Resolve scoped verify targets from runtime context signals."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any


DEFAULT_VERIFY_CMD = "pytest -q"
_MAX_VERIFY_TARGETS = 5


def resolve_verify_targeting(
    *,
    project_root: Path,
    verify_cmd: str,
    changed_files: list[str],
    feature: str = "",
    task_label: str = "",
    instinct_hints: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    requested = _normalize_verify_cmd(verify_cmd)
    explicit_payload = _explicit_verify_targeting(requested)
    if explicit_payload is not None:
        return explicit_payload
    changed_targets = _changed_test_targets(project_root, changed_files)
    if changed_targets:
        narrowed = _task_granularity_payload(
            project_root=project_root,
            requested=requested,
            changed_targets=changed_targets,
            feature=feature,
            task_label=task_label,
        )
        if narrowed is not None:
            return narrowed
        return _changed_files_payload(requested, changed_targets)
    derived_targets = _derived_test_targets(project_root, changed_files)
    if derived_targets:
        narrowed = _task_granularity_payload(
            project_root=project_root,
            requested=requested,
            changed_targets=derived_targets,
            feature=feature,
            task_label=task_label,
        )
        if narrowed is not None:
            return narrowed
        return _derived_files_payload(requested, derived_targets)
    hint_patterns = _instinct_hint_patterns(instinct_hints or [])
    hint_targets = _instinct_hint_targets(project_root, instinct_hints or [])
    if hint_targets:
        return _instinct_hints_payload(requested, hint_targets, hint_patterns=hint_patterns)
    return _default_payload(requested)


def _normalize_verify_cmd(verify_cmd: str) -> str:
    cleaned = str(verify_cmd or "").strip()
    return cleaned or DEFAULT_VERIFY_CMD


def _explicit_verify_targeting(requested: str) -> dict[str, Any] | None:
    if requested == DEFAULT_VERIFY_CMD:
        return None
    return _targeting_payload(
        requested=requested,
        resolved=requested,
        source="explicit_command",
        targets=[],
    )


def _changed_files_payload(requested: str, changed_targets: list[str]) -> dict[str, Any]:
    return _targeting_payload(
        requested=requested,
        resolved=_scoped_pytest_cmd(changed_targets),
        source="changed_test_files",
        targets=changed_targets,
    )


def _derived_files_payload(requested: str, derived_targets: list[str]) -> dict[str, Any]:
    return _targeting_payload(
        requested=requested,
        resolved=_scoped_pytest_cmd(derived_targets),
        source="derived_test_files",
        targets=derived_targets,
    )


def _instinct_hints_payload(
    requested: str,
    hint_targets: list[str],
    *,
    hint_patterns: list[str],
) -> dict[str, Any]:
    payload = _targeting_payload(
        requested=requested,
        resolved=_scoped_pytest_cmd(hint_targets),
        source="instinct_hints",
        targets=hint_targets,
    )
    payload["instinct_patterns"] = list(hint_patterns)
    if hint_patterns:
        preview = ", ".join(hint_patterns[:3])
        payload["instinct_reason"] = f"verify targets inferred from learned instinct hints: {preview}"
    else:
        payload["instinct_reason"] = "verify targets inferred from learned instinct hints"
    return payload


def _default_payload(requested: str) -> dict[str, Any]:
    return _targeting_payload(
        requested=requested,
        resolved=requested,
        source="default",
        targets=[],
    )


def _task_granularity_payload(
    *,
    project_root: Path,
    requested: str,
    changed_targets: list[str],
    feature: str,
    task_label: str,
) -> dict[str, Any] | None:
    match = _resolve_task_keyword_match(
        project_root=project_root,
        changed_targets=changed_targets,
        feature=feature,
        task_label=task_label,
    )
    if match is None:
        return None
    command = _scoped_pytest_cmd([match["target"]], keyword=match["expression"])
    payload = _targeting_payload(
        requested=requested,
        resolved=command,
        source="task_keyword_match",
        targets=[match["target"]],
    )
    payload["verify_keyword_expression"] = match["expression"]
    payload["verify_keyword_source"] = match["source"]
    payload["verify_keyword_match_count"] = int(match["match_count"])
    return payload


def _targeting_payload(
    *,
    requested: str,
    resolved: str,
    source: str,
    targets: list[str],
) -> dict[str, Any]:
    return {
        "verify_cmd": requested,
        "verify_cmd_resolved": resolved,
        "verify_target_source": source,
        "verify_targets": list(targets),
    }


def _resolve_task_keyword_match(
    *,
    project_root: Path,
    changed_targets: list[str],
    feature: str,
    task_label: str,
) -> dict[str, Any] | None:
    for candidate in _keyword_candidates(feature=feature, task_label=task_label):
        for target in changed_targets:
            test_names = _collect_test_names(project_root=project_root, target=target)
            if not test_names:
                continue
            match_count = _keyword_match_count(test_names, candidate["matcher"])
            if match_count <= 0:
                continue
            return {
                "target": target,
                "expression": candidate["expression"],
                "source": candidate["source"],
                "match_count": match_count,
            }
    return None


def _changed_test_targets(project_root: Path, changed_files: list[str]) -> list[str]:
    root = Path(project_root).resolve()
    targets: list[str] = []
    for raw in changed_files:
        candidate = (root / str(raw)).resolve()
        if not candidate.exists() or not candidate.is_file():
            continue
        relative = _relative_to_root(root, candidate)
        if _looks_like_test_path(relative):
            targets.append(relative)
    return _dedup_targets(targets)


def _derived_test_targets(project_root: Path, changed_files: list[str]) -> list[str]:
    root = Path(project_root).resolve()
    targets: list[str] = []
    for raw in changed_files:
        candidate = (root / str(raw)).resolve()
        if not candidate.exists() or not candidate.is_file() or candidate.suffix != ".py":
            continue
        source_relative = _relative_to_root(root, candidate)
        if _looks_like_test_path(source_relative):
            continue
        for maybe_target in _candidate_test_targets_for_source(source_relative):
            resolved = (root / maybe_target).resolve()
            if not resolved.exists() or not resolved.is_file():
                continue
            relative = _relative_to_root(root, resolved)
            if _looks_like_test_path(relative):
                targets.append(relative)
    return _dedup_targets(targets)


def _candidate_test_targets_for_source(source_relative: str) -> list[str]:
    source_path = Path(str(source_relative).replace("\\", "/"))
    stem = source_path.stem
    if not stem:
        return []
    parent_variants = _source_parent_variants(source_path.parent.parts)
    candidates: list[str] = []
    for parent_parts in parent_variants:
        base = Path("tests")
        if parent_parts:
            base = base.joinpath(*parent_parts)
        candidates.append(str(base / f"test_{stem}.py").replace("\\", "/"))
        candidates.append(str(base / f"{stem}_test.py").replace("\\", "/"))
    compact_tokens = [part for part in source_path.parts if part not in {"src", "app"}]
    if compact_tokens:
        compact_name = "_".join(compact_tokens).replace(".py", "")
        if compact_name:
            candidates.append(f"tests/test_{compact_name}.py")
    return _dedup_targets(candidates)


def _source_parent_variants(parts: tuple[str, ...]) -> list[tuple[str, ...]]:
    parent = tuple(str(item).strip() for item in parts if str(item).strip() and str(item) != ".")
    variants: list[tuple[str, ...]] = [parent]
    if parent and parent[0].lower() in {"src", "app", "lib"}:
        variants.append(parent[1:])
    deduped: list[tuple[str, ...]] = []
    seen: set[tuple[str, ...]] = set()
    for item in variants:
        if item in seen:
            continue
        seen.add(item)
        deduped.append(item)
    return deduped


def _keyword_candidates(*, feature: str, task_label: str) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    feature_slug = _slug_tokens(feature, separator="_")
    if feature_slug:
        candidates.append(
            {
                "expression": feature_slug,
                "source": "feature_slug",
                "matcher": lambda name, needle=feature_slug: needle in name,
            }
        )
    task_tokens = _significant_task_tokens(task_label)
    task_expr = " and ".join(task_tokens)
    if task_expr:
        candidates.append(
            {
                "expression": task_expr,
                "source": "task_label_tokens",
                "matcher": lambda name, tokens=tuple(task_tokens): all(token in name for token in tokens),
            }
        )
    feature_tokens = _slug_tokens(feature, separator=" ").split()
    feature_expr = " and ".join(feature_tokens)
    if feature_expr:
        candidates.append(
            {
                "expression": feature_expr,
                "source": "feature_tokens",
                "matcher": lambda name, tokens=tuple(feature_tokens): all(token in name for token in tokens),
            }
        )
    ordered: list[dict[str, Any]] = []
    for candidate in candidates:
        expression = str(candidate["expression"]).strip()
        if not expression or expression in seen:
            continue
        seen.add(expression)
        ordered.append(candidate)
    return ordered


def _slug_tokens(value: str, *, separator: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", separator, str(value or "").strip().lower())
    if separator == " ":
        cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip(separator + " ")


def _significant_task_tokens(task_label: str) -> list[str]:
    task_name = str(task_label or "").split(":", 1)[-1]
    tokens = [token for token in _slug_tokens(task_name, separator=" ").split() if token]
    stopwords = {"add", "adjust", "implement", "prepare", "run", "scoped", "test", "tests", "update"}
    filtered = [token for token in tokens if token not in stopwords and len(token) > 2]
    return filtered[:3]


def _collect_test_names(*, project_root: Path, target: str) -> list[str]:
    path = (project_root / target).resolve()
    if not path.exists() or not path.is_file():
        return []
    names = re.findall(r"^\s*(?:async\s+def|def)\s+(test_[a-zA-Z0-9_]+)\s*\(", path.read_text(encoding="utf-8"), re.MULTILINE)
    return [str(item).strip().lower() for item in names if str(item).strip()]


def _keyword_match_count(test_names: list[str], matcher: Any) -> int:
    return sum(1 for name in test_names if bool(matcher(name)))


def _instinct_hint_targets(project_root: Path, instinct_hints: list[dict[str, Any]]) -> list[str]:
    root = Path(project_root).resolve()
    targets = _collect_hint_targets(root, instinct_hints)
    return _dedup_targets(targets)


def _instinct_hint_patterns(instinct_hints: list[dict[str, Any]]) -> list[str]:
    patterns: list[str] = []
    seen: set[str] = set()
    for pattern in _iter_hint_patterns(instinct_hints):
        if pattern in seen:
            continue
        seen.add(pattern)
        patterns.append(pattern)
    return patterns


def _collect_hint_targets(root: Path, instinct_hints: list[dict[str, Any]]) -> list[str]:
    targets: list[str] = []
    for pattern in _iter_hint_patterns(instinct_hints):
        targets.extend(_pattern_targets(root, pattern))
        if len(targets) >= _MAX_VERIFY_TARGETS:
            break
    return targets


def _iter_hint_patterns(instinct_hints: list[dict[str, Any]]):
    for hint in instinct_hints:
        pattern = str(hint.get("pattern") or "").strip()
        if pattern and _looks_like_test_pattern(pattern):
            yield pattern


def _pattern_targets(root: Path, pattern: str) -> list[str]:
    targets: list[str] = []
    for path in root.glob(pattern):
        maybe_target = _resolve_test_target(root, path)
        if maybe_target:
            targets.append(maybe_target)
        if len(targets) >= _MAX_VERIFY_TARGETS:
            break
    return targets


def _resolve_test_target(root: Path, path: Path) -> str:
    resolved = path.resolve()
    if not resolved.exists() or not resolved.is_file():
        return ""
    relative = _relative_to_root(root, resolved)
    return relative if _looks_like_test_path(relative) else ""


def _looks_like_test_pattern(pattern: str) -> bool:
    normalized = str(pattern or "").replace("\\", "/").lower()
    return "test" in normalized or normalized.startswith("tests/")


def _relative_to_root(root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(root)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def _looks_like_test_path(path: str) -> bool:
    normalized = str(path or "").replace("\\", "/").lower()
    if "/tests/" in f"/{normalized}":
        return True
    name = Path(normalized).name
    return name.startswith("test_") or name.endswith("_test.py")


def _dedup_targets(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for raw in values:
        normalized = str(raw).strip().replace("\\", "/")
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
        if len(ordered) >= _MAX_VERIFY_TARGETS:
            break
    return ordered


def _scoped_pytest_cmd(targets: list[str], *, keyword: str = "") -> str:
    command = "pytest -q " + " ".join(str(item) for item in targets)
    kw = str(keyword).strip()
    if kw:
        # Pytest -k accepts boolean expressions like "foo and bar or not baz".
        # The expression MUST be a single shell argument — otherwise the shell
        # splits on whitespace and pytest sees `and`/`bar` as positional file
        # arguments, producing "ERROR: file or directory not found: and".
        # Quote with double quotes and escape any embedded double quote.
        escaped = kw.replace('"', '\\"')
        return f'{command} -k "{escaped}"'
    return command
