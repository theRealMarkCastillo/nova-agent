# Nova Agent Roadmap

**Status:** ✅ Active
**Last Updated:** November 2026
**Type:** GUIDE (Developer Reference)

Nova is release-ready. The roadmap now tracks priorities rather than fixed calendar phases so implementation can follow product value and integration readiness.

## Current State

- ✅ Core agent loop, streaming TUI, token budgets, context compaction, and searchable session history
- ✅ SQLite session storage with session- and message-level FTS5 indexes
- ✅ Built-in tools for files, search, terminal, Git, HTTP, web, wiki memory, skills, tasks, and delegation
- ✅ Permissions, hooks, cost tracking, retries, parallel read-only execution, and harness observability
- ✅ MCP client support for stdio, HTTP, and SSE transports
- ✅ Firecrawl integration with credit-aware confirmation, bounded results, path safety, and untrusted-content labeling
- ✅ Security and reliability hardening across MCP, web tools, sessions, context handling, background tasks, Git, and wiki memory
- ✅ Release validation: 1169 tests passing, 83.72% coverage, Ruff clean, and mypy clean

## Now: Release Readiness ✅

The current implementation is ready for release. Remaining work in this bucket is release execution rather than a feature gap:

- ✅ Validate installation, configuration examples, supported Python versions, and CI gates
- ✅ Verify security boundaries and secret handling
- ✅ Keep documentation aligned with shipped behavior
- 📋 Publish the release and maintain release notes

## Next: Reliability and Long Sessions 🟡

Prioritize predictable behavior during long sessions and provider failures:

- 📋 Multi-model fallback when a configured model or provider is unavailable
- 📋 Session archival and retention controls for large session databases
- 📋 Structured output mode for workflows that require validated JSON
- 📋 Additional end-to-end reliability tests for compaction, retries, background tasks, and delegation
- 📋 Resource cleanup and cancellation tests across all long-running operations

## Next: Web and MCP Integrations 🟡

Expand integrations without weakening safety or observability:

- 📋 Broader MCP interoperability testing against representative stdio, HTTP, and SSE servers
- 📋 Clearer MCP connection diagnostics and transport compatibility reporting
- 📋 Additional Firecrawl workflows and usage controls based on real release feedback
- 📋 Integration tests for web and MCP failures, timeouts, malformed responses, and provider limits

## Next: Callable Agent 📋

Prioritize delegation from another coding agent or application through the CLI and Python SDK. Follow [SPEC-006](SPEC-006-CALLABLE_AGENT_PLAN.md):

- 📋 Shared typed run results and progress events
- 📋 Noninteractive JSON/JSONL CLI output
- 📋 Explicit permissions, budgets, cancellation, and session ownership
- 📋 Public Python invocation API and real calling-agent validation

ACP and IDE integration have been retired; the shared engine capabilities remain.

## Later: Optional Capabilities 📋

These remain possible future investments, but are not committed to a calendar or release:

- 📋 Plugin distribution and third-party tool/skill packaging
- 📋 More provider metadata and model-routing controls
- 📋 Additional editor and automation integrations driven by user demand

## Not Planned

The following remain deliberate trade-offs:

- ❌ GUI application: Nova is terminal-first by design
- ❌ Mobile app: Nova is a desktop developer tool
- ❌ Team collaboration suite, shared sessions, RBAC, and enterprise audit logging: not current product priorities
- ❌ Multi-language UI: English-first for now

## Decision Rules

1. Security and correctness work takes precedence over new integrations.
2. New protocol capabilities are not advertised until their complete behavior is implemented and tested.
3. Optional integrations must remain opt-in and must not add prompt or dependency cost when disabled.
4. Every reliability change should include a regression test and preserve the repository quality gates.

## Related Documentation

| Document | Purpose |
|----------|---------|
| [Documentation Index](DOCUMENTATION_INDEX.md) | Full inventory of project documentation |
| [Callable Agent Plan](SPEC-006-CALLABLE_AGENT_PLAN.md) | CLI and SDK delegation milestone |
| [MCP Integration](GUIDE-007-MCP_INTEGRATION.md) | MCP transports, configuration, and security behavior |
| [Context Compaction](GUIDE-011-CONTEXT_COMPRESSION.md) | Long-session context management and historical retrieval |
| [Contributing](../CONTRIBUTING.md) | Development setup and contribution workflow |
