"""Main agent loop.

Handles the conversation loop with tool calling, streaming,
deterministic context management, and session management.
"""

from __future__ import annotations

import copy
import logging
import re
import sqlite3
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from nova.config import ensure_nova_home, load_config, set_model
from nova.cost_tracker import CostTracker, extract_usage_from_response
from nova.harness import HarnessTrace, derive_run_status
from nova.hooks import (
    EVENT_POST_LLM_CALL,
    EVENT_PRE_LLM_CALL,
    EVENT_SESSION_START,
    hooks,
)
from nova.mcp_client import McpToolInfo, build_mcp_client
from nova.microcompact import compact_to_token_budget
from nova.model_metadata import get_model_context_window, load_provider_metadata
from nova.observability import create_observability
from nova.permissions import (
    PermissionChecker,
    build_permission_checker,
    has_untrusted_output,
)
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
from nova.tool_executor import MCP_RESOURCE_TOOL, ToolExecutor
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

# Limits how far one response's usage can move the budget, so a provider that
# reports odd counts cannot shrink or inflate the usable window drastically.
_TOKEN_CALIBRATION_BOUNDS = (0.8, 2.0)


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
        # An injected MCP client (e.g. a parent agent's) belongs to its caller.
        self._owns_mcp_client = mcp_client is None
        self.mcp_client = mcp_client if mcp_client is not None else build_mcp_client(self.config)
        self._mcp_tools: dict[str, McpToolInfo] = {}
        self.last_run_trace: Any = None
        self._active_trace: HarnessTrace | None = None
        # Provider tokens per locally estimated token, from the last response.
        self._token_calibration = 1.0
        self.tool_executor = ToolExecutor(self)

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
        try:
            load_provider_metadata(self.client)

            # Discover tools (pass config so delegation tool can be gated)
            # Must happen before _create_session so system prompt includes tool summaries
            discover_builtin_tools(self.config)
            self.mcp_client.connect_all()
            self._refresh_mcp_tools()

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
        if getattr(self, "_owns_mcp_client", False):
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
                set_model(self.config, info["model"])
            # Load recent messages only — respect conversation turn limit
            turn_limit = self.config["budgets"].get("conversation_turn_limit", 15)
            self.messages = self.session_store.get_messages(
                self.session_id,
                limit=turn_limit * 4,  # ~4 msgs per turn (user+assistant+tool pairs)
            )
            self.messages = _normalize_message_history(self.messages)
            self.tool_executor.untrusted_content_seen = any(
                has_untrusted_output(call.get("function", {}).get("name", ""))
                for message in self.messages
                for call in message.get("tool_calls") or []
            )
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
            tool_names={
                definition["function"]["name"] for definition in self._builtin_tool_definitions()
            },
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

        usage = extract_usage_from_response(response_data)
        self.observability.llm(
            llm_config["model"],
            input_data=messages,
            output_data=response_data,
            usage=usage,
        )
        reported_usage = response_data.get("usage")
        if isinstance(reported_usage, dict):
            self._calibrate_token_estimate(messages, tools, reported_usage.get("prompt_tokens"))

        # Track cost from response
        if self.cost_tracker:
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
    def _truncate_to_token_budget(text: str, max_tokens: int) -> str:
        """Truncate text to fit within a token budget.

        Keeps roughly 78% of the retained characters from the head and 22%
        from the tail, around a truncation marker.
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

    def _usable_context_tokens(self) -> int:
        """Provider tokens available for the request after reserving the reply."""
        context_window = get_model_context_window(
            self.config["llm"]["model"],
            override=self.config["llm"].get("context_window") or None,
        )
        response_reserve = max(1024, int(self.config["llm"].get("max_tokens", 8192)))
        safety_margin = 1024
        return max(1, context_window - response_reserve - safety_margin)

    def _calibrate_token_estimate(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict] | None,
        prompt_tokens: Any,
    ) -> None:
        """Scale local estimates by the provider's own count of the last request.

        The local tokenizer (cl100k) can differ from the model's by tens of
        percent; the provider's reported prompt tokens are exact.
        """
        if type(prompt_tokens) is not int or prompt_tokens <= 0:
            return
        estimated = estimate_total_request_tokens(messages, tools=tools)
        if estimated <= 0:
            return
        low, high = _TOKEN_CALIBRATION_BOUNDS
        self._token_calibration = min(high, max(low, prompt_tokens / estimated))

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
        active_budget = self._usable_context_tokens()
        if force:
            active_budget = max(1, active_budget // 2)
        # The budget is in provider tokens; estimates are local tokenizer counts.
        active_budget = max(1, int(active_budget / self._token_calibration))

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
                answerable = [call for call in tool_calls if call.get("id")]
                skipped = self.tool_executor.skip_batch(answerable)
                for tool_call, result in zip(answerable, skipped, strict=True):
                    self._append_tool_result(tool_call["id"], result.content)
                return "[Interrupted]"

            # Execute tool calls — parallelize independent calls
            tool_result_max_tokens = self.config["budgets"].get("tool_result_max_tokens", 3000)
            results = self.tool_executor.run_batch(tool_calls)

            for tool_call, result in zip(tool_calls, results, strict=True):
                content = result.content
                # Enforce per-result token budget
                if estimate_tokens(content) > tool_result_max_tokens:
                    content = self._truncate_to_token_budget(content, tool_result_max_tokens)
                api_messages.append(self._append_tool_result(tool_call.get("id", ""), content))

        return f"[Max iterations ({max_iterations}) reached]"

    def _append_tool_result(self, call_id: str, content: str) -> dict[str, Any]:
        message = {"role": "tool", "content": content, "tool_call_id": call_id}
        self.messages.append(message)
        self._persist_message(self.session_id or "", "tool", content, tool_call_id=call_id)
        return message

    def _persist_message(self, session_id: str, role: str, content: str, **kwargs: Any) -> None:
        """Persist a message without making a transient DB outage kill the turn."""
        try:
            self.session_store.add_message(session_id, role, content, **kwargs)
        except sqlite3.Error as exc:
            logger.warning("Could not persist %s message: %s", role, type(exc).__name__)

    def _builtin_tool_definitions(self) -> list[dict[str, Any]]:
        """Return this agent's registry tools after per-agent config gates.

        The registry is process-global, so gates are applied here rather than
        only at registration. Both the prompt summary and the provider request
        use this list so they always agree.
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
        return available

    def _get_tool_definitions(self) -> list[dict[str, Any]]:
        """Return the tools available to this agent instance."""
        available = self._builtin_tool_definitions()
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
                        "name": MCP_RESOURCE_TOOL,
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
