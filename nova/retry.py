"""Retry logic with exponential backoff for API calls.

Handles transient errors (rate limits, server errors, timeouts)
with configurable retry counts, backoff multipliers, and
error classification.
"""

import logging
import random
import time
from collections.abc import Callable
from typing import Any

import httpx
import openai

logger = logging.getLogger(__name__)


# Error classifications
class ErrorType:
    RETRYABLE = "retryable"  # Transient — should retry
    NON_RETRYABLE = "non_retryable"  # Permanent — should not retry
    CONTEXT_OVERFLOW = "overflow"  # Context too long — needs deterministic compaction
    API_TIMEOUT = "api_timeout"  # API-level timeout — retry once only
    CONNECTION_TIMEOUT = "connection_timeout"  # Connection issue — retry with backoff


# HTTP status codes that are retryable
_RETRYABLE_STATUS = {429, 500, 502, 503, 504, 529}

# Error message patterns that indicate retryable errors
_RETRYABLE_PATTERNS = [
    "rate limit",
    "too many requests",
    "server error",
    "internal error",
    "bad gateway",
    "service unavailable",
    "upstream error",
]

# Connection-level errors (transient network issues — retry aggressively)
_CONNECTION_ERROR_PATTERNS = [
    "connection timeout",
    "connection refused",
    "connection reset",
]

# API-level timeouts (may be permanent — retry only once)
_API_TIMEOUT_PATTERNS = [
    "timeout",
    "timed out",
    "temporary failure",
    "gateway timeout",
]

# Error patterns that indicate context overflow
_OVERFLOW_PATTERNS = [
    "context length",
    "context window",
    "token limit",
    "maximum context",
    "prompt is too long",
    "input length",
    "exceeds the maximum",
]


def classify_error(status_code: int | None = None, message: str = "") -> str:
    """Classify an error as retryable, non-retryable, or context overflow.

    Args:
        status_code: HTTP status code (if available).
        message: Error message text.

    Returns:
        One of ErrorType.RETRYABLE, ErrorType.NON_RETRYABLE, ErrorType.CONTEXT_OVERFLOW,
        ErrorType.API_TIMEOUT, or ErrorType.CONNECTION_TIMEOUT.
    """
    text = (message or "").lower()

    # Check for context overflow first (highest priority)
    for pattern in _OVERFLOW_PATTERNS:
        if pattern in text:
            return ErrorType.CONTEXT_OVERFLOW

    # HTTP status is authoritative for API responses. Error text can contain
    # words such as "timeout" even when a request is invalid (for example 400).
    if status_code in _RETRYABLE_STATUS:
        return ErrorType.RETRYABLE

    if status_code and 400 <= status_code < 500:
        return ErrorType.NON_RETRYABLE

    if status_code and 500 <= status_code < 600:
        return ErrorType.RETRYABLE

    # Check connection-level errors first (transient network issues)
    for pattern in _CONNECTION_ERROR_PATTERNS:
        if pattern in text:
            return ErrorType.CONNECTION_TIMEOUT

    # Check API-level timeouts (may be permanent — retry only once)
    for pattern in _API_TIMEOUT_PATTERNS:
        if pattern in text:
            return ErrorType.API_TIMEOUT

    # Check general retryable error patterns
    for pattern in _RETRYABLE_PATTERNS:
        if pattern in text:
            return ErrorType.RETRYABLE

    # Unknown errors are usually programming or SDK errors. Retrying them
    # hides the original failure and cannot make a deterministic call valid.
    if status_code is None:
        return ErrorType.NON_RETRYABLE

    # Default: non-retryable for known non-retryable status codes
    return ErrorType.NON_RETRYABLE


def classify_exception(error: Exception) -> str:
    """Classify a raised exception, trusting its type before its message.

    SDK transport errors carry generic messages (openai's is just
    "Connection error.") that text patterns cannot recognize.
    """
    if isinstance(error, (openai.APITimeoutError, httpx.TimeoutException, TimeoutError)):
        return ErrorType.API_TIMEOUT
    if isinstance(error, (openai.APIConnectionError, httpx.TransportError, ConnectionError)):
        return ErrorType.CONNECTION_TIMEOUT
    # httpx.HTTPStatusError stores status_code on .response
    status_code = getattr(error, "status_code", None)
    if status_code is None:
        response = getattr(error, "response", None)
        if response is not None:
            status_code = getattr(response, "status_code", None)
    return classify_error(status_code, str(error))


def retry_with_backoff(
    func,
    *args: Any,
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    backoff_multiplier: float = 2.0,
    jitter: bool = True,
    retry_if: Callable[[Exception], bool] | None = None,
    **kwargs: Any,
) -> Any:
    """Call a function with exponential backoff retry.

    Args:
        func: Function to call. Should raise an exception on failure.
        *args: Positional arguments for the function.
        max_retries: Maximum number of retry attempts.
        base_delay: Initial delay in seconds.
        max_delay: Maximum delay in seconds.
        backoff_multiplier: Multiplier for each retry.
        jitter: Add random jitter to prevent thundering herd.
        retry_if: Optional callback to reject retries for a specific exception.
        **kwargs: Keyword arguments for the function.

    Returns:
        The return value of the function.

    Raises:
        The last exception if all retries are exhausted.
    """
    last_exception = None

    for attempt in range(max_retries + 1):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            last_exception = e
            if retry_if is not None and not retry_if(e):
                raise
            error_msg = str(e)
            error_type = classify_exception(e)

            # Don't retry context overflow errors
            if error_type == ErrorType.CONTEXT_OVERFLOW:
                raise

            # Don't retry non-retryable errors
            if error_type == ErrorType.NON_RETRYABLE:
                raise

            if attempt < max_retries:
                delay = min(base_delay * (backoff_multiplier**attempt), max_delay)
                if jitter:
                    delay *= 0.5 + random.random() * 0.5  # 50%-150% of delay

                # API-level timeouts: limit to a single retry to avoid
                # spinning on permanently broken endpoints
                if error_type == ErrorType.API_TIMEOUT and attempt >= 1:
                    logger.error(
                        "API timeout persisted after 1 retry (%s): %s",
                        error_type,
                        error_msg[:200],
                    )
                    raise

                logger.warning(
                    "API call failed (attempt %d/%d, %s): %s — retrying in %.1fs",
                    attempt + 1,
                    max_retries,
                    error_type,
                    error_msg[:200],
                    delay,
                )
                time.sleep(delay)
            else:
                logger.error(
                    "API call failed after %d retries (%s): %s",
                    max_retries,
                    error_type,
                    error_msg[:200],
                )

    raise last_exception  # type: ignore[misc]


def retry_api_call(
    http_client,
    method: str,
    url: str,
    *,
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    **kwargs: Any,
) -> Any:
    """Make an HTTP request with retry logic.

    Convenience wrapper around retry_with_backoff for httpx calls.

    Args:
        http_client: httpx.Client instance.
        method: HTTP method ("GET", "POST", etc.).
        url: URL to request.
        max_retries: Maximum retry attempts.
        base_delay: Initial delay in seconds.
        max_delay: Maximum delay in seconds.
        **kwargs: Additional arguments passed to httpx request.

    Returns:
        httpx.Response object.
    """

    def _do_request() -> Any:
        response = getattr(http_client, method.lower())(url, **kwargs)
        response.raise_for_status()
        return response

    return retry_with_backoff(
        _do_request,
        max_retries=max_retries,
        base_delay=base_delay,
        max_delay=max_delay,
    )
