"""Tests for agent token management and truncation."""

import tempfile
from pathlib import Path

import pytest

from nova.agent import NovaAgent
from nova.session import SessionStore
from nova.tokens import estimate_tokens, estimate_total_request_tokens


@pytest.fixture
def minimal_config():
    """Minimal test config."""
    return {
        "llm": {
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "test-key",
            "model": "test-model",
        },
        "agent": {
            "max_iterations": 3,
            "temperature": 0.7,
            "top_p": 1.0,
            "identity": "You are a test agent.",
        },
        "budgets": {
            "conversation_turn_limit": 5,
            "tool_result_max_chars": 8000,
            "system_prompt_max": 8000,
        },
        "wiki": {"enabled": False},
        "session": {"directory": str(tempfile.mkdtemp())},
        "skills": {"enabled": False},
        "context_files": [],
    }


@pytest.fixture
def mock_session_store():
    """Real SessionStore for integration testing."""
    tmpdir = tempfile.mkdtemp()
    return SessionStore(Path(tmpdir) / "test.db")


def test_truncate_to_token_budget_exact_fit(minimal_config, mock_session_store):
    """Test truncation when content fits exactly."""
    text = "Hello world"
    max_tokens = 100

    # Should not truncate if it fits
    result = NovaAgent._truncate_to_token_budget(text, max_tokens)
    assert result is not None


def test_truncate_to_token_budget_head_only(minimal_config, mock_session_store):
    """Test truncation uses head when tail exceeds limit."""
    # Create a long text
    lines = [f"Line {i}: This is a line of content for testing truncation.\n" for i in range(100)]
    text = "".join(lines)

    # Truncate to small limit (head should dominate)
    max_tokens = 50
    result = NovaAgent._truncate_to_token_budget(text, max_tokens)

    assert result is not None
    assert len(result) < len(text)


def test_truncate_to_token_budget_tail_included(minimal_config, mock_session_store):
    """Test that tail is included in truncation."""
    lines = [f"Line {i}: Content\n" for i in range(100)]
    text = "".join(lines)

    max_tokens = 50
    result = NovaAgent._truncate_to_token_budget(text, max_tokens)

    # Should have both head and tail
    assert "Line 0" in result or "Content" in result
    assert "Line 99" in result or "Content" in result


def test_truncate_to_token_budget_preserves_head(minimal_config, mock_session_store):
    """Test that head content is preserved during truncation."""
    text = (
        "START: Important beginning\n"
        + "\n".join([f"Line {i}" for i in range(100)])
        + "\nEND: Important end"
    )

    max_tokens = 100
    result = NovaAgent._truncate_to_token_budget(text, max_tokens)

    # Head should be included
    assert "START" in result or "Important beginning" in result


@pytest.mark.parametrize("max_tokens", [1, 2, 10, 50])
def test_truncate_to_token_budget_never_exceeds_cap(max_tokens):
    result = NovaAgent._truncate_to_token_budget("x " * 2000, max_tokens)
    assert estimate_tokens(result) <= max_tokens


def _turns(count: int) -> list[dict]:
    messages: list[dict] = [{"role": "system", "content": "system prompt"}]
    for turn in range(count):
        messages.append({"role": "user", "content": f"question {turn} " * 30})
        messages.append({"role": "assistant", "content": f"answer {turn} " * 30})
    return messages


def _calibration_agent(minimal_config, mock_session_store, api_messages):
    from unittest.mock import MagicMock

    from openai import OpenAI

    estimate = estimate_total_request_tokens(api_messages)
    minimal_config["llm"]["max_tokens"] = 1024
    # Raw estimate fills 70% of the usable budget (window minus 2048 reserved).
    minimal_config["llm"]["context_window"] = int(estimate / 0.7) + 2048
    return NovaAgent(
        config=minimal_config,
        openai_client=MagicMock(spec=OpenAI),
        session_store=mock_session_store,
    )


def _respond_with_prompt_tokens(prompt_tokens: int) -> dict:
    return {
        "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": 1},
    }


def test_provider_usage_calibrates_compaction(minimal_config, mock_session_store):
    from unittest.mock import patch

    api_messages = _turns(12)
    agent = _calibration_agent(minimal_config, mock_session_store, api_messages)
    agent.messages = api_messages[1:]
    estimate = estimate_total_request_tokens(api_messages)

    compacted, _ = agent._compact_if_needed(api_messages, None)
    assert compacted is api_messages  # fits by the raw estimate

    with patch(
        "nova.agent.chat_completion", return_value=_respond_with_prompt_tokens(2 * estimate)
    ):
        agent._call_llm(api_messages, stream=False)

    assert agent._token_calibration == pytest.approx(2.0)
    compacted, _ = agent._compact_if_needed(api_messages, None)
    assert compacted is not api_messages
    assert 2 * estimate_total_request_tokens(compacted) <= agent._usable_context_tokens()


@pytest.mark.parametrize(("ratio", "expected"), [(10.0, 2.0), (0.1, 0.8), (1.25, 1.25)])
def test_calibration_is_bounded(minimal_config, mock_session_store, ratio, expected):
    from unittest.mock import patch

    api_messages = _turns(2)
    agent = _calibration_agent(minimal_config, mock_session_store, api_messages)
    estimate = estimate_total_request_tokens(api_messages)

    with patch(
        "nova.agent.chat_completion",
        return_value=_respond_with_prompt_tokens(int(estimate * ratio)),
    ):
        agent._call_llm(api_messages, stream=False)

    assert agent._token_calibration == pytest.approx(expected, rel=0.01)


def test_calibration_ignores_usage_without_prompt_tokens(minimal_config, mock_session_store):
    from unittest.mock import patch

    api_messages = _turns(2)
    agent = _calibration_agent(minimal_config, mock_session_store, api_messages)
    response = _respond_with_prompt_tokens(0)
    response["usage"] = {"cache_read_input_tokens": 5, "completion_tokens": 1}

    with patch("nova.agent.chat_completion", return_value=response):
        agent._call_llm(api_messages, stream=False)

    assert agent._token_calibration == 1.0
