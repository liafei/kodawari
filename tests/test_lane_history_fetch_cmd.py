from __future__ import annotations

import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
import zipfile

from kodawari.cli import lane_history_fetch_cmd
from kodawari.cli.main import build_parser


def _run_cli(parser: Any, capsys: Any, argv: list[str]) -> tuple[int, dict[str, Any]]:
    args = parser.parse_args(argv)
    rc = int(args.handler(args))
    payload = json.loads(capsys.readouterr().out)
    return rc, payload


def _zip_payload(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def test_cli_help_includes_lane_history_fetch_command() -> None:
    parser = build_parser()
    help_text = parser.format_help()
    assert "lane-history-fetch" in help_text


def test_cli_lane_history_fetch_downloads_recent_matching_artifacts(
    tmp_path: Path,
    capsys: Any,
    monkeypatch: Any,
) -> None:
    parser = build_parser()
    now = datetime.now(timezone.utc)
    history_root = tmp_path / "planning" / "lane_history"
    manifest_path = tmp_path / "planning" / "lane_history_manifest.json"

    recent_items = [
        {
            "id": 101,
            "name": "kodawari-always-on-stability-9001",
            "archive_download_url": "https://example.test/artifacts/101.zip",
            "created_at": (now - timedelta(days=1)).isoformat().replace("+00:00", "Z"),
            "expired": False,
            "workflow_run": {"id": 9001},
        },
        {
            "id": 102,
            "name": "kodawari-integration-stability-9002",
            "archive_download_url": "https://example.test/artifacts/102.zip",
            "created_at": (now - timedelta(hours=12)).isoformat().replace("+00:00", "Z"),
            "expired": False,
            "workflow_run": {"id": 9002},
        },
        {
            "id": 103,
            "name": "unrelated-artifact",
            "archive_download_url": "https://example.test/artifacts/103.zip",
            "created_at": (now - timedelta(hours=6)).isoformat().replace("+00:00", "Z"),
            "expired": False,
            "workflow_run": {"id": 9003},
        },
        {
            "id": 104,
            "name": "kodawari-always-on-stability-old",
            "archive_download_url": "https://example.test/artifacts/104.zip",
            "created_at": (now - timedelta(days=20)).isoformat().replace("+00:00", "Z"),
            "expired": False,
            "workflow_run": {"id": 9004},
        },
    ]

    zip_map = {
        "https://example.test/artifacts/101.zip": _zip_payload(
            {
                "lane_stability_always-on.json": "{}",
                "lane_triage_always-on.json": "{}",
            }
        ),
        "https://example.test/artifacts/102.zip": _zip_payload(
            {
                "lane_stability_integration.json": "{}",
                "lane_triage_integration.json": "{}",
            }
        ),
    }

    def fake_load_json(url: str, token: str) -> dict[str, Any]:
        assert token == "test-token"
        if "page=1" in url:
            return {"artifacts": recent_items}
        return {"artifacts": []}

    monkeypatch.setattr(lane_history_fetch_cmd, "_load_json_url", fake_load_json)
    monkeypatch.setattr(lane_history_fetch_cmd, "_download_bytes_url", lambda url, token: zip_map[url])
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")

    rc, payload = _run_cli(
        parser,
        capsys,
        [
            "lane-history-fetch",
            "--project-root",
            str(tmp_path),
            "--repo",
            "owner/repo",
            "--output-dir",
            str(history_root),
            "--json-output",
            str(manifest_path),
            "--max-history-days",
            "7",
        ],
    )

    assert rc == 0
    assert payload["status"] == "PASS"
    assert payload["downloaded_artifacts_total"] == 2
    assert history_root.exists()
    assert manifest_path.exists()
    assert (history_root / "kodawari-always-on-stability-9001-101" / "lane_triage_always-on.json").exists()
    assert (history_root / "kodawari-integration-stability-9002-102" / "lane_triage_integration.json").exists()


def test_cli_lane_history_fetch_blocks_when_no_matching_artifacts(
    tmp_path: Path,
    capsys: Any,
    monkeypatch: Any,
) -> None:
    parser = build_parser()
    manifest_path = tmp_path / "planning" / "lane_history_manifest.json"

    monkeypatch.setattr(lane_history_fetch_cmd, "_load_json_url", lambda url, token: {"artifacts": []})
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")

    rc, payload = _run_cli(
        parser,
        capsys,
        [
            "lane-history-fetch",
            "--project-root",
            str(tmp_path),
            "--repo",
            "owner/repo",
            "--json-output",
            str(manifest_path),
            "--fail-on-empty",
        ],
    )

    assert rc == 2
    assert payload["status"] == "BLOCKED"
    assert payload["downloaded_artifacts_total"] == 0
    assert payload["remediation"]
    assert manifest_path.exists()
