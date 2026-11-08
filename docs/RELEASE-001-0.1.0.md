# RELEASE-001: Nova Agent 0.1.0

**Status:** ✅ Active
**Last Updated:** August 2026
**Type:** RELEASE (Customer-Facing Changelog)

> November 2026 update: ACP has been removed. References below describe historical functionality or rationale, not a supported interface. The current direction is [CLI and SDK delegation](SPEC-006-CALLABLE_AGENT_PLAN.md).

## Summary

Nova Agent 0.1.0 is an alpha release focused on local, secure, and observable agent workflows.

## Included

- ✅ OpenRouter-compatible chat and one-shot CLI workflows
- ✅ Persistent SQLite sessions with FTS5 search and legacy schema migrations
- ✅ Tool permissions, protected path checks, prompt-injection scanning, and bounded execution
- ✅ MCP client support for stdio, HTTP, and SSE transports
- ✅ ACP stdio server with session loading, workspace isolation, cancellation, and tool-call progress
- ✅ Skills, wiki memory, background tasks, retries, cost tracking, and context management
- ✅ Python 3.12 and 3.13 CI validation

## Quality Baseline

- ✅ Full test suite passes
- ✅ Ruff lint and format checks pass
- ✅ Mypy passes for `nova/`
- ✅ Coverage remains above the 70% project threshold

## Known Limitations

- ACP permission bridging and client-provided MCP configuration are not yet implemented.
- Sessions are persisted locally; cold-storage archival is not implemented.
- The terminal tool is intentionally not sandboxed and runs with the user's OS permissions.
- Model availability and pricing depend on the configured OpenRouter-compatible provider.

## Upgrade Notes

Existing session databases are migrated automatically when Nova starts. The migration adds missing
message columns without deleting existing conversation history.

## Related Documentation

| Document | Purpose |
|----------|---------|
| [README](../README.md) | Installation and user guide |
| [ACP Handoff](REPORT-002-ACP_IMPLEMENTATION_HANDOFF.md) | ACP implementation details |
| [Security Policy](../SECURITY.md) | Security model and reporting |
| [Roadmap](GUIDE-010-ROADMAP.md) | Planned work |
