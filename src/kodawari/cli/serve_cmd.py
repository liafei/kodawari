"""HTTP server bridging kodawari artifacts to the React UI.

Exposes 6 endpoints consumed by ``kodawari/web`` (and the Tauri shell that
wraps it). The server reads contract-first artifacts from disk and spawns
``kodawari work-all`` for new runs; it does not duplicate orchestration
logic.

Endpoints:
  GET  /api/projects?root=<path>                  list planning dirs
  GET  /api/projects/{feature}                    aggregated status snapshot
  GET  /api/projects/{feature}/events             SSE stream of round events
  GET  /api/projects/{feature}/artifacts/{name}   fetch a single artifact
  POST /api/projects                              spawn a new work-all run
  POST /api/projects/{feature}/approve            forward to kodawari approve
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Iterable

try:
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
    from fastapi.staticfiles import StaticFiles
except ImportError:  # pragma: no cover - optional dep at import time
    FastAPI = None  # type: ignore[assignment]
    HTTPException = None  # type: ignore[assignment]
    Request = None  # type: ignore[assignment]
    CORSMiddleware = None  # type: ignore[assignment]
    FileResponse = None  # type: ignore[assignment]
    JSONResponse = None  # type: ignore[assignment]
    Response = None  # type: ignore[assignment]
    StreamingResponse = None  # type: ignore[assignment]
    StaticFiles = None  # type: ignore[assignment]

DEFAULT_PORT = 8765
DEFAULT_HOST = "127.0.0.1"
SSE_POLL_INTERVAL_SECONDS = 0.5
ROUND_LOG_NAME = ".autopilot_rounds.jsonl"
MANIFEST_NAME = ".work_all_manifest.json"
EXECUTION_RESULT_NAME = ".execution_result.json"
TASK_RUN_RESULT_NAME = ".task_run_result.json"
AUTOPILOT_STATE_NAME = ".autopilot_state.json"
PLANNING_CONVERSATION_NAME = "PLANNING_CONVERSATION.json"
TASK_GRAPH_NAME = "TASK_GRAPH.json"
TASK_CARD_ACTIVE_NAME = "TASK_CARD_ACTIVE.json"
RUN_PID_NAME = ".serve_run.pid"
RUN_META_NAME = ".serve_run.meta.json"
SERVE_PROJECTS_SCHEMA_VERSION = "serve.projects.v1"
SERVE_PROJECT_STATUS_SCHEMA_VERSION = "serve.project_status.v1"
SERVE_EVENT_SCHEMA_VERSION = "serve.event.v1"
SERVE_CREATE_PROJECT_SCHEMA_VERSION = "serve.create_project.v1"
SERVE_APPROVE_SCHEMA_VERSION = "serve.approve.v1"
DESKTOP_TELEMETRY_ENV = "WORKFLOW_DESKTOP_TELEMETRY"

ALLOWED_ARTIFACT_NAMES = frozenset(
    {
        MANIFEST_NAME,
        EXECUTION_RESULT_NAME,
        TASK_RUN_RESULT_NAME,
        AUTOPILOT_STATE_NAME,
        PLANNING_CONVERSATION_NAME,
        TASK_GRAPH_NAME,
        TASK_CARD_ACTIVE_NAME,
        "REVIEW.md",
        "QA_REPORT.md",
        "RELEASE.md",
        "CHANGELOG.md",
        "Plans.md",
        "PLAN.md",
        "DESIGN.md",
        "STATUS.md",
        "GATE.md",
        "ACCEPTANCE.md",
        "DELIVERY_REPORT.md",
        "Ship.md",
        "TASKS.md",
        "REPO_INVENTORY.json",
        "COMPLIANCE_REPORT.md",
        "COMPLIANCE_REPORT.json",
    }
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def desktop_telemetry_enabled() -> bool:
    raw = str(os.environ.get(DESKTOP_TELEMETRY_ENV) or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _safe_load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _safe_iter_jsonl(path: Path, *, start_offset: int = 0) -> tuple[list[dict[str, Any]], int]:
    """Read newline-delimited JSON starting from byte offset.

    Returns parsed records plus the new tail offset for streaming callers.
    """

    if not path.exists():
        return [], start_offset
    records: list[dict[str, Any]] = []
    new_offset = start_offset
    try:
        with path.open("rb") as handle:
            handle.seek(start_offset)
            for raw_line in handle:
                new_offset += len(raw_line)
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if isinstance(record, dict):
                    records.append(record)
    except OSError:
        return records, new_offset
    return records, new_offset


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            output = subprocess.check_output(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=5,
            )
        except (subprocess.SubprocessError, FileNotFoundError, OSError):
            return False
        return str(pid) in output
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _planning_dirs(project_root: Path) -> list[Path]:
    candidates: list[Path] = []
    for child in (project_root / "planning").iterdir() if (project_root / "planning").exists() else []:
        if child.is_dir() and not child.name.startswith("_"):
            candidates.append(child)
    return candidates


def _list_projects(project_root: Path) -> list[dict[str, Any]]:
    projects: list[dict[str, Any]] = []
    for planning_dir in _planning_dirs(project_root):
        manifest = _safe_load_json(planning_dir / MANIFEST_NAME) or {}
        state = _safe_load_json(planning_dir / AUTOPILOT_STATE_NAME) or {}
        pid_path = planning_dir / RUN_PID_NAME
        active = False
        if pid_path.exists():
            try:
                pid = int(pid_path.read_text(encoding="utf-8").strip() or "0")
                active = _process_alive(pid)
            except (OSError, ValueError):
                active = False
        try:
            mtime = planning_dir.stat().st_mtime
        except OSError:
            mtime = 0.0
        projects.append(
            {
                "feature": planning_dir.name,
                "planning_dir": str(planning_dir),
                "status": str(manifest.get("status") or state.get("interaction_state") or "UNKNOWN"),
                "summary": str(manifest.get("summary") or ""),
                "active": active,
                "mtime": mtime,
                "mtime_iso": datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat() if mtime else "",
            }
        )
    projects.sort(key=lambda item: item["mtime"], reverse=True)
    return projects


def _resolve_planning_dir(project_root: Path, feature: str) -> Path | None:
    candidate = project_root / "planning" / feature
    return candidate if candidate.is_dir() else None


def _load_models_yaml(project_root: Path) -> dict[str, Any]:
    """Read models.yaml without requiring PyYAML at import time."""

    candidates = [
        project_root / ".claude" / "workflow" / "models.yaml",
        project_root / ".claude" / "workflow" / "models.yml",
    ]
    target: Path | None = next((path for path in candidates if path.exists()), None)
    if target is None:
        return {}
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(target.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _models_summary(project_root: Path) -> dict[str, Any]:
    raw = _load_models_yaml(project_root)
    roles_raw = raw.get("roles") if isinstance(raw, dict) else None
    if not isinstance(roles_raw, dict):
        return {}
    summary: dict[str, Any] = {}
    for role, payload in roles_raw.items():
        if isinstance(payload, dict):
            summary[str(role)] = {
                "model": str(payload.get("model") or ""),
                "transport": str(payload.get("transport") or ""),
            }
    return summary


def _project_status(project_root: Path, planning_dir: Path) -> dict[str, Any]:
    manifest = _safe_load_json(planning_dir / MANIFEST_NAME) or {}
    state = _safe_load_json(planning_dir / AUTOPILOT_STATE_NAME) or {}
    execution = _safe_load_json(planning_dir / EXECUTION_RESULT_NAME) or {}
    task_run = _safe_load_json(planning_dir / TASK_RUN_RESULT_NAME) or {}
    task_graph = _safe_load_json(planning_dir / TASK_GRAPH_NAME) or {}
    task_card = _safe_load_json(planning_dir / TASK_CARD_ACTIVE_NAME) or {}
    rounds_records, _ = _safe_iter_jsonl(planning_dir / ROUND_LOG_NAME)
    stages = _derive_stages(
        manifest=manifest,
        execution=execution,
        task_run=task_run,
        rounds=rounds_records,
        state=state,
    )
    stats = _aggregate_stats(rounds_records)
    return {
        "schema_version": SERVE_PROJECT_STATUS_SCHEMA_VERSION,
        "feature": planning_dir.name,
        "planning_dir": str(planning_dir),
        "manifest": manifest,
        "state": state,
        "execution_result": execution,
        "task_run_result": task_run,
        "task_graph": task_graph,
        "task_card_active": task_card,
        "rounds_count": len(rounds_records),
        "stages": stages,
        "stats": stats,
        "models": _models_summary(project_root),
        "active": _planning_dir_active(planning_dir),
        "generated_at": _utc_now_iso(),
    }


def _planning_dir_active(planning_dir: Path) -> bool:
    pid_path = planning_dir / RUN_PID_NAME
    if not pid_path.exists():
        return False
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        return False
    return _process_alive(pid)


def _derive_stages(
    *,
    manifest: dict[str, Any],
    execution: dict[str, Any],
    task_run: dict[str, Any],
    rounds: Iterable[dict[str, Any]],
    state: dict[str, Any],
) -> list[dict[str, Any]]:
    """Map manifest/state into the 7-stage product timeline used by the UI."""

    rounds_list = list(rounds)
    stage_status_by_name: dict[str, str] = {
        str(step.get("name") or "").lower(): str(step.get("status") or "")
        for step in (manifest.get("steps") or [])
        if isinstance(step, dict)
    }
    plan_status = stage_status_by_name.get("plan", "")
    work_status = stage_status_by_name.get("work", "")
    review_status = stage_status_by_name.get("review", "")
    release_status = stage_status_by_name.get("release", "")

    has_task_graph = bool(execution or task_run or rounds_list)
    execution_pass = str(execution.get("status") or "").upper() == "PASS"
    fix_rounds = [r for r in rounds_list if str(r.get("action") or "").lower() == "fix_round"]
    verify_passed = any(
        str(r.get("stage") or "").upper() == "VERIFY" and str(r.get("stage_status") or "").lower() == "pass"
        for r in rounds_list
    )

    def _phase(*, done: bool, active: bool, failed: bool = False) -> str:
        if failed:
            return "failed"
        if done:
            return "done"
        if active:
            return "active"
        return "pending"

    plan_done = plan_status.upper() == "PASS"
    plan_active = plan_status.upper() in {"RUNNING", "AWAITING_DECISION"}
    plan_failed = plan_status.upper() in {"BLOCKED", "FAILED"}

    split_done = plan_done and has_task_graph
    split_active = plan_done and not has_task_graph

    gen_done = execution_pass
    gen_active = bool(execution and not gen_done)
    gen_failed = bool(execution and str(execution.get("status") or "").upper() in {"BLOCKED", "FAILED"})

    review_done = review_status.upper() == "PASS"
    review_active = review_status.upper() in {"RUNNING"}
    review_failed = review_status.upper() in {"BLOCKED", "FAILED"}

    fix_done = bool(fix_rounds) and review_done
    fix_active = bool(fix_rounds) and not review_done

    test_done = verify_passed
    test_active = bool(rounds_list) and not test_done and review_done

    release_done = release_status.upper() == "PASS"
    release_active = release_status.upper() in {"RUNNING", "AWAITING_DECISION"}
    release_failed = release_status.upper() in {"BLOCKED", "FAILED"}

    stages = [
        {"id": "prd", "label": "PRD 解析", "status": _phase(done=plan_done, active=plan_active, failed=plan_failed)},
        {"id": "split", "label": "任务拆分", "status": _phase(done=split_done, active=split_active)},
        {"id": "gen", "label": "代码生成", "status": _phase(done=gen_done, active=gen_active, failed=gen_failed)},
        {"id": "review", "label": "对抗审查", "status": _phase(done=review_done, active=review_active, failed=review_failed)},
        {"id": "fix", "label": "修复迭代", "status": _phase(done=fix_done, active=fix_active)},
        {"id": "test", "label": "自动测试", "status": _phase(done=test_done, active=test_active)},
        {"id": "done", "label": "交付", "status": _phase(done=release_done, active=release_active, failed=release_failed)},
    ]
    # If nothing has happened past plan, ensure only plan shows active.
    if not has_task_graph and plan_active:
        for stage in stages[1:]:
            stage["status"] = "pending"
    return stages


def _aggregate_stats(rounds: Iterable[dict[str, Any]]) -> dict[str, int]:
    issues = fixed = passed = 0
    for record in rounds:
        details = record.get("details") if isinstance(record.get("details"), dict) else {}
        blocking_items = details.get("blocking_items") if isinstance(details, dict) else []
        if isinstance(blocking_items, list):
            issues += len(blocking_items)
        action = str(record.get("action") or "").lower()
        outcome = str(record.get("round_outcome") or "").lower()
        if action == "fix_round" and outcome == "success":
            fixed += 1
        if outcome == "success":
            passed += 1
    return {"issues": issues, "fixed": fixed, "passed": passed}


def _build_app(default_project_root: Path | None) -> Any:
    if FastAPI is None:
        raise RuntimeError(
            "fastapi is required for `kodawari serve`. Install it via `pip install fastapi uvicorn`."
        )

    app = FastAPI(title="kodawari serve", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173", "tauri://localhost"],
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )

    web_dist = Path(__file__).resolve().parents[3] / "web" / "dist"

    def _project_root_from_query(request: Request, fallback: Path | None) -> Path:
        raw = request.query_params.get("root")
        if raw:
            return Path(raw).resolve()
        if fallback is not None:
            return fallback
        raise HTTPException(status_code=400, detail="missing 'root' query parameter and no default --root")

    @app.get("/api/projects")
    def list_projects(request: Request) -> JSONResponse:
        project_root = _project_root_from_query(request, default_project_root)
        if not project_root.exists():
            raise HTTPException(status_code=404, detail=f"project root does not exist: {project_root}")
        return JSONResponse(
            {
                "schema_version": SERVE_PROJECTS_SCHEMA_VERSION,
                "project_root": str(project_root),
                "projects": _list_projects(project_root),
                "models": _models_summary(project_root),
                "generated_at": _utc_now_iso(),
            }
        )

    @app.get("/api/projects/{feature}")
    def get_project(feature: str, request: Request) -> JSONResponse:
        project_root = _project_root_from_query(request, default_project_root)
        planning_dir = _resolve_planning_dir(project_root, feature)
        if planning_dir is None:
            raise HTTPException(status_code=404, detail=f"feature not found: {feature}")
        return JSONResponse(_project_status(project_root, planning_dir))

    @app.get("/api/projects/{feature}/events")
    async def stream_events(feature: str, request: Request) -> StreamingResponse:
        project_root = _project_root_from_query(request, default_project_root)
        planning_dir = _resolve_planning_dir(project_root, feature)
        if planning_dir is None:
            raise HTTPException(status_code=404, detail=f"feature not found: {feature}")
        rounds_path = planning_dir / ROUND_LOG_NAME

        async def _event_generator() -> AsyncIterator[bytes]:
            offset = 0
            initial_records, offset = _safe_iter_jsonl(rounds_path, start_offset=0)
            for record in initial_records:
                yield _format_sse(record)
            last_status_emit = 0.0
            while True:
                if await request.is_disconnected():
                    return
                new_records, offset = _safe_iter_jsonl(rounds_path, start_offset=offset)
                for record in new_records:
                    yield _format_sse(record)
                now = time.monotonic()
                if now - last_status_emit > 5.0:
                    last_status_emit = now
                    snapshot = _project_status(project_root, planning_dir)
                    yield _format_sse(
                        {
                            "kind": "status_snapshot",
                            "stages": snapshot["stages"],
                            "stats": snapshot["stats"],
                            "active": snapshot["active"],
                            "manifest_status": str(snapshot["manifest"].get("status") or ""),
                        },
                        event="status",
                    )
                await asyncio.sleep(SSE_POLL_INTERVAL_SECONDS)

        return StreamingResponse(_event_generator(), media_type="text/event-stream")

    @app.get("/api/projects/{feature}/artifacts/{name}")
    def get_artifact(feature: str, name: str, request: Request) -> Response:
        if name not in ALLOWED_ARTIFACT_NAMES:
            raise HTTPException(status_code=403, detail=f"artifact not in allowlist: {name}")
        project_root = _project_root_from_query(request, default_project_root)
        planning_dir = _resolve_planning_dir(project_root, feature)
        if planning_dir is None:
            raise HTTPException(status_code=404, detail=f"feature not found: {feature}")
        target = planning_dir / name
        if not target.exists():
            raise HTTPException(status_code=404, detail=f"artifact not found: {name}")
        if name.endswith(".json"):
            return JSONResponse(_safe_load_json(target) or {})
        return FileResponse(target, media_type="text/markdown" if name.endswith(".md") else "text/plain")

    @app.post("/api/projects")
    async def create_project(request: Request) -> JSONResponse:
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="request body must be a JSON object")
        project_root = _project_root_from_query(request, default_project_root)
        if not project_root.exists():
            raise HTTPException(status_code=400, detail=f"project root does not exist: {project_root}")
        feature = str(body.get("feature") or "").strip() or _generate_feature_slug()
        planning_root = project_root / "planning" / feature
        planning_root.mkdir(parents=True, exist_ok=True)
        prd_path = _ensure_prd_file(planning_root, body)
        cli_args = _build_work_all_args(
            project_root=project_root,
            feature=feature,
            prd_path=prd_path,
            body=body,
        )
        out_log = planning_root / "serve_work_all.out.log"
        err_log = planning_root / "serve_work_all.err.log"
        env = dict(os.environ)
        for key in ("WORKFLOW_MIMO_KEY", "WORKFLOW_REVIEW_ENABLED", "WORKFLOW_PLAN_REVIEWER_TIMEOUT", "WORKFLOW_REVIEWER_TIMEOUT"):
            value = body.get(key)
            if isinstance(value, str) and value:
                env[key] = value
        process = subprocess.Popen(
            cli_args,
            cwd=str(_kodawari_repo_root()),
            env=env,
            stdout=out_log.open("w", encoding="utf-8"),
            stderr=err_log.open("w", encoding="utf-8"),
        )
        (planning_root / RUN_PID_NAME).write_text(str(process.pid), encoding="utf-8")
        meta = {
            "schema_version": "serve.run_meta.v1",
            "feature": feature,
            "pid": process.pid,
            "started_at": _utc_now_iso(),
            "cli_args": cli_args,
            "prd_path": str(prd_path),
            "stdout_log": str(out_log),
            "stderr_log": str(err_log),
        }
        (planning_root / RUN_META_NAME).write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        return JSONResponse(
            {
                "schema_version": SERVE_CREATE_PROJECT_SCHEMA_VERSION,
                "feature": feature,
                "pid": process.pid,
                "planning_dir": str(planning_root),
            }
        )

    @app.post("/api/projects/{feature}/approve")
    def approve_project(feature: str, request: Request) -> JSONResponse:
        project_root = _project_root_from_query(request, default_project_root)
        planning_dir = _resolve_planning_dir(project_root, feature)
        if planning_dir is None:
            raise HTTPException(status_code=404, detail=f"feature not found: {feature}")
        cli_args = [
            sys.executable,
            "-m",
            "kodawari.cli.main",
            "approve",
            "--project-root",
            str(project_root),
            "--feature",
            feature,
            "--decision",
            "approve",
        ]
        completed = subprocess.run(
            cli_args,
            cwd=str(_kodawari_repo_root()),
            capture_output=True,
            text=True,
            timeout=120,
        )
        return JSONResponse(
            {
                "schema_version": SERVE_APPROVE_SCHEMA_VERSION,
                "rc": completed.returncode,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
            }
        )

    if web_dist.exists():
        app.mount("/", StaticFiles(directory=str(web_dist), html=True), name="web")
    else:
        @app.get("/")
        def web_placeholder() -> JSONResponse:
            return JSONResponse(
                {
                    "service": "kodawari serve",
                    "web_dist_present": False,
                    "hint": "Run `cd kodawari/web && npm run build` to bundle the UI, or use Vite dev server at :5173.",
                }
            )

    return app


def _format_sse(payload: dict[str, Any], *, event: str | None = None) -> bytes:
    if "schema_version" not in payload:
        payload = {"schema_version": SERVE_EVENT_SCHEMA_VERSION, **payload}
    encoded = json.dumps(payload, ensure_ascii=False)
    parts = []
    if event:
        parts.append(f"event: {event}")
    parts.append(f"data: {encoded}")
    parts.append("")
    parts.append("")
    return "\n".join(parts).encode("utf-8")


def _generate_feature_slug() -> str:
    return f"web-run-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"


def _ensure_prd_file(planning_root: Path, body: dict[str, Any]) -> Path:
    prd_path_value = body.get("prd_path")
    if isinstance(prd_path_value, str) and prd_path_value:
        candidate = Path(prd_path_value)
        if candidate.exists():
            return candidate.resolve()
        raise _http_error(400, f"prd_path does not exist: {prd_path_value}")
    prd_text = body.get("prd_text")
    if not isinstance(prd_text, str) or not prd_text.strip():
        raise _http_error(400, "either 'prd_path' or 'prd_text' is required")
    target = planning_root / "PRD_INPUT.md"
    target.write_text(prd_text, encoding="utf-8")
    return target


def _http_error(status: int, detail: str) -> Exception:
    if HTTPException is None:
        raise RuntimeError("fastapi is not installed")
    return HTTPException(status_code=status, detail=detail)


def _build_work_all_args(
    *,
    project_root: Path,
    feature: str,
    prd_path: Path,
    body: dict[str, Any],
) -> list[str]:
    args = [
        sys.executable,
        "-m",
        "kodawari.cli.main",
        "work-all",
        "--project-root",
        str(project_root),
        "--feature",
        feature,
        "--prd",
        str(prd_path),
        "--planner-route",
        "model",
        "--rollback-on-failure",
    ]
    if body.get("real_peer_review", True):
        args.append("--real-peer-review")
    max_cycles = body.get("max_cycles")
    if isinstance(max_cycles, int) and max_cycles > 0:
        args.extend(["--max-cycles", str(max_cycles)])
    max_verify_retries = body.get("max_verify_retries")
    if isinstance(max_verify_retries, int) and max_verify_retries > 0:
        args.extend(["--max-verify-retries", str(max_verify_retries)])
    gate_profile = body.get("gate_profile")
    if isinstance(gate_profile, str) and gate_profile:
        args.extend(["--gate-profile", gate_profile])
    executor_backend = body.get("executor_backend")
    if isinstance(executor_backend, str) and executor_backend:
        args.extend(["--executor-backend", executor_backend])
    extra = body.get("extra_args")
    if isinstance(extra, list):
        for item in extra:
            if isinstance(item, str) and item.strip():
                args.append(item)
    return args


def _kodawari_repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def run_serve_command(args: argparse.Namespace) -> int:
    try:
        import uvicorn
    except ImportError:  # pragma: no cover - dependency guard
        sys.stderr.write("ERROR: uvicorn is required for `kodawari serve`. Install it via `pip install uvicorn`.\n")
        return 2
    default_root = Path(args.root).resolve() if getattr(args, "root", None) else None
    app = _build_app(default_root)
    host = str(getattr(args, "host", DEFAULT_HOST) or DEFAULT_HOST)
    port = int(getattr(args, "port", DEFAULT_PORT) or DEFAULT_PORT)
    log_level = str(getattr(args, "log_level", "info") or "info")
    sys.stderr.write(f"kodawari serve: listening on http://{host}:{port} (root={default_root or '<query-only>'})\n")
    uvicorn.run(app, host=host, port=port, log_level=log_level)
    return 0


__all__ = [
    "run_serve_command",
    "_build_app",
    "desktop_telemetry_enabled",
    "_list_projects",
    "_project_status",
    "_derive_stages",
    "_aggregate_stats",
]
