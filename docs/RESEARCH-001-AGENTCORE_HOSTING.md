# RESEARCH-001: AgentCore Hosting — Amazon Bedrock AgentCore

**Status:** 📋 Planned (research note)
**Last Updated:** August 2026
**Type:** RESEARCH (Competitive / Landscape Analysis)
**Next review:** When AgentCore's code-based Runtime or ACP story materially changes

> November 2026 update: ACP has been removed. References below describe historical functionality or rationale, not a supported interface. The current direction is [CLI and SDK delegation](SPEC-006-CALLABLE_AGENT_PLAN.md).

---

## TL;DR

> Amazon Bedrock AgentCore is AWS's **managed agent harness** — a serverless runtime that hosts agents end-to-end. It offers two paths: a config-based **harness** (declare model/tools/skills, AWS runs the loop) and a code-based **Runtime** (your full agent code in serverless microVMs). Nova fits the **Runtime** path: package NovaAgent behind a thin adapter, exactly like `NovaAcpAgent` wraps the agent for ACP. The orchestration layer AWS calls a "harness" is the same concept as Nova's SPEC-001 harness — the difference is AgentCore provides the *infrastructure* (compute, sandbox, identity, memory, observability) instead of us.

**Our positioning:** Nova is a framework-agnostic engine (any model via OpenRouter, any tool, MCP client, wiki memory, SPEC-001 verification). AgentCore Runtime is a deployment target for that engine, not a replacement for it — and not a competitor to ACP (ACP = editor interop, AgentCore = hosting).

---

## What AgentCore Is

| Fact | Detail |
|------|--------|
| What | Serverless, purpose-built runtime for deploying and scaling AI agents — "the managed agent harness" |
| Path 1: Harness | Config-based (model, tools, skills, instructions) — AWS owns the orchestration loop |
| Path 2: Runtime | Code-based — serverless microVMs (Fargate-like) running **your full agent code**; GA for runtime instances Aug 2026 |
| Framework support | Any — CrewAI, LangGraph, LlamaIndex, Google ADK, OpenAI Agents SDK, Strands, Claude Agent SDK, **custom frameworks** |
| Model support | Any foundation model in or outside Bedrock (Claude, Gemini, OpenAI, Amazon Nova, Llama, Mistral) |
| Protocol support | MCP (deploy servers, gateway for remote MCP) and A2A |
| Sessions | Isolated sessions, persistent memory, async runs up to 8 hours, fast cold starts |
| Ops | Built-in identity (IAM), observability, scaling, VPC support |
| Tooling | `agentcore` CLI (`init` → `deploy` → `invoke --prompt`), AgentCore Python SDK, CDK, AWS SDK, console |

**Key source:** [docs.aws.amazon.com/bedrock-agentcore](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/) — harness concept defined in the [AgentCore harness](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness.html) page.

---

## The Harness Connection

AgentCore's docs define the **agent harness** as "the orchestration layer: a loop that calls the model, picks tools, passes results back, manages context, and handles failures" plus the infrastructure under it (compute, sandbox, tool connections, memory, identity, observability). Their claim: standing one up locally is fast; *production* (concurrency, isolation, identity, state, scaling) is where the work explodes — so they manage it as a service.

This matches Nova's own vocabulary in [SPEC-001](SPEC-001-HARNESS_ENGINEERING.md) (verification, acceptance states, traces) — but AgentCore manages the *runtime* layer, not the *reliability* layer. Nova's harness features (postcondition verification, run status) still live in our code and would ride along inside the container.

## Where Nova Fits

**AgentCore Runtime (code-based), not the config harness.** The config harness replaces our loop with theirs; Runtime runs our loop as-is.

### Adapter pattern (same shape as `NovaAcpAgent`)

```
AWS API / console / agentcore invoke
        │
        ▼
AgentCore Runtime (serverless microVM)
        │  entrypoint: app/MyAgent/main.py
        │  implements AgentCore agent contract (AgentCore Python SDK)
        ▼
NovaAgent.run()   ← untouched engine
        │
        ├── tools (registry + permissions)
        ├── sessions (SQLite), wiki, skills
        ├── MCP client (stdio/http/sse)
        └── SPEC-001 harness verification + traces
```

### Component mapping

| Nova component | AgentCore need | Status |
|----------------|----------------|--------|
| `NovaAgent` loop, tools, permissions | The agent code in the container | ✅ exists |
| SQLite sessions + wiki memory | **Durable storage** — containers are ephemeral; point at S3/EFS/DB or accept cold-start resets | ⚠️ needs wiring |
| OpenRouter provider (OpenAI-compatible) | Works (outbound egress + key); Bedrock-native models also supported | ✅ / user choice |
| `McpClient` (stdio/http/sse) | AgentCore speaks MCP natively; synergizes with SPEC-002 client MCP servers | ✅ |
| SPEC-001 verification/traces | Runs inside container; could feed AgentCore observability | ✅ / optional |

### Known gaps to verify before committing

- **Persistent memory**: a re:Post thread reports the config harness path lacks long-term memory; code-based Runtime should be fine *if* we wire durable storage ourselves.
- **Lifecycle semantics**: exact AgentCore agent-contract interface (start/respond/stream/cancel equivalents) needs a spike against the current Python SDK before scoping the adapter.
- **Cost model**: per-invocation + compute for async runs; right-sized for "nova as a service," wrong for local `nova chat`.

---

## Positioning Implications

| Decision | Recommendation |
|----------|----------------|
| ACP vs AgentCore | Not either/or — ACP = interactive editors over stdio (local); AgentCore = hosted production (API). Nova can do both; same engine, different adapters. |
| Harness vs Runtime | Runtime. We keep our loop, harness verification, and tool model. |
| AgentCore vs Bedrock Agents | AgentCore Runtime — Bedrock Agents owns the orchestration (Lambda action groups, less control); we want to bring our own agent. |
| AgentCore vs self-hosting (ECS/Lambda) | AgentCore wins on managed session isolation, identity, observability; loses on flexibility. Revisit if we outgrow it. |

## Sources

- [Amazon Bedrock AgentCore product page](https://aws.amazon.com/bedrock/agentcore/)
- [AgentCore Developer Guide — Overview](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/what-is-bedrock-agentcore.html)
- [AgentCore harness](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness.html)
- [Develop agents — interfaces (Python SDK, CLI)](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/develop-agents.html)
- [Runtime instances GA announcement (Aug 2026)](https://aws.amazon.com/about-aws/whats-new/2026/08/aws-bedrock-agentcore-runtime-instances-generally-available/)
- [Deploying Claude Agent SDK on AgentCore Runtime (AWS builder guide)](https://builder.aws.com/content/30O5JJPjEeCugL5MAfSM9TTcd9p/deploying-claude-agent-sdk-on-amazon-bedrock-agentcore-runtime)

## Related Documentation

| Document | Purpose |
|----------|---------|
| [SPEC-002 ACP Integration](SPEC-002-ACP_INTEGRATION.md) | Editor-facing adapter; the pattern a future AgentCore adapter would follow |
| [SPEC-001 Harness Engineering](SPEC-001-HARNESS_ENGINEERING.md) | Nova's reliability layer (verification, acceptance states, traces) |
| [GUIDE-007 MCP Integration](GUIDE-007-MCP_INTEGRATION.md) | Nova's MCP client, reusable inside an AgentCore deployment |
| [REPORT-002 ACP Implementation Handoff](REPORT-002-ACP_IMPLEMENTATION_HANDOFF.md) | Implementation state of the first adapter |
| [Documentation Index](DOCUMENTATION_INDEX.md) | Complete project documentation inventory |
