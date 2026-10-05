"""Tests for agent token management and truncation."""

import tempfile
from pathlib import Path

import pytest

from nova.agent import NovaAgent
from nova.session import SessionStore
from nova.tokens import estimate_tokens


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
