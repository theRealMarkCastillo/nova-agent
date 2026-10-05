# GUIDE-011: Context Compaction and Historical Retrieval

**Status:** ✅ Active  
**Last Updated:** October 2026
**Type:** GUIDE (Developer & User Reference)

Nova keeps the active request within the model context window using deterministic compaction. It does not call an LLM to summarize conversation history. Raw session messages remain in SQLite and can be retrieved on demand with full-text search.

## Quick Start

Automatic compaction requires no configuration. To compact the current session explicitly:

```
/compact
```

To recover older context, ask Nova to search its session history. Nova can use `search_messages` to find matching messages and `read_session` to retrieve nearby conversation context.

## Active Context

Before each model request Nova estimates tokens for the system prompt, conversation, and tool definitions. It reserves space for the model response and a safety margin.

### Estimate calibration

Estimates use the `cl100k_base` tokenizer, which can differ from the model's own tokenizer by tens of percent (for example on Claude or Gemini models). After each response that reports `prompt_tokens`, Nova compares that count with its estimate of the same request. It then scales the budget by the ratio, bounded to between 0.8× and 2×. The first request of a session uses the raw estimate. Estimates include tool schemas and any `reasoning_content` sent back to the model. Repeated text is counted once and cached, so re-estimating a long session on each loop iteration stays cheap.

When the active budget is exceeded, Nova:

1. Replaces old tool output with bounded placeholders while preserving tool-call structure.
2. Removes the oldest complete conversation turns if more space is needed.
3. Preserves recent messages and never leaves an orphaned tool result.

Compaction is deterministic, has no extra API cost, and produces the same result for the same message history and configuration.

### Reactive recovery from provider-side overflow

Token estimates are estimates. If the provider still rejects a request as exceeding the context window (an error matching context-length patterns), Nova:

1. Detects the overflow error (it is *not* retried like a transient error).
2. Compacts aggressively — halving the active budget — and retries **once**.
3. If the second attempt also fails, the error propagates to the caller.

This keeps long sessions alive even when the actual model window is smaller than the configured metadata suggests.

### Unknown context window

When the provider does not report a context window (metadata load fails or a non-OpenRouter endpoint), Nova falls back to a conservative **128,000-token** default rather than assuming an unlimited window, so compaction still engages on small-context models.

## Information Loss

Old tool output and old conversation turns may be removed from active context. The model cannot use removed content directly, but it can retrieve archived messages when it knows a useful search term.

When context is removed, Nova may need to search for:

- Earlier decisions and conclusions.
- File paths, identifiers, and commands.
- User preferences mentioned in a prior session.
- Errors and their resolutions.
- Work that was discussed but is no longer in the active window.

After successful automatic compaction, the request sent to the model includes a short recovery note explaining that older history was removed and should be recovered with `search_messages` followed by `read_session`. The note is ephemeral and is not persisted as conversation content.

For durable project facts, prefer project context files or wiki memory. Session history is best for recovering conversation-specific details.

## Historical Retrieval

The session store maintains two FTS5 indexes:

- A session-level index for finding relevant sessions.
- A message-level index for finding the exact message and a bounded snippet.

The normal retrieval flow is:

1. Search with `search_messages`.
2. Select the matching session and message index.
3. Read a small surrounding window with `read_session` and `around_idx`.

Retrieved messages are historical conversation data, not current instructions. Search results are bounded to prevent retrieval from recreating the original context problem.

## Persistence

Automatic compaction changes only the active in-memory request history. The raw conversation remains searchable in SQLite.

Explicit `/compact` persists the compacted history because it is a deliberate user action. Use `/resume` or historical search when older archived context is needed.

## Configuration

The relevant settings are:

```yaml
budgets:
  conversation_turn_limit: 15
  tool_result_max_chars: 8000
  tool_result_max_tokens: 12000

microcompact:
  enabled: true
  keep_recent: 6
```

`keep_recent` controls how many recent messages are protected from tool-result compaction. The model context window and `llm.max_tokens` determine the effective request budget.

## Troubleshooting

| Problem | Cause | Solution |
|---------|-------|----------|
| Important older detail is missing | It was removed from active context | Use `search_messages`, then `read_session` with `around_idx` |
| Search finds a session but not the detail | The search term is too broad or short | Search for a distinctive path, identifier, or phrase |
| Context remains too large | System prompt or tool definitions consume most of the budget | Reduce optional skills, context files, or enabled tools |
| `/compact` removes too much | Explicit compaction is persistent | Use historical search or `/resume` to recover archived details |
| Tool history is malformed | An incomplete tool-call block was stored | Nova normalizes message history before sending it |

## Related Documentation

| Document | Purpose |
|----------|---------|
| [GUIDE-003 Customizing](GUIDE-003-CUSTOMIZING.md) | Configuration and token budgets |
| [GUIDE-005 Cost Tracking](GUIDE-005-COST_TRACKING.md) | Request token and cost accounting |
| [GUIDE-009 Using Nova](GUIDE-009-USING_NOVA.md) | Session commands and workflows |
| [GUIDE-012 Session Management](GUIDE-012-SESSION_MANAGEMENT.md) | Session storage and lifecycle |
