"""Classify flat/subpackage file pairs in src/kodawari/cli/.

For each pair of same-named (flat_file, sub_file), determine:
  - identical:       byte-exact equivalent
  - import_only:     diff only in import path lines (from kodawari.cli.X → from kodawari.cli.<sub>.X)
  - real_divergence: substantive logic difference

Used by P0/P1.0 to decide what to backport before shim-ing flat files.
"""
from __future__ import annotations

import json
import pathlib
import re
import sys
from datetime import datetime, timezone
from typing import Any

REPO = pathlib.Path(__file__).resolve().parents[1]
CLI = REPO / "src" / "kodawari" / "cli"

# Strip common cosmetic differences before diffing.
# Matches any `from kodawari.X.Y ...` or `import kodawari.X.Y`, any domain.
# Allows leading whitespace (for indented imports inside functions).
_IMPORT_LINE_RE = re.compile(
    r"^\s*(?:from|import)\s+kodawari\.[\w.]+\b.*$",
    re.MULTILINE,
)
_BOM = "\ufeff"


def normalize_imports(text: str) -> str:
    """Strip BOM, trailing whitespace, and collapse any kodawari.* import
    lines to a placeholder. If only these differ, files are considered
    import-only-divergent."""
    if text.startswith(_BOM):
        text = text[len(_BOM):]
    text = _IMPORT_LINE_RE.sub("__WSDK_IMPORT__", text)
    # Strip trailing blank lines (common whitespace-only diff)
    return text.rstrip() + "\n"


def classify_pair(flat_path: pathlib.Path, sub_path: pathlib.Path) -> dict[str, Any]:
    flat_text = flat_path.read_text(encoding="utf-8", errors="replace")
    sub_text = sub_path.read_text(encoding="utf-8", errors="replace")

    flat_lines = flat_text.splitlines()
    sub_lines = sub_text.splitlines()

    if flat_text == sub_text:
        return {
            "category": "identical",
            "flat": str(flat_path.relative_to(REPO)).replace("\\", "/"),
            "sub": str(sub_path.relative_to(REPO)).replace("\\", "/"),
            "flat_lines": len(flat_lines),
            "sub_lines": len(sub_lines),
        }

    flat_norm = normalize_imports(flat_text)
    sub_norm = normalize_imports(sub_text)
    if flat_norm == sub_norm:
        # Count import-only diff lines for informational purposes
        flat_imports = set(_IMPORT_LINE_RE.findall(flat_text))
        sub_imports = set(_IMPORT_LINE_RE.findall(sub_text))
        return {
            "category": "import_only",
            "flat": str(flat_path.relative_to(REPO)).replace("\\", "/"),
            "sub": str(sub_path.relative_to(REPO)).replace("\\", "/"),
            "flat_lines": len(flat_lines),
            "sub_lines": len(sub_lines),
            "flat_unique_imports": sorted(flat_imports - sub_imports),
            "sub_unique_imports": sorted(sub_imports - flat_imports),
        }

    # Real divergence — compute a crude summary
    flat_norm_set = set(flat_norm.splitlines())
    sub_norm_set = set(sub_norm.splitlines())
    only_in_flat = flat_norm_set - sub_norm_set
    only_in_sub = sub_norm_set - flat_norm_set
    return {
        "category": "real_divergence",
        "flat": str(flat_path.relative_to(REPO)).replace("\\", "/"),
        "sub": str(sub_path.relative_to(REPO)).replace("\\", "/"),
        "flat_lines": len(flat_lines),
        "sub_lines": len(sub_lines),
        "flat_only_lines": len(only_in_flat),
        "sub_only_lines": len(only_in_sub),
        "sample_flat_only": sorted(
            [l for l in only_in_flat if l.strip() and not l.strip().startswith("#")]
        )[:6],
        "sample_sub_only": sorted(
            [l for l in only_in_sub if l.strip() and not l.strip().startswith("#")]
        )[:6],
    }


def find_pairs() -> tuple[list[tuple[pathlib.Path, pathlib.Path]], list[pathlib.Path], list[pathlib.Path]]:
    # Map basename → flat path
    flat_by_name: dict[str, pathlib.Path] = {}
    for p in CLI.glob("*.py"):
        if p.name == "__init__.py":
            continue
        flat_by_name[p.name] = p

    # Map basename → sub path(s)
    sub_by_name: dict[str, list[pathlib.Path]] = {}
    for p in CLI.rglob("*.py"):
        if p.parent == CLI or p.name == "__init__.py":
            continue
        sub_by_name.setdefault(p.name, []).append(p)

    pairs: list[tuple[pathlib.Path, pathlib.Path]] = []
    flat_only: list[pathlib.Path] = []
    sub_only: list[pathlib.Path] = []

    for name, flat_p in flat_by_name.items():
        subs = sub_by_name.get(name, [])
        if not subs:
            flat_only.append(flat_p)
            continue
        # If multiple sub candidates, pick the first (rare case); record others separately
        pairs.append((flat_p, subs[0]))
        if len(subs) > 1:
            for extra in subs[1:]:
                sub_only.append(extra)

    for name, sp_list in sub_by_name.items():
        if name not in flat_by_name:
            for sp in sp_list:
                sub_only.append(sp)

    return pairs, flat_only, sub_only


def analyze() -> dict[str, Any]:
    pairs, flat_only, sub_only = find_pairs()

    buckets: dict[str, list[dict[str, Any]]] = {
        "identical": [],
        "import_only": [],
        "real_divergence": [],
    }

    for flat_p, sub_p in pairs:
        result = classify_pair(flat_p, sub_p)
        buckets[result["category"]].append(result)

    # Check if flat-only files are shims already
    flat_only_records: list[dict[str, Any]] = []
    for p in flat_only:
        text = p.read_text(encoding="utf-8", errors="replace")
        flat_only_records.append({
            "path": str(p.relative_to(REPO)).replace("\\", "/"),
            "lines": len(text.splitlines()),
            "is_shim": "sys.modules[__name__]" in text,
        })

    sub_only_records = [
        {"path": str(p.relative_to(REPO)).replace("\\", "/"),
         "lines": len(p.read_text(encoding="utf-8", errors="replace").splitlines())}
        for p in sub_only
    ]

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": {
            "root": str(CLI.relative_to(REPO)).replace("\\", "/"),
        },
        "summary": {
            "pairs_total": len(pairs),
            "identical": len(buckets["identical"]),
            "import_only": len(buckets["import_only"]),
            "real_divergence": len(buckets["real_divergence"]),
            "flat_only": len(flat_only_records),
            "sub_only": len(sub_only_records),
        },
        "pairs": buckets,
        "flat_only": flat_only_records,
        "sub_only": sub_only_records,
    }


def main() -> None:
    data = analyze()
    out = REPO / "_baseline" / "cli_fork_report.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {out}")
    s = data["summary"]
    print(
        f"Pairs: {s['pairs_total']}  "
        f"identical={s['identical']}  import_only={s['import_only']}  "
        f"real_divergence={s['real_divergence']}"
    )
    print(f"Flat-only: {s['flat_only']}  Sub-only: {s['sub_only']}")


if __name__ == "__main__":
    main()
