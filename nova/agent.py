"""Main agent loop.

Handles the conversation loop with tool calling, streaming,
deterministic context management, and session management.
"""

from __future__ import annotations

import contextvars
import copy
import json
import logging
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Literal

from nova.config import ensure_nova_home, load_config
from nova.cost_tracker import CostTracker, extract_usage_from_response
from nova.harness import HarnessTrace, VerificationResult, derive_run_status
from nova.hooks import (
    EVENT_POST_LLM_CALL,
    EVENT_POST_TOOL_CALL,
    EVENT_PRE_LLM_CALL,
    EVENT_PRE_TOOL_CALL,
    EVENT_SESSION_START,
    hooks,
)
from nova.mcp_client import McpToolInfo, build_mcp_client
from nova.microcompact import compact_to_token_budget
from nova.model_metadata import get_model_context_window, load_provider_metadata
from nova.observability import create_observability, redact
from nova.permissions import PermissionChecker, build_permission_checker
from nova.prompt import build_system_prompt
from nova.providers import (
    build_client,
    chat_completion,
    stream_response,
)
from nova.retry import ErrorType, classify_error, retry_with_backoff
from nova.session import SessionStore
from nova.tokens import (
    estimate_tokens,
    estimate_total_request_tokens,
)
from nova.tools.registry import discover_builtin_tools, registry
from nova.wiki_memory import WikiMemory

logger = logging.getLogger(__name__)

# Injected as a trailing system message after deterministic compaction, so the
# model knows earlier turns are recoverable instead of guessing from memory.
_COMPACTION_NOTE_PREFIX = "[older conversation history was removed"
_COMPACTION_NOTE = {
    "role": "system",
    "content": (
        "[older conversation history was removed to fit the context window. "
        "If you need details from earlier in this conversation or a past session, "
        "recover them with search_messages then read_session instead of guessing.]"
    ),
}


class ContextBudgetError(ValueError):
    """Raised when the system prompt and active turn cannot fit the model window."""


def _normalize_message_history(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove incomplete tool-call blocks before sending history to a provider."""
    normalized: list[dict[str, Any]] = []
    pending_ids: set[str] = set()
    pending_start: int | None = None

    for message in messages:
        role = message.get("role")
        if role == "assistant":
            calls = message.get("tool_calls")
            if calls:
                valid_calls = [call for call in calls if call.get("id")]
                if not valid_calls:
                    # Calls without ids cannot be answered; keep any prose but
                    # never leave an unanswerable tool_call in history.
                    if pending_ids and pending_start is not None:
                        del normalized[pending_start:]
                        pending_ids.clear()
                        pending_start = None
                    if message.get("content"):
                        normalized.append({**message, "tool_calls": None})
                    continue
                if len(valid_calls) != len(calls):
                    message = {**message, "tool_calls": valid_calls}
                if pending_ids and pending_start is not None:
                    del normalized[pending_start:]
                pending_ids = {call["id"] for call in valid_calls}
                pending_start = len(normalized)
                normalized.append(message)
            else:
                if pending_ids and pending_start is not None:
                    del normalized[pending_start:]
                    pending_ids.clear()
                    pending_start = None
                normalized.append(message)
        elif role == "tool":
            call_id = message.get("tool_call_id", "")
            if not pending_ids or call_id not in pending_ids:
                continue
            normalized.append(message)
            pending_ids.remove(call_id)
            if not pending_ids:
                pending_start = None
        else:
            if pending_ids and pending_start is not None:
                del normalized[pending_start:]
                pending_ids.clear()
                pending_start = None
            normalized.append(message)

    if pending_ids and pending_start is not None:
        del normalized[pending_start:]
    return normalized


class NovaAgent:
    """Main agent class with explicit token budgets and smart context management."""

    def __init__(
        self,
        config: dict | None = None,
        session_id: str | None = None,
        openai_client: Any | None = None,
        session_store: SessionStore | None = None,
        wiki_memory_store: WikiMemory | None = None,
        prompt_mode: str = "full",
        confirmation_callback: Callable[[str, dict[str, Any]], bool] | None = None,
        workspace: Path | None = None,
        mcp_client: Any | None = None,
    ):
        self.config = copy.deepcopy(config) if config else load_config()
        self._prompt_mode = prompt_mode
        self.session_id = session_id
        self.messages: list[dict[str, Any]] = []
        self._system_prompt: str | None = None
        self._interrupt_check: Callable[[], bool] | None = None
        self._confirmation_callback = confirmation_callback
        self._tool_lifecycle_callback: Callable[[str, str, str, str | None], None] | None = None
        self.workspace = workspace.resolve() if workspace else Path.cwd().resolve()
        self.mcp_client = mcp_client if mcp_client is not None else build_mcp_client(self.config)
        self._mcp_call_lock = threading.RLock()
        self._mcp_tools: dict[str, McpToolInfo] = {}
        self._mcp_resource_tool_name = "mcp_read_resource"
        # Token estimate cache: hash(content) → token_count (bounded to 2048 entries)
        self._token_cache: dict[int, int] = {}
        self.last_run_trace: Any = None
        self._active_trace: HarnessTrace | None = None

        # Initialize components
        ensure_nova_home()

        # Session store (injectable for testing)
        if session_store is not None:
            self.session_store = session_store
        else:
            session_dir = Path(self.config["session"]["directory"]).expanduser()
            self.session_store = SessionStore(session_dir / "sessions.db")

        # Wiki memory store (injectable for testing)
        if wiki_memory_store is not None:
            self.wiki: WikiMemory | None = wiki_memory_store
        elif self.config.get("wiki", {}).get("enabled"):
            vault_path = Path(self.config["wiki"]["vault_path"]).expanduser()
            self.wiki = WikiMemory(
                vault_path,
                max_prompt_notes=self.config["wiki"].get("max_prompt_notes", 10),
            )
        else:
            self.wiki = None

        # LLM client (injectable for testing)
        self._owns_client = openai_client is None
        self.client: Any = (
            openai_client if openai_client is not None else build_client(self.config["llm"])
        )
        load_provider_metadata(self.client)

        # Discover tools (pass config so delegation tool can be gated)
        # Must happen before _create_session so system prompt includes tool summaries
        discover_builtin_tools(self.config)
        self.mcp_client.connect_all()
        self._refresh_mcp_tools()

        try:
            # Sub-agent depth tracking
            self.depth: int = self.config.get("_subagent_depth", 0)
            max_spawn_depth = self.config.get("delegation", {}).get("max_spawn_depth", 2)
            self.is_leaf_agent: bool = self.depth >= max_spawn_depth

            # Permission checker
            self.permission_checker: PermissionChecker = build_permission_checker(
                self.config, workspace=self.workspace
            )

            # Cost tracker
            cost_cfg = self.config.get("cost_tracking", {})
            self.cost_tracker: CostTracker | None = None
            if cost_cfg.get("enabled", True):
                self.cost_tracker = CostTracker(model=self.config["llm"]["model"])
            self.observability = create_observability(self.config)

            # Create or load session (tools discovered above, so _build_system_prompt
            # will include tool summaries from the start)
            if self.session_id:
                self._load_session()
            else:
                self._create_session()

            # Fire session_start hook
            hooks.emit(EVENT_SESSION_START, session_id=self.session_id, config=self.config)
        except Exception:
            # Clean up HTTP client if init fails after creating it
            self.close()
            raise

    def close(self) -> None:
        """Close the HTTP client and release resources."""
        observer = getattr(self, "observability", None)
        if observer is not None:
            observer.shutdown()
        if self._owns_client and hasattr(self.client, "close"):
            self.client.close()
        if hasattr(self, "mcp_client"):
            self.mcp_client.disconnect_all()

    @staticmethod
    def _namespace_mcp_tool(tool: McpToolInfo) -> str:
        server = re.sub(r"[^A-Za-z0-9_-]", "_", tool.server_name)
        name = re.sub(r"[^A-Za-z0-9_-]", "_", tool.name)
        return f"mcp__{server}__{name}"

    def _refresh_mcp_tools(self) -> None:
        self._mcp_tools = {}
        for tool in self.mcp_client.list_tools():
            name = self._namespace_mcp_tool(tool)
            if name not in self._mcp_tools:
                self._mcp_tools[name] = tool

    def _mcp_tool_info(self, name: str) -> McpToolInfo | None:
        return self._mcp_tools.get(name)

    def _mcp_summary(self) -> str:
        lines = [
            f"- {name}: {tool.description.split(chr(10))[0][:100]}"
            for name, tool in sorted(self._mcp_tools.items())
        ]
        if self.mcp_client.list_resources() or self.mcp_client.connected_servers:
            lines.append("- mcp_read_resource: Read a discovered MCP resource")
        return "\n".join(lines)

    def _create_session(self):
        """Create a new session."""
        self.session_id = self.session_store.create_session(
            model=self.config["llm"]["model"],
        )
        self._build_system_prompt()
        self.session_store.update_system_prompt(self.session_id, self._system_prompt or "")

    def _load_session(self):
        """Load an existing session."""
        if not self.session_id:
            self._create_session()
            return

        info = self.session_store.get_session_info(self.session_id)
        if info:
            if info.get("model"):
                self.config["llm"]["model"] = info["model"]
            # Load recent messages only — respect conversation turn limit
            turn_limit = self.config["budgets"].get("conversation_turn_limit", 15)
            self.messages = self.session_store.get_messages(
                self.session_id,
                limit=turn_limit * 4,  # ~4 msgs per turn (user+assistant+tool pairs)
            )
            self.messages = _normalize_message_history(self.messages)
            # Always rebuild the prompt on resume so wiki notes, skills, and
            # context files reflect current state rather than the stale cache.
            self._refresh_system_prompt()
        else:
            logger.warning("Session %s not found, creating new", self.session_id)
            self._create_session()

    def _build_system_prompt(self, mode: str | None = None):
        """Build the system prompt with budget enforcement.

        The mode is resolved in priority order:
        1. Explicit ``mode`` argument (used by tests / refresh calls)
        2. ``self._prompt_mode`` — set at construction time
        3. ``"full"`` — default for root agents
        """
        resolved_mode = mode or self._prompt_mode

        wiki_content = None
        if self.wiki:
            wiki_content = self.wiki.format_for_prompt()

        self._system_prompt = build_system_prompt(
            config=self.config,
            cwd=self.workspace,
            mode=resolved_mode,
            wiki_content=wiki_content,
            extra_tool_summary=self._mcp_summary(),
        )

    def _refresh_system_prompt(self, mode: str | None = None):
        """Rebuild the system prompt (e.g., after memory changes)."""
        self._build_system_prompt(mode=mode)
        if self.session_id:
            self.session_store.update_system_prompt(
                self.session_id,
                self._system_prompt or "",
            )

    def _conversation_messages_from_api(
        self,
        api_messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        messages = list(api_messages)
        if (
            messages
            and messages[0].get("role") == "system"
            and messages[0].get("content") == (self._system_prompt or "")
        ):
            return messages[1:]
        return messages

    def _call_llm(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict] | None = None,
        stream: bool = False,
        stream_callback: Callable[[str], None] | None = None,
    ) -> dict:
        """Make an API call to the OpenAI-compatible endpoint with retry logic."""
        # Fire pre_llm_call hook
        hooks.emit(EVENT_PRE_LLM_CALL, messages=messages, tools=tools)

        llm_config = self.config["llm"]
        agent_config = self.config["agent"]
        retry_cfg = self.config.get("retry", {})

        payload = {
            "model": llm_config["model"],
            "messages": messages,
            "temperature": agent_config.get("temperature", 0.7),
            "top_p": agent_config.get("top_p", 1.0),
            "max_tokens": int(llm_config.get("max_tokens", 8192)),
            "stream_include_usage": agent_config.get("stream_include_usage", True),
        }

        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        # else: omit tools entirely — some models reject an empty tools array.

        reasoning = llm_config.get("reasoning")
        if reasoning is not None:
            payload["extra_body"] = {"reasoning": reasoning}

        max_retries = retry_cfg.get("max_retries", 3)
        base_delay = retry_cfg.get("base_delay", 1.0)
        max_delay = retry_cfg.get("max_delay", 60.0)

        if stream:
            stream_output_started = False

            def _stream_callback(text: str) -> None:
                nonlocal stream_output_started
                stream_output_started = True
                if stream_callback:
                    stream_callback(text)

            def _reasoning_callback(text: str) -> None:
                nonlocal stream_output_started
                stream_output_started = True
                reasoning_callback = getattr(self, "_reasoning_callback", None)
                if reasoning_callback:
                    reasoning_callback(text)

            response_data: dict = retry_with_backoff(
                self._stream_response,
                payload,
                _stream_callback,
                _reasoning_callback,
                max_retries=max_retries,
                base_delay=base_delay,
                max_delay=max_delay,
                retry_if=lambda _error: not stream_output_started,
            )
        else:

            def _do_post() -> dict:
                return chat_completion(self.client, payload)

            response_data = retry_with_backoff(
                _do_post,
                max_retries=max_retries,
                base_delay=base_delay,
                max_delay=max_delay,
            )

        self.observability.llm(
            llm_config["model"],
            input_data=messages,
            output_data=response_data,
            usage=extract_usage_from_response(response_data),
        )

        # Track cost from response
        if self.cost_tracker:
            usage = extract_usage_from_response(response_data)
            self.cost_tracker.add_usage(**usage)

        # Fire post_llm_call hook
        hooks.emit(EVENT_POST_LLM_CALL, response=response_data)

        return response_data

    def _stream_response(
        self,
        payload: dict,
        callback: Callable[[str], None] | None = None,
        reasoning_callback: Callable[[str], None] | None = None,
    ) -> dict:
        """Stream a response from the API (delegates to the provider layer)."""
        return stream_response(
            self.client,
            payload,
            callback,
            reasoning_callback,
            interrupt_check=getattr(self, "_interrupt_check", None),
        )

    @staticmethod
    def _is_transient_error(error_msg: str) -> bool:
        """Check if an error is transient (retryable) vs permanent."""
        error_lower = error_msg.lower()
        transient_keywords = {
            "timeout",
            "timed out",
            "connection",
            "reset",
            "refused",
            "temporarily unavailable",
            "too many requests",
            "rate limit",
            "502",
            "503",
            "504",
            "connection error",
            "deadline",
        }
        return any(kw in error_lower for kw in transient_keywords)

    def _execute_tool_call(self, tool_call: dict) -> str:
        function = tool_call.get("function", {})
        name = function.get("name", "")
        call_id = tool_call.get("id", "") or str(uuid.uuid4())
        raw_args = function.get("arguments", "{}")
        try:
            parsed_args = json.loads(raw_args)
            args = (
                self._apply_workspace_defaults(name, parsed_args)
                if isinstance(parsed_args, dict)
                else {}
            )
        except (TypeError, json.JSONDecodeError):
            args = {}
        collector = self._active_trace
        trace = None
        permission = None
        if collector is not None:
            trace = collector.start_tool(call_id, name, redact(args))
            entry = registry.get_tool(name)
            if entry is not None:
                permission = self.permission_checker.evaluate(
                    name,
                    is_read_only=entry.is_read_only,
                    file_path=self._permission_path(args),
                    command=args.get("command"),
                )
                collector.policy(
                    trace,
                    allowed=permission.allowed,
                    confirmation_required=permission.requires_confirmation,
                    reason=permission.reason,
                )
        try:
            result = self._execute_tool_call_impl(tool_call)
            verification = None
            entry = registry.get_tool(name)
            if entry is not None and entry.verifier is not None and isinstance(args, dict):
                try:
                    verification = entry.verifier(args, result, agent=self)
                except Exception as exc:
                    verification = VerificationResult("inconclusive", reason=type(exc).__name__)
            if verification is None and isinstance(result, str) and result.startswith("Error:"):
                verification = VerificationResult("failed", reason="tool returned an error")
            outcome: Literal["completed", "failed", "denied"] = (
                "denied"
                if permission is not None
                and not permission.allowed
                or (isinstance(result, str) and "requires confirmation" in result)
                else ("failed" if result.startswith("Error:") else "completed")
            )
            if trace is not None and collector is not None:
                collector.finish_tool(
                    trace, outcome=outcome, result=redact(result), verification=verification
                )
            if verification is not None:
                self.observability.verification(
                    name,
                    status=verification.status,
                    evidence=verification.evidence,
                    reason=verification.reason,
                )
            return result
        except BaseException as exc:
            if trace is not None and collector is not None:
                collector.finish_tool(trace, outcome="failed", result=type(exc).__name__)
            raise

    def _execute_tool_call_impl(self, tool_call: dict) -> str:
        """Execute a single tool call and return the result.

        Automatically retries transient errors (timeout, network) but not permanent ones.
        """
        function = tool_call.get("function", {})
        name = function.get("name", "")
        arguments_str = function.get("arguments", "{}")

        try:
            arguments = json.loads(arguments_str)
        except json.JSONDecodeError:
            self.observability.tool(name, output_data="invalid_json")
            return f"Error: Invalid JSON arguments: {redact(arguments_str)}"

        if not isinstance(arguments, dict):
            self.observability.tool(name, output_data="invalid_arguments")
            return "Error: Tool arguments must be an object"
        arguments = self._apply_workspace_defaults(name, arguments)

        # Permission check
        if self._interrupt_check is not None and self._interrupt_check():
            return "[Interrupted by user before tool execution]"
        entry = registry.get_tool(name)
        mcp_tool = self._mcp_tool_info(name)
        is_mcp_resource = name == self._mcp_resource_tool_name
        if entry is None and mcp_tool is None and not is_mcp_resource:
            self.observability.tool(name, input_data=arguments, output_data="unknown_tool")
            return f"Error: Unknown tool: {name}"
        is_read_only = entry.is_read_only if entry else is_mcp_resource

        # Resolve the path-bearing argument used by this tool before policy.
        file_path = self._permission_path(arguments)
        command = arguments.get("command")

        perm_result = self.permission_checker.evaluate(
            name,
            is_read_only=is_read_only,
            file_path=file_path,
            command=command,
        )
        self.observability.policy(name, allowed=perm_result.allowed, reason=perm_result.reason)

        if not perm_result.allowed:
            logger.warning("Tool call denied: %s — %s", name, perm_result.reason)
            self.observability.tool(name, input_data=arguments, output_data="denied")
            return f"Error: {perm_result.reason}"

        if perm_result.requires_confirmation:
            approved = bool(
                self._confirmation_callback and self._confirmation_callback(name, arguments)
            )
            if not approved:
                logger.info("Tool '%s' denied because confirmation was not granted", name)
                self.observability.tool(
                    name, input_data=arguments, output_data="confirmation_denied"
                )
                return f"Error: Tool '{name}' requires confirmation"

        if self._interrupt_check is not None and self._interrupt_check():
            return "[Interrupted by user before tool execution]"

        # Fire pre_tool_call hook (also fired in registry.dispatch, but we fire here
        # first so the permission check happens before the hook)
        hooks.emit(EVENT_PRE_TOOL_CALL, tool_name=name, args=arguments)

        # Execute with automatic retry on transient errors
        max_retries = max(0, self.config.get("agent", {}).get("tool_retry_max_attempts", 2))
        result = ""
        for attempt in range(max_retries + 1):
            if self._interrupt_check is not None and self._interrupt_check():
                return "[Interrupted by user before tool execution]"
            # Pass config, wiki, and agent reference to tool handlers via kwargs
            try:
                if mcp_tool is not None:
                    with self._mcp_call_lock:
                        if self._interrupt_check is not None and self._interrupt_check():
                            return "[Interrupted by user before tool execution]"
                        result = self.mcp_client.call_tool(
                            mcp_tool.server_name, mcp_tool.name, arguments
                        )
                elif is_mcp_resource:
                    result = self._read_mcp_resource(arguments)
                else:
                    result = registry.dispatch(
                        name,
                        arguments,
                        config=self.config,
                        wiki=self.wiki,
                        session_store=self.session_store,
                        workspace=self.workspace,
                        agent=self,
                    )
                hooks.emit(EVENT_POST_TOOL_CALL, tool_name=name, args=arguments, result=result)
            except Exception as exc:
                result = f"Error: Tool '{name}' failed: {type(exc).__name__}"
                self.observability.tool(
                    name, input_data=arguments, output_data=result, retries=attempt
                )
                self.observability.verification(
                    name, status="failed", error_type=type(exc).__name__
                )
                return result

            # Retry on transient errors (timeout, network, rate-limit)
            is_transient = (
                is_read_only
                and isinstance(result, str)
                and result.startswith("Error:")
                and self._is_transient_error(result)
                and attempt < max_retries
            )
            if is_transient:
                wait_time = 2**attempt  # exponential backoff: 1s, 2s, 4s
                logger.warning(
                    "Tool %s failed with transient error (attempt %d/%d), retrying in %ds: %s",
                    name,
                    attempt + 1,
                    max_retries + 1,
                    wait_time,
                    result[:100],
                )
                time.sleep(wait_time)
                continue

            # Success or permanent error — return
            self.observability.tool(name, input_data=arguments, output_data=result, retries=attempt)
            self.observability.verification(
                name,
                status="failed" if result.startswith("Error:") else "inconclusive",
                result=result,
            )
            return result

        self.observability.tool(name, input_data=arguments, output_data=result, retries=max_retries)
        self.observability.verification(
            name,
            status="failed" if result.startswith("Error:") else "inconclusive",
            result=result,
        )
        return result

    def _read_mcp_resource(self, arguments: dict[str, Any]) -> str:
        server_name = arguments.get("server_name")
        uri = arguments.get("uri")
        if not isinstance(server_name, str) or not server_name:
            return "Error: server_name must be a non-empty string"
        if not isinstance(uri, str) or not uri:
            return "Error: uri must be a non-empty string"
        if not self.mcp_client.is_connected(server_name):
            return f"Error: MCP server '{server_name}' is not connected."
        with self._mcp_call_lock:
            return self.mcp_client.read_resource(server_name, uri)

    def _apply_workspace_defaults(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        arguments = dict(arguments)
        if name == "terminal" and not arguments.get("workdir"):
            arguments["workdir"] = str(self.workspace)
        elif name in {"read_file", "write_file", "patch_file"}:
            path = arguments.get("path")
            if isinstance(path, str) and path and not Path(path).expanduser().is_absolute():
                arguments["path"] = str(self.workspace / path)
        elif name == "search_files":
            path = arguments.get("path", ".")
            if isinstance(path, str) and not Path(path).expanduser().is_absolute():
                arguments["path"] = str(self.workspace / path)
        elif name == "list_files":
            root = arguments.get("root", ".")
            if isinstance(root, str) and not Path(root).expanduser().is_absolute():
                arguments["root"] = str(self.workspace / root)
        elif name.startswith("git_"):
            repo = arguments.get("repo", ".")
            if isinstance(repo, str) and not Path(repo).expanduser().is_absolute():
                arguments["repo"] = str(self.workspace / repo)
        return arguments

    @staticmethod
    def _permission_path(arguments: dict[str, Any]) -> str | None:
        """Return the path-like argument that must go through policy checks."""
        for key in ("path", "file_path", "root", "repo", "workdir"):
            value = arguments.get(key)
            if isinstance(value, str) and value:
                return value
        return None

    def _execute_tool_calls_parallel(
        self,
        tool_calls: list[dict],
    ) -> list[str]:
        """Execute tool calls, parallelizing independent ones.

        Tool calls are considered independent if they don't share data
        dependencies (i.e., none reads a file another writes). For safety,
        we parallelize only read-only tool calls; write/mutate tools run
        sequentially after the parallel batch.
        """
        read_only_calls: list[tuple[int, dict]] = []
        write_calls: list[tuple[int, dict]] = []

        for idx, tc in enumerate(tool_calls):
            fn_name = tc.get("function", {}).get("name", "")
            entry = registry.get_tool(fn_name)
            if (
                entry is not None and entry.is_read_only
            ) or fn_name == self._mcp_resource_tool_name:
                read_only_calls.append((idx, tc))
            else:
                write_calls.append((idx, tc))

        results: list[str | None] = [None] * len(tool_calls)

        for tc in tool_calls:
            self._report_tool_start(tc)

        # Execute read-only tools in parallel
        if read_only_calls:
            max_workers = min(len(read_only_calls), 4)
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_idx = {}
                for idx, tc in read_only_calls:
                    context = contextvars.copy_context()
                    future_to_idx[executor.submit(context.run, self._execute_tool_call, tc)] = idx

                for future in as_completed(future_to_idx):
                    idx = future_to_idx[future]
                    try:
                        results[idx] = future.result()
                    except Exception as e:
                        fn_name = tool_calls[idx].get("function", {}).get("name", "")
                        logger.error("Parallel tool call '%s' failed: %s", fn_name, e)
                        results[idx] = f"Error: Tool '{fn_name}' failed: {e}"

                    self._report_tool_result(tool_calls[idx], results[idx] or "")

                    # Report tool name to UI callback
                    tool_cb = getattr(self, "_tool_callback", None)
                    if tool_cb:
                        fn_name = tool_calls[idx].get("function", {}).get("name", "")
                        if fn_name:
                            tool_cb(fn_name)

        # Execute write/mutate tools sequentially
        for idx, tc in write_calls:
            interrupt_check = getattr(self, "_interrupt_check", None)
            if interrupt_check is not None and interrupt_check():
                interrupted_result = "[Interrupted by user before tool execution]"
                results[idx] = interrupted_result
                self._report_tool_result(tc, interrupted_result)
                continue
            tool_cb = getattr(self, "_tool_callback", None)
            if tool_cb:
                fn_name = tc.get("function", {}).get("name", "")
                if fn_name:
                    tool_cb(fn_name)
            try:
                results[idx] = self._execute_tool_call(tc)
            except Exception as exc:
                fn_name = tc.get("function", {}).get("name", "")
                logger.exception("Sequential tool call '%s' failed", fn_name)
                results[idx] = f"Error: Tool '{fn_name}' failed: {type(exc).__name__}"
            self._report_tool_result(tc, results[idx] or "")

        return [r if r is not None else "Error: Unexpected None result" for r in results]

    def _report_tool_start(self, tool_call: dict) -> None:
        call_id = tool_call.get("id", "")
        name = tool_call.get("function", {}).get("name", "")
        if call_id and name and self._tool_lifecycle_callback:
            self._tool_lifecycle_callback(call_id, name, "start", None)

    def _report_tool_result(self, tool_call: dict, result: str) -> None:
        call_id = tool_call.get("id", "")
        name = tool_call.get("function", {}).get("name", "")
        if call_id and name and self._tool_lifecycle_callback:
            status = (
                "failed"
                if result.startswith("Error:") or result.startswith("[Interrupted")
                else "completed"
            )
            self._tool_lifecycle_callback(call_id, name, status, result)

    def _estimate_messages_tokens_cached(self, messages: list[dict[str, Any]]) -> int:
        """Estimate message list tokens using a per-agent content cache.

        Messages that haven't changed since the last call are not re-encoded.
        Cache is bounded to 2048 entries to prevent unbounded growth.
        """

        from nova.tokens import estimate_tokens

        total = 0
        for msg in messages:
            content = msg.get("content", "")
            serialized = json.dumps(msg, ensure_ascii=False, sort_keys=True, default=str)
            key = hash(serialized)

            if key not in self._token_cache:
                if len(self._token_cache) >= 2048:
                    # Evict a random entry to stay bounded
                    self._token_cache.pop(next(iter(self._token_cache)))
                if isinstance(content, str):
                    subtotal = estimate_tokens(content)
                elif isinstance(content, list):
                    subtotal = 0
                    for part in content:
                        if isinstance(part, dict):
                            subtotal += estimate_tokens(part.get("text", "") or "")
                        elif isinstance(part, str):
                            subtotal += estimate_tokens(part)
                else:
                    subtotal = 0
                tool_calls = msg.get("tool_calls")
                if tool_calls:
                    subtotal += estimate_tokens(
                        json.dumps(tool_calls, ensure_ascii=False, default=str)
                    )
                self._token_cache[key] = subtotal
            total += self._token_cache[key] + 4  # +4 for message framing

        return total

    @staticmethod
    def _truncate_to_token_budget(text: str, max_tokens: int) -> str:
        """Truncate text to fit within a token budget.

        Uses head/tail truncation (70/20 ratio) to preserve beginning
        and end of the content.
        """
        from nova.tokens import estimate_tokens

        if max_tokens <= 0:
            return ""
        total_tokens = estimate_tokens(text)
        if total_tokens <= max_tokens:
            return text

        marker = f"\n\n[...{total_tokens - max_tokens:,} tokens truncated...]\n\n"
        if estimate_tokens(marker) >= max_tokens:
            marker = ""

        low = 0
        high = len(text)
        best = ""
        while low <= high:
            kept = (low + high) // 2
            head_chars = int(kept * 0.78)
            tail_chars = kept - head_chars
            tail = text[-tail_chars:] if tail_chars else ""
            candidate = f"{text[:head_chars]}{marker}{tail}"
            if estimate_tokens(candidate) <= max_tokens:
                best = candidate
                low = kept + 1
            else:
                high = kept - 1
        return best

    def run(
        self,
        user_message: str,
        stream: bool = True,
        stream_callback: Callable[[str], None] | None = None,
    ) -> str:
        run_id = str(uuid.uuid4())
        trace = HarnessTrace(run_id, user_message)
        self._active_trace = trace
        try:
            with self.observability.run(run_id, user_message, session_id=self.session_id):
                try:
                    result = self._run(user_message, stream=stream, stream_callback=stream_callback)
                    status = derive_run_status(trace.run, has_output=bool(result))
                    self.observability.finish_run(status=status, output=result)
                    self.last_run_trace = trace.finish(status=status, output=result)
                    return result
                except BaseException as exc:
                    self.observability.finish_run(status="inconclusive", error=exc)
                    self.last_run_trace = trace.finish(status="inconclusive")
                    raise
        finally:
            self._active_trace = None

    def _compact_if_needed(
        self,
        api_messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        force: bool = False,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Compact the request to fit the model window without an extra LLM call.

        Returns the (possibly compacted) ``(api_messages, conversation_messages)``
        pair. When ``force`` is set (e.g. after a provider overflow error) the
        target budget is halved to shed context aggressively.
        """
        total_tokens = estimate_total_request_tokens(api_messages, tools=tools)
        context_window = get_model_context_window(self.config["llm"]["model"])
        response_reserve = max(1024, int(self.config["llm"].get("max_tokens", 8192)))
        safety_margin = 1024
        active_budget = max(1, context_window - response_reserve - safety_margin)
        if force:
            active_budget = max(1, active_budget // 2)

        if total_tokens <= active_budget:
            return api_messages, self.messages

        microcompact_cfg = self.config.get("microcompact", {})
        keep_recent = int(microcompact_cfg.get("keep_recent", 6))
        conversation_budget = active_budget - estimate_total_request_tokens([], tools=tools)
        if conversation_budget < 1:
            raise ContextBudgetError("Tool definitions leave no room for conversation context")
        compacted = compact_to_token_budget(
            api_messages,
            max_tokens=conversation_budget,
            keep_recent=keep_recent,
            strip_tool_results=microcompact_cfg.get("enabled", True),
        )
        compacted = _normalize_message_history(compacted)
        compacted_tokens = estimate_total_request_tokens(compacted, tools=tools)
        if compacted_tokens > active_budget:
            raise ContextBudgetError(
                "System prompt and active conversation cannot fit the model context window"
            )
        if compacted_tokens < total_tokens:
            logger.info(
                "Deterministic compaction: %d → %d tokens (saved %d)",
                total_tokens,
                compacted_tokens,
                total_tokens - compacted_tokens,
            )
            conversation = self._conversation_messages_from_api(compacted)
            note_needed = not any(
                isinstance(m.get("content"), str)
                and m["content"].startswith(_COMPACTION_NOTE_PREFIX)
                for m in compacted
            )
            if note_needed:
                noted = [*compacted, _COMPACTION_NOTE]
                if estimate_total_request_tokens(noted, tools=tools) <= active_budget:
                    compacted = noted
            return compacted, conversation

        logger.warning(
            "Context remains above active budget: %d > %d tokens; "
            "historical retrieval may be needed",
            total_tokens,
            active_budget,
        )
        return api_messages, self.messages

    def _run(
        self,
        user_message: str,
        stream: bool = True,
        stream_callback: Callable[[str], None] | None = None,
    ) -> str:
        """Run a complete conversation turn.

        Returns the final assistant response.
        """
        # Add user message
        self.messages.append({"role": "user", "content": user_message})
        self._persist_message(self.session_id or "", "user", user_message)

        # Build messages for API
        self.messages = _normalize_message_history(self.messages)
        api_messages = [{"role": "system", "content": self._system_prompt or ""}]
        api_messages.extend(self.messages)

        # Get tool definitions
        tools = self._get_tool_definitions()

        # Main tool-calling loop
        max_iterations = self.config["agent"]["max_iterations"]
        iteration = 0
        # Optional interrupt hook — set by TUI via tui._interrupt_requested
        _interrupt_check: Callable[[], bool] | None = getattr(
            self,
            "_interrupt_check",
            None,
        )

        while iteration < max_iterations:
            if _interrupt_check is not None and _interrupt_check():
                logger.info("Agent interrupted before model request")
                return "[Interrupted]"
            iteration += 1

            # Keep requests below the model window without making another LLM call.
            api_messages, self.messages = self._compact_if_needed(api_messages, tools)

            # Call LLM. If the provider rejects the request as too long despite
            # our estimate, compact aggressively and retry once before failing.
            try:
                response = self._call_llm(
                    api_messages,
                    tools=tools,
                    stream=stream,
                    stream_callback=stream_callback,
                )
            except Exception as exc:
                if classify_error(message=str(exc)) != ErrorType.CONTEXT_OVERFLOW:
                    raise
                logger.warning("Provider reported context overflow; compacting and retrying once")
                api_messages, self.messages = self._compact_if_needed(
                    api_messages, tools, force=True
                )
                response = self._call_llm(
                    api_messages,
                    tools=tools,
                    stream=stream,
                    stream_callback=stream_callback,
                )

            choice = response.get("choices", [{}])[0]
            message = choice.get("message", {})
            content = message.get("content")
            tool_calls = message.get("tool_calls")
            reasoning_content = message.get("reasoning_content")
            finish_reason = choice.get("finish_reason")

            if finish_reason == "length" and tool_calls:
                logger.warning("Discarding tool calls from length-truncated response")
                tool_calls = None

            # Add assistant message to history.
            # When content arrives alongside tool_calls, drop the content from
            # the stored message — the streaming already showed it to the user.
            # Keeping it causes models (especially qwen) to repeat it verbatim
            # after the tool result is returned.
            # reasoning_content must be echoed back for DeepSeek thinking models.
            assistant_msg: dict[str, Any] = {"role": "assistant"}
            if content and not tool_calls:
                assistant_msg["content"] = content
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            if reasoning_content is not None:
                assistant_msg["reasoning_content"] = reasoning_content

            self.messages.append(assistant_msg)
            self._persist_message(
                self.session_id or "",
                "assistant",
                content or "",
                tool_calls=tool_calls,
                reasoning_content=reasoning_content,
            )
            api_messages.append(assistant_msg)

            # If no tool calls, we're done
            if not tool_calls:
                return content or ""

            # Check for interrupt between iterations (Ctrl+C)
            if _interrupt_check is not None and _interrupt_check():
                logger.info("Agent interrupted by user")
                for tool_call in tool_calls:
                    call_id = tool_call.get("id", "")
                    if not call_id:
                        continue
                    self._report_tool_start(tool_call)
                    if self._active_trace is not None:
                        interrupted_trace = self._active_trace.start_tool(
                            call_id,
                            tool_call.get("function", {}).get("name", ""),
                            redact(tool_call.get("function", {})),
                        )
                        self._active_trace.finish_tool(
                            interrupted_trace,
                            outcome="failed",
                            result="[Interrupted by user before tool execution]",
                            verification=VerificationResult("inconclusive", reason="interrupted"),
                        )
                    interrupted_result = {
                        "role": "tool",
                        "content": "[Interrupted by user before tool execution]",
                        "tool_call_id": call_id,
                    }
                    self.messages.append(interrupted_result)
                    self._persist_message(
                        self.session_id or "",
                        "tool",
                        interrupted_result["content"],
                        tool_call_id=call_id,
                    )
                    self._report_tool_result(tool_call, interrupted_result["content"])
                return "[Interrupted]"

            # Execute tool calls — parallelize independent calls
            tool_result_max_tokens = self.config["budgets"].get("tool_result_max_tokens", 3000)
            results = self._execute_tool_calls_parallel(tool_calls)

            for tool_call, result in zip(tool_calls, results, strict=True):
                call_id = tool_call.get("id", "")

                # Enforce per-result token budget
                result_tokens = estimate_tokens(result)
                if result_tokens > tool_result_max_tokens:
                    result = self._truncate_to_token_budget(result, tool_result_max_tokens)

                tool_result_msg = {
                    "role": "tool",
                    "content": result,
                    "tool_call_id": call_id,
                }
                self.messages.append(tool_result_msg)
                self._persist_message(
                    self.session_id or "",
                    "tool",
                    result,
                    tool_call_id=call_id,
                )
                api_messages.append(tool_result_msg)

        return f"[Max iterations ({max_iterations}) reached]"

    def _persist_message(self, session_id: str, role: str, content: str, **kwargs: Any) -> None:
        """Persist a message without making a transient DB outage kill the turn."""
        try:
            self.session_store.add_message(session_id, role, content, **kwargs)
        except sqlite3.Error as exc:
            logger.warning("Could not persist %s message: %s", role, type(exc).__name__)

    def _get_tool_definitions(self) -> list[dict[str, Any]]:
        """Return the tools available to this agent instance.

        The registry is process-global for backwards compatibility, so
        per-agent configuration gates must be applied when building the
        provider request rather than only during import-time registration.
        """
        definitions = registry.get_definitions(config=self.config)
        web_config = self.config.get("web", {})
        web_enabled = isinstance(web_config, dict) and web_config.get("enabled", True)
        has_web_key = isinstance(web_config, dict) and bool(web_config.get("firecrawl_api_key"))
        delegation = self.config.get("delegation", {})
        delegation_enabled = isinstance(delegation, dict) and delegation.get("enabled", False)
        depth = self.config.get("_subagent_depth", 0)
        max_depth = delegation.get("max_spawn_depth", 2) if isinstance(delegation, dict) else 2
        available: list[dict[str, Any]] = []
        for definition in definitions:
            name = definition.get("function", {}).get("name")
            if name and name.startswith("web_") and (not web_enabled or not has_web_key):
                continue
            if name == "delegate_task" and (not delegation_enabled or depth >= max_depth):
                continue
            available.append(definition)
        for name, tool in self._mcp_tools.items():
            schema = tool.input_schema if isinstance(tool.input_schema, dict) else {}
            available.append(
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": tool.description or f"Call MCP tool {tool.name}",
                        "parameters": schema,
                    },
                }
            )
        if self.mcp_client.list_resources() or self.mcp_client.connected_servers:
            available.append(
                {
                    "type": "function",
                    "function": {
                        "name": self._mcp_resource_tool_name,
                        "description": "Read an MCP resource by server name and URI.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "server_name": {"type": "string"},
                                "uri": {"type": "string"},
                            },
                            "required": ["server_name", "uri"],
                            "additionalProperties": False,
                        },
                    },
                }
            )
        return available
