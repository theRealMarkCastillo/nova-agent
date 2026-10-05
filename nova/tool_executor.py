"""Tool execution pipeline.

Every tool call runs through one sequence:
parse -> workspace defaults -> policy -> confirmation -> hooks -> dispatch
-> transient retry -> verification -> trace and observability.
"""

from __future__ import annotations

import contextvars
import functools
import json
import logging
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from nova.harness import VerificationResult
from nova.hooks import EVENT_POST_TOOL_CALL, EVENT_PRE_TOOL_CALL, hooks
from nova.mcp_client import McpToolInfo
from nova.observability import redact
from nova.permissions import PermissionResult, has_untrusted_output, is_egress_tool
from nova.tools.registry import ToolEntry, registry
from nova.tools.result import ToolResult

if TYPE_CHECKING:
    from nova.agent import NovaAgent

logger = logging.getLogger(__name__)

MCP_RESOURCE_TOOL = "mcp_read_resource"

_MAX_PARALLEL_TOOLS = 4

# Harness traces have no "interrupted" outcome; an interruption is a failed
# call whose verification is inconclusive.
_TRACE_OUTCOMES: dict[str, Literal["completed", "failed", "denied"]] = {
    "completed": "completed",
    "failed": "failed",
    "denied": "denied",
    "interrupted": "failed",
}


@dataclass(frozen=True)
class _Call:
    id: str
    name: str
    arguments: dict[str, Any] | None
    parse_error: ToolResult | None = None


class ToolExecutor:
    """Runs one agent's tool calls.

    UI and protocol layers set callbacks (`_interrupt_check`,
    `_confirmation_callback`, `_tool_callback`, `_tool_lifecycle_callback`)
    on the agent at any time, so they are read from the agent on each use.
    """

    def __init__(self, agent: NovaAgent) -> None:
        self._agent = agent
        self._mcp_lock = threading.RLock()
        # Set once output from the web, HTTP, or MCP is in the conversation; it
        # may carry injected instructions, so outbound tools then need approval.
        self.untrusted_content_seen = False
        self._untrusted_output_pending = False

    def run_batch(self, tool_calls: list[dict]) -> list[ToolResult]:
        """Run one model response's tool calls, read-only ones in parallel."""
        parallel: list[tuple[int, dict]] = []
        sequential: list[tuple[int, dict]] = []
        for index, tool_call in enumerate(tool_calls):
            target = parallel if self._can_run_in_parallel(tool_call) else sequential
            target.append((index, tool_call))

        results: list[ToolResult | None] = [None] * len(tool_calls)
        for tool_call in tool_calls:
            self._report_start(tool_call)

        if parallel:
            with ThreadPoolExecutor(max_workers=min(len(parallel), _MAX_PARALLEL_TOOLS)) as pool:
                futures = {}
                for index, tool_call in parallel:
                    self._announce(tool_call)
                    context = contextvars.copy_context()
                    futures[pool.submit(context.run, self.run, tool_call)] = index
                for future in as_completed(futures):
                    index = futures[future]
                    results[index] = self._guarded(future.result, tool_calls[index])
                    self._report_result(tool_calls[index], results[index])

        for index, tool_call in sequential:
            if self._interrupted():
                results[index] = ToolResult.interrupted()
            else:
                self._announce(tool_call)
                results[index] = self._guarded(functools.partial(self.run, tool_call), tool_call)
            self._report_result(tool_call, results[index])

        # The model sees this batch's output only in its next response, so the
        # stricter policy starts with the next batch.
        if self._untrusted_output_pending:
            self.untrusted_content_seen = True
            self._untrusted_output_pending = False

        return [
            result if result is not None else ToolResult.failed("Error: Tool produced no result")
            for result in results
        ]

    def skip_batch(self, tool_calls: list[dict]) -> list[ToolResult]:
        """Record a batch the user interrupted before any call started."""
        collector = self._agent._active_trace
        results = []
        for tool_call in tool_calls:
            result = ToolResult.interrupted()
            self._report_start(tool_call)
            if collector is not None:
                call = self._parse(tool_call)
                trace = collector.start_tool(call.id, call.name, redact(call.arguments or {}))
                collector.finish_tool(
                    trace,
                    outcome="failed",
                    result=result.content,
                    verification=VerificationResult("inconclusive", reason="interrupted"),
                )
            self._report_result(tool_call, result)
            results.append(result)
        return results

    def run(self, tool_call: dict) -> ToolResult:
        """Run one tool call through the full pipeline."""
        call = self._parse(tool_call)
        collector = self._agent._active_trace
        trace = (
            collector.start_tool(call.id, call.name, redact(call.arguments or {}))
            if collector
            else None
        )

        def record_policy(permission: PermissionResult) -> None:
            if trace is not None and collector is not None:
                collector.policy(
                    trace,
                    allowed=permission.allowed,
                    confirmation_required=permission.requires_confirmation,
                    reason=permission.reason,
                )

        try:
            result, attempts = self._execute(call, record_policy)
        except BaseException as exc:
            if trace is not None and collector is not None:
                collector.finish_tool(trace, outcome="failed", result=type(exc).__name__)
            raise

        if attempts and has_untrusted_output(call.name):
            self._untrusted_output_pending = True
        verification = self._verify(call, result, ran=attempts > 0)
        if trace is not None and collector is not None:
            collector.finish_tool(
                trace,
                outcome=_TRACE_OUTCOMES[result.status],
                result=redact(result.content),
                verification=verification,
            )
        observability = self._agent.observability
        observability.tool(
            call.name,
            input_data=call.arguments,
            output_data=result.content,
            status=result.status,
            retries=max(0, attempts - 1),
        )
        observability.verification(
            call.name,
            status=verification.status if verification else "inconclusive",
            evidence=verification.evidence if verification else "",
            reason=verification.reason if verification else "",
        )
        return result

    def _execute(
        self, call: _Call, on_policy: Callable[[PermissionResult], None]
    ) -> tuple[ToolResult, int]:
        """Return the result and how many times the tool was dispatched."""
        if call.parse_error is not None:
            return call.parse_error, 0
        name = call.name
        arguments = call.arguments or {}
        if self._interrupted():
            return ToolResult.interrupted(), 0

        entry = registry.get_tool(name)
        mcp_tool = self._agent._mcp_tool_info(name)
        is_resource = name == MCP_RESOURCE_TOOL
        if entry is None and mcp_tool is None and not is_resource:
            return ToolResult.failed(f"Error: Unknown tool: {name}", "unknown_tool"), 0
        is_read_only = entry.is_read_only if entry else is_resource

        permission = self._evaluate_policy(name, entry, is_read_only, arguments)
        self._agent.observability.policy(name, allowed=permission.allowed, reason=permission.reason)
        on_policy(permission)
        if not permission.allowed:
            logger.warning("Tool call denied: %s — %s", name, permission.reason)
            return ToolResult.denied(f"Error: {permission.reason}"), 0
        if permission.requires_confirmation and not self._confirm(name, arguments):
            logger.info("Tool '%s' denied because confirmation was not granted", name)
            return ToolResult.denied(f"Error: Tool '{name}' requires confirmation"), 0

        if self._interrupted():
            return ToolResult.interrupted(), 0
        hooks.emit(EVENT_PRE_TOOL_CALL, tool_name=name, args=arguments)

        agent_config = self._agent.config.get("agent", {})
        max_retries = max(0, agent_config.get("tool_retry_max_attempts", 2))
        result = ToolResult.failed("Error: Tool did not run")
        for attempt in range(max_retries + 1):
            if self._interrupted():
                return ToolResult.interrupted(), attempt
            try:
                result = self._dispatch(name, arguments, mcp_tool, is_resource)
                hooks.emit(
                    EVENT_POST_TOOL_CALL, tool_name=name, args=arguments, result=result.content
                )
            except Exception as exc:
                error_type = type(exc).__name__
                return ToolResult.failed(
                    f"Error: Tool '{name}' failed: {error_type}", error_type
                ), (attempt + 1)

            if not (is_read_only and result.retryable and attempt < max_retries):
                return result, attempt + 1
            wait_time = 2**attempt
            logger.warning(
                "Tool %s failed with transient error (attempt %d/%d), retrying in %ds: %s",
                name,
                attempt + 1,
                max_retries + 1,
                wait_time,
                result.content[:100],
            )
            if self._wait_unless_interrupted(wait_time):
                return ToolResult.interrupted(), attempt + 1
        return result, max_retries + 1

    def _evaluate_policy(
        self,
        name: str,
        entry: ToolEntry | None,
        is_read_only: bool,
        arguments: dict[str, Any],
    ) -> PermissionResult:
        permission = self._agent.permission_checker.evaluate(
            name,
            is_read_only=is_read_only,
            file_path=_permission_path(arguments),
            command=arguments.get("command"),
            untrusted_context=self.untrusted_content_seen,
        )
        if (
            not permission.allowed
            or permission.requires_confirmation
            or entry is None
            or entry.always_confirm is None
        ):
            return permission
        try:
            reason = entry.always_confirm(arguments, wiki=self._agent.wiki)
        except Exception:
            logger.exception("always_confirm check failed for %s", name)
            reason = "could not be checked for safety"
        if not reason:
            return permission
        return PermissionResult(
            allowed=True, requires_confirmation=True, reason=f"Tool '{name}' {reason}"
        )

    def _dispatch(
        self,
        name: str,
        arguments: dict[str, Any],
        mcp_tool: McpToolInfo | None,
        is_resource: bool,
    ) -> ToolResult:
        agent = self._agent
        if mcp_tool is not None:
            with self._mcp_lock:
                if self._interrupted():
                    return ToolResult.interrupted()
                return ToolResult.from_output(
                    agent.mcp_client.call_tool(mcp_tool.server_name, mcp_tool.name, arguments)
                )
        if is_resource:
            return ToolResult.from_output(self._read_mcp_resource(arguments))
        return ToolResult.from_output(
            registry.dispatch(
                name,
                arguments,
                config=agent.config,
                wiki=agent.wiki,
                session_store=agent.session_store,
                workspace=agent.workspace,
                agent=agent,
            )
        )

    def _read_mcp_resource(self, arguments: dict[str, Any]) -> str:
        server_name = arguments.get("server_name")
        uri = arguments.get("uri")
        if not isinstance(server_name, str) or not server_name:
            return "Error: server_name must be a non-empty string"
        if not isinstance(uri, str) or not uri:
            return "Error: uri must be a non-empty string"
        mcp_client = self._agent.mcp_client
        if not mcp_client.is_connected(server_name):
            return f"Error: MCP server '{server_name}' is not connected."
        with self._mcp_lock:
            return mcp_client.read_resource(server_name, uri)

    def _verify(self, call: _Call, result: ToolResult, *, ran: bool) -> VerificationResult | None:
        entry = registry.get_tool(call.name)
        if ran and entry is not None and entry.verifier is not None and call.arguments is not None:
            try:
                return entry.verifier(call.arguments, result.content, agent=self._agent)
            except Exception as exc:
                return VerificationResult("inconclusive", reason=type(exc).__name__)
        if result.status in ("failed", "denied"):
            return VerificationResult("failed", reason=f"tool call {result.status}")
        if result.status == "interrupted":
            return VerificationResult("inconclusive", reason="interrupted")
        return None

    def _parse(self, tool_call: dict) -> _Call:
        function = tool_call.get("function", {})
        name = function.get("name", "")
        call_id = tool_call.get("id", "") or str(uuid.uuid4())
        raw = function.get("arguments", "{}")
        try:
            arguments = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            error = ToolResult.failed(
                f"Error: Invalid JSON arguments: {redact(raw)}", "invalid_arguments"
            )
            return _Call(call_id, name, None, error)
        if not isinstance(arguments, dict):
            error = ToolResult.failed(
                "Error: Tool arguments must be an object", "invalid_arguments"
            )
            return _Call(call_id, name, None, error)
        return _Call(call_id, name, self._apply_workspace_defaults(name, arguments))

    def _apply_workspace_defaults(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        workspace = self._agent.workspace
        arguments = dict(arguments)
        if name == "terminal" and not arguments.get("workdir"):
            arguments["workdir"] = str(workspace)
        elif name in {"read_file", "write_file", "patch_file"}:
            path = arguments.get("path")
            if isinstance(path, str) and path and not Path(path).expanduser().is_absolute():
                arguments["path"] = str(workspace / path)
        elif name == "search_files":
            path = arguments.get("path", ".")
            if isinstance(path, str) and not Path(path).expanduser().is_absolute():
                arguments["path"] = str(workspace / path)
        elif name == "list_files":
            root = arguments.get("root", ".")
            if isinstance(root, str) and not Path(root).expanduser().is_absolute():
                arguments["root"] = str(workspace / root)
        elif name.startswith("git_"):
            repo = arguments.get("repo", ".")
            if isinstance(repo, str) and not Path(repo).expanduser().is_absolute():
                arguments["repo"] = str(workspace / repo)
        return arguments

    def _can_run_in_parallel(self, tool_call: dict) -> bool:
        name = tool_call.get("function", {}).get("name", "")
        entry = registry.get_tool(name)
        read_only = (entry is not None and entry.is_read_only) or name == MCP_RESOURCE_TOOL
        # Calls that may prompt for confirmation must run on the caller's thread.
        gated_egress = self.untrusted_content_seen and is_egress_tool(name)
        return read_only and not gated_egress

    def _guarded(self, run: Callable[[], ToolResult], tool_call: dict) -> ToolResult:
        try:
            return run()
        except Exception as exc:
            name = tool_call.get("function", {}).get("name", "")
            logger.exception("Tool call '%s' failed", name)
            error_type = type(exc).__name__
            return ToolResult.failed(f"Error: Tool '{name}' failed: {error_type}", error_type)

    def _interrupted(self) -> bool:
        check = self._agent._interrupt_check
        return check is not None and check()

    def _wait_unless_interrupted(self, seconds: float) -> bool:
        """Sleep up to ``seconds``; return True as soon as the user interrupts."""
        deadline = time.monotonic() + seconds
        while (remaining := deadline - time.monotonic()) > 0:
            if self._interrupted():
                return True
            time.sleep(min(0.1, remaining))
        return self._interrupted()

    def _confirm(self, name: str, arguments: dict[str, Any]) -> bool:
        confirm = self._agent._confirmation_callback
        return bool(confirm and confirm(name, arguments))

    def _announce(self, tool_call: dict) -> None:
        callback = getattr(self._agent, "_tool_callback", None)
        name = tool_call.get("function", {}).get("name", "")
        if callback and name:
            callback(name)

    def _report_start(self, tool_call: dict) -> None:
        callback = self._agent._tool_lifecycle_callback
        call_id = tool_call.get("id", "")
        name = tool_call.get("function", {}).get("name", "")
        if callback and call_id and name:
            callback(call_id, name, "start", None)

    def _report_result(self, tool_call: dict, result: ToolResult | None) -> None:
        callback = self._agent._tool_lifecycle_callback
        call_id = tool_call.get("id", "")
        name = tool_call.get("function", {}).get("name", "")
        if callback and call_id and name and result is not None:
            status = "completed" if result.status == "completed" else "failed"
            callback(call_id, name, status, result.content)


def _permission_path(arguments: dict[str, Any]) -> str | None:
    """Return the path-like argument that must go through policy checks."""
    for key in ("path", "file_path", "root", "repo", "workdir"):
        value = arguments.get(key)
        if isinstance(value, str) and value:
            return value
    return None
