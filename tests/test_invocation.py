import json
from dataclasses import asdict
from unittest.mock import MagicMock

import pytest

from nova.invocation import NovaOptions, NovaRunResult


def response(text="done", *, calls=None, finish="stop"):
    return {
        "choices": [{"finish_reason": finish, "message": {"content": text, "tool_calls": calls}}]
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_iterations", True),
        ("max_iterations", 0),
        ("max_iterations", 1.5),
        ("max_total_tokens", -1),
        ("max_total_tokens", True),
        ("max_cost_usd", float("nan")),
        ("max_cost_usd", float("inf")),
        ("max_cost_usd", 0),
        ("max_cost_usd", True),
        ("timeout_seconds", -1),
        ("timeout_seconds", float("inf")),
    ],
)
def test_options_reject_invalid_limits(tmp_path, field, value):
    with pytest.raises(ValueError):
        NovaOptions(workspace=tmp_path, **{field: value}).validate("task")


@pytest.mark.parametrize("task", ["", "  ", None])
def test_options_reject_empty_task(tmp_path, task):
    with pytest.raises(ValueError):
        NovaOptions(workspace=tmp_path).validate(task)


def test_options_resolve_workspace_without_mutating_input(tmp_path):
    options = NovaOptions(workspace=tmp_path / ".." / tmp_path.name)
    assert options.validate("task") == tmp_path.resolve()
    assert options.workspace != options.validate("task")
    with pytest.raises(ValueError):
        NovaOptions(workspace=tmp_path / "missing").validate("task")


@pytest.mark.parametrize(
    "text", ["Error: ordinary prose", "[Interrupted]", "[Max iterations (3) reached]"]
)
def test_model_text_never_determines_outcome(agent, text):
    agent._call_llm = MagicMock(return_value=response(text))
    result = agent.run_result("task", stream=False)
    assert isinstance(result, NovaRunResult)
    assert result.status == "completed"
    assert result.stop_reason == "final_response"
    assert result.output == text
    assert result.trace is None
    assert result.verification_status == "completed"
    assert json.loads(json.dumps(result.to_dict())) == asdict(result)


def test_iteration_limit_is_explicit_and_legacy_run_stays_string(agent):
    agent.config["agent"]["max_iterations"] = 1
    calls = [{"id": "c1", "function": {"name": "read_file", "arguments": '{"path":"missing"}'}}]
    agent._call_llm = MagicMock(return_value=response(calls=calls))
    result = agent.run_result("task", stream=False)
    assert (result.status, result.stop_reason) == ("limit_reached", "iteration_limit")
    assert isinstance(agent.run("task", stream=False), str)


def test_pre_request_cancellation_is_not_completion(agent):
    agent._interrupt_check = lambda: True
    agent._call_llm = MagicMock()
    result = agent.run_result("task", stream=False)
    assert (result.status, result.stop_reason) == ("cancelled", "cancelled")
    agent._call_llm.assert_not_called()


def test_length_finish_is_not_completion(agent):
    agent._call_llm = MagicMock(return_value=response("partial", finish="length"))
    result = agent.run_result("task", stream=False)
    assert (result.status, result.stop_reason) == ("limit_reached", "token_limit")
    assert result.output == "partial"


def test_ordered_events_finish_once_and_preserve_partial_failure(agent):
    events = []

    def fail(messages, *, stream_callback, **kwargs):
        stream_callback("partial")
        raise RuntimeError("secret-provider-key")

    agent._call_llm = MagicMock(side_effect=fail)
    result = agent.run_result("task", on_event=events.append)
    assert result.status == "failed"
    assert result.stop_reason == "error"
    assert result.output == "partial"
    assert [event.type for event in events] == ["run_started", "message_delta", "run_finished"]
    assert [event.sequence for event in events] == [1, 2, 3]
    assert all(event.run_id == result.run_id for event in events)
    assert events[-1].result == result
    assert "secret-provider-key" not in json.dumps([event.to_dict() for event in events])
    assert result.usage.complete is False
    assert agent._active_trace is None


def test_event_callback_failure_propagates_without_becoming_provider_error(agent):
    events = []
    failure = RuntimeError("connection timeout in caller")

    def on_event(event):
        events.append(event)
        if event.type == "message_delta":
            raise failure

    def complete(messages, *, stream_callback, **kwargs):
        stream_callback("answer")
        return response("answer")

    agent._call_llm = MagicMock(side_effect=complete)
    with pytest.raises(RuntimeError) as raised:
        agent.run_result("task", on_event=on_event)
    assert raised.value is failure
    assert [event.type for event in events] == ["run_started", "message_delta"]
    assert agent._active_trace is None
    assert agent._active_events is None
    assert agent._offered_tool_names is None


def test_tool_events_omit_arguments_and_results(agent, tmp_path):
    path = tmp_path / "secret.txt"
    path.write_text("confidential fixture")
    calls = [
        {
            "id": "c1",
            "function": {"name": "read_file", "arguments": json.dumps({"path": str(path)})},
        }
    ]
    agent._call_llm = MagicMock(side_effect=[response(calls=calls), response()])
    events = []
    result = agent.run_result("task", stream=False, on_event=events.append)
    tool_events = [event.to_dict() for event in events if event.type.startswith("tool_")]
    assert [event["type"] for event in tool_events] == ["tool_started", "tool_finished"]
    assert all(event["call_id"] == "c1" for event in tool_events)
    assert "confidential" not in json.dumps(tool_events)
    assert str(path) not in json.dumps(tool_events)
    assert result.trace is None
    assert events[-1].type == "run_finished"


def test_result_trace_requires_explicit_opt_in(agent):
    agent._call_llm = MagicMock(return_value=response())
    result = agent.run_result("task", stream=False, include_trace=True)
    assert result.trace["run_id"] == result.run_id
    assert result.session_id == agent.session_id


def test_usage_is_per_turn_and_unknown_pricing_is_not_zero(agent):
    def complete(*args, **kwargs):
        agent.cost_tracker.add_usage(input_tokens=10, output_tokens=4, cache_read_tokens=2)
        return response()

    agent.cost_tracker.add_usage(input_tokens=1000, output_tokens=300, total_cost=5)
    agent._call_llm = MagicMock(side_effect=complete)
    first = agent.run_result("first", stream=False)
    second = agent.run_result("second", stream=False)
    assert first.run_id != second.run_id
    for result in (first, second):
        assert result.usage.input_tokens == 10
        assert result.usage.output_tokens == 4
        assert result.usage.cache_read_tokens == 2
        assert result.usage.cost_usd is None
        assert result.usage.cost_source == "unknown"


def test_reported_usage_includes_child_work(agent):
    from nova.cost_tracker import CostTracker

    def complete(*args, **kwargs):
        child = CostTracker()
        child.add_usage(input_tokens=3, output_tokens=2, total_cost=0.2)
        agent.cost_tracker.add_usage(input_tokens=10, output_tokens=4, total_cost=0.1)
        agent.cost_tracker.merge(child.total)
        return response()

    agent._call_llm = MagicMock(side_effect=complete)
    result = agent.run_result("task", stream=False)
    assert result.usage.input_tokens == 13
    assert result.usage.output_tokens == 6
    assert result.usage.cost_usd == pytest.approx(0.3)
    assert result.usage.cost_source == "reported"


def test_real_stream_interruption_has_typed_cancelled_result(agent):
    from tests.test_agent_streaming import make_mock_stream, make_text_chunk

    cancelled = False
    events = []

    def on_event(event):
        nonlocal cancelled
        events.append(event)
        if event.type == "message_delta":
            cancelled = True

    agent._interrupt_check = lambda: cancelled
    agent.client.chat.completions.create.return_value = make_mock_stream(
        make_text_chunk("partial"),
        make_text_chunk("not delivered"),
    )
    result = agent.run_result("task", on_event=on_event)
    assert result.status == "cancelled"
    assert result.output == "partial"
    assert result.usage.complete is False
    assert [event.type for event in events] == ["run_started", "message_delta", "run_finished"]


def test_real_stream_callback_error_is_not_retried(agent):
    from tests.test_agent_streaming import make_mock_stream, make_text_chunk

    agent.client.chat.completions.create.return_value = make_mock_stream(make_text_chunk("text"))

    def on_event(event):
        if event.type == "message_delta":
            raise RuntimeError("connection reset in caller")

    with pytest.raises(RuntimeError, match="connection reset in caller"):
        agent.run_result("task", on_event=on_event)
    assert agent.client.chat.completions.create.call_count == 1
    assert agent._active_trace is None


def test_missing_provider_usage_is_marked_incomplete(agent):
    from tests.conftest import make_openai_response

    completion = make_openai_response("done")
    completion.usage = None
    agent.client.chat.completions.create.return_value = completion
    result = agent.run_result("task", stream=False)
    assert result.status == "completed"
    assert result.usage.complete is False
    assert result.usage.cost_usd is None


def test_mixed_reported_and_estimated_costs_are_both_counted(agent):
    from unittest.mock import patch

    from nova.model_metadata import ModelMetadata

    def complete(*args, **kwargs):
        agent.cost_tracker.add_usage(input_tokens=10, output_tokens=4, total_cost=0.1)
        agent.cost_tracker.add_usage(input_tokens=1_000_000, output_tokens=0)
        return response()

    agent._call_llm = MagicMock(side_effect=complete)
    with patch(
        "nova.cost_tracker.get_model_metadata",
        return_value=ModelMetadata(
            input_price_per_million=2,
            output_price_per_million=4,
        ),
    ):
        result = agent.run_result("task", stream=False)
    assert result.usage.cost_usd == pytest.approx(2.1)
    assert result.usage.cost_source == "estimated"


def test_result_verification_is_separate_from_completion(agent):
    calls = [{"id": "c1", "function": {"name": "nonexistent_tool", "arguments": "{}"}}]
    agent._call_llm = MagicMock(side_effect=[response(calls=calls), response("Could not do that")])
    result = agent.run_result("task", stream=False)
    assert result.status == "completed"
    assert result.verification_status == "failed"


def test_terminal_event_callback_error_is_not_delivered_twice(agent):
    events = []

    def on_event(event):
        events.append(event)
        if event.type == "run_finished":
            raise ValueError("consumer failed")

    agent._call_llm = MagicMock(return_value=response())
    with pytest.raises(ValueError, match="consumer failed"):
        agent.run_result("task", stream=False, on_event=on_event)
    assert [event.type for event in events] == ["run_started", "run_finished"]
    assert agent._active_events is None


def test_interrupted_tool_at_last_iteration_is_cancellation(agent):
    from nova.tools.result import ToolResult

    agent.config["agent"]["max_iterations"] = 1
    calls = [{"id": "c1", "function": {"name": "read_file", "arguments": "{}"}}]
    agent._call_llm = MagicMock(return_value=response(calls=calls))
    agent.tool_executor.run_batch = MagicMock(return_value=[ToolResult.interrupted()])
    result = agent.run_result("task", stream=False)
    assert (result.status, result.stop_reason) == ("cancelled", "cancelled")
    assert agent.messages[-1]["role"] == "tool"


def test_provider_retry_cannot_report_complete_usage(agent):
    from tests.conftest import make_openai_response

    agent.config["retry"] = {"base_delay": 0, "max_delay": 0, "max_retries": 1}
    agent.client.chat.completions.create.side_effect = [
        RuntimeError("rate limit"),
        make_openai_response("done"),
    ]
    result = agent.run_result("task", stream=False)
    assert result.status == "completed"
    assert result.usage.input_tokens == 10
    assert result.usage.output_tokens == 5
    assert result.usage.complete is False
    assert result.usage.cost_usd is None


def test_context_overflow_retry_failure_has_provider_error_code(agent):
    agent._call_llm = MagicMock(
        side_effect=[RuntimeError("context length"), RuntimeError("bad request")]
    )
    result = agent.run_result("task", stream=False)
    assert result.error.code == "provider_error"


def test_transient_provider_errors_are_retryable(agent):
    agent._call_llm = MagicMock(side_effect=TimeoutError("private endpoint timed out"))
    result = agent.run_result("task", stream=False)
    assert result.error.retryable is True
    assert "private endpoint" not in result.error.message


def test_huge_numeric_limit_is_a_validation_error(tmp_path):
    with pytest.raises(ValueError):
        NovaOptions(workspace=tmp_path, timeout_seconds=10**1000).validate("task")


def test_nonstream_findings_survive_a_later_provider_failure(agent):
    calls = [{"id": "c1", "function": {"name": "nonexistent_tool", "arguments": "{}"}}]
    agent._call_llm = MagicMock(
        side_effect=[response("Important partial findings", calls=calls), RuntimeError("failed")]
    )
    result = agent.run_result("task", stream=False)
    assert result.status == "failed"
    assert result.output == "Important partial findings"


def test_keyboard_interrupt_marks_request_usage_incomplete(agent):
    from tests.conftest import make_tool_call_response

    events = []
    agent.client.chat.completions.create.side_effect = [
        make_tool_call_response("nonexistent_tool", {}),
        KeyboardInterrupt(),
    ]
    with pytest.raises(KeyboardInterrupt):
        agent.run_result("task", stream=False, on_event=events.append)
    result = agent.last_run_result
    assert result.status == "cancelled"
    assert result.usage.complete is False
    assert result.usage.cost_usd is None
    assert events[-1].result == result


def test_partial_stream_context_error_does_not_retry_or_duplicate_events(agent):
    events = []

    def fail(messages, *, stream_callback, **kwargs):
        stream_callback("partial")
        raise RuntimeError("context length exceeded")

    agent._call_llm = MagicMock(side_effect=fail)
    result = agent.run_result("task", on_event=events.append)
    assert result.status == "failed"
    assert result.output == "partial"
    assert agent._call_llm.call_count == 1
    assert [event.delta for event in events if event.type == "message_delta"] == ["partial"]
