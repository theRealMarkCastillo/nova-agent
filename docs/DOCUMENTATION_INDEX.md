# Nova Agent — Documentation Index

**Last Updated:** October 2026
**Status:** ✅ Active
**Type:** GUIDE (Documentation Index)
**Maintainer:** [Eidolon Labs LLC](https://github.com/eidolonlabs-ai)

> Systematic inventory of all Nova Agent documentation.

---

## 🎯 Start Here

| Document | Purpose |
|----------|---------|
| **[README](../README.md)** | Project overview, features, quick start, installation |
| **[GUIDE-003-CUSTOMIZING](GUIDE-003-CUSTOMIZING.md)** | Config, SOUL.md, models, budgets, skills, wiki memory — the full guide |
| **[CONTRIBUTING](../CONTRIBUTING.md)** | Development setup, code standards, PR workflow |

---

## 📚 Guides (GUIDE-NNN)

| Document | Status | What It Covers |
|----------|--------|----------------|
| [GUIDE-001-CREATING_TOOLS](GUIDE-001-CREATING_TOOLS.md) | ✅ Active | Build custom tools: schema, handler, registration, tests |
| [GUIDE-002-CREATING_SKILLS](GUIDE-002-CREATING_SKILLS.md) | ✅ Active | Write SKILL.md files for specialized knowledge domains |
| [GUIDE-003-CUSTOMIZING](GUIDE-003-CUSTOMIZING.md) | ✅ Active | Config, SOUL.md, models, token budgets, tools, sessions |
| [GUIDE-004-BACKGROUND_TASKS](GUIDE-004-BACKGROUND_TASKS.md) | ✅ Active | Fire-and-forget shell execution with status tracking |
| [GUIDE-005-COST_TRACKING](GUIDE-005-COST_TRACKING.md) | ✅ Active | Token usage, dollar cost estimation, per-model pricing |
| [GUIDE-006-HOOKS](GUIDE-006-HOOKS.md) | ✅ Active | Lifecycle callbacks: pre/post tool call, LLM call, session |
| [GUIDE-007-MCP_INTEGRATION](GUIDE-007-MCP_INTEGRATION.md) | ✅ Active | Connect stdio, HTTP, and SSE Model Context Protocol servers |
| [GUIDE-008-PERMISSIONS](GUIDE-008-PERMISSIONS.md) | ✅ Active | Defense-in-depth cascade, allow/deny lists, path rules, opinionated profiles |
| [GUIDE-009-USING_NOVA](GUIDE-009-USING_NOVA.md) | ✅ Active | Effective use patterns: task descriptions, sessions, wiki memory, tools |
| [GUIDE-010-ROADMAP](GUIDE-010-ROADMAP.md) | ✅ Active | Priority buckets, release state, completed and planned work |
| [GUIDE-011-CONTEXT_COMPRESSION](GUIDE-011-CONTEXT_COMPRESSION.md) | ✅ Active | Deterministic compaction and searchable historical retrieval |
| [GUIDE-012-SESSION_MANAGEMENT](GUIDE-012-SESSION_MANAGEMENT.md) | ✅ Active | SQLite session storage, FTS5 search, commands, lifecycle |
| [GUIDE-013-MEMORY_SYSTEM](GUIDE-013-MEMORY_SYSTEM.md) | ✅ Active | Obsidian-compatible wiki memory: markdown notes, `[[wikilinks]]`, `Core/` auto-inject, maintenance |
| [GUIDE-014-RETRY_AND_ERROR_HANDLING](GUIDE-014-RETRY_AND_ERROR_HANDLING.md) | ✅ Active | Exponential backoff, error classification, retry configuration |
| [GUIDE-015-WEB_TOOLS](GUIDE-015-WEB_TOOLS.md) | ✅ Active | Firecrawl search, scrape, map, crawl, extract, document parsing |

---

## 🏗️ Architecture Decision Records (ADR-NNN)

| Document | Status | What It Covers |
|----------|--------|----------------|
| [ADR-001-SUBAGENT_COMPARISON](ADR-001-SUBAGENT_COMPARISON.md) | ✅ Active | Sub-agent architecture comparison and tradeoffs |
| [ADR-002-SUBAGENT_DESIGN](ADR-002-SUBAGENT_DESIGN.md) | ✅ Active | Sub-agent design decisions and implementation approach |
| [ADR-003-TOOL_SYSTEM_REVIEW](ADR-003-TOOL_SYSTEM_REVIEW.md) | ✅ Active | Tool system design review and architectural notes |
| [ADR-004-SDK_PRODUCTIZATION](ADR-004-SDK_PRODUCTIZATION.md) | ✅ Accepted | Productize Nova as a public Python SDK: contract, semver, docs, publishing |

---

## 📐 Technical Specifications (SPEC-NNN)

| Document | Status | What It Covers |
|----------|--------|----------------|
| [SPEC-001-HARNESS_ENGINEERING](SPEC-001-HARNESS_ENGINEERING.md) | ✅ Active | Harness engineering: verification, acceptance states, unified traces, and optional Langfuse telemetry |
| [SPEC-002-ACP_INTEGRATION](SPEC-002-ACP_INTEGRATION.md) | 📋 Planned | ACP editor parity: client MCP servers, session management, UX features, remote transports |
| [SPEC-003-NOVA_SDK_PUBLIC_API](SPEC-003-NOVA_SDK_PUBLIC_API.md) | 📋 Planned | Public API surface: NovaAgent, typed options/events, harness traces, stores, PyPI |
| [SPEC-004-STASH_RECOVERY_PLAN](SPEC-004-STASH_RECOVERY_PLAN.md) | ✅ Implemented | Integrated updater and nested cache-usage fixes and verification evidence |
| [SPEC-005-REVIEW_IMPROVEMENT_PLAN](SPEC-005-REVIEW_IMPROVEMENT_PLAN.md) | 🟡 In Progress | October 2026 code/design review: phases 0–2 and most of 3 implemented; M7, M6, M4 remain |

---

## 📋 Reports (REPORT-NNN)

| Document | Status | What It Covers |
|----------|--------|----------------|
| [REPORT-001-PROJECT_STATUS_2026-05-02](REPORT-001-PROJECT_STATUS_2026-05-02.md) | ✅ Active | Test coverage baseline, CI status, open work as of May 2026 |
| [REPORT-002-ACP_IMPLEMENTATION_HANDOFF](REPORT-002-ACP_IMPLEMENTATION_HANDOFF.md) | ✅ Active | ACP implementation state, decisions, verification, and follow-on work |

---

## 🔬 Research (RESEARCH-NNN)

| Document | Status | What It Covers |
|----------|--------|----------------|
| [RESEARCH-001-AGENTCORE_HOSTING](RESEARCH-001-AGENTCORE_HOSTING.md) | 📋 Planned | Amazon Bedrock AgentCore as a hosting target: harness vs Runtime, adapter pattern, gaps |

---

## 🚀 Releases (RELEASE-NNN)

| Document | Status | What It Covers |
|----------|--------|----------------|
| [RELEASE-001-0.1.0](RELEASE-001-0.1.0.md) | ✅ Active | Customer-facing changelog for the 0.1.0 release |

---

## 🧩 Starter Skills

Skills live in `config/skills/` — copy to `~/.nova/skills/` to activate.

| Skill | Category | Status | What It Covers |
|-------|----------|--------|----------------|
| `python-coding` | development | ✅ Active | Type hints, PEP 8, pytest, ruff, mypy, venvs |
| `git-workflow` | development | ✅ Active | Branching, committing, pushing, PRs |
| `file-editing` | development | ✅ Active | Safe file editing patterns, verification steps |
| `code-review` | development | ✅ Active | Code review conventions and checklists |
| `documentation-template-builder` | development | ✅ Active | Generate docs in ai-companions style — README, Roadmap, GUIDE, PRD, PERSONA, SPEC, ADR, RUN, RELEASE, STRATEGY, RESEARCH, GTM, REPORT |
| `nova-development` | development | ✅ Active | Tool system, permissions, hooks, delegation, testing patterns, config, CI |
| `nova-debugging` | development | ✅ Active | Loops, hallucinations, context drift, tool failures, permission issues |
| `debugging` | development | ✅ Active | Reproduce, isolate, bisect, instrument, root-cause analysis, profiling |
| `refactoring` | development | ✅ Active | Behavior-preserving changes, seams, incremental steps, verification |
| `test-driven-development` | testing | ✅ Active | Red-green-refactor, test pyramid, mocking, coverage gates |
| `ci-cd` | devops | ✅ Active | Lint/type/test/coverage gates, GitHub Actions, semver, changelogs, rollback |
| `operations` | devops | ✅ Active | Monitoring, logging, incident response, on-call, postmortems, runbooks |
| `planning` | engineering | ✅ Active | User stories, acceptance criteria, task breakdown, estimation, DoD |
| `system-design` | engineering | ✅ Active | Architecture, components, data models, API contracts, trade-offs, ADRs |
| `security-review` | security | ✅ Active | Threat modeling, secrets handling, dependency audits, OWASP, prompt injection |
| `example-skill` | general | ✅ Active | Template demonstrating slash-command-triggered skills |

---

## 📊 Documentation Status Summary

| Category | Count | Status |
|----------|-------|--------|
| Guides (GUIDE-NNN) | 15 | ✅ All current |
| ADRs (ADR-NNN) | 4 | ✅ All current |
| Specs (SPEC-NNN) | 3 | ✅ All current |
| Research (RESEARCH-NNN) | 1 | ✅ Current |
| Releases (RELEASE-NNN) | 1 | ✅ All current |
| Reports (REPORT-NNN) | 2 | ✅ Current |
| Starter skills | 16 | ✅ All current |
| Root docs (README, CONTRIBUTING, SECURITY, AGENTS) | 4 | ✅ All current |
| **Total** | **46** | ✅ Current |

**Supported doc type prefixes:** GUIDE · PRD · PERSONA · SPEC · ADR · RUN · RELEASE · STRATEGY · RESEARCH · GTM · REPORT

---

## 🆘 Troubleshooting

1. **Can't connect to LLM API** → Check `LLM_API_KEY` env var or `llm.api_key` in `config.yaml`
2. **Tool blocked unexpectedly** → See [GUIDE-008-PERMISSIONS](GUIDE-008-PERMISSIONS.md) — check `denied_tools` and `path_rules`
3. **Context too long / compaction** → See [GUIDE-011-CONTEXT_COMPRESSION](GUIDE-011-CONTEXT_COMPRESSION.md) — adjust active context and retrieval settings
4. **MCP server not appearing** → See [GUIDE-007-MCP_INTEGRATION](GUIDE-007-MCP_INTEGRATION.md#troubleshooting)
5. **Skills not loading** → Check `~/.nova/skills/<name>/SKILL.md` exists with valid YAML frontmatter

## Related Documentation

| Document | Purpose |
|----------|---------|
| [Project Overview](../README.md) | Installation and project entry point |
| [Contribution Guide](../CONTRIBUTING.md) | Development workflow |
| [Stash Recovery Plan](SPEC-004-STASH_RECOVERY_PLAN.md) | Verified pending fixes and implementation steps |
