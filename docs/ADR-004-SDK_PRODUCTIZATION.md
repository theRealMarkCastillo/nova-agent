# ADR-004: Productize Nova as a Public Python SDK

**Status:** ✅ Accepted (pending SPEC-003)
**Last Updated:** August 2026
**Type:** ADR (Architecture Decision)
**Author:** Eidolon Labs LLC

> November 2026 update: ACP has been removed. References below describe historical functionality or rationale, not a supported interface. The current direction is [CLI and SDK delegation](SPEC-006-CALLABLE_AGENT_PLAN.md).

---

## Problem

Nova is architecturally already a library — `NovaAgent` is a public class with `run()`, streaming, injected dependencies, and a thin CLI shell (`nova chat`, `nova ask`, `nova acp`). Every adapter we've built (ACP, and the planned AgentCore) is a thin consumer of that engine, mirroring how Anthropic structures Claude Code CLI and `claude-agent-acp` on the Claude Agent SDK.

But the library surface is **accidental, not contractual**: every module is importable, there are no API docs, no deprecation policy, and no semver promise. The question is whether to productize — and at what depth.

## Options Considered

### Option 1: Status quo (library by accident)
**Pros:** Zero effort; internal freedom to refactor anything.  
**Cons:** Nobody can safely build on nova; third-party usage breaks on any internal change; no ecosystem; the SDK story is "technically true but unusable."

### Option 2: Full SDK productization (Chosen)
**Pros:** Stable public contract, semver, API docs, PyPI releases; enables the one-engine-many-surfaces architecture (CLI, ACP, AgentCore, third-party apps); differentiator: SPEC-001 harness traces/verification exposed as first-class API — something claude-sdk lacks.  
**Cons:** API stability commitment is a real tax (deprecation cycles, changelogs); curation effort; scope-creep risk ("our SDK needs X!").

### Option 3: SDK-lite (document existing classes, no freeze)
**Pros:** Cheap docs win.  
**Cons:** Half a promise — documentation without a stability contract is still a breaking-change liability; ecosystem builders can't rely on it.

## Decision

**We chose Option 2 — full SDK productization, scoped.** We declare a curated public API surface (see [SPEC-003](SPEC-003-NOVA_SDK_PUBLIC_API.md)), mark everything else internal, adopt semver + deprecation policy, publish to PyPI, and document the surface. The scope is **stabilize and expose what exists — no new engine features** in the SDK work itself.

What tipped the scales:

1. **We keep proving the adapter pattern** (ACP ~290 lines; AgentCore is the same shape). An SDK makes that the official architecture instead of an accident.
2. **The ACP work already forced the ergonomics** — streaming callbacks, tool lifecycle events, cancellation, permission callbacks are real and tested.
3. **SPEC-001 gives us a moat** — verification states and run traces as typed public API is something neither claude-sdk nor most agent frameworks offer.
4. **The cost is mostly discipline, not code** — the engine exists; the work is contract curation, docs, and release process.

## Consequences

**Good:**
- ✅ One engine, many surfaces: CLI, `nova acp`, AgentCore adapter, third-party consumers
- ✅ Public API contract + semver = safe to build on
- ✅ Harness traces/verification become a flagship SDK feature
- ✅ PyPI publishing discipline (changelog, releases) professionalizes the project

**Bad:**
- ⚠️ API stability commitment — internal refactors must route through deprecation cycles
- ⚠️ Curation burden: private helpers must be walled off (`_`-private or internal namespaces)
- ⚠️ Expectation management: public surface invites feature requests; the answer is often "no, that's out of scope for the SDK"

## Related Documentation

| Document | Purpose |
|----------|---------|
| [SPEC-003 Nova SDK Public API](SPEC-003-NOVA_SDK_PUBLIC_API.md) | The concrete public surface this decision enables |
| [SPEC-001 Harness Engineering](SPEC-001-HARNESS_ENGINEERING.md) | Verification/traces layer the SDK exposes as its differentiator |
| [SPEC-002 ACP Integration](SPEC-002-ACP_INTEGRATION.md) | First adapter built on the engine; pattern for future surfaces |
| [RESEARCH-001 AgentCore Hosting](RESEARCH-001-AGENTCORE_HOSTING.md) | Hosted deployment target that would consume the SDK |
| [REPORT-002 ACP Implementation Handoff](REPORT-002-ACP_IMPLEMENTATION_HANDOFF.md) | Current adapter implementation state |
