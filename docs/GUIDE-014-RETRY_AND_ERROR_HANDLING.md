# GUIDE-014: Retry & Error Handling

**Status:** ✅ Active  
**Last Updated:** October 2026  
**Type:** GUIDE (Developer Reference)

> Nova Agent handles API failures gracefully with configurable retry logic, exponential backoff, and intelligent error classification. This guide explains how retries work and how to tune them.

---

## Quick Start

Retry logic is enabled by default. No configuration needed for typical use.

```yaml
retry:
  max_retries: 3       # max attempts per call
  base_delay: 1.0      # seconds before the first retry
  max_delay: 60.0      # cap on backoff delay
```

`backoff_multiplier` (default `2`) and `jitter` (default `on`) are code-level constants in `nova/retry.py`, not config keys — the config controls the three values above.

---

## Error Classification

Nova classifies every error into one of five categories, each with different retry behavior:

| Error Type | Behavior | Examples |
|------------|----------|----------|
| **Retryable** | Retry with exponential backoff | 429 rate limit, 500/502/503/504 server errors |
| **Non-retryable** | Fail immediately | 400 bad request, 401 unauthorized, 403 forbidden |
| **Context overflow** | Compact the request, then retry once | Context window exceeded |
| **API timeout** | Retry once only | `openai.APITimeoutError`, `httpx.TimeoutException`, "timeout" |
| **Connection timeout** | Retry with backoff | `openai.APIConnectionError`, `httpx.TransportError`, "connection refused" |

### HTTP Status Codes

| Status | Classification | Action |
|--------|---------------|--------|
| 400 | Non-retryable | Fail immediately |
| 401 | Non-retryable | Fail immediately |
| 403 | Non-retryable | Fail immediately |
| 429 | Retryable | Backoff + retry |
| 500 | Retryable | Backoff + retry |
| 502 | Retryable | Backoff + retry |
| 503 | Retryable | Backoff + retry |
| 504 | Retryable | Backoff + retry |
| 529 | Retryable | Backoff + retry |

### Exception Types

`classify_exception` checks the exception type before its message. Transport
errors from the OpenAI SDK and httpx carry generic messages (the SDK's is just
`"Connection error."`), so they are recognized by class: timeouts
(`openai.APITimeoutError`, `httpx.TimeoutException`, `TimeoutError`) retry once,
and connection failures (`openai.APIConnectionError`, `httpx.TransportError`,
`ConnectionError`) retry with backoff.

### Error Message Patterns

For other exceptions, string matching catches errors that don't have HTTP status codes:

```python
# Retryable patterns
(
    "rate limit",
    "too many requests",
    "server error",
    "internal error",
)
"bad gateway", "service unavailable", "upstream error"

# Connection error patterns (retried aggressively)
"connection timeout", "connection refused", "connection reset"

# API timeout patterns (retried only once)
"timeout", "temporary failure", "gateway timeout"
```

---

## Retry Algorithm

The retry logic uses **exponential backoff with jitter**:

```python
wait_time = base_delay * (backoff_multiplier**attempt) + random_jitter
```

### Default Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `base_delay` | 1 second | Starting delay before first retry |
| `max_retries` | 3 | Maximum number of retry attempts |
| `backoff_multiplier` | 2 | Exponential growth factor (code constant) |
| `jitter` | on | Randomize each delay to 50%–150% of the computed value |

### Example Timeline

With defaults (`max_retries: 3`, `multiplier: 2`, `jitter: true`):

```
Attempt 1: 0s      → 429 Too Many Requests
Attempt 2: ~1.5s   → 503 Service Unavailable
Attempt 3: ~4.2s   → 500 Internal Server Error
Attempt 4: ~9.8s   → 200 OK ✅
```

Without jitter, retries from multiple clients would hit the server simultaneously ("thundering herd"). Jitter spreads them out.

---

## Configuration

### Global Config

```yaml
retry:
  max_retries: 3              # max retry attempts per call (0–10)
  base_delay: 1               # seconds before first retry
  max_delay: 60               # cap retry delay at this many seconds
```

### Example Configurations

**Aggressive retries (unreliable API):**
```yaml
retry:
  max_retries: 5
  base_delay: 2
  max_delay: 60
```

**Fast failures (don't wait around):**
```yaml
retry:
  max_retries: 1
  base_delay: 0.5
  max_delay: 30
```

**No retries (fail fast, handle manually):**
```yaml
retry:
  max_retries: 0
```

---

## Context Overflow Handling

When the context window is exceeded, retrying won't help. Nova handles this specially:

1. **Detect** — The API returns an error matching context-length patterns (`context length`, `token limit`, `prompt is too long`, …). This is classified as **context overflow**, which is never retried as if it were transient.
2. **Compact** — The active context is compacted aggressively (the request budget is halved) using deterministic compaction (see [GUIDE-011](GUIDE-011-CONTEXT_COMPRESSION.md)).
3. **Retry once** — The compacted request is re-sent.
4. **Escalate** — If the second attempt still overflows, the error propagates to the caller.

This is different from normal retries because it changes the request, not just re-sends it.

---

## Logging

Retry attempts are logged at the `WARNING` level with the classification, attempt count, and delay:

```
[WARNING] API call failed (attempt 2/4, retryable): rate limit exceeded — retrying in 2.0s
[WARNING] API call failed (attempt 3/4, retryable): service unavailable — retrying in 4.0s
[ERROR]   API call failed after 3 retries (retryable): 500 Internal Server Error
```

A context-overflow recovery is logged separately before the compacted retry:
`[WARNING] Provider reported context overflow; compacting and retrying once`

---

## Troubleshooting

| Problem | Cause | Solution |
|---------|-------|----------|
| Too many retries | API is genuinely down | Set `max_retries: 1` for faster failure |
| Retries are too slow | High `base_delay` or `max_delay` | Reduce `base_delay` to 0.5 |
| Thundering herd on API recovery | Multiple clients retrying simultaneously | Jitter is on by default (50%–150% of delay) |
| Non-retryable errors being retried | Custom error classification needed | Check error patterns in `retry.py` |
| Context overflow after auto-recovery | Session is very long | Run `/compact` to free tokens proactively |

---

## Related Documentation

| Document | Purpose |
|----------|---------|
| [GUIDE-003 Customizing](GUIDE-003-CUSTOMIZING.md) | All config options |
| [GUIDE-011 Context Compression](GUIDE-011-CONTEXT_COMPRESSION.md) | Context overflow handling |
| [GUIDE-005 Cost Tracking](GUIDE-005-COST_TRACKING.md) | How retries affect cost |
| [ADR-003 Tool System Review](ADR-003-TOOL_SYSTEM_REVIEW.md) | Retry design decisions |
