"""Heavy-tier autopilot end-to-end verification script.

Runs a clean heavy autopilot against a given project_root and reports
stage-by-stage status for: execution -> peer review -> gate -> task_cycle
-> release_tail. Exits non-zero only when a stage that should be reached
is missing or shows a regression versus the known-good baseline.

Usage (PowerShell or bash):
    python scripts/verify_heavy_chain.py \
        --project-root E:/code_rebuild/newsapp-workflow-test \
        --feature gate-fallback-verify-<tag> \
        --requirements-file <path>.txt

The script does NOT start its own Claude session; it shells out to
`kodawari autopilot` and parses the resulting JSON payload plus
the artifacts the run writes into the planning dir.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _gate_from_rounds(planning_dir: Path) -> str:
    rounds = planning_dir / ".autopilot_rounds.jsonl"
    if not rounds.exists():
        return "MISSING"
    try:
        lines = rounds.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return "UNREADABLE"
    for line in reversed(lines):
        try:
            record = json.loads(line.strip())
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        if str(record.get("stage") or "").upper() not in {"RULES_GATE", "PROCEED_TO_GATE"}:
            continue
        details = record.get("details") or {}
        gate = details.get("gate_check") or {}
        status = str(gate.get("total_status") or "").upper()
        if status:
            return status
    return "UNKNOWN"


def _run_autopilot(
    *,
    kodawari: Path,
    project_root: Path,
    feature: str,
    requirements_file: Path,
    payload_out: Path,
    stderr_out: Path,
) -> int:
    cmd = [
        str(kodawari),
        "autopilot",
        "--project-root", str(project_root),
        "--feature", feature,
        "--requirements-file", str(requirements_file),
        "--tier", "heavy",
        "--max-cycles", "1",
    ]
    print(f"[run] {' '.join(cmd)}")
    with payload_out.open("wb") as out, stderr_out.open("wb") as err:
        completed = subprocess.run(cmd, stdout=out, stderr=err, check=False)
    return int(completed.returncode)


def _summarize(payload_path: Path, planning_dir: Path) -> dict[str, Any]:
    payload = _load_json(payload_path)
    exec_result = _load_json(planning_dir / ".execution_result.json")
    review_result = _load_json(planning_dir / ".review_result.json")
    verify_report = _load_json(planning_dir / ".verify_report.json")
    qa_report = _load_json(planning_dir / ".qa_report.json")
    workflow_chain = payload.get("workflow_chain") or {}
    release_tail = payload.get("release_tail") or {}
    final_outcome = workflow_chain.get("final_outcome") or payload.get("final_outcome") or {}
    task_cycle = workflow_chain.get("task_cycle") or {}
    return {
        "status": payload.get("status"),
        "run_reason": payload.get("run_reason"),
        "final_outcome": {
            "status": final_outcome.get("status"),
            "reason": final_outcome.get("reason"),
            "blocking_reason": final_outcome.get("blocking_reason"),
        },
        "execution": {
            "status": exec_result.get("status"),
            "error_code": exec_result.get("error_code"),
            "changed_files": exec_result.get("changed_files") or [],
        },
        "review": {"status": review_result.get("status")},
        "verify": {"status": verify_report.get("status")},
        "gate_from_rounds": _gate_from_rounds(planning_dir),
        "qa": {
            "status": qa_report.get("status"),
            "blocking_reason": qa_report.get("blocking_reason"),
        },
        "task_cycle": {
            "entered": task_cycle.get("entered"),
            "tasks_total": task_cycle.get("tasks_total"),
            "tasks_completed": task_cycle.get("tasks_completed"),
            "blocked": task_cycle.get("blocked"),
        },
        "release_tail": {
            "status": release_tail.get("status"),
            "blocking_reason": release_tail.get("blocking_reason"),
            "completed_stages": release_tail.get("completed_stages") or [],
            "blocked_stage": release_tail.get("blocked_stage"),
        },
    }


def _verdict(summary: dict[str, Any]) -> tuple[str, list[str]]:
    issues: list[str] = []
    exec_status = str(summary["execution"].get("status") or "").upper()
    if exec_status in {"BLOCKED", "FAIL", "ERROR"}:
        reason = summary["execution"].get("error_code") or "unknown"
        issues.append(f"execution stage {exec_status} ({reason})")

    gate_from_rounds = summary["gate_from_rounds"]
    if gate_from_rounds == "MISSING":
        issues.append("no RULES_GATE round found in .autopilot_rounds.jsonl")
    elif gate_from_rounds not in {"PASS", "UNKNOWN"}:
        issues.append(f"gate (from rounds) was {gate_from_rounds}, expected PASS")

    qa_status = str(summary["qa"].get("status") or "").upper()
    if qa_status == "BLOCKED":
        reason = summary["qa"].get("blocking_reason") or ""
        if "gate result unavailable" in str(reason).lower():
            issues.append("QA still BLOCKED on 'gate result unavailable' (gate fallback not hit)")

    rt_status = str(summary["release_tail"].get("status") or "").upper()
    task_cycle_entered = bool(summary["task_cycle"].get("entered"))
    if exec_status == "PASS" and not task_cycle_entered:
        issues.append("task_cycle was not entered despite execution PASS")
    if rt_status == "BLOCKED" and gate_from_rounds == "PASS":
        issues.append(
            f"release_tail BLOCKED ({summary['release_tail'].get('blocking_reason','')}) "
            "even though gate PASS is derivable from rounds"
        )

    verdict = "PASS" if not issues else "FAIL"
    return verdict, issues


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Heavy-tier autopilot chain verifier")
    parser.add_argument("--project-root", required=True, type=Path)
    parser.add_argument("--feature", required=True)
    parser.add_argument("--requirements-file", required=True, type=Path)
    parser.add_argument(
        "--kodawari",
        type=Path,
        default=Path(__file__).resolve().parents[1]
        / ".workflow_runtime"
        / "local-env"
        / ".venv"
        / "Scripts"
        / "kodawari",
    )
    parser.add_argument("--output-dir", type=Path, default=None, help="Where to write run artifacts (default: <project_root>/tmp_verify_runs/<feature>)")
    args = parser.parse_args(argv)

    project_root = args.project_root.resolve()
    requirements = args.requirements_file.resolve()
    if not requirements.exists():
        print(f"[fatal] requirements file not found: {requirements}", file=sys.stderr)
        return 2

    out_dir = (args.output_dir or project_root / "tmp_verify_runs" / args.feature).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    payload_path = out_dir / "autopilot_payload.json"
    stderr_path = out_dir / "autopilot_stderr.txt"

    rc = _run_autopilot(
        kodawari=args.kodawari,
        project_root=project_root,
        feature=args.feature,
        requirements_file=requirements,
        payload_out=payload_path,
        stderr_out=stderr_path,
    )
    print(f"[run] kodawari exit: {rc}")

    planning_dir = project_root / "planning" / args.feature
    summary = _summarize(payload_path, planning_dir)
    verdict, issues = _verdict(summary)

    summary_path = out_dir / "verify_summary.json"
    summary_path.write_text(
        json.dumps({"verdict": verdict, "issues": issues, "summary": summary}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("---- summary ----")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"---- verdict: {verdict} ----")
    for item in issues:
        print(f"  - {item}")
    print(f"[out] payload : {payload_path}")
    print(f"[out] summary : {summary_path}")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
