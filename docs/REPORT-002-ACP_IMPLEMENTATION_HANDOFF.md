# REPORT-002: ACP Implementation Handoff

**Status:** 🔴 Retired
**Last Updated:** November 2026
**Type:** REPORT (Development Handoff)

> Historical document: ACP functionality was removed in November 2026. The implementation and future work described below no longer apply. Agent-to-agent invocation is planned in [SPEC-006](SPEC-006-CALLABLE_AGENT_PLAN.md).

---

## Quick Reference

| Item | Current State |
|------|---------------|
| Branch | `main` |
| Current implementation | ACP lifecycle, workspace isolation, cancellation, and tool-call progress are implemented |
| Test baseline | 1169 tests passing, 83.72% coverage |
| Quality gates | Ruff, formatting, mypy, and pytest passing locally |
| Next patch | ACP permission bridging and client-provided MCP configuration |

## Goal

Make Nova Agent interoperable with ACP-compatible editors through the official `agent-client-protocol` Python SDK while adding one independently verifiable capability per patch.

## Implemented

### ACP server transport

- `nova acp` launches an ACP server over stdio.
- Standard output is reserved for protocol traffic.
- Initialization advertises only implemented prompt and session capabilities.
- Dependency: `agent-client-protocol>=0.12.1,<0.13`.

### Session lifecycle

- `session/new` creates an isolated `NovaAgent`.
- ACP session IDs are Nova's persistent session IDs.
- `session/load` restores a persisted Nova session and replays user and assistant messages as `session/update` notifications.
- `session/prompt` accepts text blocks and streams agent message chunks.
- `session/cancel` uses Nova's interrupt hook and returns the `cancelled` stop reason.

### Workspace correctness

- ACP `cwd` must be an existing absolute directory.
- Each session uses its own workspace for context discovery.
- Relative terminal, file, search, listing, and Git operations default to that workspace.
- Multi-workspace smoke testing verified that relative writes do not leak between sessions.

## Binding Behavioral Decisions

1. Existing code receives scoped patches, not a clean-room super prompt.
2. New ACP capabilities must be advertised only after their complete protocol behavior exists.
3. `session/load` must replay the complete visible conversation before responding.
4. ACP session IDs must remain stable across process restarts, so Nova's stored session ID is canonical.
5. ACP `cwd` controls project context and relative tool execution; the server process directory does not.
6. Unsupported prompt content remains rejected; Phase 1 supports text only.

## Verification Evidence

| Gate | Last Result |
|------|-------------|
| Ruff | ✅ Pass |
| Format check | ✅ Pass |
| Mypy | ✅ Pass |
| Pytest | ✅ 1169 passed |
| Coverage | ✅ 83.72% |
| Session replay smoke | ✅ User and assistant history replayed in order |
| Workspace smoke | ✅ Distinct context and relative-write isolation |

## Completed Patch: Tool-Call Lifecycle Reporting

Implemented with public adapter tests:

1. Emit ACP `tool_call` before execution.
2. Emit `tool_call_update` with `in_progress`.
3. Emit `completed` with the tool result or `failed` for errors.
4. Preserve one stable tool-call ID across every update.
5. Map Nova tools to ACP kinds:
   - file reads → `read`
   - writes and patches → `edit`
   - searches and listings → `search`
   - terminal → `execute`
   - web and HTTP → `fetch`
   - other tools → `other`
6. Test success, failure, multiple calls, and cancellation through the public ACP adapter.
7. Full quality gates pass; stdio transport remains an integration-test follow-up.

## Planned Follow-On Patches

1. ACP permission bridging through `session/request_permission`.
2. Client-provided MCP server configuration.
3. `session/list` and `session/close` with truthful capability advertisement.
4. End-to-end validation in Zed or another ACP-compatible editor.

## Important Files

| File | Purpose |
|------|---------|
| `nova/acp_server.py` | ACP adapter, sessions, streaming, cancellation |
| `nova/agent.py` | Agent loop, workspace defaults, tool execution hooks |
| `nova/cli.py` | `nova acp` entry point and stdout-safe error handling |
| `tests/test_acp_server.py` | ACP lifecycle and transport behavior |
| `tests/test_agent.py` | Workspace and tool execution behavior |
| `pyproject.toml` | Official ACP SDK dependency |

## Resume Procedure

```bash
git status --short
git log -5 --oneline --decorate
git rev-list --left-right --count origin/main...HEAD
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy nova/
.venv/bin/pytest
```

Then read this report, `nova/acp_server.py`, and the ACP tests before starting the next RED test. Do not push or rewrite remote history unless explicitly requested.

## Related Documentation

| Document | Purpose |
|----------|---------|
| [Documentation Index](DOCUMENTATION_INDEX.md) | Complete project documentation inventory |
| [GUIDE-007 MCP Integration](GUIDE-007-MCP_INTEGRATION.md) | Nova's existing MCP client architecture |
| [GUIDE-012 Session Management](GUIDE-012-SESSION_MANAGEMENT.md) | Persistent session storage and lifecycle |
| [GUIDE-010 Roadmap](GUIDE-010-ROADMAP.md) | Broader project direction |
