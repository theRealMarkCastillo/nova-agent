"""LLM provider layer — client construction and OpenAI-compatible adapters.

All provider knowledge lives here so the agent loop stays provider-agnostic.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from openai import OpenAI

logger = logging.getLogger(__name__)


def build_client(llm_config: dict) -> Any:
    """Construct the OpenAI-compatible SDK client for the configured endpoint."""
    return OpenAI(
        api_key=llm_config["api_key"],
        base_url=llm_config["base_url"],
        timeout=120.0,
        max_retries=0,
    )


def completion_request_kwargs(payload: dict[str, Any], stream: bool = False) -> dict[str, Any]:
    request_kwargs: dict[str, Any] = {
        "model": payload["model"],
        "messages": payload["messages"],
        "temperature": payload.get("temperature", 0.7),
        "top_p": payload.get("top_p", 1.0),
    }
    if payload.get("max_tokens"):
        request_kwargs["max_tokens"] = payload["max_tokens"]
    if payload.get("tools"):
        request_kwargs["tools"] = payload["tools"]
        request_kwargs["tool_choice"] = "auto"
    if "extra_body" in payload:
        request_kwargs["extra_body"] = payload["extra_body"]
    if stream:
        request_kwargs["stream"] = True
        if payload.get("stream_include_usage", True):
            request_kwargs["stream_options"] = {"include_usage": True}
    return request_kwargs


def chat_completion(client: Any, payload: dict[str, Any]) -> dict:
    """Non-streaming OpenAI-compatible completion, normalized to agent shape."""
    request_kwargs = completion_request_kwargs(payload)
    resp = client.chat.completions.create(**request_kwargs)  # type: ignore[call-overload]
    message = resp.choices[0].message
    return {
        "choices": [
            {
                "finish_reason": (
                    resp.choices[0].finish_reason
                    if isinstance(resp.choices[0].finish_reason, str)
                    else None
                ),
                "message": {
                    "role": "assistant",
                    "content": message.content,
                    "tool_calls": (
                        [tc.model_dump() for tc in message.tool_calls]
                        if message.tool_calls
                        else None
                    ),
                    "reasoning_content": (message.model_extra or {}).get("reasoning_content"),
                },
            }
        ],
        "usage": resp.usage.model_dump() if resp.usage else None,
    }


def stream_response(
    client: Any,
    payload: dict,
    callback: Callable[[str], None] | None = None,
    reasoning_callback: Callable[[str], None] | None = None,
    interrupt_check: Callable[[], bool] | None = None,
) -> dict:
    """Stream a response from an OpenAI-compatible endpoint."""
    full_content = ""
    full_reasoning = ""
    tool_calls: list[dict[str, Any]] = []
    interrupted = False
    finish_reason: str | None = None

    request_kwargs = completion_request_kwargs(payload, stream=True)

    with client.chat.completions.create(**request_kwargs) as stream:  # type: ignore[call-overload]
        usage: dict[str, Any] | None = None
        for chunk in stream:
            # Most providers send usage on a trailing choice-less chunk; some
            # attach it to the final chunk that still carries a choice.
            chunk_usage = getattr(chunk, "usage", None)
            if chunk_usage is not None and hasattr(chunk_usage, "model_dump"):
                dumped_usage = chunk_usage.model_dump()
                if isinstance(dumped_usage, dict):
                    usage = dumped_usage
            if not chunk.choices:
                continue
            chunk_finish_reason = chunk.choices[0].finish_reason
            if isinstance(chunk_finish_reason, str):
                finish_reason = chunk_finish_reason
            delta = chunk.choices[0].delta

            if interrupt_check is not None and interrupt_check():
                logger.info("Stream interrupted by user")
                interrupted = True
                break

            if delta.content:
                full_content += delta.content
                if callback:
                    callback(delta.content)

            if delta.tool_calls:
                for tc in delta.tool_calls:
                    index = tc.index
                    while index >= len(tool_calls):
                        tool_calls.append(
                            {
                                "id": "",
                                "type": "function",
                                "function": {"name": "", "arguments": ""},
                            }
                        )
                    if tc.id:
                        tool_calls[index]["id"] = tc.id
                    if tc.function:
                        if tc.function.name:
                            tool_calls[index]["function"]["name"] = tc.function.name
                        if tc.function.arguments:
                            tool_calls[index]["function"]["arguments"] += tc.function.arguments

            extra = delta.model_extra or {}
            reasoning_chunks: list[str] = []
            for key in ("reasoning", "reasoning_content"):
                value = extra.get(key)
                if isinstance(value, str):
                    reasoning_chunks.append(value)
            details = extra.get("reasoning_details")
            if not reasoning_chunks and isinstance(details, list):
                for detail in details:
                    if isinstance(detail, dict):
                        value = detail.get("text") or detail.get("content")
                        if isinstance(value, str):
                            reasoning_chunks.append(value)
            for reasoning in reasoning_chunks:
                full_reasoning += reasoning
                if reasoning_callback:
                    reasoning_callback(reasoning)

    reasoning_content_value: str | None = full_reasoning if full_reasoning else None
    if not reasoning_content_value and tool_calls:
        reasoning_content_value = ""

    return {
        "choices": [
            {
                "finish_reason": finish_reason,
                "message": {
                    "role": "assistant",
                    "content": full_content if full_content else None,
                    "tool_calls": None if interrupted else (tool_calls if tool_calls else None),
                    "reasoning_content": reasoning_content_value,
                },
            }
        ],
        "usage": usage,
    }
