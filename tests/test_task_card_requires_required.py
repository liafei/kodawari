"""task_card.schema requires the `requires` field; build_task_card stamps it.

Locking this contract means readiness gating can rely on the field being
present (rather than handling the missing-field branch as a separate code
path), and a planner that forgets to emit preconditions gets caught at
schema validation rather than at execution time.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

try:
    import jsonschema
except ImportError:  # pragma: no cover
    jsonschema = None  # type: ignore[assignment]

from kodawari.autopilot.planning.task_card import build_task_card


SCHEMA_PATH = Path(__file__).resolve().parents[1] / "src" / "kodawari" / "schemas" / "contract_first" / "task_card.schema.json"


def _load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _minimal_task_graph() -> dict:
    return {
        "schema_version": "contract_first.task_graph.v1",
        "tasks": [
            {
                "task_id": "T01",
                "task_name": "demo",
                "why_this_layer": "demo",
                "core_files": ["src/app.py"],
                "invariants": ["app stays runnable"],
                "test_plan": "pytest tests/test_app.py -q",
            }
        ],
    }


def test_schema_lists_requires_as_required() -> None:
    schema = _load_schema()
    assert "requires" in schema["required"]


def test_build_task_card_emits_requires_even_when_planner_omits() -> None:
    card = build_task_card(_minimal_task_graph(), "T01")
    assert "requires" in card
    assert isinstance(card["requires"], list)
    assert card["requires"] == []


def test_build_task_card_preserves_planner_declared_requires() -> None:
    graph = _minimal_task_graph()
    graph["tasks"][0]["requires"] = [{"kind": "field", "name": "users.email", "existing": True}]
    card = build_task_card(graph, "T01")
    assert card["requires"] == [{"kind": "field", "name": "users.email", "existing": True}]


@pytest.mark.skipif(jsonschema is None, reason="jsonschema not installed")
def test_card_without_requires_is_rejected_by_schema() -> None:
    schema = _load_schema()
    card = build_task_card(_minimal_task_graph(), "T01")
    card.pop("requires")  # simulate a stripped-down legacy card
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(card, schema)


@pytest.mark.skipif(jsonschema is None, reason="jsonschema not installed")
def test_card_with_empty_requires_is_accepted() -> None:
    schema = _load_schema()
    card = build_task_card(_minimal_task_graph(), "T01")
    jsonschema.validate(card, schema)
