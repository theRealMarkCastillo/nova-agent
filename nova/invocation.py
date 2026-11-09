from __future__ import annotations

import math
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from nova.model_metadata import ModelMetadata

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
    include_trace: bool = False

    def validate(self, task: str) -> Path:
        if not isinstance(task, str) or not task.strip():
            raise ValueError("task must be nonempty text")
        if type(self.include_trace) is not bool:
            raise ValueError("include_trace must be a boolean")
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
    run_id: str | None
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
    def __init__(self, run_id: str | None, callback: Callable[[NovaEvent], None] | None) -> None:
        self.run_id = run_id
        self.callback = callback
        self.sequence = 0
        self.failed = False
        self.pending_tools: dict[str, str] = {}
        self._lock = threading.RLock()

    def emit(self, event_type: type[NovaEvent], **payload: Any) -> None:
        with self._lock:
            if self.failed or self.callback is None:
                return
            if event_type is RunFinished:
                for call_id, name in list(self.pending_tools.items()):
                    self.emit(ToolFinished, call_id=call_id, tool_name=name, status="interrupted")
            elif event_type is ToolStarted:
                self.pending_tools[payload["call_id"]] = payload["tool_name"]
            elif event_type is ToolFinished:
                self.pending_tools.pop(payload["call_id"], None)
            self.sequence += 1
            event = event_type(run_id=self.run_id, sequence=self.sequence, **payload)
            try:
                self.callback(event)
            except BaseException as error:
                self.failed = True
                raise _CallbackFailure(error) from error


BOUNDED_TOOLS = frozenset(
    {
        "read_file",
        "write_file",
        "patch_file",
        "list_files",
        "search_files",
        "skills_list",
        "skill_view",
        "terminal",
        "delegate_task",
        "discover_tools",
    }
)


class _StopInvocation(BaseException):
    def __init__(self, reason: StopReason, error: NovaError | None = None) -> None:
        self.reason = reason
        self.error = error


class InvocationControl:
    def __init__(self, options: NovaOptions, cancel: threading.Event | None = None) -> None:
        from nova.cost_tracker import CostTracker

        self.options = options
        self.cancel = cancel if cancel is not None else threading.Event()
        self.deadline = time.monotonic() + options.timeout_seconds
        self.tracker = CostTracker()
        self.reserved_tokens = 0
        self.reserved_cost = 0.0
        self.stopped: _StopInvocation | None = None
        self.callback_error: BaseException | None = None
        self.locked_sessions: set[str] = set()
        self.invalid_report = False
        self.metadata: dict[str, ModelMetadata] = {}
        self._lock = threading.RLock()

    def halt(self, reason: StopReason, error: NovaError | None = None) -> None:
        with self._lock:
            if self.stopped is None:
                self.stopped = _StopInvocation(reason, error)
            raise self.stopped

    def check(self) -> None:
        with self._lock:
            if self.callback_error is not None:
                raise _CallbackFailure(self.callback_error)
            if self.stopped is not None:
                raise self.stopped
            if self.cancel.is_set():
                self.halt("cancelled")
            if time.monotonic() >= self.deadline:
                self.halt("timeout")

    def remaining(self) -> float:
        self.check()
        return max(0.001, self.deadline - time.monotonic())

    def interrupted(self) -> bool:
        self.check()
        return False

    def wait(self, seconds: float) -> None:
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            self.check()
            self.cancel.wait(min(0.05, self.remaining(), until - time.monotonic()))

    def reserve(self, payload: dict) -> tuple[int, float]:
        from nova.tokens import estimate_total_request_tokens

        with self._lock:
            self.check()
            self._check_usage()
            prompt = estimate_total_request_tokens(payload["messages"], tools=payload.get("tools"))
            output = payload["max_tokens"]
            usage = self.tracker.total
            if self.options.max_total_tokens is not None:
                remaining = (
                    self.options.max_total_tokens
                    - usage.total_tokens
                    - self.reserved_tokens
                    - prompt
                )
                if remaining < 1:
                    self.halt("token_limit")
                output = min(output, remaining)
            reserved_cost = 0.0
            if self.options.max_cost_usd is not None:
                metadata = self.metadata.get(payload["model"])
                if metadata is None or any(
                    value is None or not math.isfinite(value) or value < 0
                    for value in (
                        metadata.input_price_per_million,
                        metadata.output_price_per_million,
                    )
                ):
                    self.halt(
                        "error", NovaError("usage_unavailable", "Model pricing is unavailable.")
                    )
                assert metadata is not None
                input_price = (
                    max(
                        metadata.input_price_per_million or 0,
                        metadata.cache_write_price_per_million or 0,
                    )
                    / 1_000_000
                )
                output_price = (metadata.output_price_per_million or 0) / 1_000_000
                remaining_cost = (
                    self.options.max_cost_usd
                    - usage.accounted_cost
                    - self.reserved_cost
                    - prompt * input_price
                )
                if remaining_cost < 0 or (output_price and remaining_cost < output_price):
                    self.halt("cost_limit")
                if output_price:
                    output = min(output, int(remaining_cost / output_price))
                reserved_cost = prompt * input_price + output * output_price
            payload["max_tokens"] = output
            self.reserved_tokens += prompt + output
            self.reserved_cost += reserved_cost
            return prompt + output, reserved_cost

    def record(self, model: str, response: dict | None) -> None:
        from nova.cost_tracker import CostTracker, extract_usage_from_response

        raw = response.get("usage") if response is not None else None
        complete = isinstance(raw, dict) and all(
            type(value) is int and value >= 0 and _finite(value)
            for value in (
                raw.get("prompt_tokens", raw.get("input_tokens")),
                raw.get("completion_tokens", raw.get("output_tokens")),
            )
        )
        invalid = raw is not None and not complete
        bad_cost = (
            isinstance(raw, dict)
            and raw.get("cost") is not None
            and (
                type(raw["cost"]) not in (int, float) or not _finite(raw["cost"]) or raw["cost"] < 0
            )
        )
        tracker = CostTracker(model=model, metadata_resolver=self.metadata.get)
        if complete:
            usage = extract_usage_from_response(response or {})
            if bad_cost:
                usage.pop("input_cost", None)
                usage.pop("output_cost", None)
                usage.pop("total_cost", None)
            if any(
                type(usage[key]) is not int or usage[key] < 0 or not _finite(usage[key])
                for key in ("cache_read_tokens", "cache_write_tokens")
            ):
                tracker.add_usage(usage_reported=False)
                invalid = True
            else:
                tracker.add_usage(**usage)
        else:
            tracker.add_usage(usage_reported=False)
        with self._lock:
            snapshot = tracker.total
            if bad_cost or not _finite(snapshot.accounted_cost):
                snapshot = replace(
                    snapshot, accounted_cost=0.0, unknown_cost_count=snapshot.unknown_cost_count + 1
                )
                invalid = True
            self.invalid_report = self.invalid_report or invalid
            self.tracker.merge(snapshot)

    def release(self, reservation: tuple[int, float]) -> None:
        with self._lock:
            self.reserved_tokens -= reservation[0]
            self.reserved_cost -= reservation[1]

    def _check_usage(self) -> None:
        usage = self.tracker.total
        if self.invalid_report:
            self.halt(
                "error", NovaError("usage_unavailable", "Provider usage or cost is malformed.")
            )
        if (
            self.options.max_total_tokens is not None or self.options.max_cost_usd is not None
        ) and usage.missing_usage_count:
            self.halt("error", NovaError("usage_unavailable", "Provider usage is incomplete."))
        if self.options.max_cost_usd is not None and usage.unknown_cost_count:
            self.halt("error", NovaError("usage_unavailable", "Provider cost is unavailable."))
        if (
            self.options.max_total_tokens is not None
            and usage.total_tokens > self.options.max_total_tokens
        ):
            self.halt("token_limit")
        if (
            self.options.max_cost_usd is not None
            and usage.accounted_cost > self.options.max_cost_usd
        ):
            self.halt("cost_limit")

    def checkpoint(self) -> None:
        with self._lock:
            self.check()
            self._check_usage()

    def usage(self) -> NovaUsage:
        snapshot = self.tracker.total
        known = (
            not snapshot.missing_usage_count
            and not snapshot.unknown_cost_count
            and (snapshot.reported_cost_count or snapshot.estimated_cost_count)
        )
        return NovaUsage(
            input_tokens=snapshot.input_tokens,
            output_tokens=snapshot.output_tokens,
            cache_read_tokens=snapshot.cache_read_tokens,
            cache_write_tokens=snapshot.cache_write_tokens,
            cost_usd=snapshot.accounted_cost if known else None,
            cost_source="estimated"
            if known and snapshot.estimated_cost_count
            else "reported"
            if known
            else "unknown",
            complete=not snapshot.missing_usage_count,
        )


def run_task(
    task: str,
    *,
    options: NovaOptions,
    on_event: Callable[[NovaEvent], None] | None = None,
    confirm: Callable[[str, dict[str, Any]], bool] | None = None,
    cancel_event: threading.Event | None = None,
) -> NovaRunResult:
    import copy
    from contextlib import ExitStack

    from nova.agent import NovaAgent
    from nova.config import ConfigError, load_config, set_model
    from nova.session import SessionBusyError, SessionStore

    agent = None
    store = None
    control = None
    session_id = None
    terminal = None
    callback_error: BaseException | None = None
    last_sequence = 0
    started_run_id = None

    def deliver(event: NovaEvent) -> None:
        nonlocal terminal, callback_error, last_sequence, started_run_id
        last_sequence, started_run_id = event.sequence, event.run_id
        if event.type == "run_finished":
            terminal = event
        elif on_event:
            try:
                on_event(event)
            except BaseException as error:
                callback_error = error
                if control is not None:
                    control.callback_error = error
                raise

    def confirmation(name: str, arguments: dict[str, Any]) -> bool:
        nonlocal callback_error
        if confirm is None:
            assert control is not None
            control.halt("permission_required")
        try:
            approved = bool(confirm(name, arguments))
        except BaseException as error:
            callback_error = error
            assert control is not None
            control.callback_error = error
            raise _CallbackFailure(error) from error
        if not approved:
            assert control is not None
            control.halt("permission_required")
        return True

    def failure(code: str, message: str) -> NovaRunResult:
        return NovaRunResult(
            started_run_id, session_id, "", "failed", "error", error=NovaError(code, message)
        )

    try:
        workspace = options.validate(task)
        control = InvocationControl(options, cancel_event)
        control.check()
        config = copy.deepcopy(load_config(options.config_path, workspace=workspace))
        config["agent"]["max_iterations"] = options.max_iterations
        config["cost_tracking"] = {"enabled": True}
        config["llm"]["timeout_seconds"] = min(120, control.remaining())
        if options.model:
            set_model(config, options.model)
        db_path = Path(config["session"]["directory"]).expanduser() / "sessions.db"
        if options.session_id:
            info = SessionStore.peek_session(db_path, options.session_id)
            if info is None:
                return_result = failure("session_not_found", "Session does not exist.")
                raise _PreflightResult(return_result)
            if info.get("workspace") != str(workspace):
                raise _PreflightResult(
                    failure(
                        "workspace_mismatch", "Session belongs to a different or unknown workspace."
                    )
                )
        session_id = options.session_id or str(uuid.uuid4())
        with ExitStack() as stack:
            stack.enter_context(SessionStore.writer(db_path, session_id))
            control.locked_sessions.add(session_id)
            stack.callback(control.locked_sessions.discard, session_id)
            if options.session_id:
                info = SessionStore.peek_session(db_path, session_id)
                if info is None or info.get("workspace") != str(workspace):
                    raise _PreflightResult(
                        failure("workspace_mismatch", "Session workspace changed.")
                    )
            store = SessionStore(db_path)
            if not options.session_id:
                store.create_session(
                    session_id=session_id, model=config["llm"]["model"], workspace=str(workspace)
                )
            try:
                agent = NovaAgent(
                    config=config,
                    session_id=session_id,
                    workspace=workspace,
                    session_store=store,
                    confirmation_callback=confirmation,
                    invocation_control=control,
                )
                control.check()
                result = agent.run_result(
                    task,
                    stream=on_event is not None,
                    on_event=deliver,
                    include_trace=options.include_trace,
                )
            finally:
                close_error = None
                for resource in (agent, store):
                    if resource is not None:
                        try:
                            resource.close()
                        except Exception as error:
                            close_error = error
                if (
                    close_error is not None
                    and callback_error is None
                    and agent is not None
                    and agent.last_run_result is not None
                    and agent.last_run_result.status == "completed"
                ):
                    result = replace(
                        agent.last_run_result,
                        status="failed",
                        stop_reason="error",
                        error=NovaError("internal_error", "Resource cleanup failed."),
                    )
        result = replace(result, usage=control.usage())
    except _PreflightResult as error:
        result = error.result
    except _StopInvocation as error:
        result = NovaRunResult(
            None,
            session_id,
            "",
            _status_for_reason(error.reason),
            error.reason,
            usage=control.usage() if control else NovaUsage(),
            error=error.error,
        )
    except BaseException as error:
        if callback_error is not None:
            raise callback_error from None
        if not isinstance(error, Exception):
            raise
        code = (
            "session_busy"
            if isinstance(error, SessionBusyError)
            else "invalid_options"
            if isinstance(error, (ValueError, ConfigError))
            else "internal_error"
        )
        result = failure(
            code,
            "Session already has a writer."
            if code == "session_busy"
            else "Invocation could not be started.",
        )
    if on_event:
        final = (
            replace(terminal, result=result)
            if terminal is not None
            else RunFinished(run_id=result.run_id, sequence=last_sequence + 1, result=result)
        )
        on_event(final)
    return result


class _PreflightResult(BaseException):
    def __init__(self, result: NovaRunResult) -> None:
        self.result = result


def _status_for_reason(reason: StopReason) -> RunStatus:
    return {
        "final_response": "completed",
        "permission_required": "blocked",
        "cancelled": "cancelled",
        "error": "failed",
    }.get(reason, "limit_reached")  # type: ignore[return-value]


def _finite(value: int | float) -> bool:
    try:
        return math.isfinite(value)
    except OverflowError:
        return False
