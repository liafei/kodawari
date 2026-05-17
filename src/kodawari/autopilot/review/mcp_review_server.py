"""Lightweight MCP Server for structured review context exchange.

This module implements a stdio-based MCP server that exposes two tools:

  - ``get_review_bundle``  — returns the review context (task, changed files,
    deterministic findings, etc.) so the AI model can pull what it needs.
  - ``submit_review``      — accepts the structured review JSON from the AI
    model and writes it to a result file.

Usage from cli_reviewer (mcp mode)::

    1. Write review bundle to a temp file.
    2. Launch this server as a subprocess (stdio).
    3. Launch ``claude -p ... --mcp-config <config>`` which connects to
       this server, calls get_review_bundle, reviews, then submit_review.
    4. Read the result file written by submit_review.

The server communicates via JSON-RPC 2.0 over stdin/stdout (MCP stdio transport).
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)

MCP_PROTOCOL_VERSION = "2024-11-05"

# --- MCP Tool Definitions ------------------------------------------------

TOOLS = [
    {
        "name": "get_review_bundle",
        "description": (
            "Retrieve the review bundle containing task description, "
            "changed files, context, and deterministic findings. "
            "Call this first to understand what you need to review."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "submit_review",
        "description": (
            "Submit your structured review result as JSON. "
            "Required keys: approved, summary, must_fix, should_fix, "
            "blocking_items, severity, score, target_score, "
            "min_dimension_score, gate_recommendation, evidence."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "approved": {"type": "boolean"},
                "summary": {"type": "string"},
                "must_fix": {"type": "array", "items": {"type": "string"}},
                "should_fix": {"type": "array", "items": {"type": "string"}},
                "blocking_items": {"type": "array", "items": {"type": "string"}},
                "severity": {"type": "string"},
                "score": {"type": "integer"},
                "target_score": {"type": "integer"},
                "min_dimension_score": {"type": "integer"},
                "gate_recommendation": {"type": "string"},
                "evidence": {"type": "array", "items": {"type": "string"}},
            },
            "required": [
                "approved", "summary", "must_fix", "should_fix",
                "blocking_items", "severity", "score",
            ],
        },
    },
]


# --- Server State ---------------------------------------------------------

class McpReviewServerState:
    """Holds the review bundle and result paths for the server session."""

    def __init__(self, *, bundle_path: str, result_path: str) -> None:
        self.bundle_path = Path(bundle_path).resolve()
        self.result_path = Path(result_path).resolve()

    def load_bundle(self) -> dict[str, Any]:
        if not self.bundle_path.exists():
            return {"error": "review bundle file not found"}
        try:
            return json.loads(self.bundle_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            return {"error": f"failed to read review bundle: {exc}"}

    def save_result(self, result: dict[str, Any]) -> str:
        self.result_path.parent.mkdir(parents=True, exist_ok=True)
        self.result_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return str(self.result_path)


# --- JSON-RPC / MCP message handling --------------------------------------

def _jsonrpc_response(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _jsonrpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _handle_initialize(request_id: Any) -> dict[str, Any]:
    return _jsonrpc_response(request_id, {
        "protocolVersion": MCP_PROTOCOL_VERSION,
        "capabilities": {"tools": {}},
        "serverInfo": {"name": "kodawari-review-server", "version": "1.0.0"},
    })


def _handle_tools_list(request_id: Any) -> dict[str, Any]:
    return _jsonrpc_response(request_id, {"tools": TOOLS})


def _handle_tools_call(
    request_id: Any,
    params: dict[str, Any],
    state: McpReviewServerState,
) -> dict[str, Any]:
    tool_name = str(params.get("name") or "").strip()
    arguments = dict(params.get("arguments") or {})

    if tool_name == "get_review_bundle":
        bundle = state.load_bundle()
        return _jsonrpc_response(request_id, {
            "content": [{"type": "text", "text": json.dumps(bundle, ensure_ascii=False)}],
        })

    if tool_name == "submit_review":
        saved_path = state.save_result(arguments)
        return _jsonrpc_response(request_id, {
            "content": [{"type": "text", "text": f"Review submitted to {saved_path}"}],
        })

    return _jsonrpc_error(request_id, -32601, f"unknown tool: {tool_name}")


def _handle_message(
    message: dict[str, Any],
    state: McpReviewServerState,
) -> dict[str, Any] | None:
    method = str(message.get("method") or "").strip()
    request_id = message.get("id")
    params = dict(message.get("params") or {})

    # Notifications (no id) — acknowledge silently
    if request_id is None:
        return None

    if method == "initialize":
        return _handle_initialize(request_id)
    if method == "tools/list":
        return _handle_tools_list(request_id)
    if method == "tools/call":
        return _handle_tools_call(request_id, params, state)
    if method == "ping":
        return _jsonrpc_response(request_id, {})

    return _jsonrpc_error(request_id, -32601, f"method not found: {method}")


# --- stdio transport loop -------------------------------------------------

def run_stdio_server(*, bundle_path: str, result_path: str) -> None:
    """Run the MCP review server on stdin/stdout (stdio transport).

    Args:
        bundle_path: Path to the review bundle JSON file (read by get_review_bundle).
        result_path: Path where submit_review writes the result JSON.
    """
    state = McpReviewServerState(bundle_path=bundle_path, result_path=result_path)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue

        response = _handle_message(message, state)
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()


# --- CLI entry point (for subprocess invocation) --------------------------

def main() -> None:
    """Entry point when invoked as ``python -m kodawari.autopilot.review.mcp_review_server``."""
    import argparse

    parser = argparse.ArgumentParser(description="MCP review server (stdio)")
    parser.add_argument("--bundle-path", required=True, help="Path to review bundle JSON")
    parser.add_argument("--result-path", required=True, help="Path to write review result JSON")
    args = parser.parse_args()

    run_stdio_server(bundle_path=args.bundle_path, result_path=args.result_path)


if __name__ == "__main__":
    main()


__all__ = ["McpReviewServerState", "run_stdio_server"]

