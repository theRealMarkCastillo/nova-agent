# SPEC-006: Callable Nova Agent Implementation Plan

**Status:** ✅ Implemented and locally/live validated
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
| Shared invocation contract | ✅ Implemented locally | Explicit outcomes, usage provenance, and ordered events |
| Noninteractive CLI | ✅ Implemented locally | One JSON result or ordered JSONL events; no approval prompts |
| Python SDK | ✅ Implemented locally | Root `run_task` with cancellation, permissions, budget admission, and session ownership |
| Real caller validation | ✅ Passed | Codex called the public SDK and JSON CLI against DeepSeek; artifact, resume, approval, and cancellation checks passed |

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

`NovaOptions` is a small invocation dataclass, not a replacement for all YAML configuration. Fields: `workspace: Path`, `model: str | None`, `session_id: str | None`, `config_path: Path | None`, `max_iterations: int = 20`, `max_total_tokens: int | None = None`, `max_cost_usd: float | None = None`, `timeout_seconds: float = 300`, and `include_trace: bool = False`. Optional fields default to `None`. Explicit invocation options override loaded configuration; do not mutate the caller's data. Reject nonpositive limits, booleans as numbers, NaN/infinity, empty tasks, and nonexistent workspaces before creating a session or calling a provider.

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

**Files:** create `nova/invocation.py`, `tests/test_invocation.py`; modify `nova/agent.py`, `nova/tool_executor.py`, `nova/cost_tracker.py`, `nova/providers.py`, and `nova/__init__.py`.

**Interfaces:** implement the dataclasses and `run_task` signature above; add explicit internal termination state while preserving `NovaAgent.run(...) -> str`. Translate existing lifecycle callbacks into the five public events.

- [x] Write failing tests for JSON serialization, invalid numeric options, literal interruption/error text, final response versus iteration exhaustion, and exactly one terminal event after partial output.
- [x] Run focused tests and confirm failures exercise the absent contract. Used local `.venv/bin/pytest`; `uv run` cannot access its default cache in this sandbox.
- [x] Implement typed outcomes and event translation using existing traces; distinguish run termination from verification status. Snapshot per-run usage rather than reporting cumulative session totals.
- [x] Run invocation, agent, streaming, and cost-tracker tests; all pass.
- [x] Publish the bounded `run_task` entry point after Task 2 supplies its lifecycle guarantees.

### Task 1 Implementation Record

The root package exports `NovaOptions`, `NovaRunResult`, `NovaUsage`, `NovaError`, and the discriminated `NovaEvent` union. `NovaOptions.validate(task)` validates numeric limits and existing paths, returning the canonical workspace without mutating options. It does not execute a task or enforce runtime limits.

`NovaAgent.run_result(task, stream=True, on_event=None, include_trace=False)` uses the existing agent loop and returns a structured result. It preserves the caller-owned agent/session resources. `NovaAgent.run(...)` keeps its string return and exception behavior, and also records `last_run_result`. This lower-level method does not supply Task 2's session ownership, deadline, approval-stop, or total-budget guarantees.

Ruling: postpone implementation/export of the standalone `run_task` function until Task 2 — accepting limit options before enforcement would misrepresent the callable contract. The engine result method allows this layer to be tested without claiming the bounded SDK is ready. The cost is that standalone delegation remains unavailable until the next task.

Termination is explicit engine state, never response-text parsing. Actual interrupted streams and tools are cancelled, including interruption on the final iteration. Provider length truncation is `limit_reached/token_limit`. Normal model text beginning with `Error:` or `[Interrupted]` remains completed output. Harness verification status is reported separately and can be failed even when the model returns a final response.

Events use a serialized callback path and increasing sequence numbers. Tool events contain call IDs, names, and completion status, omitting arguments/results. A working callback receives exactly one final result event. A callback that raises stops delivery and its original exception propagates without provider retries; no further terminal delivery is attempted through a broken callback. Streamed partial output is retained if a later provider failure occurs. Optional traces may contain workspace content; leave `include_trace` false unless that content is needed.

Usage reports per-turn differences, including child usage merged into the parent tracker. `usage.complete` is false when counts are unavailable, including failed provider attempts before a successful retry. `cost_usd` is null and `cost_source` is unknown when pricing or relevant usage is missing. Fully reported and metadata-estimated costs are accumulated together; mixed provenance is estimated. The legacy display's fallback pricing remains separate from these result fields. No token/cost admission limit is enforced by this increment.

Tests use injected provider clients and stores, including the actual provider stream adapter. This first slice was committed and merged into local `main` as `3700d09`. Future work stays directly on `main` per the user's instruction recorded in AGENTS.md.

Read-only review reproduced lost non-streamed findings, incomplete usage after `KeyboardInterrupt`, and duplicate partial output on an outer context-overflow retry. Each received a failing regression followed by a passing fix. Ruling: address duplicate partial output in this increment even though the retry behavior predates it — ordered public events must not repeat text because of an internal retry. This changes retry admission after visible output, which now fails rather than replaying that output. No review findings remain deferred.

Validation on November 8: 1,475 full-suite tests passed with 86.52% coverage; lint, formatting, mypy, and whitespace checks passed. The 41 invocation cases cover serialization, numeric/path validation, model-text independence, completion/verification separation, event ordering and callback errors, interruption, partial output, provider retry/error classification, usage deltas, mixed cost provenance, and child usage. No live provider calls, commit, merge, push, or deployment occurred in this increment. Next action: Task 2's bounded invocation and session ownership, followed by the machine CLI and real caller acceptance.

### Task 2: Bounded invocation and session ownership

**Files:** modify `nova/invocation.py`, `nova/agent.py`, `nova/tool_executor.py`, `nova/session.py`, `nova/cost_tracker.py`, `nova/tools/delegate_tool.py`, and affected owned-process handlers in `nova/tools/terminal.py` and `nova/tools/task_tools.py`. Locate exact handlers through Graph before editing. Extend corresponding existing tests and `tests/test_invocation.py`.

**Interfaces:** Task 1's `run_task` gains complete permission, cancellation, session-lock, budget, deadline, and cleanup semantics defined above. Add narrowly scoped engine hooks where existing callbacks are insufficient.

- [x] Write failing tests for blocked approval with closed stdin, callback failure cleanup, cross-process session contention, unknown session, and workspace mismatch without writes.
- [x] Write failing tests for retries/child usage charging the parent, missing pricing under a cost cap, deadline propagation, interrupted tool-message pairing, and owned subprocess cleanup.
- [x] Run the affected tests and confirm each fails for the intended missing behavior.
- [x] Implement limits and lifecycle ownership; use a crash-safe OS-backed session lock rather than a stale-file-only lock. Disable detached tools for bounded invocations until they meet ownership guarantees.
- [x] Run invocation/session/agent and affected tool tests using local `.venv/bin/pytest`; all pass. Real subprocess fixtures verify process-group cleanup on cancellation and after normal shell exit.

Task 2 implementation: `run_task` owns its agent/store and acquires an OS-backed session writer lock before creating or loading a session. Resume preflight reads SQLite without creating a missing database or modifying session data. Workspace ownership is canonical and mandatory; legacy unbound sessions fail with `workspace_mismatch`. Parent and child sessions are locked before initialization. Lock files are not deleted; the kernel releases locks when a process exits, avoiding stale-file recovery races.

The shared controller reserves request/output tokens and estimated cost across parent/child calls. It snapshots provider pricing for each invocation so another agent cannot change its pricing source. Unknown prices under a cost cap, incomplete required usage, or malformed reports stop with `usage_unavailable`. Reported cost must be finite and nonnegative. Actual observed usage is retained separately from admission estimates.

Bounded delegation is synchronous and shares accounting/cancellation; a child timeout stops the bounded invocation. Terminal tools poll cancellation/deadlines and always terminate their own process group, including background group members after shell exit. This is not an OS sandbox for deliberately detached processes. Files/search/skill lookup use existing workspace checks; MCP, background-task creation, HTTP/web, and other handlers remain explicitly unavailable in bounded mode until their lifecycle is supported. Existing interactive availability is preserved.

SDK callbacks remain cooperative and cannot be forcibly terminated. Provider transports receive the remaining timeout; checks run on every stream chunk, including choice-less usage chunks. Blocking reads and arbitrary callbacks are cooperative boundaries, so the timeout is not a claim of forcibly terminating arbitrary code at an exact instant. SDK code installs no process-global signal handlers. Cleanup failures do not replace an existing cancelled/blocked/limited/failed outcome, and callback errors propagate after cleanup.

### Task 3: Machine-readable CLI

**Files:** modify `nova/cli.py`, `tests/test_cli.py`; create `tests/test_cli_protocol.py`.

**Interfaces:** machine-mode `cmd_ask` consumes `run_task` and its result/events; implement the flags, output framing, stdin rules, signal handling, and exit codes above.

- [x] Write subprocess tests using a deterministic local provider fixture for JSON success/failure, JSONL ordering, Unicode stdin, no confirmation reads, stderr separation, resumed sessions, and signal cancellation.
- [x] Run CLI/protocol tests and confirm new contract cases fail before implementation.
- [x] Implement machine modes without routing UI output into stdout. Preserve current text-mode tests. Flush JSONL events promptly; handle closed output pipes without an endless retry or traceback loop.
- [x] Run CLI/protocol/invocation tests; all pass, including actual subprocess exit codes and SIGINT/SIGTERM in JSON and JSONL modes.

Task 3 implementation: all specified flags are parsed; machine modes share `run_task` and never install an interactive approval callback. JSON serialization rejects nonfinite numbers. Tool arguments containing nonfinite JSON values are rejected before trace/dispatch. Dependency output is redirected to stderr while the protocol writes through its original stdout stream. CLI-only signal handlers are restored on exit; broken pipes stop delivery and close owned resources.

### Task 4: Caller examples and end-to-end acceptance

**Files:** create `examples/call_nova.py` and `examples/call_nova_subprocess.py`; modify `docs/GUIDE-009-USING_NOVA.md`, `docs/SPEC-003-NOVA_SDK_PUBLIC_API.md`, and `docs/GUIDE-010-ROADMAP.md` with shipped behavior after implementation.

**Interfaces:** examples use only root public SDK exports or the CLI wire contract. The subprocess example uses an argument list, never shell interpolation of task text.

- [x] Add a packaged-install smoke check proving both examples import/run outside the repository with isolated config/session directories and a local provider fixture.
- [x] Document result fields, exit codes, permission callbacks, budget limitations, session ownership, cancellation, and recovery after a blocked run.
- [x] Run lint, format, mypy, and full coverage checks; all pass using local `.venv/bin/*` tools. The final full run also enables the installed-wheel smoke test.
- [x] Validate with an actual available calling agent in a disposable workspace: delegate a small code/test task, parse the result, inspect its artifact, and resume with a follow-up. Repeat with an intentionally blocked operation and cancellation. Record caller/provider/version, observed usage, results, and cleanup evidence in the tracked Entire session; do not treat the local fixture as live acceptance.
- [x] If caller credentials or paid-run authorization are unavailable, explicitly leave live acceptance pending. Do not claim release readiness from unit tests alone.

Task 4 local acceptance: `examples/call_nova.py` uses only root exports; `examples/call_nova_subprocess.py` passes a subprocess argument list. The smoke check installs the built wheel with no dependency/network resolution into a temporary target, imports it outside the repository, creates a real artifact through the SDK, resumes through the CLI, and verifies shell metacharacters in task text are not executed. Enable this optional test with `NOVA_TEST_WHEEL=/absolute/path/to/nova_agent-0.1.0-py3-none-any.whl .venv/bin/pytest tests/test_cli_protocol.py -k packaged --no-cov` after building the wheel. Ordinary deterministic CI does not download build dependencies.

Read-only review identified nonfinite reported cost bypassing limits/JSON and choice-less stream chunks bypassing cancellation. Both were reproduced and fixed through RED→GREEN regressions. Additional boundary tests cover null reported costs, preservation of cancellation on cleanup failure, terminal sequencing on backend failure, child locks before initialization, and isolation from another provider's metadata.

Initial live acceptance was blocked by HTTP 401 from DeepSeek. The user updated the key, read-only authentication succeeded, and the user explicitly authorized live checks using the provider-listed `deepseek-flash` alias. Claude Code 2.1.319 was attempted but exited before API use because it was not logged in. The actual available caller was this Codex API session (`gpt-6.1-sol`, session `01a1bd6f-bcf8-75b3-9fa3-6065969628e1`), invoking Nova's public SDK and subprocess JSON CLI. Claude-specific acceptance is not claimed.

Final local verification on November 8: **1,525 tests passed, 86.87% coverage**, including the full subprocess protocol suite and installed-wheel SDK/CLI smoke. Ruff, formatting, mypy, and whitespace checks passed. The current wheel is `/tmp/nova-packaged/nova_agent-0.1.0-py3-none-any.whl`; final suite output is `/tmp/nova-phases-final.log`. Build dependencies were downloaded once; final wheel creation ran offline. Tests use no paid completions. Review findings were fixed with failing-then-passing regressions; no minor findings were deferred.

### Live Acceptance Evidence

All four scenarios passed against DeepSeek's real `deepseek-flash` model alias using OpenAI-compatible Python SDK 3.28.0. The workspace was `/tmp/nova-live-acceptance/workspace`, outside the repository. Each scenario used a 20,000-token allowance, at most eight iterations per invocation, and a 90-second deadline. The first repair attempt correctly blocked an unapproved inspection command; its evidence was retained and its 3,252 tokens deducted from the repair retry's admission allowance. The blocked case also ran through the real JSON CLI with closed stdin, deducting its SDK usage from the same scenario allowance. There were six Nova invocations across four scenarios; no attempt or variant is omitted from the totals.

| Scenario | Status | Independent evidence | Observed tokens |
|----------|--------|----------------------|-----------------|
| Repair | ✅ Passed | Nova changed `return a - b` to `return a + b`; the unchanged unittest passed when re-run by the caller. Includes the retained blocked first attempt. | 17,263 |
| Resume | ✅ Passed | Reused session `fbe47526-3705-4017-abd1-b9e0f9d49d05`; added the requested docstring and kept tests passing. | 16,063 |
| Approval blocked | ✅ Passed | SDK returned `blocked/permission_required`; JSON CLI with closed stdin returned the same outcome and exit code 3. Neither requested file was created. | 6,572 |
| Cancellation | ✅ Passed | A real terminal job and child were observed, then cancelled. PIDs 84530 and 84531 were both absent from the process table afterward. | 3,301 |

SDK event checks passed in every case: increasing sequence numbers, exactly one terminal event, and a terminal result matching the returned object. The calculator test file retained SHA-256 `e5b6f0ea3f2e7753c4db8d26cecf02fd1d739c697c047df6028874e6d089c9b7`. The final artifact was independently inspected:

```python
def add(a, b):
    """Return the sum of a and b."""
    return a + b
```

Total observed Nova usage, including the first blocked attempt and CLI variant: **42,048 input + 1,151 output = 43,199 tokens**; cache-read tokens 36,864 and cache-write tokens 5,184 are included in input totals. Provider-reported cost and usable pricing were absent, so USD cost is `null` with `cost_source: unknown`; no zero-cost or USD-cap claim is made. Claude's failed pre-authentication launch reported zero API calls, which is separate from Nova's paid usage.

Detailed results/events and the caller's parsed summary are `/tmp/nova-live-acceptance/acceptance-summary.json` and the adjacent scenario JSON files. The tracked Entire session contains the execution and independent inspection evidence. The validated runtime source matches all 48 Python files in the tested wheel (SHA-256 `771c95488fb8d05625158d36ef6012d1be467c53f662788f5de3a867883a4fe7`). Live validation used `3700d09` plus the then-uncommitted implementation on `main`. No publishing, migrations, or deployment were performed during acceptance.

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
