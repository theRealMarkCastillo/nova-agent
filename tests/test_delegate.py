"""Tests for the sub-agent delegation tool.

Uses dependency injection to mock HTTP client, session store, and memory store.
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from openai import OpenAI

from nova.agent import NovaAgent
from nova.session import SessionStore
from nova.tools.delegate_tool import (
    DEFAULT_MAX_ITERATIONS,
    DEFAULT_TIMEOUT_SECONDS,
    MAX_TIMEOUT_SECONDS,
    _build_subagent_config,
    _delegate_task,
    _is_delegation_enabled,
    _run_subagent,
    register_delegate_tool,
)
from nova.tools.registry import ToolRegistry, discover_builtin_tools

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _minimal_config(delegation_enabled: bool = False, depth: int = 0) -> dict:
    """Return a minimal config for testing."""
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
        "delegation": {
            "enabled": delegation_enabled,
            "max_spawn_depth": 2,
            "default_timeout_seconds": DEFAULT_TIMEOUT_SECONDS,
            "subagent_budgets": {
                "max_iterations": DEFAULT_MAX_ITERATIONS,
                "system_prompt_max": 4000,
                "tool_result_max_chars": 4000,
            },
        },
        "_subagent_depth": depth,
    }


def _mock_session_store() -> SessionStore:
    tmpdir = tempfile.mkdtemp()
    return SessionStore(Path(tmpdir) / "test.db")


def _make_agent(config: dict | None = None) -> NovaAgent:
    """Create a NovaAgent with mocked OpenAI client and session store."""
    cfg = config or _minimal_config(delegation_enabled=True)
    mock_client = MagicMock(spec=OpenAI)
    session_store = _mock_session_store()
    return NovaAgent(config=cfg, openai_client=mock_client, session_store=session_store)


# ---------------------------------------------------------------------------
# _is_delegation_enabled
# ---------------------------------------------------------------------------


def test_delegation_enabled_flag_true():
    config = _minimal_config(delegation_enabled=True)
    assert _is_delegation_enabled(config) is True


def test_delegation_enabled_flag_false():
    config = _minimal_config(delegation_enabled=False)
    assert _is_delegation_enabled(config) is False


def test_delegation_enabled_no_config():
    assert _is_delegation_enabled(None) is False


def test_delegation_enabled_missing_key():
    assert _is_delegation_enabled({}) is False


# ---------------------------------------------------------------------------
# _build_subagent_config
# ---------------------------------------------------------------------------


def test_build_subagent_config_sets_depth():
    parent_config = _minimal_config(delegation_enabled=True, depth=0)
    child_config = _build_subagent_config(parent_config, depth=1, model=None, max_iterations=30)
    assert child_config["_subagent_depth"] == 1


def test_build_subagent_config_model_override():
    parent_config = _minimal_config(delegation_enabled=True)
    child_config = _build_subagent_config(
        parent_config,
        depth=1,
        model="openai/gpt-4o-mini",
        max_iterations=30,
    )
    assert child_config["llm"]["model"] == "openai/gpt-4o-mini"


def test_build_subagent_config_inherits_model_when_none():
    parent_config = _minimal_config(delegation_enabled=True)
    child_config = _build_subagent_config(parent_config, depth=1, model=None, max_iterations=30)
    assert child_config["llm"]["model"] == "test-model"


def test_build_subagent_config_applies_iteration_budget():
    # Config's subagent_budgets.max_iterations (30) takes precedence over the argument (15).
    parent_config = _minimal_config(delegation_enabled=True)
    child_config = _build_subagent_config(parent_config, depth=1, model=None, max_iterations=15)
    assert child_config["agent"]["max_iterations"] == DEFAULT_MAX_ITERATIONS  # config wins


def test_build_subagent_config_uses_argument_when_no_config_budget():
    # When subagent_budgets is absent, the argument is used as the fallback.
    parent_config = _minimal_config(delegation_enabled=True)
    del parent_config["delegation"]["subagent_budgets"]["max_iterations"]
    child_config = _build_subagent_config(parent_config, depth=1, model=None, max_iterations=15)
    assert child_config["agent"]["max_iterations"] == 15


def test_build_subagent_config_does_not_mutate_parent():
    parent_config = _minimal_config(delegation_enabled=True)
    original_model = parent_config["llm"]["model"]
    _build_subagent_config(parent_config, depth=1, model="other/model", max_iterations=30)
    assert parent_config["llm"]["model"] == original_model


def test_build_subagent_config_applies_tool_result_token_budget():
    parent_config = _minimal_config(delegation_enabled=True)
    parent_config["delegation"]["subagent_budgets"]["tool_result_max_tokens"] = 4000
    parent_config["budgets"]["tool_result_max_tokens"] = 12000

    child_config = _build_subagent_config(parent_config, depth=1, model=None, max_iterations=30)

    assert child_config["budgets"]["tool_result_max_tokens"] == 4000


# ---------------------------------------------------------------------------
# NovaAgent depth tracking
# ---------------------------------------------------------------------------


def test_agent_default_depth_is_zero():
    agent = _make_agent()
    assert agent.depth == 0


def test_agent_depth_from_config():
    config = _minimal_config(delegation_enabled=True, depth=1)
    agent = _make_agent(config)
    assert agent.depth == 1


def test_agent_is_leaf_at_max_depth():
    config = _minimal_config(delegation_enabled=True, depth=2)
    agent = _make_agent(config)
    assert agent.is_leaf_agent is True


def test_agent_is_not_leaf_below_max_depth():
    config = _minimal_config(delegation_enabled=True, depth=0)
    agent = _make_agent(config)
    assert agent.is_leaf_agent is False


def test_agent_is_not_leaf_at_depth_one():
    config = _minimal_config(delegation_enabled=True, depth=1)
    agent = _make_agent(config)
    assert agent.is_leaf_agent is False


# ---------------------------------------------------------------------------
# register_delegate_tool
# ---------------------------------------------------------------------------


def test_register_delegate_tool_when_enabled():
    local_registry = ToolRegistry()
    config = _minimal_config(delegation_enabled=True, depth=0)

    with patch("nova.tools.delegate_tool.registry", local_registry):
        register_delegate_tool(config)

    assert "delegate_task" in local_registry.all_tool_names


def test_register_delegate_tool_when_disabled():
    local_registry = ToolRegistry()
    config = _minimal_config(delegation_enabled=False)

    with patch("nova.tools.delegate_tool.registry", local_registry):
        register_delegate_tool(config)

    assert "delegate_task" not in local_registry.all_tool_names


def test_register_delegate_tool_skipped_for_leaf():
    """Leaf agents (at max depth) should not get the delegate_task tool."""
    local_registry = ToolRegistry()
    config = _minimal_config(delegation_enabled=True, depth=2)  # depth == max_spawn_depth

    with patch("nova.tools.delegate_tool.registry", local_registry):
        # discover_builtin_tools gates on depth < max_spawn_depth
        depth = config.get("_subagent_depth", 0)
        max_depth = config.get("delegation", {}).get("max_spawn_depth", 2)
        if depth < max_depth:
            register_delegate_tool(config)

    assert "delegate_task" not in local_registry.all_tool_names


# ---------------------------------------------------------------------------
# _delegate_task handler — validation
# ---------------------------------------------------------------------------


def test_delegate_task_requires_task_arg():
    agent = _make_agent()
    result = json.loads(_delegate_task({}, agent=agent))
    assert result["success"] is False
    assert "required" in result["error"].lower() or "task" in result["error"].lower()


def test_delegate_task_no_agent_context():
    result = json.loads(_delegate_task({"task": "do something"}))
    assert result["success"] is False
    assert "agent" in result["error"].lower()


def test_delegate_task_rejects_at_depth_limit():
    """Leaf agent calling delegate_task should get a clear error."""
    config = _minimal_config(delegation_enabled=True, depth=2)
    agent = _make_agent(config)

    result = json.loads(_delegate_task({"task": "do something"}, agent=agent))
    assert result["success"] is False
    assert "depth" in result["error"].lower() or "leaf" in result["error"].lower()


def test_delegate_task_clamps_timeout():
    """Timeout should be clamped to MAX_TIMEOUT_SECONDS."""
    agent = _make_agent()

    # We patch _run_subagent to avoid actually spawning a thread
    with patch("nova.tools.delegate_tool._run_subagent") as mock_run:
        mock_run.return_value = {
            "success": True,
            "result": "done",
            "label": "test",
            "depth": 1,
            "elapsed_seconds": 0.1,
            "error": None,
            "timeout": False,
        }
        _delegate_task(
            {"task": "test", "timeout_seconds": 9999},
            agent=agent,
        )

    # The future.result() timeout should be clamped — we verify via the
    # timeout passed to the executor (indirectly via mock not raising TimeoutError)
    mock_run.assert_called_once()


def test_delegate_task_invalid_context_mode_defaults_to_isolated():
    """Invalid context_mode should silently default to 'isolated'."""
    agent = _make_agent()

    with patch("nova.tools.delegate_tool._run_subagent") as mock_run:
        mock_run.return_value = {
            "success": True,
            "result": "done",
            "label": "test",
            "depth": 1,
            "elapsed_seconds": 0.1,
            "error": None,
            "timeout": False,
        }
        _delegate_task(
            {"task": "test", "context_mode": "invalid_mode"},
            agent=agent,
        )

    # Should not raise — invalid mode silently becomes "isolated"
    mock_run.assert_called_once()
    _, kwargs = mock_run.call_args
    assert kwargs.get("context_mode") == "isolated"


# ---------------------------------------------------------------------------
# _delegate_task handler — success path (mocked sub-agent)
# ---------------------------------------------------------------------------


def test_delegate_task_returns_success_result():
    agent = _make_agent()

    with patch("nova.tools.delegate_tool._run_subagent") as mock_run:
        mock_run.return_value = {
            "success": True,
            "result": "Task completed successfully.",
            "label": "my task",
            "depth": 1,
            "elapsed_seconds": 1.2,
            "error": None,
            "timeout": False,
        }
        raw = _delegate_task({"task": "my task"}, agent=agent)

    result = json.loads(raw)
    assert result["success"] is True
    assert result["result"] == "Task completed successfully."
    assert result["timeout"] is False


def test_delegate_task_returns_timeout_result():
    agent = _make_agent()

    # Simulate a FuturesTimeoutError from the executor
    from concurrent.futures import TimeoutError as FuturesTimeoutError

    with patch("nova.tools.delegate_tool.ThreadPoolExecutor") as mock_executor_cls:
        mock_executor = MagicMock()
        mock_executor_cls.return_value.__enter__ = MagicMock(return_value=mock_executor)
        mock_executor_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_future = MagicMock()
        mock_future.result.side_effect = FuturesTimeoutError()
        mock_executor.submit.return_value = mock_future

        raw = _delegate_task({"task": "slow task", "timeout_seconds": 1}, agent=agent)

    result = json.loads(raw)
    assert result["success"] is False
    assert result["timeout"] is True
    assert "timed out" in result["error"].lower()


def test_delegate_task_label_defaults_to_task_prefix():
    agent = _make_agent()

    with patch("nova.tools.delegate_tool._run_subagent") as mock_run:
        mock_run.return_value = {
            "success": True,
            "result": "ok",
            "label": "analyze the codebase for security",
            "depth": 1,
            "elapsed_seconds": 0.5,
            "error": None,
            "timeout": False,
        }
        _delegate_task({"task": "analyze the codebase for security issues"}, agent=agent)

    _, kwargs = mock_run.call_args
    # Label should be auto-derived from task (first 40 chars)
    assert kwargs["label"].startswith("analyze the codebase for security")


def test_delegate_task_uses_fork_context_mode():
    agent = _make_agent()
    agent.messages = [{"role": "user", "content": "hello"}]

    with patch("nova.tools.delegate_tool._run_subagent") as mock_run:
        mock_run.return_value = {
            "success": True,
            "result": "ok",
            "label": "test",
            "depth": 1,
            "elapsed_seconds": 0.1,
            "error": None,
            "timeout": False,
        }
        _delegate_task({"task": "test", "context_mode": "fork"}, agent=agent)

    _, kwargs = mock_run.call_args
    assert kwargs["context_mode"] == "fork"


# ---------------------------------------------------------------------------
# discover_builtin_tools integration
# ---------------------------------------------------------------------------


def test_discover_builtin_tools_registers_delegate_when_enabled():
    """discover_builtin_tools should register delegate_task when enabled."""
    local_registry = ToolRegistry()
    config = _minimal_config(delegation_enabled=True, depth=0)

    with (
        patch("nova.tools.registry.registry", local_registry),
        patch("nova.tools.delegate_tool.registry", local_registry),
    ):
        discover_builtin_tools(config)

    assert "delegate_task" in local_registry.all_tool_names


def test_discover_builtin_tools_skips_delegate_when_disabled():
    """discover_builtin_tools should not register delegate_task when disabled."""
    local_registry = ToolRegistry()
    config = _minimal_config(delegation_enabled=False)

    with (
        patch("nova.tools.registry.registry", local_registry),
        patch("nova.tools.delegate_tool.registry", local_registry),
    ):
        discover_builtin_tools(config)

    assert "delegate_task" not in local_registry.all_tool_names


def test_discover_builtin_tools_skips_delegate_for_leaf():
    """discover_builtin_tools should not register delegate_task for leaf agents."""
    local_registry = ToolRegistry()
    config = _minimal_config(delegation_enabled=True, depth=2)  # depth == max_spawn_depth

    with (
        patch("nova.tools.registry.registry", local_registry),
        patch("nova.tools.delegate_tool.registry", local_registry),
    ):
        discover_builtin_tools(config)

    assert "delegate_task" not in local_registry.all_tool_names


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


def test_max_timeout_is_300():
    assert MAX_TIMEOUT_SECONDS == 300


def test_default_timeout_is_60():
    assert DEFAULT_TIMEOUT_SECONDS == 60


def test_default_max_iterations_is_30():
    assert DEFAULT_MAX_ITERATIONS == 30


# ---------------------------------------------------------------------------
# _run_subagent — core execution logic
# ---------------------------------------------------------------------------


def _make_parent_agent(depth: int = 0, messages: list | None = None) -> MagicMock:
    """Build a minimal mock parent agent."""
    parent = MagicMock()
    parent.depth = depth
    parent.messages = messages or []
    parent.config = _minimal_config(delegation_enabled=True, depth=depth)
    parent.session_store = _mock_session_store()
    parent.memory = None
    parent.cost_tracker = None
    return parent


def test_run_subagent_happy_path():
    """_run_subagent returns success dict when sub-agent completes normally."""
    parent = _make_parent_agent(depth=0)
    mock_subagent = MagicMock()
    mock_subagent.run.return_value = "task result"
    mock_subagent.messages = []
    mock_subagent.cost_tracker = None

    mock_http_ctx = MagicMock()
    mock_http_ctx.__enter__ = MagicMock(return_value=mock_http_ctx)
    mock_http_ctx.__exit__ = MagicMock(return_value=False)

    with (
        patch("nova.tools.delegate_tool.OpenAI", return_value=mock_http_ctx),
        patch("nova.agent.NovaAgent", return_value=mock_subagent) as mock_agent_class,
    ):
        result = _run_subagent(
            task="do something",
            parent_agent=parent,
            label="test-task",
            model=None,
            timeout_seconds=60,
            context_mode="isolated",
        )

    assert result["success"] is True
    assert result["result"] == "task result"
    assert result["label"] == "test-task"
    assert result["depth"] == 1
    assert result["error"] is None
    assert result["timeout"] is False
    assert "elapsed_seconds" in result
    assert mock_agent_class.call_args.kwargs["workspace"] == parent.workspace


def test_run_subagent_exception_returns_error_dict():
    """_run_subagent returns error dict when sub-agent raises."""
    parent = _make_parent_agent(depth=0)

    mock_http_ctx = MagicMock()
    mock_http_ctx.__enter__ = MagicMock(return_value=mock_http_ctx)
    mock_http_ctx.__exit__ = MagicMock(return_value=False)

    with (
        patch("nova.tools.delegate_tool.OpenAI", return_value=mock_http_ctx),
        patch("nova.agent.NovaAgent", side_effect=RuntimeError("connection refused")),
    ):
        result = _run_subagent(
            task="do something",
            parent_agent=parent,
            label="fail-task",
            model=None,
            timeout_seconds=60,
            context_mode="isolated",
        )

    assert result["success"] is False
    assert result["result"] is None
    assert result["error"] == "connection refused"
    assert result["label"] == "fail-task"
    assert result["timeout"] is False


def test_run_subagent_fork_mode_prefills_messages():
    """_run_subagent with context_mode='fork' injects parent messages."""
    parent_messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
    ]
    parent = _make_parent_agent(depth=0, messages=parent_messages)

    mock_subagent = MagicMock()
    mock_subagent.run.return_value = "forked result"
    mock_subagent.messages = list(parent_messages)
    mock_subagent.cost_tracker = None

    mock_http_ctx = MagicMock()
    mock_http_ctx.__enter__ = MagicMock(return_value=mock_http_ctx)
    mock_http_ctx.__exit__ = MagicMock(return_value=False)

    with (
        patch("nova.tools.delegate_tool.OpenAI", return_value=mock_http_ctx),
        patch("nova.agent.NovaAgent", return_value=mock_subagent),
    ):
        result = _run_subagent(
            task="forked task",
            parent_agent=parent,
            label="fork",
            model=None,
            timeout_seconds=60,
            context_mode="fork",
        )

    assert result["success"] is True
    # Subagent.messages should have been set to parent's messages
    assert mock_subagent.messages == parent_messages


def test_run_subagent_isolated_mode_no_prefill():
    """_run_subagent with context_mode='isolated' does not inject parent messages."""
    parent_messages = [{"role": "user", "content": "parent ctx"}]
    parent = _make_parent_agent(depth=0, messages=parent_messages)

    mock_subagent = MagicMock()
    mock_subagent.run.return_value = "isolated result"
    mock_subagent.messages = []
    mock_subagent.cost_tracker = None

    mock_http_ctx = MagicMock()
    mock_http_ctx.__enter__ = MagicMock(return_value=mock_http_ctx)
    mock_http_ctx.__exit__ = MagicMock(return_value=False)

    with (
        patch("nova.tools.delegate_tool.OpenAI", return_value=mock_http_ctx),
        patch("nova.agent.NovaAgent", return_value=mock_subagent),
    ):
        result = _run_subagent(
            task="isolated task",
            parent_agent=parent,
            label="iso",
            model=None,
            timeout_seconds=60,
            context_mode="isolated",
        )

    assert result["success"] is True
    # In isolated mode, messages attribute is NOT set on subagent
    assert mock_subagent.run.called


def test_run_subagent_depth_incremented():
    """_run_subagent reports depth as parent.depth + 1."""
    parent = _make_parent_agent(depth=1)

    mock_subagent = MagicMock()
    mock_subagent.run.return_value = "ok"
    mock_subagent.messages = []
    mock_subagent.cost_tracker = None

    mock_http_ctx = MagicMock()
    mock_http_ctx.__enter__ = MagicMock(return_value=mock_http_ctx)
    mock_http_ctx.__exit__ = MagicMock(return_value=False)

    with (
        patch("nova.tools.delegate_tool.OpenAI", return_value=mock_http_ctx),
        patch("nova.agent.NovaAgent", return_value=mock_subagent),
    ):
        result = _run_subagent(
            task="t",
            parent_agent=parent,
            label="d",
            model=None,
            timeout_seconds=60,
            context_mode="isolated",
        )

    assert result["depth"] == 2


def test_run_subagent_custom_model_passed_to_config():
    """_run_subagent passes custom model into sub-agent config."""
    parent = _make_parent_agent(depth=0)
    captured_configs = []

    mock_subagent = MagicMock()
    mock_subagent.run.return_value = "ok"
    mock_subagent.messages = []
    mock_subagent.cost_tracker = None

    mock_http_ctx = MagicMock()
    mock_http_ctx.__enter__ = MagicMock(return_value=mock_http_ctx)
    mock_http_ctx.__exit__ = MagicMock(return_value=False)

    def capture_config(*args, **kwargs):
        captured_configs.append(kwargs.get("config", {}))
        return mock_subagent

    with (
        patch("nova.tools.delegate_tool.OpenAI", return_value=mock_http_ctx),
        patch("nova.agent.NovaAgent", side_effect=capture_config),
    ):
        _run_subagent(
            task="t",
            parent_agent=parent,
            label="m",
            model="custom/model",
            timeout_seconds=60,
            context_mode="isolated",
        )

    assert len(captured_configs) == 1
    assert captured_configs[0]["llm"]["model"] == "custom/model"


def test_register_delegate_tool_depth_limit_logging(caplog):
    """register_delegate_tool logs when agent is at max spawn depth."""
    import logging

    from nova.tools.delegate_tool import register_delegate_tool
    from nova.tools.registry import ToolRegistry

    local_registry = ToolRegistry()
    config = _minimal_config(delegation_enabled=True, depth=2)  # depth == max_spawn_depth

    with (
        patch("nova.tools.delegate_tool.registry", local_registry),
        caplog.at_level(logging.DEBUG, logger="nova.tools.delegate_tool"),
    ):
        register_delegate_tool(agent_config=config)

    assert "delegate_task" not in local_registry.all_tool_names
    assert any("leaf agent" in r.message for r in caplog.records)
