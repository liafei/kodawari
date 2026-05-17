from __future__ import annotations

import json
from pathlib import Path


def test_security_scan_pins_scanners_and_uses_runtime_reports() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    script = (repo_root / "scripts" / "security_scan.ps1").read_text(encoding="utf-8")
    workflow = (repo_root / ".github" / "workflows" / "kodawari-security.yml").read_text(encoding="utf-8")
    gitleaks_config = (repo_root / ".gitleaks.toml").read_text(encoding="utf-8")
    baseline_bytes = (repo_root / ".secrets.baseline").read_bytes()
    baseline = json.loads(baseline_bytes.decode("utf-8"))

    assert '$GitleaksVersion = "8.24.3"' in script
    assert '$DetectSecretsVersion = "1.5.0"' in script
    assert ".workflow_runtime\\security" in script
    assert 'GITLEAKS_VERSION: "8.24.3"' in workflow
    assert 'DETECT_SECRETS_VERSION: "1.5.0"' in workflow
    assert "github.com/gitleaks/gitleaks/v8@v$env:GITLEAKS_VERSION" in workflow
    assert "detect-secrets==$env:DETECT_SECRETS_VERSION" in workflow
    assert "^\\.workflow_runtime/" in gitleaks_config
    assert "[\\\\/]" in script
    assert "\\.pytest_cache" in script
    assert "node_modules" in script
    assert not baseline_bytes.startswith(b"\xef\xbb\xbf")
    assert baseline.get("plugins_used")
    assert "results" in baseline
