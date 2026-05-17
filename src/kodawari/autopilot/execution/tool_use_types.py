"""Shared types for the OpenAI-compatible tool-use executor."""

from __future__ import annotations


class OpenAIToolUseExecutionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
