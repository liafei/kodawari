"""Shared contract-version constant.

Lives in ``infra/`` so that both CLI and autopilot layers can reference it
without forcing autopilot→cli import edges.
"""

from __future__ import annotations

MERGED_CONTRACT_VERSION = "ws115.v1"

__all__ = ["MERGED_CONTRACT_VERSION"]
