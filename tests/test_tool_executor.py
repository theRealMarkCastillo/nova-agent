"""Tests for the tool execution pipeline and typed tool results."""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from openai import OpenAI

from nova.agent import NovaAgent
from nova.harness import HarnessTrace
from nova.session import SessionStore
from nova.tools.registry import discover_builtin_tools, registry
from nova.tools.result import INTERRUPTED_MESSAGE, ToolResult


@pytest.fixture
def agent(tmp_path: Path) -> NovaAgent:
    discover_builtin_tools()
    config = {
        "llm": {"base_url": "https://example.invalid/v1", "api_key": "k", "model": "m"},
        "agent": {"max_iterations": 3, "temperature": 0.7, "top_p": 1.0, "identity": "test"},
        "budgets": {"conversation_turn_limit": 5, "system_prompt_max": 8000},
        "wiki": {"enabled": False},
        "session": {"directory": tempfile.mkdtemp()},
        "skills": {"enabled": False},
        "context_files": [],
    }
    return NovaAgent(
        config=config,
        openai_client=MagicMock(spec=OpenAI),
        session_store=SessionStore(tmp_path / "sessions.db"),
        workspace=tmp_path,
    )


def _call(name: str, arguments: dict | None = None, call_id: str = "c1") -> dict:
    return {"id": call_id, "function": {"name": name, "arguments": json.dumps(arguments or {})}}


class TestToolResultFromOutput:
    def test_plain_output_is_completed(self):
        assert ToolResult.from_output("done") == ToolResult("completed", "done")

    def test_error_prefix_is_failed_and_classified(self):
        transient = ToolResult.from_output("Error: Connection reset by peer")
        permanent = ToolResult.from_output("Error: File not found: connection.py")
        assert (transient.status, transient.retryable) == ("failed", True)
        assert (permanent.status, permanent.retryable) == ("failed", False)

    def test_tool_result_passes_through(self):
        result = ToolResult("failed", "partial", retryable=True)
        assert ToolResult.from_output(result) is result

    def test_non_string_output_is_stringified(self):
        assert ToolResult.from_output(42).content == "42"


def test_handler_can_return_typed_result(agent: NovaAgent):
    calls = []

    def handler(args, **kwargs):
        calls.append(args)
        return ToolResult("failed", "upstream busy, partial data", retryable=True)

    registry.register(
        name="test_typed_tool",
        toolset="test",
        schema={"name": "test_typed_tool", "parameters": {"type": "object"}},
        handler=handler,
        is_read_only=True,
    )
    agent.config["agent"]["tool_retry_max_attempts"] = 1

    with patch.object(agent.tool_executor, "_wait_unless_interrupted", return_value=False):
        result = agent.tool_executor.run(_call("test_typed_tool"))

    assert result == ToolResult("failed", "upstream busy, partial data", retryable=True)
    assert len(calls) == 2  # retried on the typed retryable flag, no text matching


def test_lifecycle_status_comes_from_result_not_text(agent: NovaAgent, tmp_path: Path):
    (tmp_path / "log.txt").write_text(f"{INTERRUPTED_MESSAGE}\nrequires confirmation\n")
    events = []
    agent._tool_lifecycle_callback = lambda cid, name, status, content: events.append(status)

    agent.tool_executor.run_batch([_call("read_file", {"path": "log.txt"})])

    assert events == ["start", "completed"]


def test_each_call_emits_one_tool_and_one_verification_event(agent: NovaAgent, tmp_path: Path):
    (tmp_path / "a.txt").write_text("a")
    agent.observability = MagicMock()

    agent.tool_executor.run(_call("read_file", {"path": "a.txt"}))

    assert agent.observability.tool.call_count == 1
    assert agent.observability.verification.call_count == 1
    assert agent.observability.policy.call_count == 1
    assert agent.observability.tool.call_args.kwargs["status"] == "completed"


@pytest.mark.parametrize(
    ("tool_call", "status", "error_type"),
    [
        (
            {"id": "x", "function": {"name": "read_file", "arguments": "{bad"}},
            "failed",
            "invalid_arguments",
        ),
        (_call("no_such_tool"), "failed", "unknown_tool"),
    ],
)
def test_rejected_calls_are_typed(agent: NovaAgent, tool_call, status, error_type):
    result = agent.tool_executor.run(tool_call)
    assert (result.status, result.error_type) == (status, error_type)


def test_interrupted_call_is_traced_as_inconclusive(agent: NovaAgent):
    agent._interrupt_check = lambda: True
    agent._active_trace = HarnessTrace("run", "goal")

    result = agent.tool_executor.run(_call("read_file", {"path": "a.txt"}))

    trace = agent._active_trace.run.tool_traces[0]
    assert result.status == "interrupted"
    assert trace.outcome == "failed"
    assert trace.verification is not None
    assert trace.verification.status == "inconclusive"


def test_skip_batch_records_interrupted_calls(agent: NovaAgent):
    agent._active_trace = HarnessTrace("run", "goal")
    events = []
    agent._tool_lifecycle_callback = lambda cid, name, status, content: events.append(status)

    results = agent.tool_executor.skip_batch([_call("write_file", call_id="w")])

    assert [r.status for r in results] == ["interrupted"]
    assert events == ["start", "failed"]
    assert agent._active_trace.run.tool_traces[0].verification.status == "inconclusive"
