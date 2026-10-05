"""Central tool registry.

Each tool file calls registry.register() at module level to declare its
schema, handler, and metadata. The agent queries the registry for tool
definitions and dispatches tool calls.
"""

import logging
from collections.abc import Callable
from typing import Any

from nova.observability import redact
from nova.tools.result import ToolResult

logger = logging.getLogger(__name__)


# Display ordering and labels for the grouped tool summary in the system prompt.
_TOOLSET_ORDER: tuple[str, ...] = (
    "file",
    "git",
    "http",
    "web",
    "sessions",
    "skills",
    "tasks",
    "terminal",
    "wiki",
    "delegation",
    "mcp",
)
_TOOLSET_LABELS: dict[str, str] = {
    "file": "Files",
    "git": "Git",
    "http": "HTTP",
    "web": "Web",
    "sessions": "Sessions",
    "skills": "Skills",
    "tasks": "Background Tasks",
    "terminal": "Terminal",
    "wiki": "Wiki",
    "delegation": "Delegation",
    "mcp": "MCP",
}


class ToolEntry:
    """Metadata for a single registered tool."""

    __slots__ = (
        "name",
        "toolset",
        "schema",
        "handler",
        "check_fn",
        "description",
        "emoji",
        "is_read_only",
        "verifier",
        "always_confirm",
    )

    def __init__(
        self,
        name: str,
        toolset: str,
        schema: dict,
        handler: Callable,
        check_fn: Callable | None = None,
        description: str = "",
        emoji: str = "🔧",
        is_read_only: bool = False,
        verifier: Callable | None = None,
        always_confirm: Callable[..., str | None] | None = None,
    ):
        self.name = name
        self.toolset = toolset
        self.schema = schema
        self.handler = handler
        self.check_fn = check_fn
        self.description = description
        self.emoji = emoji
        self.is_read_only = is_read_only
        self.verifier = verifier
        # Returns a reason when a call needs confirmation even in auto mode.
        self.always_confirm = always_confirm


class ToolRegistry:
    """Singleton registry for tool schemas and handlers."""

    def __init__(self):
        self._tools: dict[str, ToolEntry] = {}
        self._generation: int = 0

    def register(
        self,
        name: str,
        toolset: str,
        schema: dict,
        handler: Callable,
        check_fn: Callable | None = None,
        emoji: str = "🔧",
        is_read_only: bool = False,
        verifier: Callable | None = None,
        always_confirm: Callable[..., str | None] | None = None,
    ):
        """Register a tool."""
        existing = self._tools.get(name)
        if existing is not None:
            if existing.handler is not handler or existing.schema != schema:
                logger.error("Tool name collision for '%s'; keeping the first registration", name)
            return
        self._tools[name] = ToolEntry(
            name=name,
            toolset=toolset,
            schema=schema,
            handler=handler,
            check_fn=check_fn,
            description=schema.get("description", ""),
            emoji=emoji,
            is_read_only=is_read_only,
            verifier=verifier,
            always_confirm=always_confirm,
        )
        self._generation += 1
        logger.debug("Registered tool: %s", name)

    def get_definitions(
        self, tool_names: set | None = None, config: dict | None = None
    ) -> list[dict]:
        """Get tool schema definitions for API calls.

        Returns tools in OpenAI-compatible format:
        {"type": "function", "function": {schema}}

        If tool_names is provided, only return those tools.
        Otherwise return all registered tools.
        """
        tools = []
        for name, entry in self._tools.items():
            if tool_names is None or name in tool_names:
                if entry.check_fn is not None:
                    try:
                        if not entry.check_fn(config or {}):
                            continue
                    except Exception:
                        logger.exception("Tool availability check failed for '%s'", name)
                        continue
                tools.append(
                    {
                        "type": "function",
                        "function": entry.schema,
                    }
                )
        return tools

    def get_tool_summary_list(self, tool_names: set | None = None) -> str:
        """Get a compact, grouped bullet list of tool names + one-line descriptions.

        Tools are grouped under small per-toolset headers so the model can scan
        the surface area by domain. Used in the system prompt for efficient
        tokenization.
        """
        by_toolset: dict[str, list[str]] = {}
        for name, entry in sorted(self._tools.items()):
            if tool_names is not None and name not in tool_names:
                continue
            desc = entry.description.split("\n")[0][:100]
            by_toolset.setdefault(entry.toolset, []).append(f"- {name}: {desc}")

        lines: list[str] = []
        for toolset in _TOOLSET_ORDER:
            if toolset in by_toolset:
                lines.append(f"### {_TOOLSET_LABELS.get(toolset, toolset)}")
                lines.extend(sorted(by_toolset.pop(toolset)))
        for toolset in sorted(by_toolset):
            lines.append(f"### {_TOOLSET_LABELS.get(toolset, toolset)}")
            lines.extend(sorted(by_toolset[toolset]))
        return "\n".join(lines)

    def dispatch(self, name: str, args: dict, **kwargs) -> Any:
        """Dispatch a tool call by name.

        Fires pre_tool_call and post_tool_call hooks if registered.
        """
        entry = self._tools.get(name)
        if not entry:
            return f"Error: Unknown tool '{name}'"

        try:
            result = entry.handler(args, **kwargs)
            if isinstance(result, (str, ToolResult)):
                return result
            return str(result)
        except Exception as e:
            logger.exception("Tool %s failed", name)
            error_result = f"Error: Tool '{name}' failed: {redact(str(e))}"
            return error_result

    def get_tool(self, name: str) -> ToolEntry | None:
        """Get a tool entry by name, or None if not found."""
        return self._tools.get(name)

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def all_tool_names(self) -> set:
        return set(self._tools.keys())


# Global registry instance
registry = ToolRegistry()


def discover_builtin_tools(config: dict | None = None):
    """Import built-in tool modules to trigger registration.

    Args:
        config: Agent config dict, used to gate optional tools like delegate_task.
    """
    import importlib

    tool_modules = [
        "nova.tools.terminal",
        "nova.tools.file_ops",
        "nova.tools.search_files",
        "nova.tools.http_client",
        "nova.tools.file_list",
        "nova.tools.git_tool",
        "nova.tools.skills_tool",
        "nova.tools.wiki_tool",
        "nova.tools.search_sessions_tool",
        "nova.tools.task_tools",
    ]

    for mod_name in tool_modules:
        try:
            importlib.import_module(mod_name)
        except Exception as e:
            logger.warning("Could not import tool module %s: %s", mod_name, e)

    # Firecrawl web tools are gated on the SDK being installed and a key being
    # configured — their schemas cost ~1.7k tokens per request.
    try:
        from nova.tools.web import register_web_tools

        register_web_tools(config)
    except Exception as e:
        logger.warning("Could not register web tools: %s", e)

    # Delegation tool is gated on config flag and agent depth
    try:
        from nova.tools.delegate_tool import register_delegate_tool

        depth = (config or {}).get("_subagent_depth", 0)
        max_depth = (config or {}).get("delegation", {}).get("max_spawn_depth", 2)
        if depth < max_depth:
            register_delegate_tool(config)
    except Exception as e:
        logger.warning("Could not register delegate_task tool: %s", e)
