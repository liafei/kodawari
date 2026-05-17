from __future__ import annotations

import ast
import importlib
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src" / "kodawari"

SINGLE_DEFINITION_CLASSES = {
    "OpenAIToolUseExecutionError",
    "ToolUseRuntime",
    "AutopilotPlanningBridgeError",
    "AutopilotPlanningSnapshot",
    "TaskStatus",
    "TaskState",
    "StateManager",
}

IDENTITY_EXPECTATIONS = {
    "OpenAIToolUseExecutionError": [
        "kodawari.autopilot.execution.tool_use_types.OpenAIToolUseExecutionError",
        "kodawari.autopilot.execution.execution_openai_tool_use.OpenAIToolUseExecutionError",
    ],
    "ToolUseRuntime": [
        "kodawari.autopilot.execution.execution_openai_tool_use.ToolUseRuntime",
    ],
    "AutopilotPlanningBridgeError": [
        "kodawari.cli.contract.bridge_types.AutopilotPlanningBridgeError",
        "kodawari.cli.contract.autopilot_contract_bridge.AutopilotPlanningBridgeError",
    ],
    "AutopilotPlanningSnapshot": [
        "kodawari.cli.contract.bridge_types.AutopilotPlanningSnapshot",
        "kodawari.cli.contract.autopilot_contract_bridge.AutopilotPlanningSnapshot",
    ],
    "TaskStatus": [
        "kodawari.autopilot.core.state_models.TaskStatus",
        "kodawari.autopilot.core.state_legacy.TaskStatus",
        "kodawari.autopilot.core.state.TaskStatus",
    ],
    "TaskState": [
        "kodawari.autopilot.core.state_models.TaskState",
        "kodawari.autopilot.core.state_legacy.TaskState",
        "kodawari.autopilot.core.state.TaskState",
    ],
    "StateManager": [
        "kodawari.autopilot.core.state_models.StateManager",
        "kodawari.autopilot.core.state_legacy.StateManager",
        "kodawari.autopilot.core.state.StateManager",
    ],
}


def _class_defs() -> dict[str, list[Path]]:
    found: dict[str, list[Path]] = {name: [] for name in SINGLE_DEFINITION_CLASSES}
    for path in SRC_ROOT.rglob("*.py"):
        module = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        for node in ast.walk(module):
            if isinstance(node, ast.ClassDef) and node.name in found:
                found[node.name].append(path.relative_to(REPO_ROOT))
    return found


def _resolve(dotted: str) -> Any:
    module_name, attr = dotted.rsplit(".", 1)
    return getattr(importlib.import_module(module_name), attr)


def test_refactor_classes_have_single_physical_definition() -> None:
    found = _class_defs()
    assert {name: len(paths) for name, paths in found.items()} == {
        name: 1 for name in SINGLE_DEFINITION_CLASSES
    }, found


def test_reexported_classes_keep_identity() -> None:
    for name, dotted_paths in IDENTITY_EXPECTATIONS.items():
        canonical = _resolve(dotted_paths[0])
        for dotted in dotted_paths[1:]:
            assert _resolve(dotted) is canonical, f"{name} identity mismatch at {dotted}"
