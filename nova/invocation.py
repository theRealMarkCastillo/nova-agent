from __future__ import annotations

import math
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

RunStatus = Literal["completed", "blocked", "cancelled", "limit_reached", "failed"]
StopReason = Literal[
    "final_response",
    "permission_required",
    "cancelled",
    "timeout",
    "token_limit",
    "cost_limit",
    "iteration_limit",
    "error",
]
CostSource = Literal["reported", "estimated", "unknown"]


@dataclass(frozen=True)
class NovaOptions:
    workspace: Path
    model: str | None = None
    session_id: str | None = None
    config_path: Path | None = None
    max_iterations: int = 20
    max_total_tokens: int | None = None
    max_cost_usd: float | None = None
    timeout_seconds: float = 300

    def validate(self, task: str) -> Path:
        if not isinstance(task, str) or not task.strip():
            raise ValueError("task must be nonempty text")
        for name in ("max_iterations", "max_total_tokens"):
            value = getattr(self, name)
            if value is None and name == "max_total_tokens":
                continue
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        for name in ("max_cost_usd", "timeout_seconds"):
            value = getattr(self, name)
            if value is None and name == "max_cost_usd":
                continue
            try:
                valid = type(value) in (int, float) and value > 0 and math.isfinite(value)
            except OverflowError:
                valid = False
            if not valid:
                raise ValueError(f"{name} must be positive and finite")
        for name in ("model", "session_id"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{name} must be nonempty text")
        if not isinstance(self.workspace, Path) or not self.workspace.is_dir():
            raise ValueError("workspace must be an existing directory")
        if self.config_path is not None and (
            not isinstance(self.config_path, Path) or not self.config_path.is_file()
        ):
            raise ValueError("config_path must be an existing file")
        return self.workspace.resolve()


@dataclass(frozen=True)
class NovaUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float | None = None
    cost_source: CostSource = "unknown"
    complete: bool = True


@dataclass(frozen=True)
class NovaError:
    code: str
    message: str
    retryable: bool = False


@dataclass(frozen=True)
class NovaRunResult:
    run_id: str | None
    session_id: str | None
    output: str
    status: RunStatus
    stop_reason: StopReason
    verification_status: str | None = None
    usage: NovaUsage = field(default_factory=NovaUsage)
    error: NovaError | None = None
    trace: dict[str, Any] | None = None
    schema_version: Literal[1] = field(default=1, init=False)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class _Event:
    run_id: str
    sequence: int
    schema_version: Literal[1] = field(default=1, init=False)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RunStarted(_Event):
    session_id: str | None
    type: Literal["run_started"] = field(default="run_started", init=False)


@dataclass(frozen=True)
class MessageDelta(_Event):
    delta: str
    type: Literal["message_delta"] = field(default="message_delta", init=False)


@dataclass(frozen=True)
class ToolStarted(_Event):
    call_id: str
    tool_name: str
    type: Literal["tool_started"] = field(default="tool_started", init=False)


@dataclass(frozen=True)
class ToolFinished(_Event):
    call_id: str
    tool_name: str
    status: str
    type: Literal["tool_finished"] = field(default="tool_finished", init=False)


@dataclass(frozen=True)
class RunFinished(_Event):
    result: NovaRunResult
    type: Literal["run_finished"] = field(default="run_finished", init=False)


NovaEvent = RunStarted | MessageDelta | ToolStarted | ToolFinished | RunFinished


class _CallbackFailure(BaseException):
    def __init__(self, error: BaseException) -> None:
        self.error = error


class _EventEmitter:
    def __init__(self, run_id: str, callback: Callable[[NovaEvent], None] | None) -> None:
        self.run_id = run_id
        self.callback = callback
        self.sequence = 0
        self.failed = False
        self._lock = threading.RLock()

    def emit(self, event_type: type[NovaEvent], **payload: Any) -> None:
        with self._lock:
            if self.failed or self.callback is None:
                return
            self.sequence += 1
            event = event_type(run_id=self.run_id, sequence=self.sequence, **payload)
            try:
                self.callback(event)
            except BaseException as error:
                self.failed = True
                raise _CallbackFailure(error) from error
