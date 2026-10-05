"""Token estimation utilities.

Uses tiktoken for accurate token counting when available,
falls back to character-based estimation.
"""

import functools
import json
import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)

# Rough chars-per-token estimate for fallback
_CHARS_PER_TOKEN = 4

# Module-level encoder cache — initialised once, reused for every estimate call
_encoder = None
_encoder_lock = threading.Lock()


def _get_encoder():
    global _encoder
    if _encoder is None:
        with _encoder_lock:
            if _encoder is None:
                try:
                    import tiktoken

                    _encoder = tiktoken.get_encoding("cl100k_base")
                except Exception:
                    pass
    return _encoder


def estimate_tokens(text: str) -> int:
    """Estimate token count for a string.

    Uses tiktoken (cl100k_base) when available, falls back to
    character-based estimation.
    """
    if not text:
        return 0
    return _estimate_tokens_cached(text)


# The agent re-estimates the whole request on every loop iteration. Message
# contents and serialized tool schemas repeat across iterations, and str
# caches its hash, so repeats cost a lookup instead of a full encode.
@functools.lru_cache(maxsize=1024)
def _estimate_tokens_cached(text: str) -> int:
    enc = _get_encoder()
    if enc is not None:
        try:
            return len(enc.encode(text))
        except Exception:
            pass
    return len(text) // _CHARS_PER_TOKEN


def estimate_message_tokens(msg: dict[str, Any]) -> int:
    """Estimate tokens for one message, including framing overhead."""
    total = 4  # role + content framing
    content = msg.get("content", "")
    if isinstance(content, str):
        total += estimate_tokens(content)
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                total += estimate_tokens(part.get("text", "") or "")
            elif isinstance(part, str):
                total += estimate_tokens(part)
    tool_calls = msg.get("tool_calls")
    if tool_calls:
        total += estimate_tokens(json.dumps(tool_calls, ensure_ascii=False, default=str))
    # Sent back to providers that require it (e.g. DeepSeek thinking models).
    reasoning = msg.get("reasoning_content")
    if isinstance(reasoning, str):
        total += estimate_tokens(reasoning)
    return total


def estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    """Estimate total tokens for a message list."""
    return sum(estimate_message_tokens(msg) for msg in messages)


def estimate_tool_tokens(tools: list[dict[str, Any]]) -> int:
    """Estimate tokens for tool schema definitions."""
    import json

    total = 0
    for tool in tools:
        total += estimate_tokens(json.dumps(tool, ensure_ascii=False))
    return total


def estimate_system_prompt_tokens(system_prompt: str) -> int:
    """Estimate tokens for the system prompt."""
    return estimate_tokens(system_prompt)


def estimate_total_request_tokens(
    messages: list[dict[str, Any]],
    system_prompt: str = "",
    tools: list[dict[str, Any]] | None = None,
) -> int:
    """Estimate total tokens for an API request."""
    total = estimate_tokens(system_prompt)
    total += estimate_messages_tokens(messages)
    if tools:
        total += estimate_tool_tokens(tools)
    return total
