from __future__ import annotations

import json
from pathlib import Path

from kodawari.cli.serve_cmd import desktop_telemetry_enabled


def test_desktop_telemetry_default_off(monkeypatch) -> None:
    monkeypatch.delenv("WORKFLOW_DESKTOP_TELEMETRY", raising=False)
    assert desktop_telemetry_enabled() is False


def test_desktop_telemetry_requires_explicit_opt_in(monkeypatch) -> None:
    monkeypatch.setenv("WORKFLOW_DESKTOP_TELEMETRY", "1")
    assert desktop_telemetry_enabled() is True
    monkeypatch.setenv("WORKFLOW_DESKTOP_TELEMETRY", "false")
    assert desktop_telemetry_enabled() is False


def test_web_package_has_no_analytics_dependency() -> None:
    package_json = Path(__file__).resolve().parents[1] / "web" / "package.json"
    payload = json.loads(package_json.read_text(encoding="utf-8"))
    deps = {**payload.get("dependencies", {}), **payload.get("devDependencies", {})}
    forbidden = {"posthog-js", "@sentry/react", "segment", "analytics", "mixpanel-browser"}
    assert forbidden.isdisjoint(deps)

