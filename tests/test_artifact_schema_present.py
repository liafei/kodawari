"""All canonical artifact writers must declare a schema_version constant.

Acceptance gate for the v5 refactor plan (PR11.5): every JSON/JSONL writer
that downstream consumers rely on must surface a stable ``schema_version``
field so that readers can dispatch on it during a SemVer bump. This test
guards against silent regressions where a refactor relocates a writer and
drops the field.
"""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src" / "kodawari"


def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8-sig")


def test_lane_artifact_writers_emit_schema_version() -> None:
    invoke_text = _read("scripts/invoke_test_lane.ps1")
    stability_text = _read("scripts/run_lane_stability.ps1")
    triage_text = _read("src/kodawari/cli/gate/lane_triage_cmd.py")
    trend_text = _read("src/kodawari/cli/gate/lane_trend_cmd.py")
    trend_report_text = _read("src/kodawari/cli/gate/lane_trend_report_cmd.py")

    assert 'schema_version = "lane.run_result.v1"' in invoke_text
    assert 'schema_version = "lane.stability.v1"' in stability_text
    assert 'schema_version = "lane.triage.v1"' in stability_text
    assert '"schema_version": _TRIAGE_SCHEMA_VERSION' in triage_text
    assert '"schema_version": TREND_SCHEMA_VERSION' in trend_text
    assert '"schema_version": "lane.trend.report.v1"' in trend_report_text
    assert 'summary_payload.get("schema_version")' in trend_report_text


def test_execution_artifact_writers_emit_schema_version() -> None:
    """Execution request/result writers (consumed by review/verify/recovery)."""
    text = _read("src/kodawari/autopilot/execution/execution_artifacts.py")
    assert 'EXECUTION_REQUEST_SCHEMA_VERSION = "execution.request.v1"' in text
    assert 'EXECUTION_RESULT_SCHEMA_VERSION = "execution.result.v1"' in text
    # Each constant must actually be wired into a payload dict.
    assert '"schema_version": EXECUTION_REQUEST_SCHEMA_VERSION' in text
    assert '"schema_version": EXECUTION_RESULT_SCHEMA_VERSION' in text


def test_review_bundle_writer_emits_schema_version() -> None:
    """Peer review bundle JSON consumed by reviewer gateways and audits."""
    text = _read("src/kodawari/autopilot/review/review_bundle.py")
    assert 'REVIEW_BUNDLE_SCHEMA_VERSION = "review.bundle.v1"' in text
    assert '"schema_version": REVIEW_BUNDLE_SCHEMA_VERSION' in text or 'REVIEW_BUNDLE_SCHEMA_VERSION,' in text


def test_verify_report_writer_emits_schema_version() -> None:
    """Verify command artifact consumed by gate/QA/release pipeline."""
    text = _read("src/kodawari/cli/evidence/verify_report.py")
    assert 'VERIFY_REPORT_SCHEMA_VERSION = "verify.report.v1"' in text
    assert 'VERIFY_REPORT_SCHEMA_VERSION' in text


def test_serve_endpoint_payloads_declare_schema_version() -> None:
    """Serve API responses consumed by the React/Tauri UI."""
    text = _read("src/kodawari/cli/serve_cmd.py")
    constants = (
        'SERVE_PROJECTS_SCHEMA_VERSION = "serve.projects.v1"',
        'SERVE_PROJECT_STATUS_SCHEMA_VERSION = "serve.project_status.v1"',
        'SERVE_EVENT_SCHEMA_VERSION = "serve.event.v1"',
        'SERVE_CREATE_PROJECT_SCHEMA_VERSION = "serve.create_project.v1"',
        'SERVE_APPROVE_SCHEMA_VERSION = "serve.approve.v1"',
    )
    for declaration in constants:
        assert declaration in text, f"missing schema constant: {declaration}"
    # Each must be referenced from a payload dict (not just declared).
    for symbol in (
        "SERVE_PROJECTS_SCHEMA_VERSION",
        "SERVE_PROJECT_STATUS_SCHEMA_VERSION",
        "SERVE_EVENT_SCHEMA_VERSION",
        "SERVE_CREATE_PROJECT_SCHEMA_VERSION",
        "SERVE_APPROVE_SCHEMA_VERSION",
    ):
        usages = text.count(symbol)
        assert usages >= 2, (
            f"schema constant {symbol} declared but not used in any payload"
        )


def test_no_canonical_writer_drops_schema_version_silently() -> None:
    """Belt-and-braces: scan candidate writer modules and require either
    'schema_version' string presence or an explicit opt-out comment.

    A new writer that legitimately should NOT carry a schema_version field
    must add the literal comment ``# schema_version_optional`` so the
    reviewer sees an explicit waiver rather than a silent omission.
    """
    candidates = [
        "src/kodawari/autopilot/execution/execution_artifacts.py",
        "src/kodawari/autopilot/review/review_bundle.py",
        "src/kodawari/cli/evidence/verify_report.py",
        "src/kodawari/cli/serve_cmd.py",
        "src/kodawari/cli/gate/lane_triage_cmd.py",
        "src/kodawari/cli/gate/lane_trend_cmd.py",
        "src/kodawari/cli/gate/lane_trend_report_cmd.py",
    ]
    for rel in candidates:
        text = _read(rel)
        if "# schema_version_optional" in text:
            continue
        assert "schema_version" in text, (
            f"writer {rel} omits 'schema_version' without a documented waiver. "
            "Add either 'schema_version' to the payload or a "
            "'# schema_version_optional' opt-out comment with reasoning."
        )
