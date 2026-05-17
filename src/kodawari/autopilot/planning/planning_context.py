"""Context collection helpers for model-driven planning."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

logger = logging.getLogger(__name__)


DEFAULT_MAX_LINES_PER_DOC = 500
DEFAULT_MAX_CHARS = 30000
DEFAULT_MANIFEST_LIMIT = 800
DEFAULT_SNIPPET_LIMIT = 20
DEFAULT_SNIPPET_LINES = 120
_TELEMETRY_WINDOW = 10  # keep last N telemetry events


@dataclass(frozen=True)
class _Section:
    key: str
    title: str
    value: str
    priority: float


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _text_hash(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8", errors="replace")).hexdigest()


def _json_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8", errors="replace")
    ).hexdigest()


def _safe_read_lines(path: Path, *, max_lines: int) -> str:
    if not path.exists() or not path.is_file():
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[: max(1, int(max_lines))]).strip()


def _first_lines(path: Path, *, max_lines: int) -> str:
    return _safe_read_lines(path, max_lines=max_lines)


def _git_text(project_root: Path, args: list[str], *, timeout_seconds: int = 10) -> str:
    try:
        run = subprocess.run(
            ["git", "-C", str(project_root), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(3, int(timeout_seconds)),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if run.returncode != 0:
        return ""
    return _clean_text(run.stdout)


def _git_recent_log(project_root: Path, *, count: int = 20) -> str:
    return _git_text(project_root, ["log", "--oneline", f"-{max(1, int(count))}"])


def _git_uncommitted_stat(project_root: Path) -> str:
    # Use full content diff (not just --stat) so any edit invalidates the fingerprint,
    # even when the changed line count happens to stay the same.
    return _git_text(project_root, ["diff", "HEAD"], timeout_seconds=20)


def _git_untracked_files(project_root: Path) -> str:
    return _git_text(project_root, ["ls-files", "--others", "--exclude-standard"])


# Caps for _untracked_content_hash. The unbounded original (read_text on every
# untracked file) stalled planning indefinitely on repos with large untracked
# debris (.executor_scratch worktree copies, build artifacts, logs). The caps
# preserve the fingerprint-sensitivity contract for normal-sized repos while
# falling back to a name+size+mtime hash when a project has accumulated many
# or large untracked files.
MAX_UNTRACKED_FILES_FOR_CONTENT_HASH = 2000
MAX_BYTES_PER_UNTRACKED_FILE = 64 * 1024
# 64MB total: on the newsapp 765-untracked case, 16MB would push ~two-thirds
# of files into stat fallback (losing mid-content change detection); 64MB
# keeps almost all of them content-hashed while still bounding worst-case
# runtime to ~1-2s on cold cache.
TOTAL_UNTRACKED_READ_BUDGET_BYTES = 64 * 1024 * 1024


def _untracked_stat_signature(project_root: Path, name: str) -> str:
    """Return ``name|size|mtime_ns`` for an untracked file when content read
    is impractical. Mtime alone is unreliable on Windows / network drives,
    but combined with size it remains a useful change-detection signal.
    Uses ``lstat`` so symlinks are signed by the link itself rather than
    the target — preventing a symlink-pointed-outside-repo from being
    silently fingerprinted."""
    try:
        stat = (project_root / name).lstat()
    except OSError:
        return f"{name}|missing"
    return f"{name}|{stat.st_size}|{stat.st_mtime_ns}"


def _untracked_content_hash(project_root: Path) -> str:
    """Hash untracked file names AND their contents (bounded).

    Preserves the fingerprint-sensitivity property — editing an untracked
    file invalidates the fingerprint, just like ``git diff HEAD`` does for
    tracked dirty files — within explicit caps:

    * up to ``MAX_UNTRACKED_FILES_FOR_CONTENT_HASH`` files get content-hashed
    * each file's content is truncated to ``MAX_BYTES_PER_UNTRACKED_FILE``
    * total bytes read are capped at ``TOTAL_UNTRACKED_READ_BUDGET_BYTES``

    Files beyond any cap contribute their name + stat (size + mtime_ns)
    instead of content. The filename list still enters the hash unconditionally
    so additions / removals are always detected; only mid-content edits past
    a cap rely on the stat fallback. The cap-fallback path emits a sentinel
    marker so the audit trail records that the hash is degraded.
    """
    names_text = _git_untracked_files(project_root)
    if not names_text:
        return _text_hash("")
    names = [line.strip() for line in names_text.splitlines() if line.strip()]
    parts: list[str] = [names_text]
    over_file_cap = len(names) > MAX_UNTRACKED_FILES_FOR_CONTENT_HASH
    bytes_read = 0
    content_hashed = 0
    stat_fallback = 0
    for index, name in enumerate(names):
        if over_file_cap or bytes_read >= TOTAL_UNTRACKED_READ_BUDGET_BYTES:
            parts.append(_untracked_stat_signature(project_root, name))
            stat_fallback += 1
            continue
        path = project_root / name
        # Symlinks are signed by lstat (size+mtime of the link), not by
        # opening the target. Reading a symlink target could fingerprint
        # external content (e.g. a /etc/passwd pointed-to link, or an
        # outside-repo 5GB file truncated to 64KB), which is both a
        # correctness hole and a minor information-leak through the
        # cap-degraded sentinel byte counter.
        if path.is_symlink():
            parts.append(_untracked_stat_signature(project_root, name))
            stat_fallback += 1
            continue
        try:
            with open(path, "rb") as fh:
                raw = fh.read(MAX_BYTES_PER_UNTRACKED_FILE)
        except OSError:
            raw = b""
        bytes_read += len(raw)
        content = raw.decode("utf-8", errors="replace")
        parts.append(f"{name}:{_text_hash(content)}")
        content_hashed += 1
    if over_file_cap or stat_fallback:
        parts.append(
            f"__cap_degraded__:files={len(names)}|"
            f"content_hashed={content_hashed}|stat_fallback={stat_fallback}|"
            f"bytes_read={bytes_read}"
        )
    return _text_hash("\n".join(parts))


def _git_head(project_root: Path) -> str:
    return _git_text(project_root, ["rev-parse", "HEAD"])


def _normalize_relpath(project_root: Path, path: Path) -> str:
    try:
        relative = path.resolve().relative_to(project_root.resolve()).as_posix()
    except ValueError:
        return ""
    return _clean_text(relative)


def _existing_dirs(project_root: Path, candidates: list[str]) -> list[Path]:
    existing: list[Path] = []
    seen: set[str] = set()
    for raw in candidates:
        text = _clean_text(raw).replace("\\", "/")
        if not text:
            continue
        if text.lower() in seen:
            continue
        candidate = (project_root / text).resolve()
        if not candidate.exists() or not candidate.is_dir():
            continue
        seen.add(text.lower())
        existing.append(candidate)
    return existing


def _manifest_roots(project_root: Path, repo_inventory: dict[str, Any]) -> list[Path]:
    layout = dict(repo_inventory.get("project_layout") or {})
    code_roots = [str(item) for item in list(layout.get("code_roots") or []) if _clean_text(item)]
    test_roots = [str(item) for item in list(layout.get("test_roots") or []) if _clean_text(item)]
    surface_roots = [
        str(root)
        for surface in list(repo_inventory.get("surfaces") or [])
        if isinstance(surface, dict)
        for root in list(surface.get("roots") or [])
        if _clean_text(root)
    ]
    candidates = (
        code_roots
        + test_roots
        + surface_roots
        + ["docs", "tests", "app", "src", "backend", "web/src", "frontend/src", "mobile", "mobile/www"]
    )
    return _existing_dirs(project_root, candidates)


def _repo_manifest(
    project_root: Path,
    *,
    repo_inventory: dict[str, Any],
    limit: int = DEFAULT_MANIFEST_LIMIT,
) -> list[str]:
    roots = _manifest_roots(project_root, repo_inventory)
    if not roots:
        roots = [project_root]
    allowed_suffixes = {
        ".py",
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".json",
        ".yaml",
        ".yml",
        ".md",
        ".html",
        ".css",
        ".toml",
        ".ini",
        ".cfg",
        ".txt",
    }
    values: list[str] = []
    seen: set[str] = set()
    for root in roots:
        for item in sorted(root.rglob("*"), key=lambda p: p.as_posix().lower()):
            if not item.is_file():
                continue
            if item.suffix.lower() not in allowed_suffixes:
                continue
            rel = _normalize_relpath(project_root, item)
            if not rel:
                continue
            key = rel.lower()
            if key in seen:
                continue
            seen.add(key)
            values.append(rel)
            if len(values) >= max(1, int(limit)):
                return values
    return values


def _tokenize(text: str) -> set[str]:
    tokens: set[str] = set()
    for raw in re.split(r"[^A-Za-z0-9]+", _clean_text(text).lower()):
        token = raw.strip().lower()
        if len(token) >= 3:
            tokens.add(token)
    return tokens


def _hint_tokens(text: str) -> set[str]:
    tokens: set[str] = set()
    for raw in re.split(r"[^A-Za-z0-9]+", _clean_text(text).lower()):
        token = raw.strip()
        if len(token) >= 3:
            tokens.add(token)
    return tokens


_PATH_LIKE_RE = re.compile(r"[A-Za-z0-9_./\\-]+\.[A-Za-z0-9]+")
_GENERIC_PATH_PARTS = {
    "api",
    "app",
    "backend",
    "docs",
    "frontend",
    "routes",
    "services",
    "src",
    "tests",
    "v1",
    "web",
}


def _extract_path_like_mentions(text: str, *, limit: int = 300) -> list[str]:
    mentions: list[str] = []
    seen: set[str] = set()
    for match in _PATH_LIKE_RE.finditer(_clean_text(text)):
        token = match.group(0).strip("`'\"()[]{}<>:,;")
        token = token.replace("\\", "/")
        if not token or "." not in token:
            continue
        lowered = token.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        mentions.append(token)
        if len(mentions) >= max(1, int(limit)):
            break
    return mentions


def _baseline_probe_enabled() -> bool:
    raw = os.environ.get("WORKFLOW_PLANNER_BASELINE_PROBE")
    if raw is None:
        return True
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


def _looks_like_test_path(path: str) -> bool:
    normalized = _clean_text(path).replace("\\", "/").lower()
    parts = [part for part in normalized.split("/") if part]
    if "tests" in parts or "test" in parts:
        return True
    name = parts[-1] if parts else normalized
    return name.startswith("test_") or name.endswith("_test.py") or name.endswith(".test.py")


def _mentioned_existing_tests(
    *,
    project_root: Path,
    task_direction: str,
    prd_excerpt: str,
    limit: int = 3,
) -> list[str]:
    mentions = _extract_path_like_mentions(f"{task_direction}\n{prd_excerpt}")
    selected: list[str] = []
    seen: set[str] = set()
    root = project_root.resolve()
    for raw in mentions:
        rel = raw.split("::", 1)[0].replace("\\", "/").lstrip("./")
        if not _looks_like_test_path(rel):
            continue
        try:
            candidate = (root / rel).resolve()
        except (OSError, ValueError):
            continue
        if not candidate.is_relative_to(root) or not candidate.is_file():
            continue
        key = rel.casefold()
        if key in seen:
            continue
        seen.add(key)
        selected.append(rel)
        if len(selected) >= max(1, int(limit)):
            break
    return selected


def _probe_output(stdout: str, stderr: str, *, limit: int = 3000) -> str:
    text = "\n".join(part for part in (_clean_text(stdout), _clean_text(stderr)) if part).strip()
    if len(text) <= limit:
        return text
    return text[-limit:].strip()


def _run_pytest_probe(project_root: Path, args: list[str], *, timeout_seconds: int) -> dict[str, Any]:
    command = [sys.executable, "-m", "pytest", *args]
    try:
        completed = subprocess.run(
            command,
            cwd=str(project_root.resolve()),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(1, int(timeout_seconds)),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "status": "TIMEOUT",
            "returncode": None,
            "command": command,
            "summary": _probe_output(str(exc.stdout or ""), str(exc.stderr or "")),
        }
    except OSError as exc:
        return {
            "status": "ERROR",
            "returncode": None,
            "command": command,
            "summary": str(exc),
        }
    return {
        "status": "PASS" if completed.returncode == 0 else "FAIL",
        "returncode": int(completed.returncode),
        "command": command,
        "summary": _probe_output(completed.stdout or "", completed.stderr or ""),
    }


def collect_failing_baseline(
    *,
    project_root: Path,
    task_direction: str,
    prd_excerpt: str,
    timeout_seconds: int = 30,
) -> dict[str, Any]:
    """Run a bounded pre-planning pytest probe for explicitly mentioned tests."""
    if not _baseline_probe_enabled():
        return {"enabled": False, "status": "SKIPPED", "reason": "disabled"}
    tests = _mentioned_existing_tests(
        project_root=project_root,
        task_direction=task_direction,
        prd_excerpt=prd_excerpt,
    )
    if not tests:
        return {"enabled": True, "status": "SKIPPED", "reason": "no_target_tests"}
    collect_args = ["--collect-only", "-q", *tests]
    collect = _run_pytest_probe(project_root, collect_args, timeout_seconds=min(10, timeout_seconds))
    if collect["status"] not in {"PASS", "FAIL"}:
        return {
            "enabled": True,
            "status": "DIAGNOSTIC",
            "target_tests": tests,
            "collect": collect,
            "run": {},
        }
    run_args = ["-q", *tests]
    run = _run_pytest_probe(project_root, run_args, timeout_seconds=timeout_seconds)
    status = "FAILING" if run["status"] in {"FAIL", "TIMEOUT", "ERROR"} else "PASS"
    return {
        "enabled": True,
        "status": status,
        "target_tests": tests,
        "collect": collect,
        "run": run,
    }


def _candidate_paths(
    *,
    project_root: Path,
    manifest: list[str],
    task_direction: str,
    prd_excerpt: str,
    recent_commits: str,
    uncommitted_changes: str,
    limit: int = DEFAULT_SNIPPET_LIMIT,
) -> list[str]:
    tokens = set()
    text_sources = (task_direction, prd_excerpt, recent_commits, uncommitted_changes)
    for source in text_sources:
        tokens.update(_tokenize(source))
    if not tokens:
        tokens = {"api", "service", "route", "schema", "test"}
    priority_paths = _mentioned_directory_paths(manifest=manifest, text_sources=text_sources)
    scored: list[tuple[int, str]] = []
    for path in manifest:
        lower = path.lower()
        parts = _tokenize(lower)
        score = len(tokens.intersection(parts))
        if lower.endswith(".md"):
            score -= 1
        if "test" in lower:
            score += 1
        if score > 0:
            scored.append((score, path))
    scored.sort(key=lambda item: (-item[0], item[1].lower()))
    selected = _merge_candidate_paths(
        priority_paths,
        [path for _, path in scored],
        limit=max(1, int(limit)),
    )
    if selected:
        return selected
    fallbacks = [
        "backend/main.py",
        "app/main.py",
        "src/main.py",
        "backend/api/router.py",
        "backend/api/v1/router.py",
    ]
    existing: list[str] = []
    for raw in fallbacks:
        if (project_root / raw).exists():
            existing.append(raw)
    return existing[: max(1, int(limit))]


def _mentioned_directory_paths(*, manifest: list[str], text_sources: tuple[str, ...]) -> list[str]:
    haystack = "\n".join(text_sources).replace("\\", "/").lower()
    if not haystack:
        return []
    mentioned_dirs: list[str] = []
    seen_dirs: set[str] = set()
    for path in manifest:
        parts = [part for part in path.replace("\\", "/").split("/") if part]
        for index in range(2, len(parts)):
            directory = "/".join(parts[:index])
            key = directory.lower()
            if key in seen_dirs or key not in haystack:
                continue
            if not _specific_directory_mention(parts[:index]):
                continue
            seen_dirs.add(key)
            mentioned_dirs.append(key)
    paths: list[str] = []
    seen_paths: set[str] = set()
    for directory in sorted(mentioned_dirs, key=lambda item: (-item.count("/"), item)):
        prefix = f"{directory}/"
        for path in manifest:
            normalized = path.replace("\\", "/")
            key = normalized.lower()
            if key in seen_paths or not key.startswith(prefix):
                continue
            if key.endswith(".md"):
                continue
            seen_paths.add(key)
            paths.append(path)
    return paths


def _specific_directory_mention(parts: list[str]) -> bool:
    if len(parts) < 2:
        return False
    leaf = parts[-1].lower()
    if leaf in _GENERIC_PATH_PARTS:
        return False
    return "_" in leaf or "-" in leaf or leaf not in _GENERIC_PATH_PARTS


def _merge_candidate_paths(*groups: list[str], limit: int) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for path in group:
            normalized = _clean_text(path).replace("\\", "/")
            if not normalized:
                continue
            key = normalized.lower()
            if key in seen:
                continue
            seen.add(key)
            merged.append(normalized)
            if len(merged) >= max(1, int(limit)):
                return merged
    return merged


def _read_snippet(path: Path, *, max_lines: int = DEFAULT_SNIPPET_LINES) -> str:
    return _safe_read_lines(path, max_lines=max_lines)


def _candidate_snippets(
    *,
    project_root: Path,
    candidates: list[str],
    max_lines: int = DEFAULT_SNIPPET_LINES,
) -> list[dict[str, str]]:
    snippets: list[dict[str, str]] = []
    for rel in candidates:
        path = (project_root / rel).resolve()
        if not path.exists() or not path.is_file():
            continue
        snippet = _read_snippet(path, max_lines=max_lines)
        if not snippet:
            continue
        snippets.append(
            {
                "path": rel,
                "reason": "token_match",
                "snippet": snippet,
            }
        )
    return snippets


def _find_docs_by_pattern(project_root: Path, patterns: list[str]) -> list[Path]:
    matches: list[Path] = []
    for pattern in patterns:
        for item in sorted(project_root.glob(pattern), key=lambda p: p.as_posix().lower()):
            if item.is_file():
                matches.append(item.resolve())
    return matches


def _read_doc_safe(path: Path, *, max_lines: int = DEFAULT_MAX_LINES_PER_DOC) -> str:
    return _safe_read_lines(path, max_lines=max_lines)


# ── feedback-loop helpers (Section 3 of Harness absorption plan) ──────────────

def _safe_avg(values: list[float]) -> float | None:
    """Average over numeric values; returns None for empty or all-non-numeric input."""
    valid = [v for v in values if isinstance(v, (int, float))]
    return sum(valid) / len(valid) if valid else None


def _collect_instinct_summary(project_root: Path) -> dict[str, Any]:
    """Extract high-confidence (≥0.75) learned instinct patterns from the instincts store."""
    try:
        from kodawari.instincts.storage import InstinctStore
    except ImportError:
        return {}

    try:
        store = InstinctStore(project_root)
        data = store.load()
    except Exception:
        logger.debug("instinct store load failed", exc_info=True)
        return {}

    if data is None:
        return {}

    high_confidence = [
        {
            "pattern": inst.pattern,
            "category": inst.category,
            "confidence": inst.confidence,
            "count": inst.count,
            "explanation": inst.explanation[:120],
        }
        for inst in (data.learned_instincts or [])
        if inst.confidence >= 0.75 and not inst.archived
    ]

    active_candidates = [
        {
            "signature": cand.signature[:120],
            "category": cand.category,
            "count": cand.count,
            "suggested_pattern": cand.suggested_pattern,
        }
        for cand in (data.learning_candidates or [])
        if cand.count >= 2 and not cand.promoted
    ]

    return {
        "high_confidence_patterns": high_confidence,
        "active_candidates": active_candidates[:5],
        "total_learned": len([i for i in (data.learned_instincts or []) if not i.archived]),
        "total_candidates": len(data.learning_candidates or []),
    }


def _collect_telemetry_summary(project_root: Path, feature: str = "") -> dict[str, Any]:
    """Extract anomaly signals from the most recent telemetry events.

    Reads planning/{feature}/.telemetry_events.jsonl when feature is given;
    otherwise scans all planning/*/.telemetry_events.jsonl and picks the first.
    Only the last _TELEMETRY_WINDOW lines are considered.
    """
    planning_base = project_root / "planning"
    if feature:
        candidates = [planning_base / feature / ".telemetry_events.jsonl"]
    else:
        try:
            candidates = sorted(planning_base.glob("*/.telemetry_events.jsonl"))
        except OSError:
            return {}

    events_path = next((p for p in candidates if p.is_file()), None)
    if events_path is None:
        return {}

    try:
        raw_lines = events_path.read_text(encoding="utf-8").strip().splitlines()
    except OSError:
        return {}

    recent: list[dict[str, Any]] = []
    for line in raw_lines[-_TELEMETRY_WINDOW:]:
        line = line.strip()
        if not line:
            continue
        try:
            recent.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    if not recent:
        return {}

    # status field holds stop_reason in the real writer schema ({status, metrics}).
    # The 'signals' key is written to .telemetry_snapshot.json but NOT to the
    # history rows in .telemetry_events.jsonl.
    token_overruns = sum(
        1 for e in recent if "TOKEN_BUDGET" in str(e.get("status", ""))
    )
    max_cycles_hits = sum(
        1 for e in recent if "MAX_CYCLES" in str(e.get("status", ""))
    )
    avg_cycles = _safe_avg([e.get("metrics", {}).get("cycle", 0) for e in recent])
    avg_cycles_val = avg_cycles if avg_cycles is not None else 0.0

    return {
        "recent_runs": len(recent),
        "token_overrun_count": token_overruns,
        "max_cycles_hit_count": max_cycles_hits,
        "avg_cycles": round(avg_cycles_val, 1),
        "signals": {
            "suggest_smaller_scope": token_overruns >= 2 or max_cycles_hits >= 3,
            "suggest_more_cycles": avg_cycles_val > 6 and max_cycles_hits == 0,
        },
    }


def _collect_lane_stability_summary(project_root: Path) -> dict[str, Any]:
    """Extract lane stability signals from planning/lane_weekly_trend.json."""
    trend_path = project_root / "planning" / "lane_weekly_trend.json"
    if not trend_path.is_file():
        return {}

    try:
        trend = json.loads(trend_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}

    if not isinstance(trend, dict):
        return {}

    lanes = list(trend.get("lanes") or [])
    unstable_lanes = [
        {
            "lane": lane.get("lane", ""),
            "state": lane.get("standing_proof_state", ""),
            "pass_rate": lane.get("metrics", {}).get("pass_rate", 0),
            "top_root_causes": (lane.get("metrics") or {}).get("top_root_causes", [])[:3],
        }
        for lane in lanes
        if lane.get("standing_proof_state") not in ("stable", "no_data")
    ]

    return {
        "unstable_lanes": unstable_lanes,
        "top_failure_signatures": list(
            (trend.get("overview") or {}).get("top_failure_signatures", [])
        )[:5],
        "overall_status": trend.get("status", ""),
    }


def _render_instinct_summary(data: dict[str, Any]) -> str:
    if not data or not data.get("high_confidence_patterns"):
        return ""
    lines = ["HIGH-RISK ZONES (from learned error patterns):"]
    for p in data["high_confidence_patterns"]:
        lines.append(
            f"- {p['pattern']}: {p.get('explanation', '')[:120]} "
            f"(confidence={p['confidence']}, seen={p['count']}x, category={p['category']})"
        )
    if data.get("active_candidates"):
        lines.append("")
        lines.append("Emerging patterns (not yet confirmed):")
        for c in data["active_candidates"][:3]:
            lines.append(
                f"- {c['suggested_pattern']}: {c['signature'][:80]} (seen={c['count']}x)"
            )
    return "\n".join(lines)


def _render_telemetry_summary(data: dict[str, Any]) -> str:
    if not data or not data.get("signals"):
        return ""
    lines: list[str] = []
    signals = data["signals"]
    if signals.get("suggest_smaller_scope"):
        lines.append(
            f"SCOPE WARNING: {data.get('token_overrun_count', 0)} token overruns + "
            f"{data.get('max_cycles_hit_count', 0)} max-cycle hits in last "
            f"{data.get('recent_runs', 0)} runs. Consider smaller task scope."
        )
    if signals.get("suggest_more_cycles"):
        lines.append(
            f"CYCLES: avg {data.get('avg_cycles', 0)} cycles per run. "
            f"May benefit from higher max_cycles budget."
        )
    return "\n".join(lines)


def _render_lane_summary(data: dict[str, Any]) -> str:
    if not data or not data.get("unstable_lanes"):
        return ""
    lines = ["UNSTABLE CI LANES:"]
    for lane in data["unstable_lanes"]:
        causes = ", ".join(lane.get("top_root_causes", []))
        lines.append(
            f"- {lane['lane']}: state={lane['state']}, "
            f"pass_rate={lane.get('pass_rate', 0):.0%}, causes=[{causes}]"
        )
    return "\n".join(lines)


def _render_failing_baseline(data: dict[str, Any]) -> str:
    if not data or not data.get("enabled"):
        return ""
    status = _clean_text(data.get("status"))
    if status in {"", "SKIPPED"}:
        return ""
    tests = [str(item) for item in list(data.get("target_tests") or []) if _clean_text(item)]
    run = dict(data.get("run") or {})
    collect = dict(data.get("collect") or {})
    lines = [
        f"BASELINE TEST PROBE: status={status}",
        f"- target_tests: {', '.join(tests) if tests else '(none)'}",
    ]
    collect_status = _clean_text(collect.get("status"))
    if collect_status:
        lines.append(f"- collect_status: {collect_status}")
    run_status = _clean_text(run.get("status"))
    if run_status:
        lines.append(f"- run_status: {run_status}")
    summary = _clean_text(run.get("summary") or collect.get("summary"))
    if summary:
        lines.append("- failure_summary:")
        lines.append(summary[-1600:])
    return "\n".join(lines)


def build_file_manifest(manifest_files: list[str]) -> dict[str, list[str]]:
    """Build a basename → [canonical_relative_paths] reverse index from the repo manifest."""
    result: dict[str, list[str]] = {}
    for rel_path in manifest_files:
        if not rel_path:
            continue
        basename = rel_path.rsplit("/", 1)[-1]
        if not basename:
            continue
        if basename not in result:
            result[basename] = []
        if rel_path not in result[basename]:
            result[basename].append(rel_path)
    return result


def _resolve_path_list(
    paths: list[str],
    file_manifest: dict[str, list[str]],
    project_root: Path,
    new_file_set: set[str],
) -> tuple[list[str], list[dict[str, Any]]]:
    """Resolve a list of path strings using the file manifest.

    Returns (resolved_paths, resolution_log).
    Each log entry has 'original', 'kind' (auto|ambiguous), and either 'resolved' or 'candidates'.
    """
    resolved: list[str] = []
    log: list[dict[str, Any]] = []
    for f in paths:
        if not f:
            continue
        if (project_root / f).exists():
            resolved.append(f)
            continue
        if f in new_file_set:
            resolved.append(f)
            continue
        normalized = f.replace("\\", "/").lstrip("./")
        basename = normalized.rsplit("/", 1)[-1]
        candidates = file_manifest.get(basename, [])
        if "/" in normalized and candidates:
            suffix_matches = [
                item
                for item in candidates
                if item == normalized or item.endswith(f"/{normalized}")
            ]
            if len(suffix_matches) == 1:
                resolved.append(suffix_matches[0])
                log.append({"original": f, "resolved": suffix_matches[0], "kind": "auto"})
                continue
            if len(suffix_matches) > 1:
                resolved.append(f)
                log.append({"original": f, "candidates": suffix_matches, "kind": "ambiguous"})
                continue
        if len(candidates) == 1:
            resolved.append(candidates[0])
            log.append({"original": f, "resolved": candidates[0], "kind": "auto"})
        elif len(candidates) > 1:
            resolved.append(f)
            log.append({"original": f, "candidates": candidates, "kind": "ambiguous"})
        else:
            resolved.append(f)
    return resolved, log


def _clean_paths(items: Any) -> list[str]:
    return [_clean_text(f).replace("\\", "/") for f in list(items or []) if _clean_text(f)]


def _plan_list(plan: dict[str, Any], key: str) -> list[Any]:
    return list(plan.get(key) or [])


def _log_filter(logs: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [r for r in logs if r.get("kind") == kind]


def _resolve_task(
    task: dict[str, Any],
    file_manifest: dict[str, list[str]],
    project_root: Path,
    all_log: list[dict[str, Any]],
) -> None:
    if not isinstance(task, dict):
        return
    new_files_raw = _clean_paths(task.get("new_files"))
    new_file_set = set(new_files_raw)
    resolved_files, flog = _resolve_path_list(_clean_paths(task.get("files_to_change")), file_manifest, project_root, new_file_set)
    task["files_to_change"] = resolved_files
    all_log.extend(flog)
    resolved_new, nlog = _resolve_path_list(new_files_raw, file_manifest, project_root, new_file_set)
    task["new_files"] = resolved_new
    all_log.extend(nlog)


def _resolve_list_field(
    plan: dict[str, Any],
    field: str,
    file_manifest: dict[str, list[str]],
    project_root: Path,
    all_log: list[dict[str, Any]],
) -> None:
    raw = _clean_paths(plan.get(field))
    if raw:
        resolved, log = _resolve_path_list(raw, file_manifest, project_root, set())
        plan[field] = resolved
        all_log.extend(log)


def _resolve_roots_field(
    item: dict[str, Any],
    file_manifest: dict[str, list[str]],
    project_root: Path,
    all_log: list[dict[str, Any]],
) -> None:
    raw = _clean_paths(item.get("roots"))
    if raw:
        resolved, log = _resolve_path_list(raw, file_manifest, project_root, set())
        item["roots"] = resolved
        all_log.extend(log)


def _resolve_items_roots(
    items: list[Any],
    file_manifest: dict[str, list[str]],
    project_root: Path,
    all_log: list[dict[str, Any]],
) -> None:
    for item in items:
        if isinstance(item, dict):
            _resolve_roots_field(item, file_manifest, project_root, all_log)


def resolve_plan_paths(
    plan: dict[str, Any],
    file_manifest: dict[str, list[str]],
    project_root: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Normalize file paths across all relevant plan fields.

    Resolves: tasks[*].files_to_change, tasks[*].new_files,
    source_of_truth, source_of_truth_canonical,
    module_boundaries[*].roots, verify_recipes[*].roots.

    Returns (updated_plan, resolution_metadata) where metadata contains
    auto_resolved and ambiguous lists for debugging.
    """
    import copy

    plan = copy.deepcopy(plan)
    all_log: list[dict[str, Any]] = []

    for task in _plan_list(plan, "tasks"):
        _resolve_task(task, file_manifest, project_root, all_log)

    for field in ("source_of_truth", "source_of_truth_canonical"):
        _resolve_list_field(plan, field, file_manifest, project_root, all_log)

    _resolve_items_roots(_plan_list(plan, "module_boundaries"), file_manifest, project_root, all_log)
    _resolve_items_roots(_plan_list(plan, "verify_recipes"), file_manifest, project_root, all_log)

    auto_resolved = _log_filter(all_log, "auto")
    ambiguous = _log_filter(all_log, "ambiguous")
    metadata: dict[str, Any] = {
        "auto_resolved": auto_resolved,
        "ambiguous": ambiguous,
        "total_changes": len(auto_resolved),
    }
    return plan, metadata


def _build_canonical_hints(
    file_manifest: dict[str, list[str]],
    task_direction: str = "",
    mention_texts: list[str] | None = None,
    max_entries: int = 100,
) -> str:
    """Build a compact canonical path hints string for the planner prompt.

    Priority 1: basenames mentioned in task_direction/docs text.
    Priority 2: nested paths (basename != full path) not yet in priority 1.
    """
    if not file_manifest:
        return ""
    task_tokens = _hint_tokens(task_direction)
    manifest_by_lower = {key.lower(): key for key in file_manifest.keys()}
    mentions: list[str] = []
    for source in list(mention_texts or []):
        mentions.extend(_extract_path_like_mentions(source))
    mentions.extend(_extract_path_like_mentions(task_direction))
    priority_basenames: list[str] = []
    seen_priority: set[str] = set()
    for mention in mentions:
        basename = mention.rsplit("/", 1)[-1].lower()
        key = manifest_by_lower.get(basename)
        if key and key not in seen_priority:
            priority_basenames.append(key)
            seen_priority.add(key)
    for basename in sorted(file_manifest.keys()):
        if basename in seen_priority:
            continue
        if _hint_tokens(basename) & task_tokens:
            priority_basenames.append(basename)
            seen_priority.add(basename)
    priority_set = set(priority_basenames)
    other_basenames: list[str] = [
        basename
        for basename in sorted(file_manifest.keys())
        if basename not in priority_set and any("/" in p for p in file_manifest[basename])
    ]
    lines: list[str] = []
    for basename in (priority_basenames + other_basenames)[:max_entries]:
        paths = file_manifest[basename]
        if len(paths) == 1:
            lines.append(f"- {basename} → {paths[0]}")
        else:
            lines.append(f"- {basename} → AMBIGUOUS: {', '.join(paths)}")
    if not lines:
        return ""
    return "RULE: Use FULL relative paths in files_to_change.\n" + "\n".join(lines)


def _filter_repo_manifest_by_keywords(
    repo_files: list[str],
    task_direction: str = "",
    mention_texts: list[str] | None = None,
    max_files: int = 50,
) -> list[str]:
    """Filter repo manifest to only include files relevant to the task.

    Strategy A: Intelligent keyword-based filtering.
    - Extract keywords from task_direction and mention texts
    - Match files by basename and path components
    - Return top N files ranked by relevance
    """
    if not repo_files:
        return []

    task_tokens = _hint_tokens(task_direction)
    for source in list(mention_texts or []):
        task_tokens.update(_hint_tokens(source))

    # If token extraction fails (e.g., non-ASCII task), use direct string matching
    if not task_tokens:
        task_direction_lower = task_direction.lower()
        scored_files: list[tuple[int, str]] = []
        for file_path in repo_files:
            path_lower = file_path.lower()
            # Simple heuristic: prioritize Python files in src/, tests/, and config files
            score = 0
            if "/src/" in path_lower or path_lower.startswith("src/"):
                score += 2
            if "/test" in path_lower:
                score += 1
            if _looks_like_config_or_manifest(file_path):
                score += 10
            if score > 0:
                scored_files.append((score, file_path))

        scored_files.sort(key=lambda x: (-x[0], x[1]))
        return [f for _, f in scored_files[:max_files]]

    scored_files: list[tuple[int, str]] = []
    for file_path in repo_files:
        file_tokens = _hint_tokens(file_path)
        common = len(file_tokens & task_tokens)
        if common > 0 or _looks_like_config_or_manifest(file_path):
            scored_files.append((common, file_path))

    scored_files.sort(key=lambda x: (-x[0], x[1]))
    return [f for _, f in scored_files[:max_files]]


def _looks_like_config_or_manifest(file_path: str) -> bool:
    """Check if file is a config/manifest that's always useful."""
    normalized = file_path.lower().replace("\\", "/")
    config_names = {
        "readme.md",
        "claude.md",
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "requirements.txt",
        "package.json",
        "tsconfig.json",
        ".env.example",
        "docker-compose.yml",
        "dockerfile",
    }
    basename = normalized.rsplit("/", 1)[-1]
    return basename in config_names or normalized.endswith("/__init__.py")


def _filter_git_diff_by_keywords(
    diff_text: str,
    task_direction: str = "",
    max_chars: int = 5000,
) -> str:
    """Filter git diff to only include changes relevant to the task.

    Strategy B: Extract file paths from diff and match against task keywords.
    Only includes diffs for files matching task tokens.
    """
    if not diff_text.strip():
        return ""

    task_tokens = _hint_tokens(task_direction)

    lines = diff_text.split("\n")
    result_lines: list[str] = []
    include_current_file = False
    included_chars = 0

    for line in lines:
        if line.startswith("diff --git "):
            parts = line.split()
            if len(parts) >= 4:
                file_path = parts[3].lstrip("b/")
                # If we have tokens, use token matching; otherwise use basic heuristics
                if task_tokens:
                    file_tokens = _hint_tokens(file_path)
                    include_current_file = bool(file_tokens & task_tokens) or _looks_like_config_or_manifest(file_path)
                else:
                    # Fallback: include Python files in src/backend/services, tests, and config
                    path_lower = file_path.lower()
                    include_current_file = (
                        ("/src/" in path_lower or path_lower.startswith("src/")) and ".py" in path_lower
                    ) or _looks_like_config_or_manifest(file_path)
            else:
                include_current_file = False

        if include_current_file:
            result_lines.append(line)
            included_chars += len(line) + 1
            if included_chars >= max_chars:
                break

    # If no files matched, include nothing rather than everything
    if not result_lines:
        return ""

    return "\n".join(result_lines[:1000])[:max_chars]


PRECONDITION_REPLAN_HINT_FILENAME = ".precondition_replan_hint.json"


def _load_precondition_replan_hint(planning_dir: Path | None) -> dict[str, Any]:
    """Read the structured replan hint left by autopilot when a previous run
    hit a readiness BLOCK. The planner uses this to insert a prerequisite
    schema / migration task in front of the dependent task."""

    if planning_dir is None:
        return {}
    path = Path(planning_dir) / PRECONDITION_REPLAN_HINT_FILENAME
    if not path.exists() or not path.is_file():
        return {}
    try:
        import json as _json
        payload = _json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def collect_planning_context(
    *,
    project_root: Path,
    repo_inventory: dict[str, Any],
    prd_path: Path | None = None,
    task_direction: str = "",
    feature: str = "",
    max_lines_per_doc: int = DEFAULT_MAX_LINES_PER_DOC,
    planning_dir: Path | None = None,
) -> dict[str, Any]:
    root = project_root.resolve()
    claude_md = _read_doc_safe(root / "CLAUDE.md", max_lines=max_lines_per_doc * 4)
    task_plan_docs = _find_docs_by_pattern(root, ["docs/任务计划*.md"])
    task_plans = "\n\n".join(_read_doc_safe(path, max_lines=max_lines_per_doc) for path in task_plan_docs if path.exists()).strip()
    dev_status = _read_doc_safe(root / "docs" / "开发交付现状.md", max_lines=max_lines_per_doc)
    prd_coverage = _read_doc_safe(root / "docs" / "prd_coverage_matrix.md", max_lines=max_lines_per_doc)
    readme_excerpt = _first_lines(root / "README.md", max_lines=100)
    prd_excerpt = _first_lines(prd_path.resolve(), max_lines=300) if prd_path is not None and prd_path.exists() else ""
    recent_commits = _git_recent_log(root, count=20)
    uncommitted_changes = _git_uncommitted_stat(root)
    git_head = _git_head(root)
    manifest = _repo_manifest(root, repo_inventory=repo_inventory, limit=DEFAULT_MANIFEST_LIMIT)
    file_manifest = build_file_manifest(manifest)
    candidates = _candidate_paths(
        project_root=root,
        manifest=manifest,
        task_direction=task_direction,
        prd_excerpt=prd_excerpt,
        recent_commits=recent_commits,
        uncommitted_changes=uncommitted_changes,
        limit=DEFAULT_SNIPPET_LIMIT,
    )
    snippets = _candidate_snippets(project_root=root, candidates=candidates)

    instinct_risk_zones = _collect_instinct_summary(root)
    telemetry_summary = _collect_telemetry_summary(root, feature=feature)
    lane_stability = _collect_lane_stability_summary(root)
    failing_baseline = collect_failing_baseline(
        project_root=root,
        task_direction=task_direction,
        prd_excerpt=prd_excerpt,
    )

    precondition_hint = _load_precondition_replan_hint(planning_dir)
    payload: dict[str, Any] = {
        "schema_version": "planning.context.v1",
        "task_direction": _clean_text(task_direction),
        "project_root": str(root),
        "prd_path": str(prd_path.resolve()) if prd_path is not None and prd_path.exists() else "",
        "claude_md": claude_md,
        "task_plans": task_plans,
        "dev_status": dev_status,
        "prd_coverage": prd_coverage,
        "prd_excerpt": prd_excerpt,
        "readme_excerpt": readme_excerpt,
        "recent_commits": recent_commits,
        "uncommitted_changes": uncommitted_changes,
        "precondition_replan_hint": precondition_hint,
        "repo_inventory_summary": {
            "archetype": _clean_text(repo_inventory.get("archetype")),
            "code_roots": [str(item) for item in list(dict(repo_inventory.get("project_layout") or {}).get("code_roots") or []) if _clean_text(item)],
            "capabilities": [str(item) for item in list(repo_inventory.get("capabilities") or []) if _clean_text(item)],
        },
        "repo_manifest": {
            "files": manifest,
        },
        "file_manifest": file_manifest,
        "candidate_snippets": snippets,
        "instinct_risk_zones": instinct_risk_zones,
        "telemetry_summary": telemetry_summary,
        "lane_stability": lane_stability,
        "failing_baseline": failing_baseline,
        "hashes": {
            "task_direction": _text_hash(task_direction),
            "prd": _text_hash(prd_excerpt),
            "claude_md": _text_hash(claude_md),
            "docs": _text_hash("\n".join([task_plans, dev_status, prd_coverage, readme_excerpt])),
            "repo_inventory": _json_hash(dict(repo_inventory or {})),
            "git_head": _text_hash(git_head),
            "git_diff_stat": _text_hash(uncommitted_changes),
            "untracked_files": _untracked_content_hash(root),
            "instinct_risk_zones": _text_hash(json.dumps(instinct_risk_zones, sort_keys=True)),
            "telemetry_signals": _text_hash(json.dumps(telemetry_summary, sort_keys=True)),
            "lane_stability": _text_hash(json.dumps(lane_stability, sort_keys=True)),
            "failing_baseline": _text_hash(json.dumps(failing_baseline, sort_keys=True)),
        },
    }
    payload["input_fingerprint"] = compute_input_fingerprint(payload)
    return payload


def compute_input_fingerprint(context: dict[str, Any]) -> str:
    hashes = dict(context.get("hashes") or {})
    pieces = [
        _clean_text(context.get("task_direction")),
        _clean_text(context.get("prd_path")),
        _clean_text(hashes.get("task_direction")),
        _clean_text(hashes.get("prd")),
        _clean_text(hashes.get("claude_md")),
        _clean_text(hashes.get("docs")),
        _clean_text(hashes.get("repo_inventory")),
        _clean_text(hashes.get("git_head")),
        _clean_text(hashes.get("git_diff_stat")),
        _clean_text(hashes.get("untracked_files")),
        # feedback-loop hashes (Section 3.4 of Harness absorption plan)
        _clean_text(hashes.get("instinct_risk_zones")),
        _clean_text(hashes.get("telemetry_signals")),
        _clean_text(hashes.get("lane_stability")),
        _clean_text(hashes.get("failing_baseline")),
    ]
    digest = hashlib.sha256("|".join(pieces).encode("utf-8", errors="replace")).hexdigest()
    return f"sha256:{digest}"


_MIGRATION_FILENAME_RE = re.compile(r"^(\d{8})_(\d{3})_(.+)\.sql$", re.IGNORECASE)


def _suggest_next_migration_filename(project_root: Path | None, sample_column: str) -> str:
    """Look up the highest existing ``YYYYMMDD_NNN_*.sql`` migration and
    suggest the next concrete filename so the planner does not paste a
    ``<NEXT>`` placeholder into task_card.files_to_change. The runtime path
    guard rejects placeholder paths because they do not match the resolved
    concrete file the executor would write."""

    fallback = f"backend/db/migration_sql/<USE_REAL_DATE>_<NEXT_NUMBER>_add_{sample_column}.sql"
    if project_root is None:
        return fallback
    migration_dir = Path(project_root) / "backend" / "db" / "migration_sql"
    if not migration_dir.exists() or not migration_dir.is_dir():
        return fallback
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    highest_num = 0
    for entry in migration_dir.iterdir():
        if not entry.is_file():
            continue
        match = _MIGRATION_FILENAME_RE.match(entry.name)
        if match is None:
            continue
        try:
            number = int(match.group(2))
        except ValueError:
            continue
        if number > highest_num:
            highest_num = number
    next_num = highest_num + 1
    return f"backend/db/migration_sql/{today}_{next_num:03d}_add_{sample_column}.sql"


def _render_field_evidence_lines(evidence: dict[str, Any]) -> list[str]:
    create_count = int(evidence.get("ddl_create_matches") or 0)
    alter_count = int(evidence.get("ddl_alter_matches") or 0)
    code_files = list(evidence.get("code_string_files") or [])
    out = [
        f"      structural DDL evidence: CREATE TABLE matches={create_count}, "
        f"ALTER TABLE ADD COLUMN matches={alter_count}"
    ]
    if code_files:
        sample = ", ".join(code_files[:3])
        suffix = " (...)" if len(code_files) > 3 else ""
        out.append(
            f"      code-string mentions ({len(code_files)} files, NOT proof of column existence): {sample}{suffix}"
        )
    conclusion = _clean_text(evidence.get("conclusion"))
    if conclusion:
        out.append(f"      conclusion: {conclusion}")
    return out


def _render_missing_field_lines(missing_field: list[str], field_evidence: dict[str, Any]) -> list[str]:
    out = ["", "Missing schema fields (require ALTER/CREATE in a prereq task):"]
    for item in missing_field:
        out.append(f"  - {item}")
        evidence = field_evidence.get(item) if isinstance(field_evidence.get(item), dict) else None
        if evidence:
            out.extend(_render_field_evidence_lines(evidence))
    return out


def _render_prereq_task_template(missing_field: list[str], project_root: Path | None) -> list[str]:
    sample = missing_field[0]
    sample_table = sample.split(".", 1)[0] if "." in sample else "table"
    sample_column = sample.split(".", 1)[1] if "." in sample else "column"
    suggested_path = _suggest_next_migration_filename(project_root, sample_column)
    return [
        "",
        "Concrete prereq task shape — emit one task like this BEFORE the dependent task:",
        "CRITICAL: copy the suggested file path verbatim. Do NOT substitute placeholders",
        "like <NEXT> or <DATE>; the runtime path guard rejects placeholder strings.",
        "```json",
        "{",
        f'  "task_id": "T0_ADD_{sample_column.upper()}",',
        f'  "task_name": "Add {sample} via migration",',
        '  "why_this_layer": "schema migration unblocks downstream task",',
        f'  "files_to_change": ["{suggested_path}"],',
        f'  "new_files": ["{suggested_path}"],',
        '  "invariants": ["additive migration only; existing rows nullable"],',
        f'  "test_plan": "PRAGMA table_info({sample_table}) shows the column",',
        '  "requires": []',
        "}",
        "```",
    ]


def _render_precondition_hint(hint: dict[str, Any], *, project_root: Path | None = None) -> str:
    if not hint:
        return ""
    missing_field = [str(item) for item in list(hint.get("missing_field_preconditions") or []) if str(item).strip()]
    missing_symbol = [str(item) for item in list(hint.get("missing_symbol_preconditions") or []) if str(item).strip()]
    suggested = _clean_text(hint.get("suggested_next_task"))
    if not (missing_field or missing_symbol or suggested):
        return ""
    field_evidence = dict(hint.get("field_evidence") or {})
    lines: list[str] = [
        "A previous autopilot run hit BLOCKED_BY_PRECONDITION on this feature.",
        "You MUST insert a prerequisite schema/migration/symbol task BEFORE the dependent task.",
        "Do NOT mark these as `existing: true` again — readiness verified they are absent from the DDL.",
    ]
    if missing_field:
        lines.extend(_render_missing_field_lines(missing_field, field_evidence))
    if missing_symbol:
        lines.append("")
        lines.append("Missing Python symbols (require module/function in a prereq task):")
        lines.extend(f"  - {item}" for item in missing_symbol)
    if missing_field:
        lines.extend(_render_prereq_task_template(missing_field, project_root))
    if suggested:
        lines.append("")
        lines.append(f"Suggested next task hint: {suggested}")
    return "\n".join(lines)


def _render_review_triggered_evidence(packs: list[Any]) -> str:
    rendered: list[str] = []
    for pack in packs[:3]:
        if not isinstance(pack, dict):
            continue
        round_number = _clean_text(pack.get("round_number"))
        for request in list(pack.get("requests") or [])[:4]:
            if not isinstance(request, dict):
                continue
            finding_id = _clean_text(request.get("finding_id"))
            status = _clean_text(request.get("status"))
            category = _clean_text(request.get("category"))
            claim = _clean_text(request.get("reviewer_claim"))[:700]
            rendered.append(
                f"- finding_id={finding_id} round={round_number} category={category} status={status}\n"
                f"  reviewer_claim: {claim}"
            )
            for evidence in list(request.get("evidence") or [])[:5]:
                if not isinstance(evidence, dict):
                    continue
                ref_id = _clean_text(evidence.get("ref_id"))
                source = _clean_text(evidence.get("source"))
                excerpt = _clean_text(evidence.get("excerpt"))[:360]
                if ref_id and excerpt:
                    rendered.append(f"  evidence_ref={ref_id} source={source}: {excerpt}")
    if not rendered:
        return ""
    return (
        "Reviewer raised factual objections. For each finding_id below add an "
        "evidence_resolutions entry with status, evidence_refs, and rationale. "
        "Allowed status values: finding_refuted (cite refs from this pack to "
        "disprove the claim), finding_supported (accept the finding and revise "
        "the plan accordingly — closing the request), ambiguous (only when "
        "evidence is genuinely inconclusive; a finding that stays ambiguous "
        "for 2 consecutive rounds will escalate the run).\n"
        + "\n".join(rendered)
    )


def _coerce_project_root(value: Any) -> Path | None:
    if isinstance(value, Path):
        return value
    if not value:
        return None
    try:
        return Path(str(value))
    except (TypeError, ValueError):
        return None


def render_context_for_prompt(
    context: dict[str, Any],
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    planning_round: int = 1,
    incremental_mode: bool = False,
) -> str:
    """Render planning context with optional incremental loading (Strategy C).

    Args:
        context: Full planning context dict
        max_chars: Maximum chars to include
        planning_round: Current planning round (1, 2, 3+)
        incremental_mode: If True, load context progressively based on round
            - Round 1: Minimal (task, PRD, hints only)
            - Round 2: Add docs and uncommitted changes
            - Round 3+: Full context
    """
    precondition_hint_text = _render_precondition_hint(
        dict(context.get("precondition_replan_hint") or {}),
        project_root=_coerce_project_root(context.get("project_root")),
    )

    # Strategy C: Incremental context loading by planning round
    # Round 1: Minimal (task + PRD + hints) - ~20KB
    # Round 2: Add docs/uncommitted - ~40KB
    # Round 3+: Full context - ~100KB+
    if incremental_mode:
        # Round 1: Core task definition only
        if planning_round == 1:
            sections = [
                _Section("task_direction", "Task Direction", _clean_text(context.get("task_direction")), 0),
                _Section(
                    "precondition_replan_hint",
                    "PRECONDITION REPLAN HINT (override task ordering)",
                    precondition_hint_text,
                    0.1,
                ),
                _Section(
                    "canonical_path_hints",
                    "Canonical Path Hints — use full paths in files_to_change",
                    _build_canonical_hints(
                        dict(context.get("file_manifest") or {}),
                        task_direction=_clean_text(context.get("task_direction")),
                        mention_texts=[
                            _clean_text(context.get("task_direction")),
                            _clean_text(context.get("claude_md")),
                            _clean_text(context.get("task_plans")),
                        ],
                        max_entries=100,
                    )[:4000],
                    0.5,
                ),
                _Section(
                    "prd_excerpt",
                    "PRD Excerpt (AUTHORITATIVE — the single source of truth for routes, "
                    "function signatures, error contracts, and response shapes; do NOT invent "
                    "alternatives)",
                    _clean_text(context.get("prd_excerpt")),
                    0.6,
                ),
                _Section("claude_md", "CLAUDE.md", _clean_text(context.get("claude_md")), 1),
                _Section("task_plans", "Task Plans", _clean_text(context.get("task_plans")), 2),
            ]
        # Round 2: Add docs and recent activity
        elif planning_round == 2:
            sections = [
                _Section("task_direction", "Task Direction", _clean_text(context.get("task_direction")), 0),
                _Section(
                    "precondition_replan_hint",
                    "PRECONDITION REPLAN HINT (override task ordering)",
                    precondition_hint_text,
                    0.1,
                ),
                _Section(
                    "canonical_path_hints",
                    "Canonical Path Hints — use full paths in files_to_change",
                    _build_canonical_hints(
                        dict(context.get("file_manifest") or {}),
                        task_direction=_clean_text(context.get("task_direction")),
                        mention_texts=[
                            _clean_text(context.get("task_direction")),
                            _clean_text(context.get("claude_md")),
                            _clean_text(context.get("task_plans")),
                        ],
                        max_entries=100,
                    )[:4000],
                    0.5,
                ),
                _Section(
                    "prd_excerpt",
                    "PRD Excerpt (AUTHORITATIVE — the single source of truth for routes, "
                    "function signatures, error contracts, and response shapes; do NOT invent "
                    "alternatives)",
                    _clean_text(context.get("prd_excerpt")),
                    0.6,
                ),
                _Section("claude_md", "CLAUDE.md", _clean_text(context.get("claude_md")), 1),
                _Section("task_plans", "Task Plans", _clean_text(context.get("task_plans")), 2),
                _Section("dev_status", "Dev Status", _clean_text(context.get("dev_status")), 3),
                _Section("readme_excerpt", "README Excerpt", _clean_text(context.get("readme_excerpt")), 9),
                _Section("recent_commits", "Recent Commits", _clean_text(context.get("recent_commits")), 7),
                _Section(
                    "uncommitted_changes",
                    "Uncommitted Changes",
                    _filter_git_diff_by_keywords(
                        _clean_text(context.get("uncommitted_changes")),
                        task_direction=_clean_text(context.get("task_direction")),
                        max_chars=5000,
                    ),
                    8,
                ),
            ]
        # Round 3+: Full context
        else:
            sections = [
                _Section("task_direction", "Task Direction", _clean_text(context.get("task_direction")), 0),
                _Section(
                    "precondition_replan_hint",
                    "PRECONDITION REPLAN HINT (override task ordering)",
                    precondition_hint_text,
                    0.1,
                ),
                _Section(
                    "canonical_path_hints",
                    "Canonical Path Hints — use full paths in files_to_change",
                    _build_canonical_hints(
                        dict(context.get("file_manifest") or {}),
                        task_direction=_clean_text(context.get("task_direction")),
                        mention_texts=[
                            _clean_text(context.get("task_direction")),
                            _clean_text(context.get("claude_md")),
                            _clean_text(context.get("task_plans")),
                        ],
                        max_entries=100,
                    )[:4000],
                    0.5,
                ),
                _Section(
                    "prd_excerpt",
                    "PRD Excerpt (AUTHORITATIVE — the single source of truth for routes, "
                    "function signatures, error contracts, and response shapes; do NOT invent "
                    "alternatives)",
                    _clean_text(context.get("prd_excerpt")),
                    0.6,
                ),
                _Section("claude_md", "CLAUDE.md", _clean_text(context.get("claude_md")), 1),
                _Section("task_plans", "Task Plans", _clean_text(context.get("task_plans")), 2),
                _Section("dev_status", "Dev Status", _clean_text(context.get("dev_status")), 3),
                _Section("prd_coverage", "PRD Coverage Matrix", _clean_text(context.get("prd_coverage")), 5),
                _Section("repo_inventory", "Repo Inventory Summary", json.dumps(context.get("repo_inventory_summary") or {}, ensure_ascii=False, indent=2), 6),
                _Section("recent_commits", "Recent Commits", _clean_text(context.get("recent_commits")), 7),
                _Section(
                    "uncommitted_changes",
                    "Uncommitted Changes",
                    _filter_git_diff_by_keywords(
                        _clean_text(context.get("uncommitted_changes")),
                        task_direction=_clean_text(context.get("task_direction")),
                        max_chars=5000,
                    ),
                    8,
                ),
                _Section(
                    "instinct_risk",
                    "Instinct Risk Zones",
                    _render_instinct_summary(dict(context.get("instinct_risk_zones") or {}))[:2000],
                    8.5,
                ),
                _Section(
                    "telemetry_signals",
                    "Telemetry Signals",
                    _render_telemetry_summary(dict(context.get("telemetry_summary") or {}))[:1000],
                    8.6,
                ),
                _Section(
                    "lane_stability",
                    "Lane Stability",
                    _render_lane_summary(dict(context.get("lane_stability") or {}))[:1000],
                    8.7,
                ),
                _Section(
                    "failing_baseline",
                    "Failing Baseline Probe",
                    _render_failing_baseline(dict(context.get("failing_baseline") or {}))[:2200],
                    0.65,
                ),
                _Section(
                    "review_triggered_evidence",
                    "Review-Triggered Evidence Pack",
                    _render_review_triggered_evidence(list(context.get("review_triggered_evidence") or []))[:6000],
                    0.66,
                ),
                _Section("readme_excerpt", "README Excerpt", _clean_text(context.get("readme_excerpt")), 9),
                _Section(
                    "repo_manifest",
                    "Repo Manifest",
                    "\n".join(
                        f"- {item}"
                        for item in _filter_repo_manifest_by_keywords(
                            list(dict(context.get("repo_manifest") or {}).get("files") or []),
                            task_direction=_clean_text(context.get("task_direction")),
                            mention_texts=[
                                _clean_text(context.get("task_direction")),
                                _clean_text(context.get("claude_md")),
                                _clean_text(context.get("prd_excerpt")),
                            ],
                            max_files=50,
                        )
                    ),
                    10,
                ),
                _Section(
                    "candidate_snippets",
                    "Candidate Snippets",
                    "\n\n".join(
                        f"[{item.get('path', '')}]\n{item.get('snippet', '')}"
                        for item in list(context.get("candidate_snippets") or [])[:8]
                        if isinstance(item, dict)
                    ),
                    0.7,
                ),
            ]
    else:
        # Non-incremental: full context with smart filtering (Strategies A + B)
        sections = [
            _Section("task_direction", "Task Direction", _clean_text(context.get("task_direction")), 0),
            _Section(
                "precondition_replan_hint",
                "PRECONDITION REPLAN HINT (override task ordering)",
                precondition_hint_text,
                0.1,
            ),
            _Section(
                "canonical_path_hints",
                "Canonical Path Hints — use full paths in files_to_change",
                _build_canonical_hints(
                    dict(context.get("file_manifest") or {}),
                    task_direction=_clean_text(context.get("task_direction")),
                    mention_texts=[
                        _clean_text(context.get("task_direction")),
                        _clean_text(context.get("claude_md")),
                        _clean_text(context.get("task_plans")),
                    ],
                    max_entries=100,
                )[:4000],
                0.5,
            ),
            _Section(
                "prd_excerpt",
                "PRD Excerpt (AUTHORITATIVE — the single source of truth for routes, "
                "function signatures, error contracts, and response shapes; do NOT invent "
                "alternatives)",
                _clean_text(context.get("prd_excerpt")),
                0.6,
            ),
            _Section("claude_md", "CLAUDE.md", _clean_text(context.get("claude_md")), 1),
            _Section("task_plans", "Task Plans", _clean_text(context.get("task_plans")), 2),
            _Section("dev_status", "Dev Status", _clean_text(context.get("dev_status")), 3),
            _Section("prd_coverage", "PRD Coverage Matrix", _clean_text(context.get("prd_coverage")), 5),
            _Section("repo_inventory", "Repo Inventory Summary", json.dumps(context.get("repo_inventory_summary") or {}, ensure_ascii=False, indent=2), 6),
            _Section("recent_commits", "Recent Commits", _clean_text(context.get("recent_commits")), 7),
            _Section(
                "uncommitted_changes",
                "Uncommitted Changes",
                _filter_git_diff_by_keywords(
                    _clean_text(context.get("uncommitted_changes")),
                    task_direction=_clean_text(context.get("task_direction")),
                    max_chars=5000,
                ),
                8,
            ),
            _Section(
                "instinct_risk",
                "Instinct Risk Zones",
                _render_instinct_summary(dict(context.get("instinct_risk_zones") or {}))[:2000],
                8.5,
            ),
            _Section(
                "telemetry_signals",
                "Telemetry Signals",
                _render_telemetry_summary(dict(context.get("telemetry_summary") or {}))[:1000],
                8.6,
            ),
            _Section(
                "lane_stability",
                "Lane Stability",
                _render_lane_summary(dict(context.get("lane_stability") or {}))[:1000],
                8.7,
            ),
            _Section(
                "failing_baseline",
                "Failing Baseline Probe",
                _render_failing_baseline(dict(context.get("failing_baseline") or {}))[:2200],
                0.65,
            ),
            _Section(
                "review_triggered_evidence",
                "Review-Triggered Evidence Pack",
                _render_review_triggered_evidence(list(context.get("review_triggered_evidence") or []))[:6000],
                0.66,
            ),
            _Section("readme_excerpt", "README Excerpt", _clean_text(context.get("readme_excerpt")), 9),
            _Section(
                "repo_manifest",
                "Repo Manifest",
                "\n".join(
                    f"- {item}"
                    for item in _filter_repo_manifest_by_keywords(
                        list(dict(context.get("repo_manifest") or {}).get("files") or []),
                        task_direction=_clean_text(context.get("task_direction")),
                        mention_texts=[
                            _clean_text(context.get("task_direction")),
                            _clean_text(context.get("claude_md")),
                            _clean_text(context.get("prd_excerpt")),
                        ],
                        max_files=50,
                    )
                ),
                10,
            ),
            _Section(
                "candidate_snippets",
                "Candidate Snippets",
                "\n\n".join(
                    f"[{item.get('path', '')}]\n{item.get('snippet', '')}"
                    for item in list(context.get("candidate_snippets") or [])[:8]
                    if isinstance(item, dict)
                ),
                0.7,
            ),
        ]
    rendered_parts: list[str] = []
    unlimited = int(max_chars) <= 0
    remaining = max(2000, int(max_chars)) if not unlimited else 0
    for section in sorted(sections, key=lambda item: item.priority):
        text = _clean_text(section.value)
        if not text:
            continue
        block = f"## {section.title}\n{text}\n"
        if unlimited:
            rendered_parts.append(block)
            continue
        if len(block) <= remaining:
            rendered_parts.append(block)
            remaining -= len(block)
            continue
        if remaining <= 200:
            break
        truncated = block[: remaining - 1].rstrip()
        rendered_parts.append(f"{truncated}\n")
        break
    return "\n".join(rendered_parts).strip()


__all__ = [
    "build_file_manifest",
    "collect_failing_baseline",
    "collect_planning_context",
    "compute_input_fingerprint",
    "render_context_for_prompt",
    "resolve_plan_paths",
    "_safe_avg",
    "_collect_instinct_summary",
    "_collect_telemetry_summary",
    "_collect_lane_stability_summary",
]

