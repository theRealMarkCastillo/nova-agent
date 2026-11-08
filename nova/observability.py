"""Optional, failure-isolated Langfuse observability."""

from __future__ import annotations

import contextlib
import contextvars
import logging
import os
import random
import re
from collections.abc import Callable, Iterator
from typing import Any

logger = logging.getLogger(__name__)
_MAX_PREVIEW = 1000
_MAX_AGGREGATE = 4000
_MAX_DEPTH = 8
_MAX_ITEMS = 100
_SECRET_KEYS = {
    "apikey",
    "apitoken",
    "authtoken",
    "secretkey",
    "password",
    "token",
    "authorization",
    "accesstoken",
    "accesskey",
    "clientsecret",
    "privatekey",
    "cookie",
    "setcookie",
    "credential",
    "credentials",
    "xapikey",
    "xapitoken",
    "xauthtoken",
    "xaccesstoken",
    "xsecret",
    "xsecretkey",
    "proxyauthorization",
    "sessiontoken",
    "refreshtoken",
    "csrftoken",
    "idtoken",
    "jwt",
}
_BEARER_VALUE = re.compile(
    r"^(?P<prefix>\s*Bearer\s+)(?P<token>\S+)(?P<suffix>\s*)$", re.IGNORECASE
)
_SECRET_TEXT = re.compile(
    r"(?P<key>\b(?:api[_-]?(?:key|token)|token|auth(?:[_-]?token)?|"
    r"access(?:[_-]?(?:key|token))?|x[-_]?(?:api[-_]?key|api[-_]?token|auth[-_]?token)|"
    r"password|secret(?:[_-]?key)?|client[_-]?secret|private[_-]?key|authorization|"
    r"cookie|credential(?:s)?))\b"
    r"(?P<key_quote>[\"']?)(?P<separator>\s*[:=]\s*)(?P<prefix>Bearer\s+)?"
    r"(?P<value>\"[^\"]*\"|'[^']*'|[^\s,;}]*)",
    re.IGNORECASE,
)
_UNRESOLVED_ENV = re.compile(r"^\$\{?\w+\}?$")
_RAW_SECRET_TEXT = re.compile(
    r"(?<![A-Za-z0-9])(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9_]{8,}|"
    r"AKIA[0-9A-Z]{12,}|xox[baprs]-[A-Za-z0-9-]{8,})(?![A-Za-z0-9])"
)


def _redact_text(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        value = match.group("value")
        quote = value[0] if len(value) >= 2 and value[0] == value[-1] else ""
        return (
            f"{match.group('key')}{match.group('key_quote')}"
            f"{match.group('separator')}{match.group('prefix') or ''}"
            f"{quote}[REDACTED]{quote}"
        )

    value = _SECRET_TEXT.sub(replace, value)
    return _RAW_SECRET_TEXT.sub("[REDACTED]", value)


def redact_text(value: str) -> str:
    """Mask common credentials embedded in otherwise unstructured text."""
    return _redact_text(value)


def _normalize_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


def _is_secret_key(key: str) -> bool:
    normalized = _normalize_key(key)
    return normalized in _SECRET_KEYS or normalized.endswith(
        ("apikey", "apitoken", "authtoken", "secret", "password", "credential")
    )


def redact(value: Any, *, limit: int = _MAX_PREVIEW) -> Any:
    budget = [min(limit, _MAX_AGGREGATE)]

    def _redact(item: Any, depth: int = 0) -> Any:
        if budget[0] <= 0 or depth >= _MAX_DEPTH:
            return "[TRUNCATED]"
        if isinstance(item, dict):
            result: dict[str, Any] = {}
            for index, (key, child) in enumerate(item.items()):
                if index >= _MAX_ITEMS:
                    result["[TRUNCATED_ITEMS]"] = "[TRUNCATED]"
                    break
                key_string = str(key)
                result[key_string] = (
                    "[REDACTED]" if _is_secret_key(key_string) else _redact(child, depth + 1)
                )
            return result
        if isinstance(item, (list, tuple, set)):
            return [_redact(child, depth + 1) for child in list(item)[:_MAX_ITEMS]]
        if isinstance(item, str):
            bearer = _BEARER_VALUE.fullmatch(item)
            if bearer:
                return f"{bearer.group('prefix')}[REDACTED]{bearer.group('suffix')}"
            redacted = redact_text(item)
            available = max(0, min(len(redacted), budget[0], limit - len("[TRUNCATED]")))
            budget[0] -= available
            return redacted[:available] + ("[TRUNCATED]" if available < len(redacted) else "")
        if item is None or isinstance(item, (bool, int, float)):
            return item
        return _redact(str(item), depth + 1)

    return _redact(value)


def should_sample(rate: float, *, random_value: float | None = None) -> bool:
    return rate > 0 and (
        rate >= 1 or (random_value if random_value is not None else random.random()) < rate
    )


class NoOpObservability:
    enabled = False

    @contextlib.contextmanager
    def run(self, run_id: str, goal: str, *, session_id: str | None = None) -> Iterator[None]:
        yield None

    def finish_run(self, *, status: str, output: Any = None, error: Any = None) -> None:
        return None

    def llm(
        self, model: str, *, input_data: Any = None, output_data: Any = None, **kwargs: Any
    ) -> None:
        return None

    def tool(
        self, name: str, *, input_data: Any = None, output_data: Any = None, **kwargs: Any
    ) -> None:
        return None

    def policy(self, name: str, *, allowed: bool, **kwargs: Any) -> None:
        return None

    def verification(self, name: str, *, status: str, **kwargs: Any) -> None:
        return None

    def shutdown(self) -> None:
        return None


class LangfuseObservability(NoOpObservability):
    enabled = True

    def __init__(self, config: dict[str, Any], client: Any) -> None:
        self.config = config
        self.client = client
        self._flushed = False
        self._root: contextvars.ContextVar[Any] = contextvars.ContextVar(
            "langfuse_root", default=None
        )
        self._sampled: contextvars.ContextVar[bool] = contextvars.ContextVar(
            "langfuse_sampled", default=False
        )

    def _payload(self, input_data: Any, output_data: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if self.config.get("capture_input", False):
            payload["input"] = redact(input_data)
        if self.config.get("capture_output", False):
            payload["output"] = redact(output_data)
        return payload

    @contextlib.contextmanager
    def _observation(self, kind: str, name: str, **kwargs: Any) -> Iterator[Any]:
        try:
            context_manager = self.client.start_as_current_observation(
                as_type="generation" if kind == "llm" else "span", name=name, **kwargs
            )
        except Exception as exc:
            logger.warning("Langfuse observation failed: %s", type(exc).__name__)
            yield None
            return
        body_exception: BaseException | None = None
        try:
            # The SDK owns enter/exit and returns the actual observation from __enter__.
            with context_manager as observation:
                try:
                    yield observation
                except BaseException as exc:
                    body_exception = exc
                    raise
                finally:
                    if observation is not None:
                        try:
                            observation.update()
                            # Compatibility with lightweight legacy test doubles;
                            # the real v4 context manager owns its lifecycle.
                            if not type(context_manager).__module__.startswith("langfuse"):
                                end = getattr(observation, "end", None)
                                if callable(end):
                                    end()
                        except Exception as exc:
                            logger.warning(
                                "Langfuse observation update failed: %s", type(exc).__name__
                            )
        except BaseException as exc:
            logger.warning("Langfuse observation failed: %s", type(exc).__name__)
            if body_exception is not None:
                if exc is body_exception:
                    raise
                raise body_exception.with_traceback(body_exception.__traceback__) from None

    @contextlib.contextmanager
    def run(self, run_id: str, goal: str, *, session_id: str | None = None) -> Iterator[Any]:
        token_sample = self._sampled.set(should_sample(float(self.config.get("sample_rate", 1.0))))
        if not self._sampled.get():
            try:
                yield None
            finally:
                self._sampled.reset(token_sample)
            return
        metadata: dict[str, Any] = {"run_id": run_id}
        if session_id:
            metadata["session_id"] = session_id
        for key in ("environment", "release"):
            if self.config.get(key):
                metadata[key] = self.config[key]
        token_root = self._root.set(None)
        try:
            with self._observation("run", "nova.run", metadata=metadata) as observation:
                self._root.set(observation)
                yield observation
        finally:
            self._root.reset(token_root)
            self._sampled.reset(token_sample)

    def finish_run(self, *, status: str, output: Any = None, error: Any = None) -> None:
        root = self._root.get()
        if root is None or not self._sampled.get():
            return
        metadata: dict[str, Any] = {"status": status}
        if error is not None:
            metadata["error_type"] = type(error).__name__
        payload = {"metadata": metadata, **self._payload(None, output)}
        try:
            root.update(**payload)
        except Exception as exc:
            logger.warning("Langfuse root update failed: %s", type(exc).__name__)

    def _event(
        self, kind: str, name: str, input_data: Any = None, output_data: Any = None, **kwargs: Any
    ) -> None:
        if not self._sampled.get():
            return
        if not self.config.get("capture_output", False):
            kwargs.pop("result", None)
            kwargs.pop("output", None)
        native = kwargs.pop("_native", {})
        payload = {**self._payload(input_data, output_data), **native, "metadata": redact(kwargs)}
        with self._observation(kind, name, **payload) as observation:
            if observation is not None:
                try:
                    observation.update(**payload)
                except Exception as exc:
                    logger.warning("Langfuse observation update failed: %s", type(exc).__name__)

    def llm(
        self, model: str, *, input_data: Any = None, output_data: Any = None, **kwargs: Any
    ) -> None:
        usage = kwargs.pop("usage", None)
        cost = kwargs.pop("cost", None)
        native: dict[str, Any] = {"model": model}
        if usage is not None:
            native["usage_details"] = usage
        if cost is not None:
            native["cost_details"] = cost
        self._event("llm", "nova.llm", input_data, output_data, _native=native, **kwargs)

    def tool(
        self, name: str, *, input_data: Any = None, output_data: Any = None, **kwargs: Any
    ) -> None:
        self._event("tool", "nova.tool", input_data, output_data, tool_name=name, **kwargs)

    def policy(self, name: str, *, allowed: bool, **kwargs: Any) -> None:
        self._event("policy", "nova.policy", allowed=allowed, tool_name=name, **kwargs)

    def verification(self, name: str, *, status: str, **kwargs: Any) -> None:
        self._event("verification", "nova.verification", status=status, tool_name=name, **kwargs)

    def shutdown(self) -> None:
        langfuse = self.config.get("langfuse", {})
        flush = (
            langfuse.get("flush_at_shutdown", self.config.get("flush_at_shutdown", True))
            if isinstance(langfuse, dict)
            else self.config.get("flush_at_shutdown", True)
        )
        if self._flushed or not flush:
            return
        self._flushed = True
        try:
            self.client.flush()
        except Exception as exc:
            logger.warning("Langfuse flush failed: %s", type(exc).__name__)


def create_observability(
    config: dict[str, Any], *, client_factory: Callable[[dict[str, Any]], Any] | None = None
) -> NoOpObservability | LangfuseObservability:
    settings = config.get("observability", {})
    if (
        isinstance(settings, dict)
        and settings.get("enabled")
        and settings.get("provider") == "mlflow"
    ):
        from nova.mlflow_observability import create_mlflow_observability

        return create_mlflow_observability(settings)
    if (
        not isinstance(settings, dict)
        or not settings.get("enabled")
        or settings.get("provider", "langfuse") != "langfuse"
    ):
        return NoOpObservability()
    rate = settings.get("sample_rate", 1.0)
    if isinstance(rate, bool) or not isinstance(rate, (int, float)) or not 0 <= rate <= 1:
        return NoOpObservability()
    raw = settings.get("langfuse", {})
    if not isinstance(raw, dict):
        return NoOpObservability()
    langfuse_config = dict(raw)
    for key in ("public_key", "secret_key"):
        value = langfuse_config.get(key)
        if isinstance(value, str) and _UNRESOLVED_ENV.fullmatch(value):
            env_name = value[2:-1] if value.startswith("${") else value[1:]
            if env_name in os.environ:
                langfuse_config[key] = os.environ[env_name]
    unresolved_credentials = any(
        isinstance(langfuse_config.get(key), str)
        and _UNRESOLVED_ENV.fullmatch(langfuse_config[key])
        for key in ("public_key", "secret_key")
    )
    for key, env_name, default in (
        ("base_url", "LANGFUSE_BASE_URL", "https://cloud.langfuse.com"),
        ("public_key", "LANGFUSE_PUBLIC_KEY", ""),
        ("secret_key", "LANGFUSE_SECRET_KEY", ""),
    ):
        if not langfuse_config.get(key) or (
            key == "base_url" and langfuse_config.get(key) == "https://cloud.langfuse.com"
        ):
            langfuse_config[key] = os.environ.get(env_name, default)
    if unresolved_credentials:
        return NoOpObservability()
    normalized = {**settings, "langfuse": langfuse_config, "sample_rate": rate}
    try:
        if client_factory is not None:
            return LangfuseObservability(normalized, client_factory(langfuse_config))
        from langfuse import Langfuse

        if not langfuse_config.get("public_key") or not langfuse_config.get("secret_key"):
            return NoOpObservability()
        client = Langfuse(
            public_key=langfuse_config["public_key"],
            secret_key=langfuse_config["secret_key"],
            base_url=langfuse_config["base_url"],
        )
        return LangfuseObservability(normalized, client)
    except Exception as exc:
        logger.warning("Langfuse initialization failed: %s", type(exc).__name__)
        return NoOpObservability()


__all__ = [
    "LangfuseObservability",
    "NoOpObservability",
    "create_observability",
    "redact",
    "redact_text",
    "should_sample",
]
