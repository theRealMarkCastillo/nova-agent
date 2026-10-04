"""Slash command handler registry.

Provides a decorator-based pattern for registering slash command handlers.
Handlers are extracted from the agent's chat_loop into this module for
clean separation of concerns.

Usage:
    @command_handler("status", aliases=("st",))
    def cmd_status(agent: NovaAgent, args: str) -> None:
        ...
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from nova.config import set_model

if TYPE_CHECKING:
    from nova.agent import NovaAgent

logger = logging.getLogger(__name__)

# Type alias for command handlers
CommandHandler = Callable[["NovaAgent", str], None]

# Registry: canonical name -> handler function
_HANDLERS: dict[str, CommandHandler] = {}


def command_handler(
    name: str, aliases: tuple[str, ...] = ()
) -> Callable[[CommandHandler], CommandHandler]:
    """Decorator to register a slash command handler.

    Args:
        name: Canonical command name (without leading /).
        aliases: Tuple of alternative names.

    Example:
        @command_handler("status", aliases=("st",))
        def cmd_status(agent: NovaAgent, args: str) -> None:
            ...
    """

    def decorator(func: CommandHandler) -> CommandHandler:
        _HANDLERS[name] = func
        for alias in aliases:
            _HANDLERS[alias] = func
        logger.debug("Registered command handler: %s (aliases: %s)", name, aliases)
        return func

    return decorator


def dispatch_command(name: str, agent: NovaAgent, args: str) -> bool:
    """Dispatch a slash command to its registered handler.

    Args:
        name: Command name (without leading /).
        agent: The NovaAgent instance.
        args: Raw argument string after the command name.

    Returns:
        True if a handler was found and executed, False otherwise.
    """
    handler = _HANDLERS.get(name.lower())
    if handler is None:
        skill_match = _resolve_skill(name, agent.config)
        if skill_match is not None:
            _handle_skill(agent, skill_match)
            return True
        return False
    try:
        handler(agent, args)
        return True
    except Exception as e:
        logger.error("Command '%s' failed: %s", name, e)
        from nova.display import _DIM, _RST, _cprint

        _cprint(f"{_DIM}Command error: {e}{_RST}")
        return True


def get_registered_commands() -> set[str]:
    """Return the set of canonical command names (excludes aliases)."""
    # Import here to avoid circular dependency
    from nova.commands import COMMAND_REGISTRY

    return {cmd.name for cmd in COMMAND_REGISTRY}


def get_skill_names(config: dict) -> set[str]:
    """Get all available skill names from the configured skills directory."""
    from pathlib import Path

    from nova.skills import discover_skills

    skills_dir = Path(config.get("skills", {}).get("directory", "~/.nova/skills")).expanduser()
    skills = discover_skills(skills_dir)
    return {skill["name"] for skill in skills}


def _resolve_skill(name: str, config: dict) -> str | None:
    """Match a slash-command name to a skill, case-insensitive. Returns the canonical name."""
    target = name.lower()
    for skill_name in get_skill_names(config):
        if skill_name.lower() == target:
            return skill_name
    return None


# ─── Built-in command handlers ───────────────────────────────────────────────


@command_handler("new", aliases=("reset",))
def cmd_new(agent: NovaAgent, args: str) -> None:
    from nova.display import _DIM, _RST, _cprint

    agent._create_session()
    agent.messages = []
    _cprint(f"{_DIM}New session started{_RST}")


@command_handler("history")
def cmd_history(agent: NovaAgent, args: str) -> None:
    from nova.display import print_message_history

    print_message_history(agent.messages)


@command_handler("status", aliases=("st",))
def cmd_status(agent: NovaAgent, args: str) -> None:
    from nova.display import _DIM, _RST, _cprint
    from nova.tokens import estimate_total_request_tokens

    ctx = estimate_total_request_tokens(
        agent.messages,
        system_prompt=agent._system_prompt or "",
    )
    _cprint(f"{_DIM}Session: {agent.session_id}")
    _cprint(f"Model:   {agent.config['llm']['model']}")
    _cprint(f"Context: {ctx:,} tokens")
    # Delegation state
    delegation_cfg = agent.config.get("delegation", {})
    if delegation_cfg.get("enabled"):
        max_depth = delegation_cfg.get("max_spawn_depth", 2)
        role = "leaf" if agent.is_leaf_agent else "orchestrator"
        _cprint(f"Delegation: enabled  depth={agent.depth}/{max_depth}  role={role}")
    else:
        _cprint("Delegation: disabled")
    _cprint(f"Messages: {len(agent.messages)}{_RST}")


@command_handler("sessions")
def cmd_sessions(agent: NovaAgent, args: str) -> None:
    from nova.display import _DIM, _RST, _cprint

    sessions = agent.session_store.list_sessions(limit=10)
    if not sessions:
        _cprint(f"{_DIM}No sessions found{_RST}")
    for s in sessions:
        _cprint(f"{_DIM}{s.get('session_id', '')}  {s.get('created_at', '')}{_RST}")


@command_handler("model")
def cmd_model(agent: NovaAgent, args: str) -> None:
    from nova.display import _DIM, _RST, _cprint

    if args:
        set_model(agent.config, args.strip())
        _cprint(f"{_DIM}Model switched to: {args.strip()}{_RST}")
    else:
        _cprint(f"{_DIM}Current model: {agent.config['llm']['model']}{_RST}")


@command_handler("config")
def cmd_config(agent: NovaAgent, args: str) -> None:
    from nova.display import _DIM, _RST, _cprint
    from nova.observability import redact

    _cprint(f"{_DIM}{json.dumps(redact(agent.config), indent=2, default=str)}{_RST}")


@command_handler("reasoning")
def cmd_reasoning(agent: NovaAgent, args: str) -> None:
    from nova.display import _DIM, _RST, _cprint

    value = args.strip().lower()
    if value in {"show", "hide"}:
        agent.config.setdefault("ui", {})["show_reasoning"] = value == "show"
    current = agent.config.get("ui", {}).get("show_reasoning", True)
    _cprint(f"{_DIM}Reasoning display: {'shown' if current else 'hidden'}{_RST}")


@command_handler("retry")
def cmd_retry(agent: NovaAgent, args: str) -> None:
    from nova.display import _DIM, _RST, _cprint

    last_user = next(
        (
            message.get("content", "")
            for message in reversed(agent.messages)
            if message.get("role") == "user"
        ),
        "",
    )
    if not last_user:
        _cprint(f"{_DIM}Nothing to retry{_RST}")
        return
    last_user_index = max(
        i for i, message in enumerate(agent.messages) if message.get("role") == "user"
    )
    agent.messages = agent.messages[:last_user_index]
    if agent.session_id:
        agent.session_store.replace_messages(agent.session_id, agent.messages)
    callback = getattr(agent, "_stream_callback", None)
    response = agent.run(last_user, stream=True, stream_callback=callback)
    if callback is None and response:
        _cprint(response)


@command_handler("resume")
def cmd_resume(agent: NovaAgent, args: str) -> None:
    from nova.display import _DIM, _RST, _cprint

    # Session listings include metadata after the ID; accept a copied row as
    # well as the bare ID.
    session_id = args.strip().split(maxsplit=1)[0] if args.strip() else ""
    if not session_id:
        _cprint(f"{_DIM}Usage: /resume <session-id>{_RST}")
        return
    info = agent.session_store.get_session_info(session_id)
    if info is None:
        _cprint(f"{_DIM}Session not found: {session_id}{_RST}")
        return
    agent.session_id = session_id
    agent._load_session()
    _cprint(f"{_DIM}Resumed session: {session_id}{_RST}")
    from nova.display import print_message_history

    print_message_history(agent.messages)


@command_handler("title")
def cmd_title(agent: NovaAgent, args: str) -> None:
    from nova.display import _DIM, _RST, _cprint

    title = args.strip()
    if not title:
        if not agent.session_id:
            _cprint(f"{_DIM}Title: (untitled){_RST}")
            return
        info = agent.session_store.get_session_info(agent.session_id) or {}
        _cprint(f"{_DIM}Title: {info.get('title') or '(untitled)'}{_RST}")
        return
    if not agent.session_id:
        _cprint(f"{_DIM}Cannot set a title without an active session{_RST}")
        return
    agent.session_store.update_title(agent.session_id, title)
    _cprint(f"{_DIM}Session title updated{_RST}")


@command_handler("tools")
def cmd_tools(agent: NovaAgent, args: str) -> None:
    from nova.display import _CYAN, _DIM, _RST, _cprint
    from nova.tools.registry import registry

    tools = registry.get_definitions()
    _cprint(f"{_DIM}Available tools ({len(tools)}):{_RST}")
    for t in tools:
        _cprint(
            f"  {_CYAN}{t['function']['name']}{_RST}{_DIM}  —  {t['function'].get('description', '')[:60]}{_RST}"
        )


@command_handler("skills")
def cmd_skills(agent: NovaAgent, args: str) -> None:
    from pathlib import Path

    from nova.display import _CYAN, _DIM, _RST, _cprint
    from nova.skills import discover_skills

    if args.strip() not in {"", "list"}:
        _cprint(f"{_DIM}Usage: /skills [list]{_RST}")
        return

    skills_dir = Path(
        agent.config.get("skills", {}).get("directory", "~/.nova/skills")
    ).expanduser()
    skills = discover_skills(skills_dir)

    if not skills:
        _cprint(f"{_DIM}No skills found. Create skills in ~/.nova/skills/.{_RST}")
        return

    _cprint(f"{_DIM}Available skills ({len(skills)}):{_RST}")

    by_category: dict[str, list[dict]] = {}
    for skill in skills:
        by_category.setdefault(skill["category"], []).append(skill)

    for category in sorted(by_category.keys()):
        _cprint(f"{_CYAN}{category}{_RST}")
        for skill in by_category[category]:
            desc = skill["description"]
            if len(desc) > 60:
                desc = desc[:60] + "…"
            _cprint(f"  {_DIM}/{skill['name']:<20} — {desc}{_RST}")


@command_handler("usage")
def cmd_usage(agent: NovaAgent, args: str) -> None:
    from nova.display import _DIM, _RST, _cprint
    from nova.model_metadata import get_model_context_window
    from nova.tokens import estimate_total_request_tokens

    ctx = estimate_total_request_tokens(
        agent.messages,
        system_prompt=agent._system_prompt or "",
    )
    cw = get_model_context_window(
        agent.config["llm"]["model"],
        override=agent.config.get("llm", {}).get("context_window") or None,
    )
    pct = int(ctx / cw * 100) if cw else 0
    _cprint(f"{_DIM}Context used: {ctx:,} / {cw:,} tokens ({pct}%){_RST}")
    if agent.cost_tracker:
        _cprint(f"{_DIM}{agent.cost_tracker.format_summary()}{_RST}")


@command_handler("undo")
def cmd_undo(agent: NovaAgent, args: str) -> None:
    from nova.display import _DIM, _RST, _cprint

    # Remove everything back to (and including) the last user message so the
    # remaining history never strands tool results without their calls.
    last_user = max(
        (i for i, msg in enumerate(agent.messages) if msg.get("role") == "user"),
        default=-1,
    )
    if 0 <= last_user < len(agent.messages) - 1:
        agent.messages = agent.messages[:last_user]
        if agent.session_id:
            agent.session_store.replace_messages(agent.session_id, agent.messages)
        _cprint(f"{_DIM}Last exchange removed{_RST}")
    else:
        _cprint(f"{_DIM}Nothing to undo{_RST}")


@command_handler("compact")
def cmd_compact(agent: NovaAgent, args: str) -> None:
    from nova.agent import _normalize_message_history
    from nova.display import _DIM, _RST, _cprint
    from nova.microcompact import compact_to_token_budget
    from nova.model_metadata import get_model_context_window
    from nova.tokens import estimate_messages_tokens, estimate_total_request_tokens

    _cprint(f"{_DIM}Reducing active context…{_RST}")
    api_messages = [{"role": "system", "content": agent._system_prompt or ""}]
    api_messages.extend(agent.messages)
    tools = agent._get_tool_definitions()
    context_window = get_model_context_window(
        agent.config["llm"]["model"],
        override=agent.config.get("llm", {}).get("context_window") or None,
    )
    reserve = max(1024, int(agent.config["llm"].get("max_tokens", 8192))) + 1024
    budget = max(1, context_window - reserve)
    tool_tokens = estimate_total_request_tokens([], tools=tools)
    keep_recent = int(agent.config.get("microcompact", {}).get("keep_recent", 6))
    forced_target = estimate_messages_tokens([*api_messages[:1], *api_messages[-keep_recent:]])
    compacted = compact_to_token_budget(
        api_messages,
        max_tokens=min(max(1, budget - tool_tokens), forced_target),
        keep_recent=keep_recent,
    )
    agent.messages = _normalize_message_history(agent._conversation_messages_from_api(compacted))
    if agent.session_id:
        agent.session_store.replace_messages(agent.session_id, agent.messages)
    _cprint(f"{_DIM}Context compacted to {len(agent.messages)} messages{_RST}")


@command_handler("copy")
def cmd_copy(agent: NovaAgent, args: str) -> None:
    import subprocess

    from nova.display import _DIM, _RST, _cprint

    # Find last assistant message
    for msg in reversed(agent.messages):
        if msg.get("role") == "assistant" and msg.get("content"):
            subprocess.run(
                ["pbcopy"],
                input=msg["content"].encode(),
                check=False,
            )
            _cprint(f"{_DIM}Copied to clipboard{_RST}")
            return
    _cprint(f"{_DIM}No response to copy{_RST}")


def _handle_skill(agent: NovaAgent, skill_name: str) -> None:
    """Load and display a skill via slash command."""
    from pathlib import Path

    from nova.display import _CYAN, _DIM, _RST, _cprint
    from nova.skills import load_skill_content

    skills_dir = Path(
        agent.config.get("skills", {}).get("directory", "~/.nova/skills")
    ).expanduser()
    skill_dir_path = skills_dir / skill_name
    skill_path = skill_dir_path / "SKILL.md"

    content = load_skill_content(str(skill_path), skill_dir=skill_dir_path)
    if content is None:
        _cprint(f"{_DIM}Error: Skill '{skill_name}' not found.{_RST}")
        return

    divider = "─" * 60
    _cprint(f"\n{_CYAN}{divider}")
    _cprint(f"Skill: {skill_name}")
    _cprint(f"{divider}{_RST}\n")
    _cprint(content)
    _cprint(f"\n{_CYAN}{divider}{_RST}\n")
