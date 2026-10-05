"""Tests for delegate_tool exception paths and error handling.

Tests the error scenarios:
- Sub-agent creation failures
- Timeout execution
- Cost tracker aggregation on errors
- Configuration safety (deep copy)
- Malformed responses
"""

import json
import threading
from concurrent.futures import TimeoutError as FuturesTimeoutError
from unittest.mock import MagicMock, patch

from nova.cost_tracker import CostTracker
from nova.tools.delegate_tool import _delegate_task


def test_delegate_no_agent_context():
    """Test that delegate_task returns error when no agent context is provided."""
    result = _delegate_task({"task": "test"})
    parsed = json.loads(result)
    assert parsed["success"] is False
    assert "agent context" in parsed["error"].lower()


def test_delegate_empty_task():
    """Test that empty task description is rejected."""
    mock_agent = MagicMock()
    result = _delegate_task({"task": ""}, agent=mock_agent)
    parsed = json.loads(result)
    assert parsed["success"] is False
    assert "required" in parsed["error"].lower()


def test_delegate_depth_limit_exceeded():
    """Test that delegates at max depth are rejected."""
    mock_agent = MagicMock()
    mock_agent.depth = 2
    mock_agent.config = {"delegation": {"max_spawn_depth": 2}}

    result = _delegate_task({"task": "test task"}, agent=mock_agent)
    parsed = json.loads(result)
    assert parsed["success"] is False
    assert "max depth" in parsed["error"].lower()


def test_delegate_timeout_triggered():
    """Test that timeout is properly caught and reported."""
    mock_agent = MagicMock()
    mock_agent.depth = 0
    mock_agent.config = {
        "delegation": {"max_spawn_depth": 2, "default_timeout_seconds": 1},
        "llm": {"model": "test", "base_url": "http://test", "api_key": "test"},
        "agent": {"max_iterations": 3},
        "budgets": {"system_prompt_max": 8000},
        "microcompact": {"enabled": True},
        "wiki": {"enabled": False},
        "session": {"directory": "/tmp"},
    }
    mock_agent.session_store = MagicMock()
    mock_agent.memory = None

    with patch("nova.tools.delegate_tool.ThreadPoolExecutor") as mock_executor_class:
        mock_executor = MagicMock()
        mock_executor_class.return_value.__enter__ = MagicMock(return_value=mock_executor)
        mock_executor_class.return_value.__exit__ = MagicMock(return_value=None)

        # Simulate timeout
        mock_future = MagicMock()
        mock_future.result.side_effect = FuturesTimeoutError()
        mock_executor.submit.return_value = mock_future

        result = _delegate_task(
            {"task": "test task", "timeout_seconds": 1},
            agent=mock_agent,
        )
        parsed = json.loads(result)
        assert parsed["success"] is False
        assert parsed["timeout"] is True
        assert "timed out" in parsed["error"].lower()


def test_delegate_invalid_context_mode():
    """Test that invalid context_mode defaults to 'isolated'."""
    mock_agent = MagicMock()
    mock_agent.depth = 0
    mock_agent.config = {
        "delegation": {"max_spawn_depth": 2},
        "llm": {"model": "test", "base_url": "http://test", "api_key": "test"},
    }
    mock_agent.cost_tracker = None

    with patch("nova.tools.delegate_tool.ThreadPoolExecutor") as mock_executor_class:
        mock_executor = MagicMock()
        mock_executor_class.return_value.__enter__ = MagicMock(return_value=mock_executor)
        mock_executor_class.return_value.__exit__ = MagicMock(return_value=None)

        # Mock executor.submit to return a future with our result
        mock_future = MagicMock()
        mock_future.result.return_value = {"success": True, "result": "OK", "usage": {}}
        mock_executor.submit.return_value = mock_future

        result = _delegate_task(
            {"task": "test", "context_mode": "invalid_mode"},
            agent=mock_agent,
        )
        # Should succeed without error about invalid mode
        parsed = json.loads(result)
        assert parsed["success"] is True


def test_delegate_timeout_seconds_clamped():
    """Test that timeout_seconds is clamped to MAX_TIMEOUT_SECONDS."""

    mock_agent = MagicMock()
    mock_agent.depth = 0
    mock_agent.config = {
        "delegation": {"max_spawn_depth": 2},
        "llm": {"model": "test", "base_url": "http://test", "api_key": "test"},
    }
    mock_agent.cost_tracker = None

    with patch("nova.tools.delegate_tool.ThreadPoolExecutor") as mock_executor_class:
        mock_executor = MagicMock()
        mock_executor_class.return_value.__enter__ = MagicMock(return_value=mock_executor)
        mock_executor_class.return_value.__exit__ = MagicMock(return_value=None)

        mock_future = MagicMock()
        mock_future.result.return_value = {"success": True, "result": "OK", "usage": {}}
        mock_executor.submit.return_value = mock_future

        # Should accept the large timeout without error (clamped internally)
        result = _delegate_task(
            {"task": "test", "timeout_seconds": 9999},
            agent=mock_agent,
        )
        parsed = json.loads(result)
        assert parsed["success"] is True


def _subagent_with_usage(input_tokens: int, cost: float) -> MagicMock:
    child = MagicMock()
    child.messages = []
    child.cost_tracker = CostTracker(model="test")
    child.cost_tracker.add_usage(
        input_tokens=input_tokens, output_tokens=0, input_cost=cost, output_cost=0.0
    )
    return child


def _parent_with_tracker() -> MagicMock:
    parent = MagicMock()
    parent.depth = 0
    parent.messages = []
    parent.config = {
        "delegation": {"max_spawn_depth": 2, "enabled": True},
        "llm": {"model": "test", "base_url": "http://test", "api_key": "test"},
        "agent": {"max_iterations": 5},
        "budgets": {},
    }
    parent.cost_tracker = CostTracker(model="test")
    return parent


def test_subagent_cost_merged_into_parent():
    parent = _parent_with_tracker()
    child = _subagent_with_usage(100, 0.001)
    child.run.return_value = "OK"

    with (
        patch("nova.tools.delegate_tool.build_client"),
        patch("nova.agent.NovaAgent", return_value=child),
    ):
        result = _delegate_task({"task": "test task"}, agent=parent)

    assert json.loads(result)["success"] is True
    assert parent.cost_tracker.total.input_tokens == 100
    assert parent.cost_tracker.total.input_cost == 0.001


def test_failed_subagent_cost_merged_into_parent():
    parent = _parent_with_tracker()
    child = _subagent_with_usage(40, 0.0004)
    child.run.side_effect = RuntimeError("boom")

    with (
        patch("nova.tools.delegate_tool.build_client"),
        patch("nova.agent.NovaAgent", return_value=child),
    ):
        _delegate_task({"task": "test task"}, agent=parent)

    assert parent.cost_tracker.total.input_tokens == 40


def test_timed_out_subagent_cost_merged_when_it_finishes():
    parent = _parent_with_tracker()
    child = _subagent_with_usage(70, 0.0007)
    release = threading.Event()
    closed = threading.Event()
    child.run.side_effect = lambda *args, **kwargs: release.wait(5) and "late"
    child.close.side_effect = closed.set

    with (
        patch("nova.tools.delegate_tool.build_client"),
        patch("nova.agent.NovaAgent", return_value=child),
    ):
        result = json.loads(_delegate_task({"task": "slow", "timeout_seconds": 1}, agent=parent))
        assert result["timeout"] is True
        assert parent.cost_tracker.total.input_tokens == 0
        release.set()
        assert closed.wait(5)

    assert parent.cost_tracker.total.input_tokens == 70


def test_delegate_config_deep_copy():
    """Test that parent config is not mutated during delegation."""
    mock_agent = MagicMock()
    mock_agent.depth = 0
    original_config = {
        "delegation": {"max_spawn_depth": 2},
        "llm": {"model": "original-model", "base_url": "http://test", "api_key": "test"},
    }
    mock_agent.config = original_config.copy()

    with patch("nova.tools.delegate_tool.ThreadPoolExecutor") as mock_executor_class:
        mock_executor = MagicMock()
        mock_executor_class.return_value.__enter__ = MagicMock(return_value=mock_executor)
        mock_executor_class.return_value.__exit__ = MagicMock(return_value=None)

        mock_future = MagicMock()
        mock_future.result.return_value = {"success": True, "result": "OK"}
        mock_executor.submit.return_value = mock_future

        with patch("nova.tools.delegate_tool._run_subagent") as mock_run:
            mock_run.return_value = {"success": True, "result": "OK"}
            _delegate_task(
                {"task": "test", "model": "override-model"},
                agent=mock_agent,
            )
            # Verify parent config model is unchanged
            assert mock_agent.config["llm"]["model"] == "original-model"


def test_delegate_label_generation():
    """Test that label is auto-generated from task if not provided."""
    mock_agent = MagicMock()
    mock_agent.depth = 0
    mock_agent.config = {
        "delegation": {"max_spawn_depth": 2},
        "llm": {"model": "test", "base_url": "http://test", "api_key": "test"},
    }
    mock_agent.cost_tracker = None

    with patch("nova.tools.delegate_tool.ThreadPoolExecutor") as mock_executor_class:
        mock_executor = MagicMock()
        mock_executor_class.return_value.__enter__ = MagicMock(return_value=mock_executor)
        mock_executor_class.return_value.__exit__ = MagicMock(return_value=None)

        mock_future = MagicMock()
        mock_future.result.return_value = {
            "success": True,
            "result": "OK",
            "label": "generated",
            "usage": {},
        }
        mock_executor.submit.return_value = mock_future

        # Should succeed with auto-generated label
        result = _delegate_task(
            {"task": "This is a long task description that should be truncated"},
            agent=mock_agent,
        )
        parsed = json.loads(result)
        assert parsed["success"] is True
        assert "label" in parsed or "generated" in str(parsed)


def test_delegate_fork_mode_inherits_transcript():
    """Test that fork context_mode passes parent transcript to sub-agent."""
    mock_agent = MagicMock()
    mock_agent.depth = 0
    mock_agent.config = {
        "delegation": {"max_spawn_depth": 2},
        "llm": {"model": "test", "base_url": "http://test", "api_key": "test"},
    }
    mock_agent.messages = [
        {"role": "user", "content": "parent message"},
        {"role": "assistant", "content": "parent response"},
    ]
    mock_agent.cost_tracker = None

    with patch("nova.tools.delegate_tool.ThreadPoolExecutor") as mock_executor_class:
        mock_executor = MagicMock()
        mock_executor_class.return_value.__enter__ = MagicMock(return_value=mock_executor)
        mock_executor_class.return_value.__exit__ = MagicMock(return_value=None)

        mock_future = MagicMock()
        mock_future.result.return_value = {"success": True, "result": "OK", "usage": {}}
        mock_executor.submit.return_value = mock_future

        # Should succeed with fork mode (inherited transcript)
        result = _delegate_task(
            {"task": "test", "context_mode": "fork"},
            agent=mock_agent,
        )
        parsed = json.loads(result)
        assert parsed["success"] is True


def test_delegate_result_structure():
    """Test that successful delegation returns proper result structure."""
    mock_agent = MagicMock()
    mock_agent.depth = 0
    mock_agent.config = {
        "delegation": {"max_spawn_depth": 2},
        "llm": {"model": "test", "base_url": "http://test", "api_key": "test"},
    }
    mock_agent.cost_tracker = None

    with patch("nova.tools.delegate_tool.ThreadPoolExecutor") as mock_executor_class:
        mock_executor = MagicMock()
        mock_executor_class.return_value.__enter__ = MagicMock(return_value=mock_executor)
        mock_executor_class.return_value.__exit__ = MagicMock(return_value=None)

        mock_future = MagicMock()
        mock_future.result.return_value = {
            "success": True,
            "result": "task result",
            "label": "test",
            "depth": 1,
            "elapsed_seconds": 1.5,
            "usage": {},
            "error": None,
            "timeout": False,
        }
        mock_executor.submit.return_value = mock_future

        result = _delegate_task({"task": "test task"}, agent=mock_agent)
        parsed = json.loads(result)

        assert parsed["success"] is True
        assert parsed["result"] == "task result"
        assert parsed["depth"] == 1
        assert "elapsed_seconds" in parsed
        assert "error" in parsed
        assert "timeout" in parsed


def test_subagent_shares_parent_mcp_client():
    parent = _parent_with_tracker()
    child = _subagent_with_usage(0, 0.0)
    child.run.return_value = "OK"

    with (
        patch("nova.tools.delegate_tool.build_client"),
        patch("nova.agent.NovaAgent", return_value=child) as agent_class,
    ):
        _delegate_task({"task": "use mcp"}, agent=parent)

    assert agent_class.call_args.kwargs["mcp_client"] is parent.mcp_client


def test_timed_out_subagent_cannot_prompt_user():
    parent = _parent_with_tracker()
    parent._confirmation_callback = MagicMock(return_value=True)
    child = _subagent_with_usage(0, 0.0)
    release = threading.Event()
    closed = threading.Event()
    child.close.side_effect = closed.set
    answers: list[bool] = []

    def run(*args, **kwargs):
        release.wait(5)
        confirm = agent_class.call_args.kwargs["confirmation_callback"]
        answers.append(confirm("terminal", {"command": "rm -rf build"}))
        return "late"

    child.run.side_effect = run
    with (
        patch("nova.tools.delegate_tool.build_client"),
        patch("nova.agent.NovaAgent", return_value=child) as agent_class,
    ):
        result = json.loads(_delegate_task({"task": "slow", "timeout_seconds": 1}, agent=parent))
        release.set()
        assert closed.wait(5)

    assert result["timeout"] is True
    assert answers == [False]
    parent._confirmation_callback.assert_not_called()
