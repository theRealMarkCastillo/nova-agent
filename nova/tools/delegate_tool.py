"""Delegation tool — spawn sub-agents to handle tasks.

Allows an orchestrator agent to delegate tasks to child agents that run
in worker threads with explicit budgets and hard timeouts.

Design principles:
- Isolated context by default (fresh conversation per sub-agent)
- Depth-based role system (orchestrator vs. leaf)
- Explicit token/iteration budgets at every layer
- Thread-safe execution via ThreadPoolExecutor
- Hard timeout enforcement (max 300s)
"""

import json
import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from threading import Event
from typing import Any

from openai import OpenAI

from nova.tools.registry import registry

logger = logging.getLogger(__name__)

# Maximum allowed timeout for any sub-agent
MAX_TIMEOUT_SECONDS = 300
DEFAULT_TIMEOUT_SECONDS = 60
DEFAULT_MAX_ITERATIONS = 30


DELEGATE_TASK_SCHEMA = {
    "name": "delegate_task",
    "description": (
        "Spawn a sub-agent to handle a specific task. "
        "Use for tasks that can be isolated, parallelized, or require focused execution. "
        "The sub-agent has access to all tools except delegate_task (if at depth limit). "
        "Returns a JSON result with success status, output, and budget usage."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "Clear description of the task for the sub-agent to complete.",
            },
            "label": {
                "type": "string",
                "description": "Optional short label for logging/display (e.g. 'lint check').",
            },
            "model": {
                "type": "string",
                "description": (
                    "Optional model override (e.g. 'openai/gpt-4o-mini' for cheaper tasks). "
                    "Defaults to parent's model."
                ),
            },
            "timeout_seconds": {
                "type": "integer",
                "description": f"Timeout in seconds (default {DEFAULT_TIMEOUT_SECONDS}, max {MAX_TIMEOUT_SECONDS}).",
            },
            "context_mode": {
                "type": "string",
                "enum": ["isolated", "fork"],
                "description": (
                    "Context mode: 'isolated' (fresh conversation, default) or "
                    "'fork' (inherit parent transcript for context-aware tasks)."
                ),
            },
        },
        "required": ["task"],
    },
}


def _build_subagent_config(
    parent_config: dict,
    depth: int,
    model: str | None,
    max_iterations: int,
) -> dict:
    """Build a config dict for the sub-agent, inheriting from parent."""
    import copy

    config = copy.deepcopy(parent_config)

    # Override model if specified
    if model:
        config["llm"]["model"] = model

    # Set sub-agent depth
    config["_subagent_depth"] = depth

    # Apply sub-agent budget overrides from delegation config.
    # Config value wins; max_iterations argument is the fallback default.
    delegation = config.get("delegation", {})
    subagent_budgets = delegation.get("subagent_budgets", {})

    config["agent"]["max_iterations"] = subagent_budgets.get("max_iterations", max_iterations)

    if "system_prompt_max" in subagent_budgets:
        config["budgets"]["system_prompt_max"] = subagent_budgets["system_prompt_max"]
    if "context_total_max_chars" in subagent_budgets:
        config["budgets"]["context_total_max_chars"] = subagent_budgets["context_total_max_chars"]
    if "tool_result_max_chars" in subagent_budgets:
        config["budgets"]["tool_result_max_chars"] = subagent_budgets["tool_result_max_chars"]
    if "tool_result_max_tokens" in subagent_budgets:
        config["budgets"]["tool_result_max_tokens"] = subagent_budgets["tool_result_max_tokens"]

    return config


def _extract_cost_data(subagent: Any | None) -> dict:
    """Extract cost tracking data from sub-agent if available."""
    if subagent is None:
        return {}
    cost_tracker = getattr(subagent, "cost_tracker", None)
    if not cost_tracker:
        return {}
    total_usage = cost_tracker.total
    return {
        "input_tokens": total_usage.input_tokens,
        "output_tokens": total_usage.output_tokens,
        "input_cost": total_usage.input_cost,
        "output_cost": total_usage.output_cost,
    }


def _run_subagent(
    task: str,
    parent_agent: Any,
    label: str,
    model: str | None,
    timeout_seconds: int,
    context_mode: str,
    cancel_event: Event | None = None,
) -> dict:
    """Core sub-agent execution logic (runs in worker thread)."""
    # Import here to avoid circular imports at module level
    from nova.agent import NovaAgent

    depth = getattr(parent_agent, "depth", 0) + 1
    task_id = str(uuid.uuid4())[:8]
    log_prefix = f"[subagent:{label}:{task_id}]"

    logger.info("%s spawning at depth=%d, timeout=%ds", log_prefix, depth, timeout_seconds)
    start_time = time.monotonic()
    subagent = None  # initialize before try so except block can safely reference it

    # Build sub-agent config
    delegation_cfg = parent_agent.config.get("delegation", {})
    max_iterations = delegation_cfg.get("subagent_budgets", {}).get(
        "max_iterations",
        DEFAULT_MAX_ITERATIONS,
    )
    subagent_config = _build_subagent_config(
        parent_config=parent_agent.config,
        depth=depth,
        model=model,
        max_iterations=max_iterations,
    )

    # Build initial messages based on context mode
    if context_mode == "fork" and parent_agent.messages:
        # Inherit parent transcript — sub-agent has full context
        prefill_messages = list(parent_agent.messages)
        logger.debug("%s using fork context (%d messages)", log_prefix, len(prefill_messages))
    else:
        # Fresh conversation — sub-agent starts clean
        prefill_messages = []
        logger.debug("%s using isolated context", log_prefix)

    try:
        llm_cfg = subagent_config["llm"]
        subagent_openai_client = OpenAI(
            api_key=llm_cfg["api_key"],
            base_url=llm_cfg["base_url"],
            timeout=120.0,
            max_retries=0,
        )
        with subagent_openai_client:
            subagent = NovaAgent(
                config=subagent_config,
                openai_client=subagent_openai_client,
                session_store=parent_agent.session_store,
                wiki_memory_store=parent_agent.wiki,
                prompt_mode="minimal",
                confirmation_callback=getattr(parent_agent, "_confirmation_callback", None),
                workspace=parent_agent.workspace,
            )

            # Inject prefill messages if forking
            if prefill_messages:
                subagent.messages = prefill_messages

            if cancel_event is not None:
                deadline = start_time + timeout_seconds
                subagent._interrupt_check = lambda: (
                    cancel_event.is_set() or time.monotonic() >= deadline
                )
            result = subagent.run(task, stream=False)

        elapsed = time.monotonic() - start_time
        # Count messages to estimate iterations (each tool round = 2 messages: assistant + tool)
        tool_msgs = sum(1 for m in subagent.messages if m.get("role") == "tool")
        logger.info(
            "%s completed in %.1fs, ~%d tool calls",
            log_prefix,
            elapsed,
            tool_msgs,
        )

        usage_data = _extract_cost_data(subagent)

        return {
            "success": True,
            "result": result,
            "label": label,
            "depth": depth,
            "elapsed_seconds": round(elapsed, 1),
            "usage": usage_data,
            "error": None,
            "timeout": False,
        }

    except Exception as e:
        elapsed = time.monotonic() - start_time
        logger.error("%s failed after %.1fs: %s", log_prefix, elapsed, e)

        usage_data = _extract_cost_data(subagent)

        return {
            "success": False,
            "result": None,
            "label": label,
            "depth": depth,
            "elapsed_seconds": round(elapsed, 1),
            "usage": usage_data,
            "error": str(e),
            "timeout": False,
        }
    finally:
        if subagent is not None:
            try:
                subagent.close()
            except Exception:
                logger.exception("%s failed to close", log_prefix)


def _delegate_task(args: dict[str, Any], **kwargs) -> str:
    """Handler for the delegate_task tool."""
    agent = kwargs.get("agent")
    if agent is None:
        return json.dumps({"success": False, "error": "No agent context available."})

    task = args.get("task", "").strip()
    if not task:
        return json.dumps({"success": False, "error": "Task description is required."})

    label = args.get("label") or task[:40].replace("\n", " ")
    model = args.get("model")
    context_mode = args.get("context_mode", "isolated")
    # Read default timeout from config, fall back to module constant
    config_default_timeout = agent.config.get("delegation", {}).get(
        "default_timeout_seconds",
        DEFAULT_TIMEOUT_SECONDS,
    )
    try:
        timeout_seconds = max(
            1, min(int(args.get("timeout_seconds", config_default_timeout)), MAX_TIMEOUT_SECONDS)
        )
    except (TypeError, ValueError):
        timeout_seconds = max(1, min(int(config_default_timeout), MAX_TIMEOUT_SECONDS))

    # Validate context_mode
    if context_mode not in ("isolated", "fork"):
        context_mode = "isolated"

    # Check depth limit
    depth = getattr(agent, "depth", 0)
    max_spawn_depth = agent.config.get("delegation", {}).get("max_spawn_depth", 2)
    if depth >= max_spawn_depth:
        return json.dumps(
            {
                "success": False,
                "error": (
                    f"Cannot spawn sub-agent: already at max depth ({depth}/{max_spawn_depth}). "
                    "This agent is a leaf and cannot delegate further."
                ),
            }
        )

    # Older programmatic callers may omit the flag; an explicit false value
    # must still disable a dispatch that was already registered.
    if agent.config.get("delegation", {}).get("enabled", True) is False:
        return json.dumps({"success": False, "error": "Delegation is disabled."})

    # Run sub-agent in worker thread with hard timeout
    executor_context = ThreadPoolExecutor(max_workers=1)
    executor = executor_context.__enter__()
    cancel_event = Event()
    try:
        future = executor.submit(
            _run_subagent,
            task=task,
            parent_agent=agent,
            label=label,
            model=model,
            timeout_seconds=timeout_seconds,
            context_mode=context_mode,
            cancel_event=cancel_event,
        )
        try:
            result = future.result(timeout=timeout_seconds)
            # Aggregate costs into parent agent
            usage = result.pop("usage", {})
            if usage and getattr(agent, "cost_tracker", None):
                agent.cost_tracker.add_usage(**usage)
        except FuturesTimeoutError:
            cancel_event.set()
            logger.warning("Sub-agent '%s' timed out after %ds", label, timeout_seconds)
            result = {
                "success": False,
                "result": None,
                "label": label,
                "depth": depth + 1,
                "elapsed_seconds": timeout_seconds,
                "error": f"Sub-agent timed out after {timeout_seconds}s.",
                "timeout": True,
            }
    finally:
        # A timed-out child may be cooperative, but never make the caller wait
        # for it. Its finally block still closes the child when it exits.
        executor.shutdown(wait=False, cancel_futures=True)

    return json.dumps(result, indent=2)


def _is_delegation_enabled(config: dict | None = None) -> bool:
    """Check if delegation is enabled in config."""
    if config is None:
        return False
    return config.get("delegation", {}).get("enabled", False)


def register_delegate_tool(agent_config: dict | None = None) -> None:
    """Register the delegate_task tool if delegation is enabled and agent is not a leaf.

    Gating rules (both must be true):
    - delegation.enabled is True in config
    - _subagent_depth < delegation.max_spawn_depth (not a leaf agent)

    Called from discover_builtin_tools() with the agent's config.
    """
    if not _is_delegation_enabled(agent_config):
        logger.debug("Delegation disabled — skipping delegate_task registration")
        return

    cfg = agent_config or {}
    depth = cfg.get("_subagent_depth", 0)
    max_depth = cfg.get("delegation", {}).get("max_spawn_depth", 2)
    if depth >= max_depth:
        logger.debug(
            "Agent at depth %d >= max_spawn_depth %d — skipping delegate_task (leaf agent)",
            depth,
            max_depth,
        )
        return

    registry.register(
        name="delegate_task",
        toolset="delegation",
        schema=DELEGATE_TASK_SCHEMA,
        handler=_delegate_task,
        emoji="🤖",
    )
    logger.debug("Registered tool: delegate_task")
