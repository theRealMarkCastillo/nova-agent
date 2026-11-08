from __future__ import annotations

import contextvars
import logging
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from nova.observability import NoOpObservability, redact, should_sample

logger = logging.getLogger(__name__)
_destination_lock = threading.Lock()
_destination: str | None = None


class MlflowObservability(NoOpObservability):
    enabled = True

    def __init__(self, config: dict, api: Any, experiment_id: str) -> None:
        self.config = config
        self.api = api
        self.experiment_id = experiment_id
        self._closed = False
        self._root: contextvars.ContextVar[Any] = contextvars.ContextVar(
            "mlflow_root", default=None
        )

    @contextmanager
    def run(self, run_id: str, goal: str, *, session_id: str | None = None) -> Iterator[Any]:
        root = None
        if should_sample(float(self.config.get("sample_rate", 1.0))):
            try:
                root = self.api.start_span_no_context(
                    name="nova.run",
                    span_type="AGENT",
                    experiment_id=self.experiment_id,
                    inputs=redact(goal) if self.config.get("capture_input") else None,
                    attributes={"nova.run_id": run_id, "nova.session_id": session_id or ""},
                )
            except Exception as exc:
                logger.warning("MLflow trace start failed: %s", type(exc).__name__)
        token = self._root.set(root)
        try:
            yield root
        except BaseException as exc:
            self.finish_run(status="failed", error=exc)
            raise
        finally:
            self._root.reset(token)
            if root is not None:
                try:
                    root.end()
                except Exception as exc:
                    logger.warning("MLflow trace end failed: %s", type(exc).__name__)

    def finish_run(self, *, status: str, output: Any = None, error: Any = None) -> None:
        root = self._root.get()
        if root is None:
            return
        try:
            root.set_attribute("nova.status", status)
            if error is not None:
                root.set_attribute("nova.error_type", type(error).__name__)
                root.set_status("ERROR")
            if self.config.get("capture_output"):
                root.set_outputs(redact(output))
        except Exception as exc:
            logger.warning("MLflow trace update failed: %s", type(exc).__name__)

    def _event(
        self, kind: str, name: str, input_data: Any = None, output_data: Any = None, **kwargs: Any
    ) -> None:
        root = self._root.get()
        if root is None:
            return
        try:
            ended = time.time_ns()
            duration = max(0, float(kwargs.get("duration_ms", 0)))
            if not self.config.get("capture_output"):
                for key in ("result", "output", "evidence", "reason"):
                    kwargs.pop(key, None)
            span = self.api.start_span_no_context(
                name=name,
                span_type=kind,
                parent_span=root,
                start_time_ns=ended - int(duration * 1_000_000),
                inputs=redact(input_data) if self.config.get("capture_input") else None,
                attributes=redact(kwargs),
            )
            try:
                if self.config.get("capture_output"):
                    span.set_outputs(redact(output_data))
            finally:
                span.end(end_time_ns=ended)
        except Exception as exc:
            logger.warning("MLflow event failed: %s", type(exc).__name__)

    def llm(
        self, model: str, *, input_data: Any = None, output_data: Any = None, **kwargs: Any
    ) -> None:
        self._event("LLM", "nova.llm", input_data, output_data, model=model, **kwargs)

    def tool(
        self, name: str, *, input_data: Any = None, output_data: Any = None, **kwargs: Any
    ) -> None:
        self._event("TOOL", name, input_data, output_data, **kwargs)

    def policy(self, name: str, *, allowed: bool, **kwargs: Any) -> None:
        self._event("GUARDRAIL", f"policy:{name}", allowed=allowed, **kwargs)

    def verification(self, name: str, *, status: str, **kwargs: Any) -> None:
        self._event("EVALUATOR", f"verification:{name}", status=status, **kwargs)

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True

        def flush() -> None:
            try:
                self.api.flush_trace_async_logging()
            except Exception as exc:
                logger.warning("MLflow flush failed: %s", type(exc).__name__)

        worker = threading.Thread(target=flush, daemon=True)
        worker.start()
        worker.join(timeout=2.0)
        if worker.is_alive():
            logger.warning("MLflow flush exceeded shutdown wait; export may be incomplete")


def create_mlflow_observability(settings: dict) -> NoOpObservability:
    try:
        import mlflow

        config = settings.get("mlflow", {})
        global _destination
        uri = config.get("tracking_uri", "http://127.0.0.1:5000")
        with _destination_lock:
            if _destination is not None and _destination != uri:
                raise ValueError("MLflow supports one tracking destination per process")
            mlflow.set_tracking_uri(uri)
            experiment = mlflow.set_experiment(config.get("experiment", "nova-agent"))
            _destination = uri
        return MlflowObservability(settings, mlflow, experiment.experiment_id)
    except Exception as exc:
        logger.warning("MLflow initialization failed: %s", type(exc).__name__)
        return NoOpObservability()
