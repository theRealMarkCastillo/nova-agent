"""Typed outcome of a tool call."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from nova.retry import ErrorType, classify_error

ToolStatus = Literal["completed", "failed", "denied", "interrupted"]

INTERRUPTED_MESSAGE = "[Interrupted by user before tool execution]"

_TRANSIENT_ERRORS = frozenset(
    {ErrorType.RETRYABLE, ErrorType.CONNECTION_TIMEOUT, ErrorType.API_TIMEOUT}
)


@dataclass(frozen=True)
class ToolResult:
    """What a tool call produced, and how it ended.

    ``content`` is what the model sees. ``status`` is what the agent, traces,
    and UI act on, so none of them need to inspect the text.
    """

    status: ToolStatus
    content: str
    error_type: str | None = None
    retryable: bool = False

    @classmethod
    def from_output(cls, output: Any) -> ToolResult:
        """Convert a handler's return value.

        Handlers signal failure with an ``"Error:"`` prefix by convention.
        This is the only place that convention is read; a handler that needs
        more precision can return a ToolResult itself.
        """
        if isinstance(output, ToolResult):
            return output
        text = output if isinstance(output, str) else str(output)
        if not text.startswith("Error:"):
            return cls("completed", text)
        return cls("failed", text, retryable=classify_error(message=text) in _TRANSIENT_ERRORS)

    @classmethod
    def failed(cls, content: str, error_type: str | None = None) -> ToolResult:
        return cls("failed", content, error_type=error_type)

    @classmethod
    def denied(cls, content: str) -> ToolResult:
        return cls("denied", content)

    @classmethod
    def interrupted(cls) -> ToolResult:
        return cls("interrupted", INTERRUPTED_MESSAGE)
