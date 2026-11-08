from unittest.mock import MagicMock

import pytest

from nova.mlflow_observability import MlflowObservability


def test_trace_parentage_redaction_and_timing():
    api = MagicMock()
    root, child = MagicMock(), MagicMock()
    api.start_span_no_context.side_effect = [root, child]
    observer = MlflowObservability({"capture_input": True, "capture_output": True}, api, "1")
    with observer.run("run-1", "task", session_id="session-1"):
        observer.tool("read", input_data={"api_key": "secret-value"}, duration_ms=250)
        observer.finish_run(status="completed", output="done")
    args = api.start_span_no_context.call_args.kwargs
    assert args["parent_span"] is root
    assert args["inputs"]["api_key"] != "secret-value"
    assert args["start_time_ns"] < child.end.call_args.kwargs["end_time_ns"] - 200_000_000
    root.end.assert_called_once()
    child.end.assert_called_once()


def test_backend_failure_does_not_swallow_application_error():
    api = MagicMock()
    api.start_span_no_context.side_effect = RuntimeError("backend unavailable")
    observer = MlflowObservability({}, api, "1")
    with pytest.raises(ValueError, match="application"), observer.run("run-1", "task"):
        raise ValueError("application")


def test_unsampled_run_has_no_spans():
    api = MagicMock()
    observer = MlflowObservability({"sample_rate": 0}, api, "1")
    with observer.run("run-1", "task"):
        observer.llm("model", input_data="private")
    api.start_span_no_context.assert_not_called()


def test_factory_rejects_conflicting_process_destination(monkeypatch):
    import sys

    from nova import mlflow_observability as module

    api = MagicMock()
    api.set_experiment.return_value.experiment_id = "1"
    monkeypatch.setitem(sys.modules, "mlflow", api)
    monkeypatch.setattr(module, "_destination", None)
    first = module.create_mlflow_observability(
        {"mlflow": {"tracking_uri": "http://localhost:5000"}}
    )
    second = module.create_mlflow_observability(
        {"mlflow": {"tracking_uri": "http://localhost:5001"}}
    )
    assert first.enabled
    assert not second.enabled
    api.set_tracking_uri.assert_called_once_with("http://localhost:5000")
