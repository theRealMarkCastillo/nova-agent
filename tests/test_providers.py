"""Tests for the LLM provider layer (nova/providers.py)."""

from unittest.mock import MagicMock

import pytest
from openai import OpenAI
from openai.types.completion_usage import CompletionUsage

from nova.cost_tracker import extract_usage_from_response
from nova.providers import (
    build_client,
    chat_completion,
    completion_request_kwargs,
    stream_response,
)


def test_build_client_openai_compatible(minimal_config):
    client = build_client(minimal_config["llm"])
    assert isinstance(client, OpenAI)
    assert "openrouter.ai/api/v1" in str(client.base_url)


def test_completion_request_kwargs_plain(minimal_config):
    payload = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "Hi"}],
        "temperature": 0.7,
        "top_p": 1.0,
        "stream_include_usage": True,
    }
    kwargs = completion_request_kwargs(payload)
    assert kwargs["model"] == "test-model"
    assert "tools" not in kwargs
    assert "stream" not in kwargs


def test_completion_request_kwargs_forwards_max_tokens():
    payload = {
        "model": "test-model",
        "messages": [],
        "max_tokens": 4096,
    }
    kwargs = completion_request_kwargs(payload)
    assert kwargs["max_tokens"] == 4096


def test_completion_request_kwargs_omits_zero_max_tokens():
    payload = {"model": "test-model", "messages": [], "max_tokens": 0}
    kwargs = completion_request_kwargs(payload)
    assert "max_tokens" not in kwargs


def test_completion_request_kwargs_stream_and_tools():
    payload = {
        "model": "test-model",
        "messages": [],
        "temperature": 0.7,
        "top_p": 1.0,
        "stream_include_usage": False,
        "tools": [{"type": "function"}],
        "extra_body": {"reasoning": {"enabled": True}},
    }
    kwargs = completion_request_kwargs(payload, stream=True)
    assert kwargs["stream"] is True
    assert kwargs["tool_choice"] == "auto"
    assert "stream_options" not in kwargs
    assert kwargs["extra_body"] == {"reasoning": {"enabled": True}}


def test_chat_completion_normalizes_response(minimal_config, mock_openai_client):
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].finish_reason = "stop"
    msg = resp.choices[0].message
    msg.content = "Hello"
    msg.tool_calls = None
    msg.model_extra = {}
    resp.usage.model_dump.return_value = {"prompt_tokens": 1, "completion_tokens": 1}
    mock_openai_client.chat.completions.create.return_value = resp

    result = chat_completion(
        mock_openai_client,
        {"model": "test-model", "messages": [], "temperature": 0.7, "top_p": 1.0},
    )

    assert result["choices"][0]["message"]["content"] == "Hello"
    assert result["usage"] == {"prompt_tokens": 1, "completion_tokens": 1}


def make_mock_stream(*chunks):
    """Create a mock stream context manager that yields the given chunks."""

    class MockStream:
        def __iter__(self):
            return iter(chunks)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    return MockStream()


def make_text_chunk(content: str) -> MagicMock:
    chunk = MagicMock()
    chunk.choices = [MagicMock()]
    chunk.choices[0].delta.content = content
    chunk.choices[0].delta.tool_calls = None
    chunk.choices[0].delta.model_extra = {}
    return chunk


def make_usage_chunk(usage: dict) -> MagicMock:
    chunk = MagicMock()
    chunk.choices = []
    chunk.usage.model_dump.return_value = usage
    return chunk


def test_stream_response_accumulates_text(mock_openai_client):
    mock_openai_client.chat.completions.create.return_value = make_mock_stream(
        make_text_chunk("Hello"), make_text_chunk(" world")
    )
    received = []
    result = stream_response(
        mock_openai_client,
        {"model": "test-model", "messages": [], "temperature": 0.7, "top_p": 1.0},
        callback=received.append,
    )
    assert received == ["Hello", " world"]
    assert result["choices"][0]["message"]["content"] == "Hello world"


def test_stream_response_interrupt_stops_early(mock_openai_client):
    mock_openai_client.chat.completions.create.return_value = make_mock_stream(
        make_text_chunk("Hello"), make_text_chunk(" world")
    )
    result = stream_response(
        mock_openai_client,
        {"model": "test-model", "messages": [], "temperature": 0.7, "top_p": 1.0},
        interrupt_check=lambda: True,
    )
    assert result["choices"][0]["message"]["content"] is None


def test_stream_response_captures_usage(mock_openai_client):
    mock_openai_client.chat.completions.create.return_value = make_mock_stream(
        make_text_chunk("answer"),
        make_usage_chunk({"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16}),
    )
    result = stream_response(
        mock_openai_client,
        {"model": "test-model", "messages": [], "temperature": 0.7, "top_p": 1.0},
    )
    assert result["usage"]["prompt_tokens"] == 12
    call_kwargs = mock_openai_client.chat.completions.create.call_args.kwargs
    assert call_kwargs["stream_options"] == {"include_usage": True}


def test_stream_response_empty_stream(mock_openai_client):
    mock_openai_client.chat.completions.create.return_value = make_mock_stream()
    result = stream_response(
        mock_openai_client,
        {"model": "test-model", "messages": [], "temperature": 0.7, "top_p": 1.0},
    )
    assert result["choices"][0]["message"]["content"] is None


@pytest.mark.parametrize("streaming", [False, True])
def test_provider_preserves_nested_cache_usage(mock_openai_client, streaming):
    usage = CompletionUsage(
        prompt_tokens=100,
        completion_tokens=5,
        total_tokens=105,
        prompt_tokens_details={"cached_tokens": 90},
    )
    payload = {"model": "test-model", "messages": []}
    if streaming:
        final_chunk = MagicMock(choices=[], usage=usage)
        mock_openai_client.chat.completions.create.return_value = make_mock_stream(
            make_text_chunk("Hello"), final_chunk
        )
        response = stream_response(mock_openai_client, payload)
    else:
        result = MagicMock(usage=usage)
        result.choices = [MagicMock()]
        result.choices[0].message.tool_calls = None
        mock_openai_client.chat.completions.create.return_value = result
        response = chat_completion(mock_openai_client, payload)
    extracted = extract_usage_from_response(response)
    assert extracted["input_tokens"] == 100
    assert extracted["cache_read_tokens"] == 90
    assert extracted["output_tokens"] == 5
