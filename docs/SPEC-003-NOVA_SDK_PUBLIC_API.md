# SPEC-003: Nova SDK — Public API Surface

**Status:** 🟡 Callable subset implemented; broader SDK planned
**Last Updated:** November 2026
**Type:** SPEC (Technical Specification)
**Author:** Eidolon Labs LLC

---

## Quick Reference

| Item | State |
|------|-------|
| Goal | Declare a stable, documented, semver'd public API for building on Nova's engine |
| Decision | [ADR-004](ADR-004-SDK_PRODUCTIZATION.md) — full productization, no new engine features |
| Surface | `NovaAgent`, typed options, streaming events, harness traces, stores, MCP |
| Deliverables | Public contract + `__init__` exports, API docs, PyPI release pipeline, deprecation policy |
| Code changes | **None in this document** — contract and acceptance criteria only |

---

## Implemented Callable Subset

✅ The current root exports are `run_task`, `NovaOptions`, `NovaRunResult`, `NovaUsage`, `NovaError`, and `NovaEvent`, plus `__version__`. [SPEC-006](SPEC-006-CALLABLE_AGENT_PLAN.md) defines the implemented invocation contract; [Using Nova](GUIDE-009-USING_NOVA.md#calling-nova-from-another-agent) documents options, events, exit codes, permissions, limits, and session ownership. The same contract powers machine CLI modes.

The broader API below remains a proposal. `NovaAgent`, stores, MCP configuration types, and configuration helpers have not been added to the root exports or frozen by this work. Real Codex-to-Nova acceptance against DeepSeek has passed the callable milestone's repair/resume/blocked/cancellation scenarios; this is bounded acceptance evidence, not a general live-model quality benchmark. Package publication remains pending.

## Problem

`NovaAgent` is already usable as a library, but the surface is accidental: every `nova.*` module is importable, nothing is documented as public/private, there are no API docs, and there is no versioning promise. Application callers and third-party consumers cannot safely build on it. We need a **curated, frozen, documented contract** — with the rest of the package marked internal.

## Solution

Declare the public API as a small set of stable entry points, exported from the package root, with everything else internal. The engine (`NovaAgent`) stays exactly as it is; the SDK work is **contract curation, ergonomics, docs, and release process** — no new agent features.

## Architecture

```
nova-agent (PyPI: nova-agent)
│
├── nova/__init__.py          ← THE public surface (curated exports only)
│     NovaAgent · NovaOptions · NovaRunResult · NovaEvent
│     SessionStore · WikiMemory · load_config · CostTracker
│     hooks · McpStdioConfig/McpHttpConfig/McpSseConfig
│
├── nova/agent.py             ← engine (public class, private methods)
├── nova/*.py                 ← internal unless re-exported
│
└── consumers (all thin shells on the same surface)
      ├── nova chat / ask      (CLI)
      ├── AgentCore adapter    (planned — RESEARCH-001)
      └── third-party apps     (nova-sdk consumers)
```

**Rule:** if it isn't re-exported from `nova/__init__.py`, it is internal — no stability promise, may change at any time.

## Public API

### `NovaAgent` (engine — already exists, frozen as-is)

```python
class NovaAgent:
    def __init__(
        self,
        config: dict | None = None,  # or NovaOptions (below)
        session_id: str | None = None,  # resume a persisted session
        openai_client: Any | None = None,  # injectable HTTP client
        session_store: SessionStore | None = None,
        wiki_memory_store: WikiMemory | None = None,
        prompt_mode: str = "full",  # full | minimal | none
        confirmation_callback: Callable[[str, dict], bool] | None = None,
        workspace: Path | None = None,  # context + relative tool root
        mcp_client: Any | None = None,  # injectable MCP client
    ) -> None: ...

    def run(
        self,
        user_message: str,
        stream: bool = True,
        stream_callback: Callable[[str], None] | None = None,
    ) -> str: ...  # final response text

    def close(self) -> None: ...  # persist, disconnect MCP, release resources

    # Public state (read-only contract):
    session_id: str
    messages: list[dict]
    workspace: Path
    last_run_trace: HarnessTrace | None  # SPEC-001 verification trace
```

### `NovaOptions` (new — typed config facade)

A typed dataclass that maps 1:1 onto the config dict, so callers don't hand-roll YAML:

```python
@dataclass
class NovaOptions:
    model: str = "qwen/qwen3.6-flash"  # any OpenRouter/OpenAI-compatible ID
    system_prompt_mode: str = "full"
    budgets: dict[str, int] = field(default_factory=dict)  # token budgets
    workspace: Path | None = None
    mcp_servers: list[McpServerConfig] = field(default_factory=list)
    permissions: dict | None = None  # allow/deny overrides
    session_dir: Path | None = None
    wiki_vault: Path | None = None
    # ... mirrors config.yaml keys; unknown keys rejected at build time


def build_config(options: NovaOptions, base_config: dict | None = None) -> dict: ...
```

### Events (new — typed streaming)

Unify the existing engine callbacks into one typed event stream:

```python
@dataclass
class NovaEvent: ...  # base


@dataclass
class AgentMessageChunk(NovaEvent):
    text: str


@dataclass
class ToolCallEvent(NovaEvent):
    call_id: str
    name: str
    status: Literal["pending", "in_progress", "completed", "failed"]
    kind: Literal["read", "edit", "search", "execute", "fetch", "other"]
    raw_input: dict | None = None
    raw_output: str | None = None


@dataclass
class RunFinished(NovaEvent):
    run_id: str
    status: Literal["verified", "completed", "failed", "inconclusive"]  # SPEC-001
    output: str | None = None
```

```python
def run_stream(
    agent: NovaAgent,
    user_message: str,
    on_event: Callable[[NovaEvent], None],
) -> NovaRunResult: ...  # convenience wrapper over run() + callbacks
```

### Stores & utilities (already public, documented as stable)

| Symbol | Notes |
|--------|-------|
| `SessionStore` | SQLite sessions — used for resume; persistent session IDs |
| `WikiMemory` | Obsidian vault memory (`Core/` auto-inject) |
| `load_config()` | Layered YAML + env resolution |
| `CostTracker` | Token + dollar tracking |
| `hooks` (registry) | `pre_tool_call`, `post_tool_call`, `pre_llm_call`, `post_llm_call`, `session_start`, `session_end` |
| `McpStdioConfig` / `McpHttpConfig` / `McpSseConfig` | MCP server configs — injectable via `NovaAgent(mcp_client=...)` |

## Examples

```python
from nova import NovaAgent, NovaOptions, run_stream, ToolCallEvent, RunFinished

options = NovaOptions(
    model="openai/gpt-4o-mini",
    workspace=Path("/Users/mark/Projects/hasu"),
    mcp_servers=[
        McpStdioConfig(
            command="npx", args=["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
        )
    ],
)


def on_event(event):
    if isinstance(event, ToolCallEvent):
        print(f"[{event.kind}] {event.name} → {event.status}")
    elif isinstance(event, RunFinished):
        print(f"done: {event.status}")


agent = NovaAgent(config=build_config(options))
try:
    result = run_stream(agent, "Refactor the auth module", on_event=on_event)
    assert result.status in {"verified", "completed"}  # harness contract
finally:
    agent.close()
```

## Acceptance Criteria

- [ ] `nova/__init__.py` exports exactly the documented surface; every other module is internal (no accidental `import nova.anything` stability promise).
- [ ] `NovaOptions` accepts every key that `config.yaml` accepts, rejects unknown keys, and round-trips through `build_config()` into an identical agent behavior (tested against the existing test suite).
- [ ] `run_stream()` emits typed events covering: message chunks, tool lifecycle (pending → in_progress → completed/failed), and `RunFinished` with a SPEC-001 status.
- [ ] API reference (mkdocs or equivalent) generated from docstrings; quickstart + two examples (basic chat, MCP-enabled agent) in a `examples/` dir.
- [ ] PyPI pipeline: `nova-agent` publishes from CI on version tags; semver policy + deprecation policy documented in `CONTRIBUTING.md`.
- [ ] CLI (`nova chat`, `nova ask`) consumes only the public surface — proof the contract is complete.
- [ ] Full quality gates pass (`ruff`, `format --check`, `mypy`, `pytest`); coverage unchanged or better.

## Trade-offs

| Decision | Alternative | Rationale |
|----------|-------------|-----------|
| Curated exports from package root | Export everything (status quo) | One stable door; everything else free to evolve |
| Typed `NovaOptions` facade | Raw `dict` config forever | Typed config = discoverability + validation; dict still supported for compat |
| New `run_stream()` + typed events | Raw `stream_callback` strings | Existing raw callbacks are sufficient but unergonomic; events are the v1 contract |
| Python-only (v1) | TS/JS SDK alongside | Niche is Python agent builders; a TS SDK is a v2 conversation, not v1 scope |
| Stabilize, don't build | Add SDK-specific engine features | ADR-004 binding: no new engine features in the SDK work itself |

## Out of Scope

- New engine features (planning, new tools, async loop rewrite) — engine work is tracked separately.
- TypeScript/JS SDK — v2 conversation.
- LangChain-style framework adapters — SDK is the primitive; adapters are consumer projects.
- AgentCore adapter implementation — scoped in [RESEARCH-001](RESEARCH-001-AGENTCORE_HOSTING.md).

## Related Documentation

| Document | Purpose |
|----------|---------|
| [ADR-004 SDK Productization](ADR-004-SDK_PRODUCTIZATION.md) | The decision this spec implements |
| [SPEC-001 Harness Engineering](SPEC-001-HARNESS_ENGINEERING.md) | Verification states and traces exposed via `RunFinished` / `last_run_trace` |
| [SPEC-006 Callable Agent](SPEC-006-CALLABLE_AGENT_PLAN.md) | First SDK milestone and machine-readable CLI contract |
| [RESEARCH-001 AgentCore Hosting](RESEARCH-001-AGENTCORE_HOSTING.md) | Future consumer of the SDK |
| [GUIDE-003 Customizing](GUIDE-003-CUSTOMIZING.md) | Config reference that `NovaOptions` must mirror |
| [Documentation Index](DOCUMENTATION_INDEX.md) | Complete project documentation inventory |
