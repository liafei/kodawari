"""Download recent lane artifacts so standing-proof trends can run in CI."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import re
from typing import Any
from urllib import request
import zipfile

from kodawari.cli.contract.command_contract import build_error_payload, normalize_mutating_payload
from kodawari.cli.io_atomic import atomic_write_json
from kodawari.cli.provenance import build_cli_provenance


GITHUB_API_ROOT = "https://api.github.com"
DEFAULT_HISTORY_DIR = "lane_history"
DEFAULT_HISTORY_MANIFEST = "lane_history_manifest.json"
DEFAULT_ARTIFACT_PREFIXES = (
    "kodawari-always-on-stability-",
    "kodawari-integration-stability-",
)


def _parse_iso_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso_or_empty(value: datetime | None) -> str:
    return value.isoformat().replace("+00:00", "Z") if value is not None else ""


def _github_headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "kodawari-lane-history-fetch",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _load_json_url(url: str, token: str) -> dict[str, Any]:
    req = request.Request(url, headers=_github_headers(token))
    with request.urlopen(req, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object from GitHub API: {url}")
    return payload


def _download_bytes_url(url: str, token: str) -> bytes:
    req = request.Request(url, headers=_github_headers(token))
    with request.urlopen(req, timeout=60) as response:
        return response.read()


def _sanitize_dir_name(name: str, artifact_id: int) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", str(name).strip()).strip(".-") or "lane-artifact"
    return f"{stem}-{artifact_id}"


def _artifact_matches(item: dict[str, Any], prefixes: tuple[str, ...]) -> bool:
    name = str(item.get("name") or "").strip()
    return bool(name) and any(name.startswith(prefix) for prefix in prefixes)


def _list_recent_artifacts(
    repo: str,
    token: str,
    *,
    prefixes: tuple[str, ...],
    max_history_days: int,
    per_page: int,
    max_pages: int,
    now: datetime,
) -> tuple[list[dict[str, Any]], list[str]]:
    cutoff = now - timedelta(days=max_history_days)
    selected: list[dict[str, Any]] = []
    warnings: list[str] = []
    seen_ids: set[int] = set()

    for page in range(1, max_pages + 1):
        payload = _load_json_url(
            f"{GITHUB_API_ROOT}/repos/{repo}/actions/artifacts?per_page={per_page}&page={page}",
            token,
        )
        raw_items = list(payload.get("artifacts") or [])
        if not raw_items:
            break
        page_all_before_cutoff = True
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            created_at = _parse_iso_datetime(raw.get("created_at"))
            if created_at is None:
                warnings.append(f"artifact id={raw.get('id', '')} skipped invalid created_at timestamp")
                continue
            if created_at >= cutoff:
                page_all_before_cutoff = False
            if created_at < cutoff:
                continue
            if bool(raw.get("expired", False)):
                continue
            if not _artifact_matches(raw, prefixes):
                continue
            artifact_id = int(raw.get("id") or 0)
            if artifact_id <= 0 or artifact_id in seen_ids:
                continue
            seen_ids.add(artifact_id)
            selected.append(
                {
                    "artifact_id": artifact_id,
                    "name": str(raw.get("name") or "").strip(),
                    "created_at_utc": _iso_or_empty(created_at),
                    "archive_download_url": str(raw.get("archive_download_url") or "").strip(),
                    "workflow_run": dict(raw.get("workflow_run") or {}),
                }
            )
        if len(raw_items) < per_page or page_all_before_cutoff:
            break
    selected.sort(key=lambda item: (item["created_at_utc"], item["artifact_id"]), reverse=True)
    return selected, warnings


def _extract_artifact(record: dict[str, Any], output_dir: Path, token: str) -> dict[str, Any]:
    archive_url = str(record.get("archive_download_url") or "").strip()
    if not archive_url:
        raise ValueError(f"artifact missing archive_download_url: {record.get('name', '')}")
    payload = _download_bytes_url(archive_url, token)
    target_dir = output_dir / _sanitize_dir_name(str(record.get("name") or ""), int(record.get("artifact_id") or 0))
    target_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        archive.extractall(target_dir)
    files = sorted(path.relative_to(output_dir).as_posix() for path in target_dir.rglob("*") if path.is_file())
    return {
        "artifact_id": int(record.get("artifact_id") or 0),
        "name": str(record.get("name") or ""),
        "created_at_utc": str(record.get("created_at_utc") or ""),
        "artifact_dir": str(target_dir),
        "files_total": len(files),
        "files_sample": files[:10],
        "workflow_run": dict(record.get("workflow_run") or {}),
    }


def run_lane_history_fetch_command(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root).resolve()
    output_dir = Path(getattr(args, "output_dir", "") or (project_root / "planning" / DEFAULT_HISTORY_DIR)).resolve()
    json_output = Path(
        getattr(args, "json_output", "") or (project_root / "planning" / DEFAULT_HISTORY_MANIFEST)
    ).resolve()
    repo = str(getattr(args, "repo", "") or os.environ.get("GITHUB_REPOSITORY") or "").strip()
    token_env = str(getattr(args, "token_env", "GITHUB_TOKEN") or "GITHUB_TOKEN").strip() or "GITHUB_TOKEN"
    token = str(os.environ.get(token_env) or "").strip()
    prefixes = tuple(
        str(item).strip()
        for item in list(getattr(args, "artifact_prefix", []) or [])
        if str(item).strip()
    ) or DEFAULT_ARTIFACT_PREFIXES
    max_history_days = max(1, int(getattr(args, "max_history_days", 7)))
    per_page = max(1, min(100, int(getattr(args, "per_page", 100))))
    max_pages = max(1, int(getattr(args, "max_pages", 10)))
    now = datetime.now(timezone.utc)

    try:
        if not repo:
            raise ValueError("lane-history-fetch requires --repo or GITHUB_REPOSITORY")
        if not token:
            raise ValueError(f"lane-history-fetch requires a GitHub token in {token_env}")

        output_dir.mkdir(parents=True, exist_ok=True)
        selected, warnings = _list_recent_artifacts(
            repo,
            token,
            prefixes=prefixes,
            max_history_days=max_history_days,
            per_page=per_page,
            max_pages=max_pages,
            now=now,
        )
        downloads = [_extract_artifact(record, output_dir, token) for record in selected]
        status = "PASS" if downloads else "BLOCKED"
        remediation = []
        next_action = "Run `kodawari lane-trend` against the downloaded history directory."
        if not downloads:
            remediation = [
                "Verify the selected GitHub repo, artifact prefixes, and history window.",
                "Confirm the lane workflows produced artifacts and that the token has `actions:read` access.",
            ]
            next_action = "Restore recent lane artifacts, then rerun `kodawari lane-history-fetch`."
        payload = normalize_mutating_payload(
            {
                "status": status,
                "entrypoint": "kodawari lane-history-fetch",
                "repo": repo,
                "selected_prefixes": list(prefixes),
                "output_dir": str(output_dir),
                "generated_at_utc": _iso_or_empty(now),
                "max_history_days": max_history_days,
                "per_page": per_page,
                "max_pages": max_pages,
                "downloaded_artifacts_total": len(downloads),
                "downloaded_artifacts": downloads,
                "warnings": warnings,
                "remediation": remediation,
                "next_action": next_action,
                "provenance": build_cli_provenance(
                    command="lane-history-fetch",
                    project_root=project_root,
                    planning_dir=None,
                    module_file=Path(__file__),
                ),
                "artifacts": {
                    "LANE_HISTORY_MANIFEST.json": str(json_output),
                },
            }
        )
        atomic_write_json(json_output, payload)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        if bool(getattr(args, "fail_on_empty", False)) and not downloads:
            return 2
        return 0
    except Exception as exc:
        payload = normalize_mutating_payload(
            build_error_payload(
                command="lane-history-fetch",
                project_root=project_root,
                planning_dir=None,
                module_file=Path(__file__),
                error=str(exc),
                error_code="lane_history_fetch_failed",
                remediation=[
                    "Verify GitHub token access and the selected repository before rerunning `kodawari lane-history-fetch`."
                ],
                extra={
                    "repo": repo,
                    "output_dir": str(output_dir),
                    "artifacts": {
                        "LANE_HISTORY_MANIFEST.json": str(json_output),
                    },
                },
            )
        )
        atomic_write_json(json_output, payload)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 2


__all__ = ["DEFAULT_HISTORY_DIR", "DEFAULT_HISTORY_MANIFEST", "run_lane_history_fetch_command"]

