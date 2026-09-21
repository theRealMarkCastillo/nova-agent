"""Microcompact — cheap context window reduction without LLM calls.

Strips old tool result content while preserving message structure,
reducing token count without losing conversation flow.

This is Tier 1 of a multi-tier compaction strategy:
1. Microcompact (this module) — strip old tool content, no LLM call
2. Context collapse — remove oversized context elements
3. Full LLM summarization — summarize older messages (future)
"""

from __future__ import annotations

import copy
import logging
from typing import Any

logger = logging.getLogger(__name__)

# How many recent messages to preserve fully (default: last 6 messages)
_DEFAULT_KEEP_RECENT = 6


def microcompact_messages(
    messages: list[dict[str, Any]],
    keep_recent: int = _DEFAULT_KEEP_RECENT,
) -> list[dict[str, Any]]:
    """Strip old tool result content while preserving message structure.

    This reduces token count by replacing old tool result content with
    a short summary placeholder, while keeping the message structure
    intact so the model understands the conversation flow.

    Args:
        messages: Full message list (system + conversation).
        keep_recent: Number of recent messages to preserve fully.

    Returns:
        New message list with old tool content stripped.
    """
    if len(messages) <= keep_recent:
        return copy.deepcopy(messages)

    result = []
    split_point = len(messages) - keep_recent

    for i, msg in enumerate(messages):
        role = msg.get("role", "")
        new_msg = copy.deepcopy(msg)  # shallow copy (safe: tool_calls replaced entirely)

        if i < split_point and role == "tool":
            # Strip old tool content — keep just a placeholder
            original = msg.get("content", "")
            # Extract exit code if present (useful context)
            exit_code = _extract_exit_code(original)
            if exit_code is not None:
                new_msg["content"] = f"[tool result: exit code {exit_code}, content stripped]"
            else:
                new_msg["content"] = "[tool result stripped]"
        elif i < split_point and role == "assistant":
            # Keep assistant message but strip tool_calls content
            # (the tool call structure is preserved, arguments truncated)
            if "tool_calls" in new_msg and new_msg["tool_calls"]:
                truncated_calls = []
                for tc in new_msg["tool_calls"]:
                    truncated_calls.append(
                        {
                            "id": tc.get("id", ""),
                            "type": tc.get("type", "function"),
                            "function": {
                                "name": tc.get("function", {}).get("name", ""),
                                "arguments": "{}",  # Strip arguments to save tokens
                            },
                        }
                    )
                new_msg["tool_calls"] = truncated_calls

        result.append(new_msg)

    stripped_count = sum(
        1 for i, m in enumerate(messages) if i < split_point and m.get("role") == "tool"
    )
    if stripped_count > 0:
        logger.info(
            "Microcompact: stripped %d old tool results (kept last %d messages)",
            stripped_count,
            keep_recent,
        )

    return result


def _extract_exit_code(content: str) -> int | None:
    """Extract exit code from terminal tool output if present."""
    if content.startswith("exit code: "):
        try:
            first_line = content.split("\n")[0]
            return int(first_line.split(":")[1].strip())
        except (ValueError, IndexError):
            pass
    return None


def estimate_savings(
    original: list[dict[str, Any]],
    compacted: list[dict[str, Any]],
) -> dict[str, int]:
    """Estimate token savings from microcompaction.

    Returns dict with original_tokens, compacted_tokens, saved_tokens.
    """
    from nova.tokens import estimate_messages_tokens

    original_tokens = estimate_messages_tokens(original)
    compacted_tokens = estimate_messages_tokens(compacted)

    return {
        "original_tokens": original_tokens,
        "compacted_tokens": compacted_tokens,
        "saved_tokens": original_tokens - compacted_tokens,
    }


def compact_to_token_budget(
    messages: list[dict[str, Any]],
    max_tokens: int,
    keep_recent: int = _DEFAULT_KEEP_RECENT,
    strip_tool_results: bool = True,
) -> list[dict[str, Any]]:
    """Compact tool output and remove oldest complete turns to fit a budget."""
    from nova.tokens import estimate_messages_tokens

    result = (
        microcompact_messages(messages, keep_recent=keep_recent)
        if strip_tool_results
        else copy.deepcopy(messages)
    )
    while estimate_messages_tokens(result) > max_tokens:
        user_indexes = [
            index for index, message in enumerate(result) if message.get("role") == "user"
        ]
        if len(user_indexes) < 2:
            break

        # Remove one complete turn, ending immediately before the next user turn.
        start = user_indexes[0]
        end = user_indexes[1]
        del result[start:end]

    if estimate_messages_tokens(result) <= max_tokens:
        return result

    for message in result:
        if message.get("role") != "tool" or not message.get("content"):
            continue
        message["content"] = "[tool result stripped to fit context budget]"
        if estimate_messages_tokens(result) <= max_tokens:
            return result

    for message in result:
        if message.get("role") != "assistant":
            continue
        calls = message.get("tool_calls")
        if not isinstance(calls, list):
            continue
        for call in calls:
            function = call.get("function")
            if isinstance(function, dict):
                function["arguments"] = "{}"
        if estimate_messages_tokens(result) <= max_tokens:
            return result

    return result
