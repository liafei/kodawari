"""Contract version freeze guard.

Snapshots all enum values declared in src/kodawari/schemas/**/*.schema.json.
If any enum is added, removed, or renamed, this test fails — signalling that
MERGED_CONTRACT_VERSION must be bumped before merging.

To update this baseline after a legitimate contract bump:
1. Bump MERGED_CONTRACT_VERSION in src/kodawari/infra/contract_version.py
2. Run: python -m pytest tests/test_contract_version_freeze.py -v
   The test prints the new baseline JSON — copy it into _ENUM_BASELINE below.
"""

from __future__ import annotations

import json
from pathlib import Path

from kodawari.infra.contract_version import MERGED_CONTRACT_VERSION

# ---------------------------------------------------------------------------
# Baseline snapshot — regenerate after each legitimate contract bump.
# Last updated: ws115.v1  (2026-05-02)  contract_version bump alongside refactor
# ---------------------------------------------------------------------------
_ENUM_BASELINE: dict[str, dict[str, list[str]]] = {
    "contract_first/architecture_plan.schema.json": {
        "properties.planning_mode": ["existing", "greenfield"],
        "properties.confidence": ["high", "low"],
    },
    "contract_first/compliance_report.schema.json": {
        "properties.status": ["FAIL", "PASS"],
        "properties.checks.items.properties.check_name": [
            "cache_consistency",
            "domain_source_of_truth",
            "duplication",
            "import_rules",
            "invariant_proof",
            "layer_boundary",
            "layer_boundary_debt",
            "prd_coverage",
            "review_evidence",
            "runtime_contract_scatter",
            "scope_drift",
            "source_of_truth_conflict",
        ],
        "properties.checks.items.properties.status": ["FAIL", "PASS", "WARN"],
    },
    "contract_first/planning_conversation.schema.json": {
        "properties.status": ["approved", "auto_skipped", "error", "escalation_required", "precondition_blocked"],
    },
    "contract_first/prd_intake.schema.json": {
        "properties.path_type": ["both", "read", "write"],
        "properties.layers.items": [
            "frontend", "model", "repository", "route", "schema", "service", "util",
        ],
        "properties.confidence": ["high", "low"],
    },
    "contract_first/repo_inventory.schema.json": {
        "properties.mode": ["auto", "existing", "greenfield"],
    },
    "contract_first/task_card.schema.json": {
        "properties.schema_version": [
            "contract_first.task_card.v1",
            "contract_first.task_card.v1.1",
        ],
        "definitions.symbolRef.properties.kind": ["class", "function", "method"],
        "definitions.allowedTestMutation.properties.match_kind": [
            "decorator",
            "function_call",
            "import_path",
            "literal_assert",
        ],
    },
    "contract_first/task_graph.schema.json": {
        "properties.boundary_debt.properties.status": ["PASS", "WARN"],
        "properties.boundary_debt.properties.items.items.properties.severity": ["high", "low", "medium"],
        "properties.executability.properties.status": ["FAIL", "PASS", "WARN"],
        "properties.tasks.items.properties.executability.properties.status": ["FAIL", "PASS", "WARN"],
    },
    "coverage_matrix.schema.json": {
        "properties.items.items.properties.priority": ["P0", "P1", "P2"],
        "properties.items.items.properties.status": ["FAIL", "PARTIAL", "PASS"],
    },
    "observability/eval_report.schema.json": {
        "properties.status": ["BLOCKED", "PASS"],
    },
    "observability/field_report.schema.json": {
        "properties.severity": ["critical", "high", "low", "medium"],
        "properties.status": ["in_progress", "open", "resolved"],
    },
    "observability/review_evidence.schema.json": {
        "properties.status": ["FAIL", "MISSING", "PASS", "UNKNOWN", "WARN"],
        "properties.review_mode": ["real_peer_review", "simulated"],
    },
    "observability/telemetry_snapshot.schema.json": {
        "properties.signals.properties.reasoning_tier": ["deep_reasoning", "economy", "standard"],
    },
    "observability/verify_report.schema.json": {
        "properties.requested_command_kind": ["default", "file", "inline"],
        "properties.input_confidence": ["curated", "explicit", "fallback"],
        "properties.status": ["BLOCKED", "FAIL", "PASS", "UNKNOWN"],
    },
    "observability/worktree_baseline.schema.json": {
        "properties.mode": ["fail", "warn"],
        "properties.status": ["FAIL", "PASS", "WARN"],
    },
    "runtime/peer_review_response.schema.json": {
        "properties.severity": ["critical", "high", "info", "low", "medium"],
        "properties.gate_recommendation": [
            "APPROVED", "ESCALATE_TO_HUMAN", "PROCEED_TO_GATE",
            "REVIEW_FIX_REQUIRED", "REVIEW_PENDING", "REVIEW_SCOPE_CONFLICT",
        ],
        "properties.global_consistency_verdict": ["FAIL", "INSUFFICIENT_CONTEXT", "PASS"],
        "properties.local_implementation_verdict": ["FAIL", "PASS"],
        "properties.global_failure_attribution": ["sibling_tasks", "this_task", "unknown"],
    },
    "spec.schema.json": {
        "properties.priority": ["P0", "P1", "P2"],
    },
}

_SCHEMAS_ROOT = Path(__file__).parent.parent / "src" / "kodawari" / "schemas"


def _collect_enums(obj: object, path: str = "", result: dict | None = None) -> dict[str, list[str]]:
    if result is None:
        result = {}
    if isinstance(obj, dict):
        if "enum" in obj and isinstance(obj["enum"], list):
            result[path] = sorted(str(v) for v in obj["enum"])
        for k, v in obj.items():
            _collect_enums(v, (path + "." + k) if path else k, result)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _collect_enums(v, path + "[" + str(i) + "]", result)
    return result


def _load_current_baseline() -> dict[str, dict[str, list[str]]]:
    current: dict[str, dict[str, list[str]]] = {}
    for f in sorted(_SCHEMAS_ROOT.rglob("*.schema.json")):
        try:
            raw = f.read_bytes()
            if raw.startswith(b"\xef\xbb\xbf"):
                raw = raw[3:]
            d = json.loads(raw.decode("utf-8"))
        except Exception:
            continue
        enums = _collect_enums(d)
        if enums:
            rel = str(f.relative_to(_SCHEMAS_ROOT)).replace("\\", "/")
            current[rel] = enums
    return current


def test_schema_enums_match_baseline() -> None:
    """Fail if any schema enum changed without a contract version bump."""
    current = _load_current_baseline()

    added_schemas = sorted(set(current) - set(_ENUM_BASELINE))
    removed_schemas = sorted(set(_ENUM_BASELINE) - set(current))
    changed_fields: list[str] = []

    for schema, fields in _ENUM_BASELINE.items():
        if schema not in current:
            continue
        for field, expected in fields.items():
            actual = current[schema].get(field)
            if actual != expected:
                changed_fields.append(
                    f"  {schema} :: {field}\n"
                    f"    baseline : {expected}\n"
                    f"    current  : {actual}"
                )
    for schema, fields in current.items():
        if schema not in _ENUM_BASELINE:
            continue
        for field, actual in fields.items():
            if field not in _ENUM_BASELINE[schema]:
                changed_fields.append(
                    f"  {schema} :: {field} (NEW ENUM FIELD not in baseline)\n"
                    f"    current  : {actual}"
                )

    diffs = []
    if added_schemas:
        diffs.append("New schemas with enums (add to baseline):\n" + "\n".join(f"  {s}" for s in added_schemas))
    if removed_schemas:
        diffs.append("Removed schemas (remove from baseline):\n" + "\n".join(f"  {s}" for s in removed_schemas))
    if changed_fields:
        diffs.append("Changed enum fields:\n" + "\n".join(changed_fields))

    assert not diffs, (
        f"Schema enum drift detected — bump MERGED_CONTRACT_VERSION "
        f"(currently {MERGED_CONTRACT_VERSION!r}) before merging.\n\n"
        + "\n\n".join(diffs)
    )


def test_contract_version_is_known() -> None:
    """Sanity-check that MERGED_CONTRACT_VERSION is a non-empty string."""
    assert isinstance(MERGED_CONTRACT_VERSION, str) and MERGED_CONTRACT_VERSION
