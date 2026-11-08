# SPEC-002: ACP Integration — Client MCP Servers & Editor Parity

**Status:** 🔴 Retired
**Last Updated:** November 2026
**Type:** SPEC (Feature Specification)
**Author:** Eidolon Labs LLC

> Historical document: ACP functionality was removed in November 2026. The implementation and future work described below no longer apply. Agent-to-agent invocation is planned in [SPEC-006](SPEC-006-CALLABLE_AGENT_PLAN.md).

---

## Quick Reference

| Item | State |
|------|-------|
| Goal | Close the ACP gap vs. the Claude Agent SDK adapter (`claude-agent-acp`) |
| Phase 1 | Client-provided MCP servers via `session/new` — 🟡 scoped, not started |
| Phase 2 | Session management completeness (`session/list`, `session/close`) — 📋 planned |
| Phase 3 | Editor UX parity (images, edit review, TODO lists, subagent transcripts) — 📋 planned |
| Phase 4 | Remote transports + protocol extensions — 📋 planned, depends on ACP spec |
| Code changes | **None in this document** — scope and acceptance criteria only |

---

## Problem

Nova already ships an ACP stdio server (`nova acp`) with session lifecycle, streaming, tool-call progress, cancellation, and permission requests — see [REPORT-002](REPORT-002-ACP_IMPLEMENTATION_HANDOFF.md). However, compared to Anthropic's `claude-agent-acp` adapter (which wraps the Claude Agent SDK), three gaps block editor parity:

1. **Client-provided MCP servers are accepted and ignored.** ACP clients (Zed, JetBrains, emacs) pass `mcpServers` in `session/new` so the agent can use the client's registered servers (e.g. a filesystem server scoped to the editor's project). Nova's `new_session()` receives them in `**kwargs` and drops them.
2. **Session management is half-claimed.** `load_session=True` is advertised, but `session/list` and `session/close` are not implemented, so clients can't enumerate or cleanly dispose of sessions.
3. **Rich editor UX is missing.** No images/@-mentions, edit review (following), TODO lists, nested subagent transcripts, or slash commands — features claude-agent-acp treats as table stakes.

---

## Solution

Land ACP parity in four independent phases, each gated by its own acceptance criteria and truthful capability advertisement (existing binding decision: *advertise only what is fully implemented*). Phase 1 is the highest-value, lowest-effort item: honor client MCP servers by mapping them onto Nova's existing `McpClient` — no new transport code needed.

## Current State

| Capability | Nova (`nova acp`) | claude-agent-acp |
|------------|-------------------|------------------|
| `session/new` (isolated agent, workspace validation) | ✅ | ✅ |
| `session/load` (SQLite resume + history replay) | ✅ | ✅ |
| `session/prompt` (streamed chunks + tool-call lifecycle) | ✅ | ✅ |
| `session/cancel` (interrupt → `cancelled`) | ✅ | ✅ |
| Permission requests (`session/request_permission`) | ✅ `allow_once` / reject | ✅ + editable choices |
| Client-provided MCP servers | 🔴 ignored | ✅ |
| `session/list`, `session/close` | 🔴 missing | ✅ |
| Images / context @-mentions | 🔴 text only | ✅ |
| Edit review / following | 🔴 | ✅ |
| TODO lists | 🔴 | ✅ |
| Nested subagent transcripts | 🔴 | ✅ (opt-in) |
| Interactive/background terminals | 🔴 | ✅ |
| Slash commands | 🔴 (TUI only today) | ✅ |
| Protocol extensions (goal, failure, permission) | 🔴 | ✅ |

---

## Phase 1 — Client-Provided MCP Servers 🟡

### Scope

Map the ACP `mcpServers` array from `session/new` onto Nova's existing `McpClient` (stdio/HTTP/SSE transports already implemented in `nova/mcp_client.py`), per session.

### Architecture

```
ACP client (Zed, JetBrains, ...)
        │  session/new { cwd, mcpServers: [...] }
        ▼
nova/acp_server.py ─ NovaAcpAgent.new_session()
        │  normalize mcpServers → McpServerConfig dataclasses
        │  build McpClient (config servers + client servers)
        ▼
nova/agent.py ─ NovaAgent(mcp_client=...)   ← already injected via __init__
        │  connect_all() → _refresh_mcp_tools() → mcp__{server}__{tool}
        ▼
Tool registry exposes client MCP tools to the model in this session only
```

### Interface

#### ACP → Nova config mapping

| ACP `mcpServers[]` field | Nova dataclass | Notes |
|--------------------------|----------------|-------|
| `type: "stdio"` | `McpStdioConfig` | `command`, `args`, `env` |
| `type: "http"` | `McpHttpConfig` | `url`, `headers`, `timeout` |
| `type: "sse"` | `McpSseConfig` | `url`, `post_url`, `headers`, `timeout` |
| `name` | `add_server_named(name, cfg)` | Explicit name → stable `mcp__{server}__{tool}` namespace |
| unknown keys | ignored | Forward-compatible with newer ACP versions |

#### New config keys (`config.yaml`)

```yaml
acp:
  client_mcp_servers: false      # opt-in; stdio servers execute subprocesses
  max_mcp_servers_per_session: 8 # bound subprocess/resource usage
```

**Default `false` (secure by default).** A client-provided stdio MCP server runs an arbitrary command on the host. Nova's permission philosophy is defense-in-depth; this is a new remote-triggerable execution surface. Flipping the flag to `true` provides claude-agent-acp parity. (See Trade-offs.)

#### Example exchange

```json
// → session/new
{
  "jsonrpc": "2.0", "id": 1, "method": "session/new",
  "params": {
    "cwd": "/Users/mark/Projects/hasu",
    "mcpServers": [
      { "type": "stdio", "name": "project-fs",
        "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/Users/mark/Projects/hasu"] }
    ]
  }
}
// ← result
{ "jsonrpc": "2.0", "id": 1, "result": { "sessionId": "20260827_093412_ab12cd" } }
```

### Acceptance Criteria

- [ ] `session/new` with `mcpServers` connects each valid server and exposes its tools as `mcp__{name}__{tool}` to the model.
- [ ] A client MCP server is **session-scoped** — a second session never sees the first session's servers.
- [ ] Invalid/malformed server entries are skipped with a logged warning; the session still starts with config-only servers. Never fail the session on client server config.
- [ ] `session/load` rebuilds the same client-server toolset when the client re-supplies `mcpServers`; absent `mcpServers` falls back to config-only.
- [ ] Server resources are bounded by `acp.max_mcp_servers_per_session`; excess servers are rejected with a warning.
- [ ] `acp.client_mcp_servers: false` (default) ignores the array exactly as today.
- [ ] Tests: tool discovery, namespace isolation, invalid-config skip, per-session isolation, config-gate off. Full quality gates pass (`ruff`, `format --check`, `mypy`, `pytest`).
- [ ] Capability advertisement unchanged until the feature is behind a config that can be enabled (no false advertising of an off-by-default feature).

### Effort

**S** (small–medium). Reuses `McpClient.add_server_named`, `connect_all`, and agent injection; the work is normalization + wiring + tests.

---

## Phase 2 — Session Management Completeness 📋

### Scope

- Implement `session/list` (enumerate active ACP sessions with metadata) and `session/close` (tear down agent + MCP clients, free resources) if the pinned ACP SDK version supports them; otherwise advertise only what exists.
- Reconcile capability advertisement: `load_session=True` stays truthful; add `listSessions`/`closeSession` only when fully implemented.
- On `close`, disconnect MCP clients before agent teardown (mirror `NovaAcpAgent.close()` ordering).

### Acceptance Criteria

- [ ] `session/close` releases all subprocesses (MCP stdio servers) and removes session state — verifiable via process listing in tests.
- [ ] Prompting or loading a closed session returns a structured error, not a hang.
- [ ] `session/list` returns only live sessions; metadata matches Nova's session store.
- [ ] Tests cover close-then-prompt, close-then-load, double-close (idempotent).

### Effort

**S**, pending SDK method availability.

---

## Phase 3 — Editor UX Parity 📋

Deferred, listed for roadmap visibility. Each item is a separate workstream with its own RED-first TDD patch (per REPORT-002 binding decision).

| Item | Notes | Effort |
|------|-------|--------|
| Images / @-mentions | Prompt content currently restricted to text (`TextContentBlock`); requires extending prompt normalization + model input support | M |
| Edit review / following | Diffs + `update_agent_message` with file-edit presentation; needs a diff renderer for tool results | M |
| TODO lists | `session/update` with plan/task updates from agent planning; nova has no plan artifact today — requires one | L |
| Nested subagent transcripts | Advertise via `clientCapabilities._meta["subagent-transcript"]`; relay `delegate_task` events with parent tool-use linkage | M |
| Slash commands | Route TUI slash commands (`/skill-*`, `/sessions`, …) through ACP prompt content when prefixed | S–M |

## Phase 4 — Remote Transports & Extensions 📋

- HTTP / WebSocket transport (ACP spec marks remote as work-in-progress — block on upstream stability before committing).
- Provider-neutral extensions: goal, session-failure, permission (structured errors, editable choices, durable effects) — port only when the ACP spec or SDK stabilizes them.

---

## Out of Scope

- **Remote (HTTP/WS) transport** — blocked on ACP spec maturity (Phase 4).
- **Non-text prompt content** — Phase 3, not Phase 1.
- **Editor-specific integrations** — Nova speaks ACP; it will not ship Zed/JetBrains/emacs plugins.
- **MCP resource passthrough beyond `mcp_read_resource`** — client servers' resources ride the existing tool, no new surface.
- **Persistent client-server registries across restarts** — `session/load` only restores what the client re-supplies.

## Trade-offs

| Decision | Alternative | Rationale |
|----------|-------------|-----------|
| Client MCP servers opt-in via `acp.client_mcp_servers` (default `false`) | On by default for instant parity | Stdio MCP = arbitrary subprocess execution from a remote-triggerable surface; secure-by-default matches Nova's permission model. Users flip one flag for parity. |
| Reuse existing `McpClient` + agent injection | New ACP-specific MCP transport | `McpClient` already has stdio/HTTP/SSE, name namespacing, locking, and tool refresh; a second path duplicates security-sensitive code. |
| Skip invalid servers, never fail the session | Reject `session/new` on bad config | ACP clients send whatever they have; a broken server shouldn't brick the whole session. Log + continue. |
| Per-session MCP clients | Shared client across sessions | Session isolation is an existing binding decision; a shared client would leak tools and subprocesses across workspaces. |
| Advertise capabilities only when implemented | Advertise full parity early | Existing binding decision; keeps the adapter honest with clients (REPORT-002). |

## Related Documentation

| Document | Purpose |
|----------|---------|
| [REPORT-002 ACP Implementation Handoff](REPORT-002-ACP_IMPLEMENTATION_HANDOFF.md) | Current implementation state, binding decisions, verification evidence |
| [GUIDE-007 MCP Integration](GUIDE-007-MCP_INTEGRATION.md) | Nova's existing MCP client architecture (stdio/HTTP/SSE) |
| [GUIDE-012 Session Management](GUIDE-012-SESSION_MANAGEMENT.md) | SQLite session storage and lifecycle backing `session/load` |
| [GUIDE-010 Roadmap](GUIDE-010-ROADMAP.md) | Broader project direction and release state |
| [Documentation Index](DOCUMENTATION_INDEX.md) | Complete project documentation inventory |
