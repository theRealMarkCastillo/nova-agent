"""Tests for token estimation utilities."""

from unittest.mock import patch

import pytest

from nova import tokens
from nova.tokens import estimate_messages_tokens, estimate_tokens


def test_estimate_tokens_empty():
    assert estimate_tokens("") == 0


def test_estimate_tokens_basic():
    # "hello world" should be 2 tokens with tiktoken
    tokens = estimate_tokens("hello world")
    assert tokens > 0


def test_estimate_messages_tokens():
    messages = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there!"},
    ]
    tokens = estimate_messages_tokens(messages)
    assert tokens > 0


def test_estimate_tokens_multimodal():
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "What's in this image?"},
                {"type": "image_url", "image_url": {"url": "https://example.com/img.jpg"}},
            ],
        }
    ]
    tokens = estimate_messages_tokens(messages)
    assert tokens > 0


def test_reasoning_content_is_counted():
    plain = {"role": "assistant", "content": "answer"}
    with_reasoning = {**plain, "reasoning_content": "step " * 200}
    assert estimate_messages_tokens([with_reasoning]) >= estimate_messages_tokens([plain]) + 150


def test_repeated_estimates_do_not_re_encode():
    text = "unique text for the encoder cache test " * 50
    messages = [{"role": "user", "content": text}]
    encoder = tokens._get_encoder()
    if encoder is None:
        pytest.skip("tiktoken unavailable")
    with patch.object(tokens, "_get_encoder", wraps=tokens._get_encoder) as get_encoder:
        first = estimate_messages_tokens(messages)
        second = estimate_messages_tokens(messages)
    assert first == second
    assert get_encoder.call_count == 1
