"""Status.md rendering contract tests.

Host Probe must surface the home_probe subfield and any remediation list
attached by the Claude CLI backend preflight. Without this the structured
hints stored in host_probe stay invisible to operators reading STATUS.md.
"""

from __future__ import annotations

from kodawari.cli.status_markdown import render_status_markdown


def _base_payload() -> dict:
    return {
        "feature": "f1",
        "execution_host_probe": {
            "status": "blocked",
            "surface": "claude_cli",
            "reason": "home_inaccessible",
            "executable": "claude",
            "executable_available": True,
        },
    }


def test_host_probe_renders_five_base_fields_when_no_home_probe() -> None:
    out = render_status_markdown(_base_payload())
    assert "## Host Probe" in out
    assert "- status: blocked" in out
    assert "- surface: claude_cli" in out
    assert "- reason: home_inaccessible" in out
    assert "- executable: claude" in out
    assert "- executable_available: True" in out
    assert "## Remediation" not in out


def test_home_probe_subfields_render_when_present() -> None:
    payload = _base_payload()
    payload["execution_host_probe"]["home_probe"] = {
        "status": "blocked",
        "home": "C:\\Users\\liafei",
        "error": "PermissionError: lstat denied",
    }
    out = render_status_markdown(payload)
    assert "- home_probe.status: blocked" in out
    assert "- home_probe.home: C:\\Users\\liafei" in out
    assert "- home_probe.error: PermissionError: lstat denied" in out


def test_remediation_section_renders_when_present() -> None:
    payload = _base_payload()
    payload["execution_host_probe"]["remediation"] = [
        "Check Controlled Folder Access",
        "Verify USERPROFILE exists",
    ]
    out = render_status_markdown(payload)
    assert "## Remediation" in out
    assert "- Check Controlled Folder Access" in out
    assert "- Verify USERPROFILE exists" in out


def test_remediation_section_absent_when_list_empty() -> None:
    payload = _base_payload()
    payload["execution_host_probe"]["remediation"] = []
    out = render_status_markdown(payload)
    assert "## Remediation" not in out
