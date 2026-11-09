import json
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from nova import NovaOptions, run_task
from nova.session import SessionStore
from tests.conftest import make_openai_response, make_tool_call_response


@pytest.fixture
def bounded(monkeypatch, tmp_path, minimal_config, mock_openai_client):
    minimal_config["session"]["directory"] = str(tmp_path / "sessions")
    minimal_config["permissions"] = {"mode": "ask"}
    minimal_config["retry"] = {"max_retries": 0}
    monkeypatch.setattr("nova.config.load_config", lambda *args, **kwargs: minimal_config)
    monkeypatch.setattr("nova.agent.ensure_nova_home", lambda: tmp_path)
    monkeypatch.setattr("nova.agent.build_client", lambda *_: mock_openai_client)
    mock_openai_client.chat.completions.create.return_value = make_openai_response("done")
    return NovaOptions(workspace=tmp_path, timeout_seconds=5), mock_openai_client, minimal_config


def test_run_task_fresh_sessions_resume_and_model_override(bounded):
    options, client, config = bounded
    first = run_task("first", options=options)
    second = run_task("second", options=options)
    assert first.status == second.status == "completed"
    assert first.session_id != second.session_id
    resumed = run_task(
        "third", options=replace(options, session_id=first.session_id, model="other")
    )
    assert resumed.session_id == first.session_id
    assert client.chat.completions.create.call_args.kwargs["model"] == "other"
    assert config["llm"]["model"] == "test-model"
    store = SessionStore(Path(config["session"]["directory"]) / "sessions.db")
    assert store.get_session_info(first.session_id)["workspace"] == str(options.workspace.resolve())


def test_invalid_unknown_and_workspace_mismatch_fail_before_provider(bounded):
    options, client, config = bounded
    invalid = run_task("", options=options)
    unknown = run_task("task", options=replace(options, session_id="missing"))
    assert invalid.error.code == "invalid_options"
    assert unknown.error.code == "session_not_found"
    assert not (Path(config["session"]["directory"]) / "sessions.db").exists()
    client.chat.completions.create.assert_not_called()
    first = run_task("first", options=options)
    other = options.workspace / "other"
    other.mkdir()
    db = Path(config["session"]["directory"]) / "sessions.db"
    before = db.read_bytes()
    mismatch = run_task(
        "task", options=replace(options, session_id=first.session_id, workspace=other)
    )
    assert mismatch.error.code == "workspace_mismatch"
    assert db.read_bytes() == before


def test_permission_required_stops_and_preserves_tool_pairing(bounded, monkeypatch):
    options, client, config = bounded
    monkeypatch.setattr("builtins.input", lambda *_: pytest.fail("stdin was read"))
    target = options.workspace / "created.txt"
    client.chat.completions.create.return_value = make_tool_call_response(
        "write_file", {"path": str(target), "content": "new"}
    )
    result = run_task("write", options=options)
    assert (result.status, result.stop_reason) == ("blocked", "permission_required")
    assert not target.exists()
    assert client.chat.completions.create.call_count == 1
    store = SessionStore(Path(config["session"]["directory"]) / "sessions.db")
    messages = store.get_messages(result.session_id)
    assert messages[-1]["role"] == "tool"
    assert messages[-1]["tool_call_id"] == "call_1"


def test_callback_exception_propagates_after_close_and_lock_release(bounded):
    options, client, config = bounded
    session = []

    def event_callback(event):
        if event.type == "run_started":
            session.append(event.session_id)
            raise ValueError("caller failed")

    with pytest.raises(ValueError, match="caller failed"):
        run_task("task", options=options, on_event=event_callback)
    assert client.close.called
    result = run_task("again", options=replace(options, session_id=session[0]))
    assert result.status == "completed"


def test_cancelled_before_start_makes_no_provider_request(bounded):
    options, client, _ = bounded
    cancel = threading.Event()
    cancel.set()
    result = run_task("task", options=options, cancel_event=cancel)
    assert result.status == "cancelled"
    client.chat.completions.create.assert_not_called()


def test_missing_pricing_and_insufficient_tokens_stop_before_provider(bounded):
    options, client, _ = bounded
    result = run_task("task", options=replace(options, max_cost_usd=1))
    assert result.error.code == "usage_unavailable"
    result = run_task("task", options=replace(options, max_total_tokens=1))
    assert result.stop_reason == "token_limit"
    client.chat.completions.create.assert_not_called()


def test_unknown_usage_under_token_limit_stops_after_first_response(bounded):
    options, client, _ = bounded
    completion = make_openai_response("done")
    completion.usage = None
    client.chat.completions.create.return_value = completion
    result = run_task("task", options=replace(options, max_total_tokens=100000))
    assert result.error.code == "usage_unavailable"
    assert result.usage.complete is False
    assert client.chat.completions.create.call_count == 1


def test_terminal_result_delivered_once_after_cleanup(bounded):
    options, client, _ = bounded
    events = []

    def callback(event):
        events.append(event)
        if event.type == "run_finished":
            assert client.close.called

    result = run_task("task", options=options, on_event=callback)
    assert events[-1].result == result
    assert sum(event.type == "run_finished" for event in events) == 1
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    json.dumps(result.to_dict())


def test_session_writer_is_cross_process_and_released_on_crash(bounded):
    options, _, config = bounded
    first = run_task("first", options=options)
    db = Path(config["session"]["directory"]) / "sessions.db"
    script = "from nova.session import SessionStore; from pathlib import Path; import sys,time\nwith SessionStore.writer(Path(sys.argv[1]),sys.argv[2]):\n print('locked',flush=True)\n time.sleep(30)"
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(db), first.session_id], stdout=subprocess.PIPE, text=True
    )
    try:
        assert process.stdout.readline().strip() == "locked"
        busy = run_task("again", options=replace(options, session_id=first.session_id))
        assert busy.error.code == "session_busy"
    finally:
        process.kill()
        process.wait(timeout=5)
    assert (
        run_task("again", options=replace(options, session_id=first.session_id)).status
        == "completed"
    )


def test_bounded_terminal_cancels_and_cleans_up_descendants(bounded):
    options, client, _ = bounded
    marker = options.workspace / "escaped"
    command = f"(sleep 0.5; echo escaped > '{marker}') & wait"
    client.chat.completions.create.return_value = make_tool_call_response(
        "terminal", {"command": command}
    )
    cancel = threading.Event()
    timer = threading.Timer(0.15, cancel.set)
    timer.start()
    started = time.monotonic()
    try:
        result = run_task("task", options=options, confirm=lambda *_: True, cancel_event=cancel)
    finally:
        timer.join()
    assert result.status == "cancelled"
    assert time.monotonic() - started < 1
    time.sleep(0.55)
    assert not marker.exists()


def test_bounded_terminal_kills_background_children_after_normal_exit(bounded):
    options, client, _ = bounded
    marker = options.workspace / "escaped"
    client.chat.completions.create.side_effect = [
        make_tool_call_response(
            "terminal", {"command": f"(sleep 0.3; echo escaped > '{marker}') & echo done"}
        ),
        make_openai_response("done"),
    ]
    result = run_task("task", options=options, confirm=lambda *_: True)
    assert result.status == "completed"
    time.sleep(0.4)
    assert not marker.exists()


def test_timeout_reaches_provider_and_retry_waits(bounded):
    options, client, config = bounded
    config["retry"] = {"max_retries": 3, "base_delay": 5}
    client.chat.completions.create.side_effect = RuntimeError("rate limit")
    start = time.monotonic()
    result = run_task("task", options=replace(options, timeout_seconds=0.15))
    assert (result.status, result.stop_reason) == ("limit_reached", "timeout")
    assert time.monotonic() - start < 1
    assert client.chat.completions.create.call_count == 1
    assert 0 < client.chat.completions.create.call_args.kwargs["timeout"] <= 0.15


def test_invocation_admission_reduces_output(bounded):
    from nova.tokens import estimate_total_request_tokens

    options, client, config = bounded
    run_task("probe", options=options)
    kwargs = client.chat.completions.create.call_args.kwargs
    prompt_tokens = estimate_total_request_tokens(
        kwargs["messages"][:-1], tools=kwargs.get("tools")
    )
    client.chat.completions.create.reset_mock()
    result = run_task("probe", options=replace(options, max_total_tokens=prompt_tokens + 8))
    assert result.status == "completed"
    assert client.chat.completions.create.call_args.kwargs["max_tokens"] == 8


def test_unsupported_background_tool_is_explicitly_unavailable(bounded):
    options, client, _ = bounded
    client.chat.completions.create.side_effect = [
        make_tool_call_response("task_create", {"command": "sleep 10"}),
        make_openai_response("done"),
    ]
    result = run_task("task", options=replace(options, include_trace=True))
    tool = result.trace["tool_traces"][0]
    assert "unavailable in bounded invocations" in tool["result_preview"]
    assert result.verification_status == "failed"


def test_deadline_expiry_during_provider_failure_is_a_timeout(bounded):
    options, client, _ = bounded

    def fail(**kwargs):
        time.sleep(0.12)
        raise TimeoutError("request timed out")

    client.chat.completions.create.side_effect = fail
    result = run_task("task", options=replace(options, timeout_seconds=0.08))
    assert (result.status, result.stop_reason) == ("limit_reached", "timeout")


def test_nested_work_charges_shared_budget_and_preserves_confirmation_errors(bounded, monkeypatch):
    options, client, config = bounded
    config["delegation"] = {"enabled": True, "max_spawn_depth": 2}
    monkeypatch.setattr("nova.tools.delegate_tool.build_client", lambda *_: client)
    client.chat.completions.create.side_effect = [
        make_tool_call_response("delegate_task", {"task": "child"}),
        make_openai_response("child done"),
        make_openai_response("parent done"),
    ]
    result = run_task("task", options=options, confirm=lambda *_: True)
    assert result.status == "completed"
    assert result.usage.input_tokens == 30
    assert result.usage.output_tokens == 15
    client.chat.completions.create.reset_mock()
    target = options.workspace / "must-not-exist"
    client.chat.completions.create.side_effect = [
        make_tool_call_response("delegate_task", {"task": "child"}),
        make_tool_call_response("write_file", {"path": str(target), "content": "bad"}),
        make_openai_response("must not continue"),
    ]

    def confirmation(name, arguments):
        if name == "write_file":
            raise ValueError("callback failed")
        return True

    with pytest.raises(ValueError, match="callback failed"):
        run_task("task", options=options, confirm=confirmation)
    assert not target.exists()
    assert client.chat.completions.create.call_count == 2


def test_token_limited_failed_retry_stops_with_usage_unavailable(bounded):
    options, client, config = bounded
    config["retry"] = {"max_retries": 3, "base_delay": 0}
    client.chat.completions.create.side_effect = RuntimeError("rate limit")
    result = run_task("task", options=replace(options, max_total_tokens=100000))
    assert result.error.code == "usage_unavailable"
    assert client.chat.completions.create.call_count == 1


def test_known_cost_cap_reduces_output_and_reports_actual_usage(bounded):
    options, client, _ = bounded
    client.models.list.return_value = {
        "data": [{"id": "test-model", "pricing": {"prompt": "0.000002", "completion": "0.000004"}}]
    }
    from nova.tokens import estimate_total_request_tokens

    run_task("probe", options=options)
    kwargs = client.chat.completions.create.call_args.kwargs
    prompt = estimate_total_request_tokens(kwargs["messages"][:-1], tools=kwargs.get("tools"))
    result = run_task(
        "probe", options=replace(options, max_cost_usd=prompt * 0.000002 + 8 * 0.000004)
    )
    assert result.status == "completed"
    assert 1 <= client.chat.completions.create.call_args.kwargs["max_tokens"] <= 8
    assert result.usage.cost_source == "estimated"
    assert result.usage.cost_usd == pytest.approx(10 * 0.000002 + 5 * 0.000004)


def test_explicitly_rejected_confirmation_stops_without_more_requests(bounded):
    options, client, _ = bounded
    client.chat.completions.create.return_value = make_tool_call_response(
        "write_file", {"path": "rejected", "content": "bad"}
    )
    result = run_task("task", options=options, confirm=lambda *_: False)
    assert (result.status, result.stop_reason) == ("blocked", "permission_required")
    assert client.chat.completions.create.call_count == 1


@pytest.mark.parametrize("cost", [float("nan"), float("inf"), -1, True, "bad"])
def test_malformed_provider_cost_cannot_evade_cap(tmp_path, cost):
    from nova.invocation import InvocationControl, _StopInvocation

    control = InvocationControl(NovaOptions(workspace=tmp_path, max_cost_usd=1))
    control.record("fixture", {"usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": cost}})
    with pytest.raises(_StopInvocation) as stopped:
        control.checkpoint()
    assert stopped.value.error.code == "usage_unavailable"
    assert control.usage().cost_usd is None
    json.dumps(control.usage().__dict__, allow_nan=False)


def test_choice_less_stream_chunks_still_check_deadline(bounded):
    from nova.invocation import InvocationControl, _StopInvocation
    from nova.providers import stream_response

    options, client, _ = bounded
    control = InvocationControl(replace(options, timeout_seconds=0.05))

    class Stream:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def __iter__(self):
            for _ in range(10):
                time.sleep(0.02)
                yield type("Chunk", (), {"choices": [], "usage": None})()

    client.chat.completions.create.return_value = Stream()
    with pytest.raises(_StopInvocation) as stopped:
        stream_response(
            client, {"model": "fixture", "messages": []}, interrupt_check=control.interrupted
        )
    assert stopped.value.reason == "timeout"


def test_nonfinite_tool_arguments_never_enter_wire_trace(bounded):
    options, client, _ = bounded
    call = make_tool_call_response("terminal", {"command": "echo test", "timeout": float("nan")})
    client.chat.completions.create.side_effect = [call, make_openai_response("done")]
    result = run_task("task", options=replace(options, include_trace=True), confirm=lambda *_: True)
    assert result.trace["tool_traces"][0]["arguments"] == {}
    json.dumps(result.to_dict(), allow_nan=False)


def test_cleanup_error_does_not_hide_cancellation(bounded):
    options, client, _ = bounded
    cancel = threading.Event()

    def completion(**kwargs):
        cancel.set()
        return make_openai_response("partial")

    client.chat.completions.create.side_effect = completion
    client.close.side_effect = RuntimeError("close failed")
    result = run_task("task", options=options, cancel_event=cancel)
    assert result.status == "cancelled"


def test_null_reported_cost_still_estimates_output_tokens(bounded):
    options, client, _ = bounded
    client.models.list.return_value = {
        "data": [{"id": "test-model", "pricing": {"prompt": "0.000002", "completion": "0.000004"}}]
    }
    completion = make_openai_response("done")
    completion.usage.model_dump.return_value = {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "cost": None,
    }
    client.chat.completions.create.return_value = completion
    result = run_task("task", options=options)
    assert result.usage.cost_usd == pytest.approx(0.00004)


def test_backend_failure_after_start_preserves_terminal_sequence(bounded, monkeypatch):
    from contextlib import nullcontext

    options, client, _ = bounded
    observer = type(
        "Observer",
        (),
        {
            "run": lambda *args, **kwargs: nullcontext(),
            "finish_run": lambda *args, **kwargs: (_ for _ in ()).throw(
                RuntimeError("backend failed")
            ),
            "llm": lambda *args, **kwargs: None,
            "shutdown": lambda *args, **kwargs: None,
        },
    )()
    monkeypatch.setattr("nova.agent.create_observability", lambda *_: observer)
    events = []
    result = run_task("task", options=options, on_event=events.append)
    assert result.status == "failed"
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert result.run_id == events[0].run_id == events[-1].run_id
    assert result.run_id is not None


def test_parent_and_child_hold_writer_lock_before_session_initialization(bounded, monkeypatch):
    from nova.session import SessionBusyError

    options, client, config = bounded
    config["delegation"] = {"enabled": True, "max_spawn_depth": 2}
    monkeypatch.setattr("nova.tools.delegate_tool.build_client", lambda *_: client)
    create_session = SessionStore.create_session
    created = []

    def checked_create(store, *args, **kwargs):
        session = create_session(store, *args, **kwargs)
        created.append(session)
        with pytest.raises(SessionBusyError), SessionStore.writer(store.db_path, session):
            pass
        return session

    monkeypatch.setattr(SessionStore, "create_session", checked_create)
    client.chat.completions.create.side_effect = [
        make_tool_call_response("delegate_task", {"task": "child"}),
        make_openai_response("child"),
        make_openai_response("parent"),
    ]
    assert run_task("task", options=options, confirm=lambda *_: True).status == "completed"
    assert len(created) == 2


def test_bounded_pricing_uses_its_provider_snapshot(bounded, monkeypatch):
    from nova.model_metadata import ModelMetadata

    options, client, _ = bounded
    prices = ModelMetadata(input_price_per_million=2, output_price_per_million=4)
    foreign = ModelMetadata(input_price_per_million=1e9, output_price_per_million=1e9)
    monkeypatch.setattr("nova.agent.load_provider_metadata", lambda *_: {"test-model": prices})
    monkeypatch.setattr("nova.model_metadata.get_model_metadata", lambda *args, **kwargs: foreign)
    monkeypatch.setattr("nova.cost_tracker.get_model_metadata", lambda *_: foreign)
    result = run_task("task", options=replace(options, max_cost_usd=1))
    assert result.status == "completed"
    assert result.usage.cost_usd == pytest.approx(0.00004)
