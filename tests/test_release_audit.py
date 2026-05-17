from __future__ import annotations

import importlib.util
import sys
import tarfile
import zipfile
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "release_audit.py"


def _load_release_audit() -> Any:
    spec = importlib.util.spec_from_file_location("release_audit", SCRIPT_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_release_audit_passes_clean_wheel(tmp_path: Path) -> None:
    release_audit = _load_release_audit()
    wheel = tmp_path / "kodawari-0.1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("kodawari/__init__.py", "")
        archive.writestr("kodawari-0.1.0.dist-info/METADATA", "Name: kodawari\n")

    payload = release_audit.audit_paths([wheel])

    assert payload["schema_version"] == "release.audit.v1"
    assert payload["status"] == "PASS"
    assert payload["violations"] == []


def test_release_audit_flags_runtime_and_planning_residue_in_sdist(tmp_path: Path) -> None:
    release_audit = _load_release_audit()
    sdist = tmp_path / "kodawari-0.1.0.tar.gz"
    with tarfile.open(sdist, "w:gz") as archive:
        root = tmp_path / "kodawari-0.1.0"
        runtime_file = root / ".workflow_runtime" / "root_artifacts" / ".execution_result.json"
        planning_file = root / "planning" / "lane_stability_always-on.json"
        for path, content in ((runtime_file, "{}\n"), (planning_file, "{}\n")):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            archive.add(path, arcname=path.relative_to(tmp_path).as_posix())

    payload = release_audit.audit_paths([sdist])

    assert payload["status"] == "FAIL"
    reasons = {item["reason"] for item in payload["violations"]}
    assert "forbidden_runtime_directory" in reasons
    assert "forbidden_runtime_or_planning_root" in reasons


def test_release_audit_supports_explicit_allow_globs(tmp_path: Path) -> None:
    release_audit = _load_release_audit()
    directory = tmp_path / "release-tree"
    allowed_log = directory / "tests" / "fixtures" / "sample.log"
    allowed_log.parent.mkdir(parents=True)
    allowed_log.write_text("fixture log\n", encoding="utf-8")

    payload = release_audit.audit_paths([directory], allow_globs=["tests/fixtures/*.log"])

    assert payload["status"] == "PASS"


def test_release_audit_workflow_is_release_scoped() -> None:
    workflow = (REPO_ROOT / ".github" / "workflows" / "kodawari-release-audit.yml").read_text(encoding="utf-8")

    assert 'branches:\n      - "release/**"' in workflow
    assert 'tags:\n      - "v*"' in workflow
    assert "pull_request:" not in workflow
    assert "python scripts/release_audit.py dist --output planning/release_audit.json" in workflow
