import importlib.util
from pathlib import Path
import re

from kodawari.cli.runtime import work_all_runtime


_SHIM_RE = re.compile(r'^"""Shim: real implementation lives at (?P<target>kodawari\.[^"]+)\."""')


def test_work_all_runtime_only_has_canonical_module_path() -> None:
    repo_root = Path(__file__).resolve().parents[1]

    assert not (repo_root / "src" / "kodawari" / "cli" / "work_all_runtime.py").exists()
    assert importlib.util.find_spec("kodawari.cli.work_all_runtime") is None
    assert work_all_runtime.__name__ == "kodawari.cli.runtime.work_all_runtime"
    assert hasattr(work_all_runtime, "run_work_all_command")


def test_legacy_import_shims_point_to_existing_canonical_modules() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    src_root = repo_root / "src"
    shim_records: list[tuple[str, str]] = []

    for path in sorted((src_root / "kodawari").rglob("*.py")):
        first_line = path.read_text(encoding="utf-8-sig").splitlines()[0:1]
        if not first_line:
            continue
        match = _SHIM_RE.match(first_line[0])
        if not match:
            continue
        module = ".".join(path.relative_to(src_root).with_suffix("").parts)
        target = match.group("target")
        shim_records.append((module, target))
        assert target != module
        assert importlib.util.find_spec(target) is not None, f"{module} points to missing {target}"
        assert target != "kodawari.cli.runtime.work_all_runtime", "work_all_runtime shim must stay deleted"

    assert shim_records, "expected existing legacy shims to be scanned"
