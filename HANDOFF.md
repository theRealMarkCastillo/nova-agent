# ACTIVE WORKSTREAM HANDOFF

**Status:** 🟡 In Progress
**Last Updated:** October 2026
**Type:** REPORT (Session Handoff)

- **Workstream:** Code/design review fixes ([SPEC-005](docs/SPEC-005-REVIEW_IMPROVEMENT_PLAN.md))
- **Active Branch:** `main`. 21 commits on top of `863bc26`, all local and **not pushed**.
- **Current Status:** IN_PROGRESS

## 1. Ground Truth & State

SPEC-005 phases 0, 1, and 2 are implemented, along with M5 and M3b from phase 3. Each fix landed as its own commit with regression tests. Most of those tests were confirmed to fail before the fix. See `git log --oneline 863bc26..HEAD` and the per-item status in SPEC-005.

Main behavior changes users may notice:
- `ask` mode: once `http_*`, `web_*`, or MCP output is in the session, later `http_*`/`web_*` calls prompt for confirmation (H1).
- A path `allow: true` rule no longer skips confirmation for `terminal` (H3).
- The permission checker now protects the same broader sensitive-path set as the file tools, e.g. `~/.aws/*` and `.envrc` (S1).
- A flagged `Core/` or pinned wiki note, or a flagged skill description, is replaced in the prompt by a placeholder (M8).
- `NovaAgent.close()` no longer disconnects an injected `mcp_client`, and sub-agents share the parent's (M3b).

Validation at HEAD:
- `.venv/bin/ruff check .` and `.venv/bin/ruff format --check .`: passed.
- `.venv/bin/mypy nova/`: passed, 43 source files.
- `.venv/bin/pytest -q`: 1356 passed, 85.39% coverage (baseline 1302, 84.68%).
- Each test file also passes when run on its own.
- No paid provider calls, network access, or real MCP servers were used; all are mocked.

Retained history from earlier workstreams: stashes `stash@{0}` (`533f21d`) and `stash@{1}` (`023ec2d`) remain intact pending authorized cleanup; the archived worktree bundle remains under `.git/cleanup-archives/`.

## 2. Active Hypothesis & Blockers

Remaining items need maintainer input before starting:
- **M8 auto-mode decision:** should writes to `Core/` or `inject: true` notes require confirmation even in `auto` mode?
- **M4** changes the session database schema (`session_fts` aggregation replaced by `message_search`) and needs a migration of existing user databases.
- **M7** (ToolExecutor plus typed `ToolResult`) is a broad refactor of `nova/agent.py`, best done before freezing SPEC-003's public API.
- **M6** (token accounting anchored on provider-reported usage) is self-contained.

## 3. Immediate Next Action (Start Here)

1. Run `.venv/bin/pytest -q` to confirm 1356 passing.
2. Push only when the maintainer authorizes it.
3. If the maintainer approves continuing, start with M6: `nova/agent.py` `_compact_if_needed` and `nova/microcompact.py` `compact_to_token_budget`, following SPEC-005 Phase 4.

## Related Documentation

| Document | Purpose |
|----------|---------|
| [SPEC-005 Review Improvement Plan](docs/SPEC-005-REVIEW_IMPROVEMENT_PLAN.md) | Findings, per-item status, commits, acceptance checks |
| [GUIDE-008 Permissions](docs/GUIDE-008-PERMISSIONS.md) | Untrusted-content rule, path rules, guardrail limits |
| [GUIDE-014 Retry and Error Handling](docs/GUIDE-014-RETRY_AND_ERROR_HANDLING.md) | Exception classification |
| [Contribution Guide](CONTRIBUTING.md) | Development and validation workflow |
