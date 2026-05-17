#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import re
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


FREEZE_BEGIN = "<!-- BEGIN WS-224:V2_FREEZE -->"
FREEZE_END = "<!-- END WS-224:V2_FREEZE -->"
WS224_HEADER = "##### WS-224 V2 基线冻结与文档回写"
WS224_STATUS_PREFIX = "状态: done"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_text(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        text = path.read_text(encoding="utf-8-sig", errors="ignore")
    return text.lstrip("\ufeff")


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(_read_text(path))
    if not isinstance(payload, dict):
        raise ValueError(f"expected object json: {path}")
    return payload


def _git_head(project_root: Path) -> str:
    try:
        run = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            check=False,
        )
        if run.returncode == 0:
            value = run.stdout.strip()
            if value:
                return value
    except OSError:
        pass
    return "UNKNOWN"


def _standing_proof_status(project_root: Path) -> dict[str, Any]:
    planning_dir = project_root / "planning"
    candidates = sorted(planning_dir.glob("*standing*proof*.json"))
    if candidates:
        latest = candidates[-1]
        payload = _read_json(latest)
        return {
            "status": str(payload.get("status") or "UNKNOWN").upper(),
            "source": str(latest.resolve()),
        }
    workflow_file = project_root / ".github" / "workflows" / "kodawari-standing-proof.yml"
    if workflow_file.exists():
        return {
            "status": "CONFIGURED_NO_ARTIFACT",
            "source": str(workflow_file.resolve()),
        }
    return {
        "status": "MISSING",
        "source": "UNAVAILABLE",
    }


def _backend_matrix(project_root: Path) -> list[dict[str, Any]]:
    import sys

    src_path = project_root / "src"
    if str(src_path) not in sys.path:
        sys.path.insert(0, str(src_path))
    from kodawari.autopilot import execution_backend  # pylint: disable=import-outside-toplevel

    names = sorted(name for name in execution_backend.ALLOWED_EXECUTION_BACKENDS if name)
    matrix: list[dict[str, Any]] = []
    for name in names:
        descriptor = execution_backend.execution_backend_descriptor(name)
        matrix.append(descriptor.capabilities())
    return matrix


def _normalize_status(raw: str) -> str:
    text = (raw or "").strip().lower()
    if not text:
        return "unknown"
    if "done" in text or "completed" in text:
        return "done"
    if "deferred" in text:
        return "deferred"
    if "in_progress" in text or "in progress" in text:
        return "in_progress"
    if "pending" in text or "todo" in text:
        return "pending"
    return text


def _ws_statuses(doc_text: str, start: int = 201, end: int = 223) -> dict[str, str]:
    pattern = re.compile(r"#####\s+WS-(\d{3})\b([\s\S]*?)(?=\n#####\s+WS-\d{3}\b|\n###\s+23\.5|\Z)")
    statuses: dict[str, str] = {}
    for match in pattern.finditer(doc_text):
        number = int(match.group(1))
        if number < start or number > end:
            continue
        block = match.group(2)
        status_match = re.search(r"状态\s*:\s*([^\n]+)", block)
        value = _normalize_status(status_match.group(1) if status_match else "unknown")
        statuses[f"WS-{number}"] = value
    for number in range(start, end + 1):
        key = f"WS-{number}"
        statuses.setdefault(key, "unknown")
    return statuses


def _evidence_summary(manifest: dict[str, Any]) -> dict[str, Any]:
    items = manifest.get("items")
    rows = items if isinstance(items, list) else []
    verdicts = Counter(str((row or {}).get("verdict") or "UNKNOWN").upper() for row in rows)
    return {
        "total": int(manifest.get("evidence_total") or len(rows)),
        "verdict_counts": dict(sorted(verdicts.items())),
        "source": str(manifest.get("source_commit") or "UNKNOWN"),
    }


def _build_snapshot(
    *,
    project_root: Path,
    doc_path: Path,
    dynamic_baseline: dict[str, Any],
    evidence_manifest: dict[str, Any],
    next_entry: str,
    timestamp: str,
    source_commit: str,
) -> dict[str, Any]:
    ws_statuses = _ws_statuses(_read_text(doc_path))
    status_counts = Counter(ws_statuses.values())
    return {
        "schema_version": "v2.baseline.freeze.v1",
        "generated_at_utc": timestamp,
        "source_commit": source_commit,
        "project_root": str(project_root.resolve()),
        "dynamic_baseline": dynamic_baseline,
        "redline_status": {
            "src_redline": dict(dynamic_baseline.get("src_redline") or {}),
            "gate": dict(dynamic_baseline.get("gate") or {}),
        },
        "lane_status": {
            "lane": dict(dynamic_baseline.get("lane") or {}),
            "standing_proof": _standing_proof_status(project_root),
        },
        "backend_capability_matrix": _backend_matrix(project_root),
        "ws_statuses_201_223": ws_statuses,
        "ws_status_counts": {
            "done": int(status_counts.get("done", 0)),
            "deferred": int(status_counts.get("deferred", 0)),
            "in_progress": int(status_counts.get("in_progress", 0)),
            "pending": int(status_counts.get("pending", 0)),
            "unknown": int(status_counts.get("unknown", 0)),
        },
        "evidence_overview": _evidence_summary(evidence_manifest),
        "v2_next_entry": next_entry,
    }


def _render_freeze_block(snapshot: dict[str, Any]) -> str:
    baseline = dict(snapshot.get("dynamic_baseline") or {})
    pytest_data = dict(baseline.get("pytest") or {})
    gate = dict(baseline.get("gate") or {})
    lane = dict((snapshot.get("lane_status") or {}).get("lane") or {})
    standing = dict((snapshot.get("lane_status") or {}).get("standing_proof") or {})
    ws_counts = dict(snapshot.get("ws_status_counts") or {})
    evidence = dict(snapshot.get("evidence_overview") or {})
    verdict_counts = dict(evidence.get("verdict_counts") or {})
    matrix = list(snapshot.get("backend_capability_matrix") or [])
    implemented = [row.get("backend") for row in matrix if row.get("implemented")]
    lines = [
        FREEZE_BEGIN,
        "<!-- auto-generated by kodawari/scripts/freeze_v2_baseline.py; do not edit manually -->",
        f"- generated_at_utc: `{snapshot.get('generated_at_utc', '')}`",
        f"- source_commit: `{snapshot.get('source_commit', '')}`",
        f"- pytest_baseline: `collected={pytest_data.get('collected', 0)}, passed={pytest_data.get('passed', 0)}, skipped={pytest_data.get('skipped', 0)}, failed={pytest_data.get('failed', 0)}`",
        f"- redline_status: `limit={((baseline.get('src_redline') or {}).get('limit', 1000))}, violations={((baseline.get('src_redline') or {}).get('violations_over_1000', 0))}, strict_gate={gate.get('status', 'MISSING')}`",
        f"- lane_status: `always-on={lane.get('always_on_status', 'MISSING')}, integration={lane.get('integration_status', 'MISSING')}, standing_proof={standing.get('status', 'MISSING')}`",
        f"- backend_capability_matrix: `total={len(matrix)}, implemented={len([x for x in matrix if x.get('implemented')])}`",
        f"- backend_implemented: `{', '.join([str(x) for x in implemented]) if implemented else '(none)'}`",
        f"- ws_201_223: `done={ws_counts.get('done', 0)}, deferred={ws_counts.get('deferred', 0)}, in_progress={ws_counts.get('in_progress', 0)}, pending={ws_counts.get('pending', 0)}, unknown={ws_counts.get('unknown', 0)}`",
        f"- evidence_overview: `total={evidence.get('total', 0)}, verdicts={json.dumps(verdict_counts, ensure_ascii=False)}`",
        f"- v2_next_entry: `{snapshot.get('v2_next_entry', '')}`",
        FREEZE_END,
    ]
    return "\n".join(lines)


def _replace_or_insert_block(section_text: str, rendered: str) -> str:
    begin = section_text.find(FREEZE_BEGIN)
    end = section_text.find(FREEZE_END)
    if begin >= 0 and end > begin:
        end += len(FREEZE_END)
        return section_text[:begin] + rendered + section_text[end:]
    stripped = section_text.rstrip() + "\n\n" + rendered + "\n"
    return stripped


def _upsert_ws224_status(section_text: str, summary_line: str) -> str:
    status_line = f"{WS224_STATUS_PREFIX} – {summary_line}"
    pattern = re.compile(r"状态\s*:\s*[^\n]+")
    if pattern.search(section_text):
        return pattern.sub(status_line, section_text, count=1)
    return section_text.rstrip() + "\n" + status_line + "\n"


def _update_ws224_section(doc_text: str, rendered_block: str, summary_line: str) -> str:
    start = doc_text.find(WS224_HEADER)
    if start < 0:
        raise ValueError("WS-224 section header not found in document")
    next_header = doc_text.find("\n### 23.5", start)
    if next_header < 0:
        raise ValueError("cannot locate end of WS-224 section")
    section = doc_text[start:next_header]
    section = _replace_or_insert_block(section, rendered_block)
    section = _upsert_ws224_status(section, summary_line)
    return doc_text[:start] + section + doc_text[next_header:]


def run(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root).resolve()
    doc_path = Path(args.doc_path).resolve()
    dynamic_path = Path(args.dynamic_baseline_json).resolve()
    evidence_path = Path(args.evidence_manifest_json).resolve()
    output_json = Path(args.output_json).resolve()
    next_entry = str(args.next_entry or "").strip() or "WS-225"
    timestamp = str(args.timestamp_utc or "").strip() or _utc_now_iso()
    source_commit = str(args.source_commit or "").strip() or _git_head(project_root)

    dynamic_baseline = _read_json(dynamic_path)
    evidence_manifest = _read_json(evidence_path)

    snapshot = _build_snapshot(
        project_root=project_root,
        doc_path=doc_path,
        dynamic_baseline=dynamic_baseline,
        evidence_manifest=evidence_manifest,
        next_entry=next_entry,
        timestamp=timestamp,
        source_commit=source_commit,
    )

    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")

    summary_line = (
        "v2_baseline_snapshot 已冻结；"
        f"WS-201~223: done={snapshot['ws_status_counts']['done']}, deferred={snapshot['ws_status_counts']['deferred']}, "
        f"unknown={snapshot['ws_status_counts']['unknown']}；"
        f"后续入口={snapshot['v2_next_entry']}"
    )
    rendered_block = _render_freeze_block(snapshot)
    doc_text = _read_text(doc_path)
    updated = _update_ws224_section(doc_text, rendered_block, summary_line)
    doc_path.write_text(updated, encoding="utf-8")

    print(
        json.dumps(
            {
                "status": "PASS",
                "output_json": str(output_json),
                "doc_path": str(doc_path),
                "source_commit": source_commit,
                "next_entry": next_entry,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Freeze V2 baseline snapshot and update WS-224 section.")
    parser.add_argument("--project-root", default=".", help="Kodawari project root path.")
    parser.add_argument(
        "--doc-path",
        default="..\\workflowsdk-rebuild.md",
        help="Path to workflowsdk-rebuild.md",
    )
    parser.add_argument(
        "--dynamic-baseline-json",
        default="planning/dynamic_baseline_snapshot.json",
        help="Path to dynamic baseline snapshot JSON.",
    )
    parser.add_argument(
        "--evidence-manifest-json",
        default="evidence/evidence_manifest.json",
        help="Path to evidence manifest JSON.",
    )
    parser.add_argument(
        "--output-json",
        default="planning/v2_baseline_snapshot.json",
        help="Output path for frozen V2 baseline snapshot JSON.",
    )
    parser.add_argument("--next-entry", default="WS-225", help="V2 follow-up entry task identifier.")
    parser.add_argument("--timestamp-utc", default="", help="Optional UTC timestamp override.")
    parser.add_argument("--source-commit", default="", help="Optional commit SHA override.")
    return parser


def main() -> int:
    parser = build_parser()
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
