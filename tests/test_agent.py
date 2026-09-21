"""Tests for the main agent loop.

Uses dependency injection to mock OpenAI client, session store, and memory store.
"""

import json
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nova.agent import ContextBudgetError, NovaAgent, _normalize_message_history
from nova.mcp_client import McpResourceInfo, McpToolInfo
from nova.tools.registry import discover_builtin_tools


def make_openai_response(content: str = "OK", tool_calls=None) -> MagicMock:
    """Build a mock OpenAI ChatCompletion response."""
    msg = MagicMock()
    msg.content = content
    msg.tool_calls = tool_calls
    msg.model_extra = {}
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message = msg
    resp.usage = MagicMock()
    resp.usage.model_dump.return_value = {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
    }
    return resp


def test_normalize_message_history_removes_orphaned_tool_messages():
    messages = [
        {"role": "user", "content": "Do it"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "call_1"}]},
        {"role": "tool", "content": "ok", "tool_call_id": "call_1"},
        {"role": "tool", "content": "orphan", "tool_call_id": "call_old"},
        {"role": "assistant", "content": "finished"},
    ]
    result = _normalize_message_history(messages)
    assert len(result) == 4
    assert result[-1]["content"] == "finished"


def test_normalize_message_history_removes_incomplete_tool_call():
    messages = [
        {"role": "user", "content": "Do it"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "call_1"}]},
    ]
    assert _normalize_message_history(messages) == messages[:1]


def test_normalize_message_history_drops_empty_id_tool_calls():
    """Tool calls without ids cannot be answered; they must not dangle."""
    messages = [
        {"role": "user", "content": "Do it"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": ""}]},
        {"role": "user", "content": "hello?"},
    ]
    result = _normalize_message_history(messages)
    assert [m["role"] for m in result] == ["user", "user"]


def test_normalize_message_history_keeps_valid_subset_of_mixed_ids():
    messages = [
        {"role": "user", "content": "Do it"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "a"}, {"id": ""}],
        },
        {"role": "tool", "content": "ok", "tool_call_id": "a"},
        {"role": "user", "content": "thanks"},
    ]
    result = _normalize_message_history(messages)
    assert len(result) == 4
    assert result[1]["tool_calls"] == [{"id": "a"}]
    assert result[2]["tool_call_id"] == "a"


def _agent_with_tool_response(minimal_config, mock_session_store, mock_openai_client):
    tc_mock = MagicMock()
    tc_mock.model_dump.return_value = {
        "id": "call_1",
        "type": "function",
        "function": {"name": "terminal", "arguments": json.dumps({"command": "echo confirmed"})},
    }
    tool_response = make_openai_response(content=None, tool_calls=[tc_mock])
    mock_openai_client.chat.completions.create.return_value = tool_response
    return NovaAgent(
        config=minimal_config,
        openai_client=mock_openai_client,
        session_store=mock_session_store,
    )


def test_confirmation_gate_denies_without_callback(
    minimal_config, mock_session_store, mock_openai_client
):
    agent = _agent_with_tool_response(minimal_config, mock_session_store, mock_openai_client)
    agent.run("run echo confirmed", stream=False)
    tool_msgs = [m for m in agent.messages if m.get("role") == "tool"]
    assert tool_msgs
    assert all("requires confirmation" in m["content"] for m in tool_msgs)
    assert not any("confirmed" in m["content"] for m in tool_msgs)


def test_confirmation_gate_executes_when_approved(
    minimal_config, mock_session_store, mock_openai_client
):
    agent = _agent_with_tool_response(minimal_config, mock_session_store, mock_openai_client)
    agent._confirmation_callback = lambda name, arguments: True
    agent.run("run echo confirmed", stream=False)
    tool_msgs = [m for m in agent.messages if m.get("role") == "tool"]
    assert tool_msgs and "confirmed" in tool_msgs[-1]["content"]


def test_agent_creation_with_injected_deps(minimal_config, mock_session_store, mock_openai_client):
    """Test that agent accepts injected dependencies."""
    agent = NovaAgent(
        config=minimal_config,
        openai_client=mock_openai_client,
        session_store=mock_session_store,
    )

    assert agent.session_store is mock_session_store
    assert agent.client is mock_openai_client
    assert agent.wiki is None  # wiki disabled in minimal_config
    assert agent.session_id is not None


def test_agent_creates_session_on_init(minimal_config, mock_session_store, mock_openai_client):
    """Test that a new session is created when no session_id is provided."""
    agent = NovaAgent(
        config=minimal_config,
        openai_client=mock_openai_client,
        session_store=mock_session_store,
    )

    assert agent.session_id is not None
    assert agent._system_prompt is not None
    assert "test agent" in agent._system_prompt


class FakeMcpClient:
    def __init__(self):
        self.connected = False
        self.calls = []
        self.resources = [McpResourceInfo("server", "doc", "file://doc")]
        self.tools = [McpToolInfo("server", "echo", "MCP echo", {"type": "object"})]

    def connect_all(self):
        self.connected = True
        return ["server"]

    def list_tools(self):
        return self.tools

    def list_resources(self):
        return self.resources

    def is_connected(self, name):
        return self.connected and name == "server"

    @property
    def connected_servers(self):
        return {"server"} if self.connected else set()

    def call_tool(self, server, name, arguments):
        self.calls.append((server, name, arguments))
        return "mcp result"

    def read_resource(self, server, uri):
        self.calls.append((server, uri))
        return "resource result"

    def disconnect_all(self):
        self.connected = False


def test_mcp_tools_are_agent_local_and_namespaced(
    minimal_config, mock_session_store, mock_openai_client
):
    mcp = FakeMcpClient()
    agent = NovaAgent(
        config=minimal_config,
        openai_client=mock_openai_client,
        session_store=mock_session_store,
        mcp_client=mcp,
    )

    names = {item["function"]["name"] for item in agent._get_tool_definitions()}
    assert "mcp__server__echo" in names
    assert "mcp_read_resource" in names
    assert "mcp__server__echo" not in agent_module_registry_names()


def agent_module_registry_names():
    from nova.tools.registry import registry

    return registry.all_tool_names


def test_mcp_calls_use_normal_permission_path_and_close(
    minimal_config, mock_session_store, mock_openai_client
):
    mcp = FakeMcpClient()
    agent = NovaAgent(
        config=minimal_config,
        openai_client=mock_openai_client,
        session_store=mock_session_store,
        mcp_client=mcp,
        confirmation_callback=lambda _name, _args: True,
    )
    call = {"function": {"name": "mcp__server__echo", "arguments": json.dumps({"value": 1})}}
    assert agent._execute_tool_call(call) == "mcp result"
    assert mcp.calls == [("server", "echo", {"value": 1})]
    assert (
        agent._execute_tool_call(
            {
                "function": {
                    "name": "mcp_read_resource",
                    "arguments": json.dumps({"server_name": "server", "uri": "file://doc"}),
                }
            }
        )
        == "resource result"
    )
    agent.close()
    assert not mcp.connected


def test_mcp_resource_arguments_are_validated(
    minimal_config, mock_session_store, mock_openai_client
):
    mcp = FakeMcpClient()
    agent = NovaAgent(
        config=minimal_config,
        openai_client=mock_openai_client,
        session_store=mock_session_store,
        mcp_client=mcp,
    )
    result = agent._execute_tool_call(
        {"function": {"name": "mcp_read_resource", "arguments": json.dumps({"uri": "x"})}}
    )
    assert result == "Error: server_name must be a non-empty string"


def test_agent_workspace_controls_context_and_relative_tools(
    minimal_config, mock_session_store, mock_openai_client, tmp_path
):
    minimal_config["context_files"] = ["AGENTS.md"]
    (tmp_path / "AGENTS.md").write_text("workspace sentinel")
    agent = NovaAgent(
        config=minimal_config,
        openai_client=mock_openai_client,
        session_store=mock_session_store,
        workspace=tmp_path,
        confirmation_callback=lambda name, arguments: True,
    )

    assert "workspace sentinel" in (agent._system_prompt or "")

    terminal_call = {
        "function": {
            "name": "terminal",
            "arguments": json.dumps({"command": "pwd"}),
        }
    }
    result = agent._execute_tool_call(terminal_call)
    assert str(tmp_path) in result

    write_call = {
        "function": {
            "name": "write_file",
            "arguments": json.dumps({"path": "nested.txt", "content": "workspace data"}),
        }
    }
    agent._execute_tool_call(write_call)
    assert (tmp_path / "nested.txt").read_text() == "workspace data"


def test_agent_loads_existing_session(minimal_config, mock_session_store, mock_openai_client):
    """Test that an existing session is loaded correctly."""
    agent1 = NovaAgent(
        config=minimal_config,
        openai_client=mock_openai_client,
        session_store=mock_session_store,
    )
    session_id = agent1.session_id

    mock_session_store.add_message(session_id, "user", "hello")

    agent2 = NovaAgent(
        config=minimal_config,
        session_id=session_id,
        openai_client=mock_openai_client,
        session_store=mock_session_store,
    )

    assert agent2.session_id == session_id
    messages = mock_session_store.get_messages(session_id)
    assert len(messages) >= 1


def test_agent_run_no_tool_calls(minimal_config, mock_session_store, mock_openai_client):
    """Test a simple run with no tool calls from the model."""
    mock_openai_client.chat.completions.create.return_value = make_openai_response(
        content="The answer is 42."
    )

    agent = NovaAgent(
        config=minimal_config,
        openai_client=mock_openai_client,
        session_store=mock_session_store,
    )

    result = agent.run("What is the meaning of life?", stream=False)

    assert result == "The answer is 42."
    mock_openai_client.chat.completions.create.assert_called_once()
    call_kwargs = mock_openai_client.chat.completions.create.call_args[1]
    assert any("meaning of life" in str(m.get("content", "")) for m in call_kwargs["messages"])
    assert "extra_body" not in call_kwargs
    # max_tokens must be forwarded so the reserved output budget is real.
    assert call_kwargs["max_tokens"] == minimal_config["llm"]["max_tokens"]


def test_agent_recovers_from_context_overflow(
    minimal_config, mock_session_store, mock_openai_client
):
    """A provider overflow error triggers aggressive compaction and one retry."""
    overflow = Exception("This model's maximum context length is 8192 tokens")
    ok = make_openai_response(content="recovered")
    mock_openai_client.chat.completions.create.side_effect = [overflow, ok]

    agent = NovaAgent(
        config=minimal_config,
        openai_client=mock_openai_client,
        session_store=mock_session_store,
    )

    result = agent.run("hello", stream=False)

    assert result == "recovered"
    assert mock_openai_client.chat.completions.create.call_count == 2


def test_agent_reraises_non_overflow_errors(minimal_config, mock_session_store, mock_openai_client):
    """A non-overflow error is not retried by the overflow path."""
    mock_openai_client.chat.completions.create.side_effect = ValueError("bad request 400")

    agent = NovaAgent(
        config=minimal_config,
        openai_client=mock_openai_client,
        session_store=mock_session_store,
    )

    with pytest.raises(ValueError):
        agent.run("hello", stream=False)


def test_agent_continues_when_session_persistence_is_locked(
    minimal_config, mock_session_store, mock_openai_client
):
    mock_openai_client.chat.completions.create.return_value = make_openai_response("OK")
    agent = NovaAgent(
        config=minimal_config,
        openai_client=mock_openai_client,
        session_store=mock_session_store,
    )

    with patch.object(
        mock_session_store,
        "add_message",
        side_effect=sqlite3.OperationalError("database is locked"),
    ):
        assert agent.run("hello", stream=False) == "OK"


def test_agent_run_with_tool_call(minimal_config, mock_session_store, mock_openai_client):
    """Test a run where the model calls a tool."""
    discover_builtin_tools()

    tc_mock = MagicMock()
    tc_mock.model_dump.return_value = {
        "id": "call_123",
        "type": "function",
        "function": {
            "name": "terminal",
            "arguments": json.dumps({"command": "echo hello"}),
        },
    }
    tool_call_response = make_openai_response(content=None, tool_calls=[tc_mock])
    final_response = make_openai_response(content="The command output was: hello")

    mock_openai_client.chat.completions.create.side_effect = [
        tool_call_response,
        final_response,
    ]

    agent = NovaAgent(
        config=minimal_config,
        openai_client=mock_openai_client,
        session_store=mock_session_store,
    )

    result = agent.run("Run echo hello", stream=False)

    assert result == "The command output was: hello"
    assert mock_openai_client.chat.completions.create.call_count == 2


def test_agent_history_compacts_to_token_budget(
    minimal_config, mock_session_store, mock_openai_client
):
    """Test that old complete turns are removed when the active budget is exceeded."""
    minimal_config["llm"]["max_tokens"] = 100

    mock_openai_client.chat.completions.create.return_value = make_openai_response(content="OK")

    agent = NovaAgent(
        config=minimal_config,
        openai_client=mock_openai_client,
        session_store=mock_session_store,
    )

    for i in range(10):
        agent.messages.append({"role": "user", "content": f"msg {i} " * 100})
        agent.messages.append({"role": "assistant", "content": f"reply {i} " * 100})

    assert len(agent.messages) == 20

    with patch("nova.agent.get_model_context_window", return_value=10000):
        agent.run("latest message", stream=False)

    assert len(agent.messages) < 22
    assert not any("msg 0" in message.get("content", "") for message in agent.messages)


def test_compaction_injects_recovery_note(minimal_config, mock_session_store, mock_openai_client):
    """After compaction, the model is told history is recoverable via search."""
    minimal_config["llm"]["max_tokens"] = 100
    mock_openai_client.chat.completions.create.return_value = make_openai_response(content="OK")

    agent = NovaAgent(
        config=minimal_config,
        openai_client=mock_openai_client,
        session_store=mock_session_store,
    )
    for i in range(10):
        agent.messages.append({"role": "user", "content": f"msg {i} " * 100})
        agent.messages.append({"role": "assistant", "content": f"reply {i} " * 100})

    with patch("nova.agent.get_model_context_window", return_value=10000):
        agent.run("latest message", stream=False)

    sent = mock_openai_client.chat.completions.create.call_args.kwargs["messages"]
    notes = [
        m
        for m in sent
        if m.get("role") == "system"
        and "older conversation history was removed" in m.get("content", "")
    ]
    assert notes, "compaction note missing from the request sent to the model"
    # The note is ephemeral guidance — it must not be persisted into the
    # canonical conversation history.
    assert not any(
        "older conversation history was removed" in m.get("content", "") for m in agent.messages
    )


def test_compaction_fails_without_altering_required_context(
    minimal_config, mock_session_store, mock_openai_client
):
    agent = NovaAgent(
        config=minimal_config,
        openai_client=mock_openai_client,
        session_store=mock_session_store,
    )
    agent._system_prompt = "system " * 1000
    api_messages = [
        {"role": "system", "content": agent._system_prompt},
        {"role": "user", "content": "keep this request"},
    ]

    with (
        patch("nova.agent.get_model_context_window", return_value=1200),
        pytest.raises(ContextBudgetError),
    ):
        agent._compact_if_needed(api_messages, tools=[])

    assert api_messages[0]["content"] == agent._system_prompt
    assert api_messages[1]["content"] == "keep this request"


def test_agent_execute_tool_call_invalid_json(
    minimal_config, mock_session_store, mock_openai_client
):
    """Test that invalid JSON in tool call arguments is handled gracefully."""
    agent = NovaAgent(
        config=minimal_config,
        openai_client=mock_openai_client,
        session_store=mock_session_store,
    )

    tool_call = {
        "id": "call_bad",
        "function": {
            "name": "terminal",
            "arguments": "{not valid json",
        },
    }

    result = agent._execute_tool_call(tool_call)
    assert "Error" in result
    assert "Invalid JSON" in result


def test_agent_execute_tool_call_unknown_tool(
    minimal_config, mock_session_store, mock_openai_client
):
    """Test that unknown tool names return an error."""
    agent = NovaAgent(
        config=minimal_config,
        openai_client=mock_openai_client,
        session_store=mock_session_store,
    )

    tool_call = {
        "id": "call_unknown",
        "function": {
            "name": "nonexistent_tool_xyz",
            "arguments": "{}",
        },
    }

    result = agent._execute_tool_call(tool_call)
    assert "Error" in result


def test_agent_build_system_prompt_with_wiki(
    minimal_config, mock_session_store, mock_openai_client, mock_wiki_store
):
    """Test that system prompt includes wiki content when wiki is enabled."""
    minimal_config["wiki"]["enabled"] = True
    mock_wiki_store.write("Preferences", "User prefers dark mode")

    agent = NovaAgent(
        config=minimal_config,
        openai_client=mock_openai_client,
        session_store=mock_session_store,
        wiki_memory_store=mock_wiki_store,
    )

    assert agent.wiki is not None
    assert agent._system_prompt is not None
    assert "Preferences" in agent._system_prompt


def test_agent_refresh_system_prompt(
    minimal_config, mock_session_store, mock_openai_client, mock_wiki_store
):
    """Test that _refresh_system_prompt updates the prompt and session."""
    minimal_config["wiki"]["enabled"] = True

    agent = NovaAgent(
        config=minimal_config,
        openai_client=mock_openai_client,
        session_store=mock_session_store,
        wiki_memory_store=mock_wiki_store,
    )

    initial_prompt = agent._system_prompt
    mock_wiki_store.write("NYC Note", "user lives in NYC")
    agent._refresh_system_prompt()

    assert agent._system_prompt != initial_prompt or "NYC" in agent._system_prompt
    info = mock_session_store.get_session_info(agent.session_id)
    assert "NYC" in info.get("system_prompt", "")


def test_session_resume_refreshes_wiki_content(
    minimal_config, mock_session_store, mock_openai_client, mock_wiki_store
):
    """Session resume rebuilds the system prompt with current wiki state."""
    minimal_config["wiki"]["enabled"] = True

    agent1 = NovaAgent(
        config=minimal_config,
        openai_client=mock_openai_client,
        session_store=mock_session_store,
        wiki_memory_store=mock_wiki_store,
    )
    session_id = agent1.session_id
    assert "NewFact" not in (agent1._system_prompt or "")

    mock_wiki_store.write("NewFact", "This fact was added between sessions.")

    agent2 = NovaAgent(
        config=minimal_config,
        openai_client=mock_openai_client,
        session_store=mock_session_store,
        session_id=session_id,
        wiki_memory_store=mock_wiki_store,
    )
    assert "NewFact" in (agent2._system_prompt or "")


def test_agent_max_iterations_limit(minimal_config, mock_session_store, mock_openai_client):
    """Test that the agent stops after max_iterations."""
    minimal_config["agent"]["max_iterations"] = 2
    discover_builtin_tools()

    tc_mock = MagicMock()
    tc_mock.model_dump.return_value = {
        "id": "call_loop",
        "type": "function",
        "function": {
            "name": "terminal",
            "arguments": json.dumps({"command": "echo test"}),
        },
    }
    mock_openai_client.chat.completions.create.return_value = make_openai_response(
        content=None, tool_calls=[tc_mock]
    )

    agent = NovaAgent(
        config=minimal_config,
        openai_client=mock_openai_client,
        session_store=mock_session_store,
    )

    agent.run("test", stream=False)

    assert mock_openai_client.chat.completions.create.call_count == 2


def test_agent_depth_defaults_to_zero(delegation_config, mock_openai_client, mock_session_store):
    """Root agents should have depth=0."""
    agent = NovaAgent(
        config=delegation_config,
        openai_client=mock_openai_client,
        session_store=mock_session_store,
    )
    assert agent.depth == 0


def test_agent_depth_from_subagent_config(
    delegation_config, mock_openai_client, mock_session_store
):
    """Sub-agent config sets depth correctly."""
    delegation_config["_subagent_depth"] = 1
    agent = NovaAgent(
        config=delegation_config,
        openai_client=mock_openai_client,
        session_store=mock_session_store,
    )
    assert agent.depth == 1


def test_agent_is_leaf_at_max_depth(delegation_config, mock_openai_client, mock_session_store):
    """Agent at max_spawn_depth is a leaf."""
    delegation_config["_subagent_depth"] = 2  # == max_spawn_depth
    agent = NovaAgent(
        config=delegation_config,
        openai_client=mock_openai_client,
        session_store=mock_session_store,
    )
    assert agent.is_leaf_agent is True


def test_agent_is_not_leaf_below_max_depth(
    delegation_config, mock_openai_client, mock_session_store
):
    """Agent below max_spawn_depth is an orchestrator."""
    delegation_config["_subagent_depth"] = 1  # < max_spawn_depth=2
    agent = NovaAgent(
        config=delegation_config,
        openai_client=mock_openai_client,
        session_store=mock_session_store,
    )
    assert agent.is_leaf_agent is False


def test_agent_prompt_mode_respected_for_subagent(
    delegation_config, mock_openai_client, mock_session_store
):
    """Sub-agent with prompt_mode='minimal' should produce a minimal prompt."""
    delegation_config["_subagent_depth"] = 1
    delegation_config["skills"]["enabled"] = True
    delegation_config["skills"]["directory"] = str(Path(tempfile.mkdtemp()))

    agent = NovaAgent(
        config=delegation_config,
        openai_client=mock_openai_client,
        session_store=mock_session_store,
        prompt_mode="minimal",
    )

    assert agent._system_prompt is not None
    assert "<skills>" not in (agent._system_prompt or "")
