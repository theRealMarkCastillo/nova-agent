# SPEC-005: Code and Design Review Improvement Plan

**Status:** 🟡 In Progress
**Last Updated:** October 2026
**Type:** SPEC (Implementation Plan)

---

## Problem

An October 2026 code and design review of `main` (`863bc26`) found that the
modules themselves are well built and tested, but that problems sit where
modules meet:

- The safety model relies on denylists that the `terminal` tool can bypass.
- Tool outcomes are carried as strings.
- The process-wide tool registry is filtered differently by the prompt and by the API request.
- Token accounting re-tokenizes everything on each loop iteration.

The baseline passed lint, mypy, and 1302 tests at 84.7% coverage.

This plan orders the remaining findings into independently shippable phases.
Each item names its acceptance check so the next contributor can pick it up
without re-running the review.

## Quick Reference

| Phase | Theme | Items | Status |
|-------|-------|-------|--------|
| 0 | High-value bug fixes | H2, H3, M1 | ✅ Done (`b0ae781`, `455ffce`, `62f273c`) |
| 1 | Correctness quick wins | M2, M3a, M7a, L1–L8 | ✅ Done (`47ee7eb`…`3894d5b`) |
| 2 | Safety model | H1, M8, S1–S3 | 📋 Planned |
| 3 | Architecture | M5, M7, M3b | 📋 Planned |
| 4 | Performance | M6, M4 | 📋 Planned |

Phases 1 and 4 have no dependencies on each other. Phase 3 should land before
the public SDK surface in [SPEC-003](SPEC-003-NOVA_SDK_PUBLIC_API.md) is
frozen, because it changes how tools are scoped per agent.

## Phase 0: Fixed

| ID | Finding | Fix | Tests |
|----|---------|-----|-------|
| H2 | `openai.APIConnectionError` ("Connection error.", no status) was classified non-retryable, so one network blip ended the turn | `retry.classify_exception` classifies SDK/httpx/builtin transport errors by type before falling back to message patterns | `test_retry_transport_errors_by_type` (5 cases) |
| H3 | A matching `allow: true` path rule skipped confirmation and the command deny check for `terminal`, whose path is only its `workdir` | Allow rules return early only when no command is present; deny rules still apply to `workdir` | 3 tests in `test_permissions.py` |
| M1 | `write_file`/`patch_file` reset modes to `0600` (scripts lost `+x`) and replaced symlinks with regular files | Shared `_atomic_write` resolves symlinks, keeps the existing mode, and applies the import-time umask to new files | `TestAtomicWritePreservation` (4 tests) |

## Phase 1: Correctness Quick Wins

✅ Done. Each item landed with regression tests. Notes on how they were resolved:

- M2 decodes HTML entities and `\uXXXX` escapes before matching, rather than dropping detection, and ignores a leading byte order mark.
- M3a adds a lock-protected `CostTracker.merge`, called from the sub-agent's own `finally`.
- M7a passes the pipeline's single policy decision to the trace, so permissions are evaluated once and MCP tools are traced too.
- L5 also unwraps IPv4-mapped IPv6 addresses.
- L6 classifies tool errors with `classify_error`. Matching is still text-based until M7 introduces typed results.


| ID | Problem | Change | Acceptance |
|----|---------|--------|------------|
| M2 | `\\u[0-9a-f]{4}` and `&#x…;` scanner patterns block entire context files that document JSON or HTML | Drop both patterns, or treat them as log-only signals that do not block | A context file containing `"é"` and `&#x26;` loads unmodified |
| M3a | Sub-agents build `OpenAI(...)` directly, and a timed-out sub-agent's usage never reaches the parent | Use `providers.build_client`; aggregate usage in a `finally` path that also covers timeouts | Timed-out sub-agent cost appears in the parent's `cost_tracker` |
| M7a | Trace outcome is `denied` when a result merely contains "requires confirmation" (`agent.py` `_execute_tool_call`) | Derive the outcome from the permission result returned by the impl, not from text | Reading a file containing that phrase is traced `completed` |
| L1 | `mcp_client.connect_all()` runs before the cleanup `try` in `NovaAgent.__init__` | Move it inside the guarded block | Failing `connect_all` closes the owned LLM client |
| L2 | Parallel tool failures embed `str(e)`; sequential ones use the type name. `_tool_callback` fires after the tool runs in the parallel path but before it in the sequential path | Use the type name and a consistent callback point | Both paths produce the same error shape |
| L3 | Streaming reads `usage` only from choice-less chunks | Also read usage on the final chunk that carries choices | Provider fixture with usage on the final choice chunk records tokens |
| L4 | `read_file` and `patch_file` check existence before workspace safety, revealing which outside paths exist | Run `path_safety_error` first | Missing file outside workspace returns access-denied |
| L5 | SSRF filter misses `100.64.0.0/10` (Python's `is_private` is false for it) | Block the shared address space network | `100.64.1.1` is rejected |
| L6 | Dead code: `_MUTATING_TOOLS`/`is_mutating_tool`, `_estimate_messages_tokens_cached`/`_token_cache`, `_is_transient_error` (duplicates `classify_error`), git's private env sanitizer | Delete, or route to the shared implementation | `grep` finds one definition of each concept |
| L7 | `_truncate_to_token_budget` docstring says 70/20; code uses 78/22 | Fix the docstring | — |
| L8 | Transient tool retries sleep without checking interrupt | Check interrupt between retries | Ctrl+C during a retry wait returns promptly |

## Phase 2: Safety Model

The real boundary today is `ask`-mode confirmation plus the workspace check.
Denylists and the injection scanner are guardrails. This phase makes the docs
say so and closes the paths where untrusted content can act without asking.

| ID | Problem | Change | Acceptance |
|----|---------|--------|------------|
| H1 | `read_file`, `http_get`, and `web_scrape` are read-only, so they are auto-approved and run in parallel. Injected instructions can read a workspace file and send it out in a URL | Track content that arrived from an untrusted source (`http_*`, `web_*`, MCP results) during a turn. Once any has arrived, require confirmation for outbound tools (`http_*`, `web_*`, MCP calls) | In `ask` mode, an `http_get` that follows a `web_scrape` in the same turn prompts; a first-call `http_get` does not |
| M8 | The model can write wiki notes under `Core/` or with `inject: true`, and create skills via `skill_manage`. All of these load into future system prompts without scanning | Scan injected notes and skills with `find_content_threats` at prompt build. Always require confirmation for writes that set `inject` or target `Core/`, even in `auto` mode | A poisoned `Core/` note is labeled or blocked in the next prompt |
| S1 | Sensitive paths are defined twice and disagree (`permissions._SENSITIVE_PATH_PATTERNS` vs `path_safety._PROTECTED_DIRS`) | One shared definition used by both layers | One list; both layers have parity tests |
| S2 | Read-only status has three sources (`_READ_ONLY_TOOLS`, `is_read_only=True`, `_MUTATING_TOOLS`) | Registration flag is the single source; delete the frozensets | Each tool declares read-only status at registration |
| S3 | Docs call sensitive-path protection "cannot be overridden" while `terminal` can `cat` anything | Reword GUIDE-008; document an optional sandbox wrapper for `auto` mode (`sandbox-exec` on macOS, bubblewrap on Linux) | GUIDE-008 describes denylists as guardrails |

## Phase 3: Architecture

### M7: ToolExecutor with typed results

`agent.py` (1212 lines) repeats the tool pipeline across `_execute_tool_call`,
`_execute_tool_call_impl`, and the parallel and sequential runners:
permissions are evaluated twice, interrupt checks happen five times, and
observability calls happen four times. Success or failure is inferred from an
`"Error:"` prefix, so reading a log that starts with `Error: connection reset`
is marked failed and retried.

```text
ToolExecutor.run(call) -> ToolResult
  parse args -> workspace defaults -> policy -> confirm -> pre hook
  -> dispatch (registry | MCP | resource) -> transient retry -> post hook
  -> verify -> trace + observability (once)

@dataclass
class ToolResult:
    status: Literal["completed", "failed", "denied", "interrupted"]
    content: str
    error_type: str | None = None
    retryable: bool = False
```

Handlers keep returning strings. The registry wraps them: exceptions become
`failed`, and policy outcomes become `denied`. Only handlers that need to signal
a retryable failure return a `ToolResult` directly. Acceptance: permission
evaluated once per call; no outcome logic reads result text.

### M5: Per-agent tool view

The registry is process-global. Registration depends on config and happens in
whatever order agents are created. `prompt.py` lists every registered tool,
while `_get_tool_definitions` filters by config. As a result, a leaf
sub-agent's prompt can advertise `delegate_task` or web tools it cannot call.
Build a `ToolSet` once per agent from registry plus config. Use it for the
prompt summary, the API definitions, and dispatch. Acceptance: for any config,
the tools named in the prompt equal the tools in the API request.

### M3b: Sub-agents share parent resources

Each sub-agent calls `build_mcp_client(config)` and starts fresh copies of
every MCP server. Pass the parent's MCP client (calls are already serialized by
`_mcp_call_lock`), its client factory, and a cost sink. After a timeout, stop
the child from calling the parent's confirmation callback.
Acceptance: delegating spawns no new MCP processes; a timed-out child cannot
prompt the user.

## Phase 4: Performance

### M6: Token accounting anchored on provider usage

Each loop iteration currently re-tokenizes the full request, including tool
schemas, with `cl100k`. `compact_to_token_budget` re-estimates the whole list
after removing each turn, which is quadratic. `reasoning_content` is sent back
to the provider but never counted. The safety margin is only 1024 tokens.

- Use the last response's `usage.prompt_tokens` as the base, and estimate only the messages added since then.
- Cache per-message estimates (keyed by message identity) and compute tool-schema tokens once per `ToolSet` generation.
- Count `reasoning_content`.
- Make the compaction loop subtract the removed turn's cached count instead of re-estimating.

Acceptance: a long-session benchmark shows estimation work linear in new messages, with estimates within 5% of provider-reported prompt tokens.

### M4: Session search write amplification

`add_message` appends every message to a single `session_fts` row per session
(`content = content || ' ' || ?`). The trigram index re-indexes the whole
session on every write, and `message_search` already indexes each message.
Migrate session search to aggregate over `message_search` and drop the
appended content column. This needs a schema migration that rebuilds the index
from `messages`. Acceptance: write time is flat as sessions grow;
`search_sessions` results are unchanged on the existing test corpus.

## Trade-offs

| Decision | Alternative | Rationale |
|----------|-------------|-----------|
| Allow rules still skip confirmation for file tools (H3) | Allow rules never skip confirmation | Keeps documented, tested behavior for file writes; only the command case was unsafe |
| Import-time umask (M1) | Query umask at each write | `os.umask` can only be read by setting it, which races with concurrent tool threads |
| Gate outbound tools after untrusted content arrives (H1) | Mark `http_get` mutating | Keeps first-party fetches prompt-free; prompts only once untrusted content could be steering the agent |
| Handlers keep returning strings (M7) | Change every handler signature | Contained migration; typed results enter at the registry boundary |
| Anchor on provider usage (M6) | Ship a tokenizer per model | Provider counts are exact and already in every response |

## Related Documentation

| Document | Purpose |
|----------|---------|
| [GUIDE-008 Permissions](GUIDE-008-PERMISSIONS.md) | Permission cascade, including the H3 path-rule semantics |
| [GUIDE-014 Retry and Error Handling](GUIDE-014-RETRY_AND_ERROR_HANDLING.md) | Error classification, including the H2 exception types |
| [GUIDE-011 Context Compression](GUIDE-011-CONTEXT_COMPRESSION.md) | Compaction behavior affected by M6 |
| [GUIDE-012 Session Management](GUIDE-012-SESSION_MANAGEMENT.md) | Session storage affected by M4 |
| [ADR-002 Sub-agent Design](ADR-002-SUBAGENT_DESIGN.md) | Sub-agent model affected by M3 |
| [SPEC-003 Nova SDK Public API](SPEC-003-NOVA_SDK_PUBLIC_API.md) | Public surface that Phase 3 should precede |
| [Session Handoff](../HANDOFF.md) | Current work state |
