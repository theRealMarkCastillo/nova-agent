# SPEC-006: Callable Nova Agent Implementation Plan

**Status:** 📋 Planned
**Last Updated:** November 2026
**Type:** SPEC (Implementation Plan)

**Goal:** Another coding agent or application can delegate a bounded task to Nova, observe progress, resume the session, and consume an unambiguous result.

**Architecture:** One typed invocation layer wraps the existing engine. The shell CLI and Python SDK consume that layer, sharing permissions, limits, events, and result serialization.

**Tech Stack:** Python 3.12+, existing argparse CLI, dataclasses, existing provider client and SQLite session storage.

**Spec:** This document defines the first callable-agent milestone. It narrows [SPEC-003](SPEC-003-NOVA_SDK_PUBLIC_API.md) to a usable delegation contract while preserving [ADR-004](ADR-004-SDK_PRODUCTIZATION.md)'s direction. Full configuration mirroring, exporting every store and MCP type, and publishing to PyPI are follow-on work.

For implementation, use the executing-plans skill task by task. This plan does not authorize commits, pushes, publication, or paid integration runs.

## Problem and Current Evidence

The user wants Nova to be callable like an agent CLI or SDK from other agents and application code. IDE embedding is not a target.

Source inspected on November 7, 2026:

- `nova/cli.py:cmd_ask` loads config, installs an interactive confirmation callback, prints a string, and closes the agent.
- `nova/agent.py:NovaAgent.run` returns a string and retains a harness trace; `_run` can return an interruption string. Callers must not infer execution outcomes from response text.
- `nova/__init__.py` exports only the package version.
- ACP was removed after this initial assessment; shared engine callbacks remain available for CLI and SDK use.
- Entire tracking is active, but remote history retrieval was blocked by sandbox Keychain access. Current source establishes this baseline; no new test run was performed for planning.

## Quick Reference

| Deliverable | Status | Acceptance |
|------------|--------|------------|
| Shared invocation contract | 📋 Planned | Explicit completion and stop reasons; serializable results |
| Noninteractive CLI | 📋 Planned | One JSON result or ordered JSONL events; no approval prompts |
| Python SDK | 📋 Planned | Public typed entry point with cancellation and permission callbacks |
| Real caller validation | 📋 Planned | Another agent delegates, inspects results, and resumes a session |

## Scope and Global Constraints

- Preserve existing interactive chat and default text `nova ask` behavior.
- Reuse the engine, tool policy, traces, session store, and cost tracker; do not build a second agent loop.
- Add no mandatory agent-framework dependency. Callers in any language can spawn the CLI.
- ACP functionality is retired. IDE integrations, MCP server wrappers, hosted services, and a TypeScript SDK are outside this milestone.
- Use the existing Python >=3.12 floor and local `uv run` tooling.
- CLI and SDK cannot silently grant approval, weaken workspace checks, or elevate a configured permission profile.
- Runs use a fresh session unless a caller explicitly supplies a session ID. Unknown IDs fail instead of silently starting over.
- Resume requires the same canonical workspace; do not attach a session to an unrelated workspace.
- Allow one writer per session across processes. Reject concurrent invocation with `session_busy`; release the lock on crash or normal cleanup.
- Synchronous SDK cancellation is cooperative. Do not claim it can forcibly terminate arbitrary Python threads or third-party callbacks.

## Public Contract

Implement `nova/invocation.py` and export the following from `nova/__init__.py`:

```python
def run_task(
    task: str,
    *,
    options: NovaOptions,
    on_event: Callable[[NovaEvent], None] | None = None,
    confirm: Callable[[str, dict[str, Any]], bool] | None = None,
    cancel_event: threading.Event | None = None,
) -> NovaRunResult: ...
```

`NovaOptions` is a small invocation dataclass, not a replacement for all YAML configuration. Fields: `workspace: Path`, `model: str | None`, `session_id: str | None`, `config_path: Path | None`, `max_iterations: int = 20`, `max_total_tokens: int | None = None`, `max_cost_usd: float | None = None`, `timeout_seconds: float = 300`. Optional fields default to `None`. Explicit invocation options override loaded configuration; do not mutate the caller's data. Reject nonpositive limits, booleans as numbers, NaN/infinity, empty tasks, and nonexistent workspaces before creating a session or calling a provider.

`NovaRunResult` contains `schema_version: 1`, nullable `run_id` and `session_id` for pre-start failures, `output: str`, `status`, `stop_reason`, `verification_status`, `usage`, `error`, and nullable serialized `trace`.

- `status`: `completed`, `blocked`, `cancelled`, `limit_reached`, or `failed`.
- `stop_reason`: `final_response`, `permission_required`, `cancelled`, `timeout`, `token_limit`, `cost_limit`, `iteration_limit`, or `error`.
- `verification_status`: existing harness status or `null` when no trace exists. Completion is not a claim that the task's changes passed verification.
- `usage`: per-invocation input/output/cached token counts, nullable USD cost, and `cost_source` (`reported`, `estimated`, or `unknown`). Do not label missing pricing as zero cost. Include child work in the same invocation accounting.
- `error`: nullable object with stable `code`, sanitized `message`, and `retryable`. Codes include `invalid_options`, `session_not_found`, `session_busy`, `workspace_mismatch`, `provider_error`, `usage_unavailable`, and `internal_error`.

`NovaEvent` is a discriminated union: `run_started`, `message_delta`, `tool_started`, `tool_finished`, `run_finished`. Each event has `schema_version`, `run_id`, and monotonically increasing `sequence`; tool events include a call ID. `run_finished` carries the complete result and occurs exactly once, last. Default events omit raw tool arguments/results; the explicit trace option may expose workspace content and must be documented accordingly.

Run outcomes come from typed engine state, never string-prefix parsing. SDK runtime/configuration failures return failed results; programmer errors in a caller callback propagate after owned resources are cleaned up. CLI maps such unexpected failures to sanitized failed results. Cleanup errors must not hide the original failure.

### CLI interface

```bash
nova ask "Inspect the test failure" --workspace /path/to/repo --output-format json
nova ask --prompt-file - --workspace /path/to/repo --output-format jsonl < task.txt
nova ask "Continue with the proposed fix" --session SESSION_ID --workspace /path/to/repo --output-format json
```

Add `--output-format text|json|jsonl`, `--prompt-file PATH|-`, `--workspace`, `--session`, `--model`, `--config`, `--max-iterations`, `--max-total-tokens`, `--max-cost-usd`, `--timeout-seconds`, and `--include-trace`. A positional task and prompt file are mutually exclusive. JSON and JSONL modes are always noninteractive; they must never consume stdin for confirmation.

JSON stdout contains exactly one result; JSONL stdout contains only events. Diagnostics go to stderr. Exit codes: `0` completed, `1` failed, `2` invalid input/configuration, `3` blocked, `4` limit reached, `130` cancelled. Argument parser errors may use stderr without a result; failures after parsing use the selected output contract. Ordinary text mode remains compatible.

### Permissions, limits, and lifecycle

In machine modes, an operation requiring approval stops with `blocked/permission_required` unless the SDK caller supplies a callback that grants it. Existing policy denials remain denials. Never default to auto approval merely because stdin is not a terminal. A later invocation may resume with an explicitly changed policy; no approval is persisted by this feature.

Limits are per invocation, including provider retries and nested delegation. Check before dispatch, before retries, and after usage updates. Reserve estimated request plus maximum output tokens before launching a model request, reducing output allowance to the remaining budget where valid. If required usage or pricing is unavailable under a corresponding active limit, stop with `usage_unavailable` rather than claiming enforcement. Cost/token estimates and late provider reports can differ; report actual observed usage and document possible overshoot. These are admission limits, not a provider billing guarantee. Paid web-tool credits are not included in the LLM USD cap.

Propagate remaining deadline to provider/tool timeouts and retry waits; signal cancellation to child work and terminate owned subprocess groups. No detached background work may survive a bounded invocation. Tools that cannot meet this lifecycle must be unavailable in this mode with an explicit explanation. Do not close caller-owned dependencies. Resume must preserve valid tool-message pairing after interruption. Handle SIGINT/SIGTERM in the CLI without installing process-global signal handlers from the SDK.

## Review Focus

1. Output beginning with `Error:` or `[Interrupted]` must remain ordinary model text (Task 1).
2. Noninteractive permission requests and callbacks that raise must not hang or leak resources (Tasks 2–3).
3. Missing usage, retries, and nested work must not silently evade configured limits (Task 2).
4. Concurrent session writers and workspace mismatch must fail before mutation (Task 2).
5. Diagnostics, Unicode task text, partial streams, and signals must preserve the machine-output contract (Task 3).

## Implementation Tasks

### Task 1: Shared outcomes and events

**Files:** create `nova/invocation.py`, `tests/test_invocation.py`; modify `nova/agent.py`, `nova/tool_executor.py`, and `nova/__init__.py`.

**Interfaces:** implement the dataclasses and `run_task` signature above; add explicit internal termination state while preserving `NovaAgent.run(...) -> str`. Translate existing lifecycle callbacks into the five public events.

- [ ] Write failing tests for JSON serialization, invalid numeric options, literal interruption/error text, final response versus iteration exhaustion, and exactly one terminal event after partial output.
- [ ] Run `uv run pytest tests/test_invocation.py -q`; confirm failures exercise the absent contract.
- [ ] Implement typed outcomes and event translation using existing traces; distinguish run termination from verification status. Snapshot per-run usage rather than reporting cumulative session totals.
- [ ] Run `uv run pytest tests/test_invocation.py tests/test_agent.py tests/test_agent_streaming.py -q`; all pass.

### Task 2: Bounded invocation and session ownership

**Files:** modify `nova/invocation.py`, `nova/agent.py`, `nova/tool_executor.py`, `nova/session.py`, `nova/cost_tracker.py`, `nova/tools/delegate_tool.py`, and affected owned-process handlers in `nova/tools/terminal.py` and `nova/tools/task_tools.py`. Locate exact handlers through Graph before editing. Extend corresponding existing tests and `tests/test_invocation.py`.

**Interfaces:** Task 1's `run_task` gains complete permission, cancellation, session-lock, budget, deadline, and cleanup semantics defined above. Add narrowly scoped engine hooks where existing callbacks are insufficient.

- [ ] Write failing tests for blocked approval with closed stdin, callback failure cleanup, cross-process session contention, unknown session, and workspace mismatch without writes.
- [ ] Write failing tests for retries/child usage charging the parent, missing pricing under a cost cap, deadline propagation, interrupted tool-message pairing, and owned subprocess cleanup.
- [ ] Run the affected tests and confirm each fails for the intended missing behavior.
- [ ] Implement limits and lifecycle ownership; use a crash-safe OS-backed session lock rather than a stale-file-only lock. Disable detached tools for bounded invocations until they meet ownership guarantees.
- [ ] Run `uv run pytest tests/test_invocation.py tests/test_session.py tests/test_agent.py -q` plus tests for each modified tool; all pass. Verify no owned child process remains after cancellation.

### Task 3: Machine-readable CLI

**Files:** modify `nova/cli.py`, `tests/test_cli.py`; create `tests/test_cli_protocol.py`.

**Interfaces:** machine-mode `cmd_ask` consumes `run_task` and its result/events; implement the flags, output framing, stdin rules, signal handling, and exit codes above.

- [ ] Write subprocess tests using a deterministic local provider fixture for JSON success/failure, JSONL ordering, Unicode stdin, no confirmation reads, stderr separation, resumed sessions, and SIGINT cancellation.
- [ ] Run `uv run pytest tests/test_cli.py tests/test_cli_protocol.py -q`; confirm new contract cases fail before implementation.
- [ ] Implement machine modes without routing UI output into stdout. Preserve current text-mode tests. Flush JSONL events promptly; handle closed output pipes without an endless retry or traceback loop.
- [ ] Run `uv run pytest tests/test_cli.py tests/test_cli_protocol.py tests/test_invocation.py -q`; all pass, including actual subprocess exit codes.

### Task 4: Caller examples and end-to-end acceptance

**Files:** create `examples/call_nova.py` and `examples/call_nova_subprocess.py`; modify `docs/GUIDE-009-USING_NOVA.md`, `docs/SPEC-003-NOVA_SDK_PUBLIC_API.md`, and `docs/GUIDE-010-ROADMAP.md` with shipped behavior after implementation.

**Interfaces:** examples use only root public SDK exports or the CLI wire contract. The subprocess example uses an argument list, never shell interpolation of task text.

- [ ] Add a packaged-install smoke check proving both examples import/run outside the repository with isolated config/session directories and a local provider fixture.
- [ ] Document result fields, exit codes, permission callbacks, budget limitations, session ownership, cancellation, and recovery after a blocked run.
- [ ] Run `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy nova/`, and `uv run pytest --cov=nova --cov-fail-under=70`; all must pass.
- [ ] Validate with an actual available calling agent in a disposable workspace: delegate a small code/test task, parse the result, inspect its artifact, and resume with a follow-up. Repeat with an intentionally blocked operation and cancellation. Record caller/provider/version, observed usage, results, and cleanup evidence in the tracked Entire session; do not treat the local fixture as live acceptance.
- [ ] If caller credentials or paid-run authorization are unavailable, explicitly leave live acceptance pending. Do not claim release readiness from unit tests alone.

## Trade-offs and Release Gate

A small synchronous SDK and CLI protocol reach Python and non-Python callers without a daemon or new protocol. Structured events add a stable contract while keeping internal tool and provider details replaceable. Cancellation requires lifecycle work beyond a thin wrapper; that work is necessary to call Nova reliably from another agent.

The milestone is complete only when both interfaces pass the same contract checks and the real caller scenario succeeds. Publishing is a separate authorized action. SDK expansion and optional MCP wrapping require separate scope decisions.

## Related Documentation

| Document | Purpose |
|----------|---------|
| [SDK Public API](SPEC-003-NOVA_SDK_PUBLIC_API.md) | Broader SDK direction narrowed by this milestone |
| [SDK Productization](ADR-004-SDK_PRODUCTIZATION.md) | Existing product decision |
| [Harness Engineering](SPEC-001-HARNESS_ENGINEERING.md) | Verification and trace semantics |
| [Permissions](GUIDE-008-PERMISSIONS.md) | Existing policy boundaries |
| [Roadmap](GUIDE-010-ROADMAP.md) | Overall project priorities |
