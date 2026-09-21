"""Tests for the microcompact module."""

import copy
import json

from nova.microcompact import (
    _extract_exit_code,
    compact_to_token_budget,
    estimate_savings,
    microcompact_messages,
)
from nova.tokens import estimate_messages_tokens

# ── _extract_exit_code ─────────────────────────────────────────────────────


def test_extract_exit_code_success():
    content = "exit code: 0\nsome output"
    assert _extract_exit_code(content) == 0


def test_extract_exit_code_failure():
    content = "exit code: 1\nerror message"
    assert _extract_exit_code(content) == 1


def test_extract_exit_code_no_exit_code():
    content = "just some output"
    assert _extract_exit_code(content) is None


def test_extract_exit_code_malformed():
    content = "exit code: abc\noutput"
    assert _extract_exit_code(content) is None


# ── microcompact_messages ──────────────────────────────────────────────────


def test_microcompact_short_message_list():
    messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]
    result = microcompact_messages(messages, keep_recent=6)
    assert len(result) == 2
    assert result == messages


def test_microcompact_strips_old_tool_results():
    messages = [
        {"role": "user", "content": "run ls"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {"name": "terminal", "arguments": '{"command": "ls"}'},
                }
            ],
        },
        {
            "role": "tool",
            "content": "exit code: 0\nfile1.txt\nfile2.txt\n" + "x" * 1000,
            "tool_call_id": "1",
        },
        {"role": "user", "content": "now run pwd"},
        {"role": "assistant", "content": "ok"},
        {"role": "tool", "content": "/home/user", "tool_call_id": "2"},
    ]
    result = microcompact_messages(messages, keep_recent=2)

    # First tool result should be stripped
    assert result[2]["role"] == "tool"
    assert "stripped" in result[2]["content"]
    assert "exit code 0" in result[2]["content"]

    # Last messages should be preserved
    assert result[-1]["content"] == "/home/user"


def test_microcompact_preserves_assistant_structure():
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {"name": "terminal", "arguments": '{"command": "ls"}'},
                },
            ],
        },
        {"role": "tool", "content": "lots of output" * 100, "tool_call_id": "1"},
        {"role": "user", "content": "done"},
    ]
    result = microcompact_messages(messages, keep_recent=1)

    # Assistant message tool_calls should have truncated arguments
    assert result[0]["role"] == "assistant"
    assert result[0]["tool_calls"][0]["function"]["arguments"] == "{}"


def test_microcompact_keep_recent_parameter():
    messages = [
        {"role": "tool", "content": "old1"},
        {"role": "tool", "content": "old2"},
        {"role": "tool", "content": "old3"},
        {"role": "tool", "content": "recent1"},
        {"role": "tool", "content": "recent2"},
    ]
    result = microcompact_messages(messages, keep_recent=2)

    # First 3 should be stripped
    assert "stripped" in result[0]["content"]
    assert "stripped" in result[1]["content"]
    assert "stripped" in result[2]["content"]

    # Last 2 should be preserved
    assert result[3]["content"] == "recent1"
    assert result[4]["content"] == "recent2"


def test_microcompact_non_tool_messages_untouched():
    messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
        {"role": "user", "content": "how are you"},
    ]
    result = microcompact_messages(messages, keep_recent=1)

    # User and assistant messages should not be stripped
    assert result[0]["content"] == "hello"
    assert result[1]["content"] == "hi there"


# ── estimate_savings ───────────────────────────────────────────────────────


def test_estimate_savings():
    messages = [
        {"role": "tool", "content": "x" * 5000, "tool_call_id": "1"},
        {"role": "tool", "content": "y" * 5000, "tool_call_id": "2"},
        {"role": "user", "content": "hello"},
    ]
    compacted = microcompact_messages(messages, keep_recent=1)
    savings = estimate_savings(messages, compacted)

    assert savings["original_tokens"] > 0
    assert savings["compacted_tokens"] > 0
    assert savings["saved_tokens"] > 0
    assert savings["compacted_tokens"] < savings["original_tokens"]


def test_compact_single_turn_tool_result_to_budget():
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "read it"},
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "1", "content": "data " * 5000},
    ]

    result = compact_to_token_budget(messages, max_tokens=1000)

    assert estimate_messages_tokens(result) <= 1000
    assert result[2]["tool_calls"][0]["id"] == result[3]["tool_call_id"]


def test_compact_large_recent_tool_call_preserves_required_messages():
    messages = [
        {"role": "system", "content": "system instructions"},
        {"role": "user", "content": "keep this request"},
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {
                        "name": "write_file",
                        "arguments": json.dumps({"content": "data " * 5000}),
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "1", "content": "completed"},
    ]

    result = compact_to_token_budget(messages, max_tokens=1000)

    assert estimate_messages_tokens(result) <= 1000
    assert result[0]["content"] == "system instructions"
    assert result[1]["content"] == "keep this request"
    assert result[2]["tool_calls"][0]["function"]["arguments"] == "{}"


def test_compact_does_not_mutate_input_messages():
    messages = [
        {"role": "user", "content": "request"},
        {"role": "tool", "tool_call_id": "1", "content": "large output " * 1000},
    ]
    original = copy.deepcopy(messages)

    compact_to_token_budget(messages, max_tokens=10, strip_tool_results=False)

    assert messages == original
