# SPEC-007: Catalog Scaling and Local Evaluation

**Status:** 🟡 In progress
**Last Updated:** November 2026
**Type:** SPEC (Review and Implementation Direction)

## Goal

Keep tool and skill discovery useful as catalogs grow, and measure task success, context cost, latency, and policy behavior with repeatable evaluations. Use local MLflow for initial tracing and evaluations, with the same instrumentation usable in production.

## Review Findings

| Area | Status | Evidence and consequence |
|------|--------|--------------------------|
| Tool schemas | 🟡 Scaling gap | `NovaAgent._get_tool_definitions` sends all discovered MCP schemas. Short prompt descriptions do not reduce API schema tokens. Compaction cannot remove this fixed catalog overhead. |
| MCP enumeration | 🔴 Correctness gap | `McpClient._connect_server` fetches one tools page and one resources page, ignoring continuation cursors. Later entries can be undiscoverable. |
| MCP startup | 🟡 Scaling gap | Agent initialization calls `connect_all`; connections are sequential. Startup waits for every configured server. |
| Skill discovery | 🔴 Correctness gap | `discover_skills` sorts then returns the first 50 by default; `skills_list` uses that default without a continuation mechanism. Exact-name loading still works when a caller already knows the name. |
| Skill indexing | 🟡 Scaling gap | Discovery reads each skill file before slicing. Bodies load on demand for the model, but filesystem work still scales with the entire catalog. |
| Prompt budgets | 🔴 Correctness gap | Skills truncate by character slicing and append a marker outside the budget. System prompt fallback truncates concatenated sections rather than selecting complete entries. |
| Observability | ✅ Existing seam | `nova/observability.py` has no-op and Langfuse implementations, redaction, sampling, and run/LLM/tool/policy/verification hooks. |
| Evaluation | 🟡 Incomplete | Existing pytest suite covers components. No dedicated catalog-scale/live task evaluation runner was found in tracked filenames. |

Current-source review, not a complete MCP conformance audit. Entire remote history remained unavailable because the sandbox could not read its Keychain login.

## Proposed Catalog Design

Keep the complete catalog separate from the tools exposed on each model request. Use one per-agent selected view for prompt descriptions, API schemas, and allowed dispatch; recheck policy at execution. Discovery never grants permission.

1. Fix MCP pagination and skill overflow discoverability before ranking changes. Bound page counts and response bytes, detect repeated cursors, and report incomplete catalogs explicitly. Add transport-level integration tests as well as mocked unit tests.
2. Keep a small core of common tools plus catalog search always available. Search returns bounded summaries and stable identifiers. Explicitly named tools/skills get exact lookup before ranked search.
3. Activate selected tool schemas for subsequent model calls within an explicit schema-token budget. Retain active tools across a task to reduce churn and preserve stable ordering. Provide search/expansion when initial selection misses a needed tool; do not silently drop the only way to discover more.
4. Start with deterministic lexical/category ranking; add embeddings or reranking only if measured recall warrants them. Use the full catalog at small sizes when it fits the budget.
5. Index skill metadata once, invalidate on changes, return complete entries, and load skill bodies/references only when requested. Use canonical IDs distinct from display names. Preserve path checks and treat metadata as untrusted input.
6. Cache MCP metadata with server/config identity and freshness markers. Deferred connections can use cached summaries, but schema validity and availability must be checked before dispatch. Test stale entries, reconnects, removals, duplicate names, and catalog refresh during a run.

Do not truncate JSON schemas to fit a budget. Select fewer complete schemas. Budget system instructions, catalog summaries, schemas, loaded skills, and tool results separately. Preserve required instructions when dropping optional context.

## Evaluation Layers

| Layer | Status | Cases and measurements |
|-------|--------|------------------------|
| Deterministic contracts | 📋 Expand | Pagination, cursor loops, duplicate names, exact lookup beyond entry 50, complete prompt entries, schema/prompt/dispatch parity, policy after activation, cache invalidation |
| Scale benchmarks | 📋 Add | 10/100/1,000 tools and skills; catalog build time, cold/warm discovery, schema tokens, prompt tokens, peak memory, retrieval recall |
| Protocol integration | 📋 Add | Disposable stdio and localhost HTTP/SSE servers: paging, timeout, malformed response, reconnect, cleanup, session lifecycle |
| Agent task evals | 📋 Add | Correct tool and arguments, relevant skill loading, completion against workspace artifacts/tests, no unnecessary tools, denial behavior, cancellation |
| Local tracing acceptance | 📋 Add | Stored parent/child spans, real durations, token/cost accounting, retries, redaction, export failure isolation |

Use fixed fixtures and versioned case data in the repository. Each case records expected capabilities, prohibited actions, artifact assertions, catalog size, and budget. Separate development/tuning cases from held-out evaluation cases. Compare full-catalog and selected-catalog variants on identical tasks/models, repeating live cases to report variance rather than one success percentage.

Prefer code-based artifact and policy scorers. Use model judges only for genuinely subjective criteria, with judge model/prompt/version recorded. Report retrieval recall and task completion together: fewer schema tokens are not an improvement if Nova cannot find the necessary tool. Measure total billed tokens/cost, including discovery turns and retries, rather than only first-request size.

No invented quality baseline or arbitrary release threshold: record the initial baseline, then set explicit non-regression tolerances. Deterministic safety invariants must have zero violations. CI runs deterministic checks without MLflow or provider credentials; live evaluations are separately budgeted.

## MLflow Recommendation

Target MLflow for both development and production, starting locally. Avoid a permanent development-MLflow/production-Langfuse split unless a concrete requirement emerges. Retain the existing Langfuse adapter until MLflow has passed acceptance; no backend was removed or installed in this review.

- Local tracking server bound to loopback, SQLite metadata and local artifacts outside the source tree. Pin the selected MLflow version when implementing, and verify its actual installed APIs.
- Optional MLflow dependency and backend selection behind Nova's existing observer interface. Ordinary CLI/SDK use must work without a tracing server.
- Explicit spans for catalog search/activation, skill load, model requests/retries, tools, delegation, policy, and verification. Existing post-hoc observer events need true start/end timing for useful latency debugging.
- Trace attributes: run/session/parent IDs, model, code revision and dirty state, config fingerprint without secrets, catalog revision/count, selected IDs, schema/prompt tokens, usage/cost provenance, and stop reason.
- Preserve redaction and explicit content capture controls. Local storage is not permission to capture credentials. Keep export failure nonfatal with bounded flushing on shutdown.
- Attach eval results and dataset/scorer versions to comparable runs. Do not double-instrument LLM calls with both automatic and manual spans.
- For production, reuse instrumentation and scorers; choose durable database/artifact storage and access controls appropriate to deployment instead of copying the local SQLite setup blindly.

Local deterministic evaluations can be offline. Live agent or judge runs still call their configured model providers unless those models are local too.

MLflow supports custom offline scorers through `mlflow.genai.evaluate`, trace-based evaluation, and OpenTelemetry trace interoperability. Automatic production evaluation has different scorer support; do not assume every offline Python scorer runs automatically online. Scheduled explicit evaluation is sufficient for the first milestone.

## Delivery Order

1. Capture current failures and scale baselines.
2. Correct pagination, skill discovery, and whole-entry budget handling; remove each expected-failure marker when its fix lands.
3. Add local MLflow adapter and trace acceptance, retaining the existing backend until validated.
4. Add budgeted tool activation and metadata caching; compare against the unchanged full-catalog baseline.
5. Run held-out caller tasks through the CLI/SDK from SPEC-006, including model and catalog-size comparisons.
6. Decide production backend retirement after actual MLflow debugging/eval evidence, not a feature checklist.

## Execution Plan and Progress

Work is local on `feat/catalog-evals`, preserving the earlier ACP removal. No commits or publication are authorized by this plan.

### 1. Catalog correctness — ✅ Implemented

- [x] Reproduce the three strict expected failures, then replace them with passing regressions.
- [x] Enumerate MCP tool/resource pages with a 100-page and 8 MiB decoded-catalog limit; reject repeated cursors and discard incomplete tool catalogs. Resource discovery failure is logged and does not prevent tool connection. These limits apply after transport decoding, not to raw network allocation.
- [x] Add query/cursor/limit to `skills_list`; return structured metadata with canonical directory IDs and next cursor. Keep the full catalog separate from the 50-item default prompt index.
- [x] Render whole skill entries within the character cap, including truncation notice; escape XML metadata.
- [x] Test overflow discovery, search, repeated cursors, long metadata, and valid XML under tiny budgets. A real stdio subprocess fixture also verifies tool/resource pagination and process cleanup.

Files: `nova/mcp_client.py`, `nova/skills.py`, `nova/tools/skills_tool.py`, `tests/test_catalog_scaling.py`.

### 2. Local observability and baseline — 🟡 Initial implementation

- [x] Install optional `mlflow` extra (lockfile resolves 3.18.0); keep ordinary runtime dependencies unchanged apart from ACP removal.
- [x] Add the MLflow observer behind existing hooks, input/output controls, redaction, sampling, measured successful LLM/tool duration, and a two-second shutdown flush wait.
- [x] Block automatic project configuration from overriding tracing provider/destination.
- [x] Reject conflicting MLflow destinations in one process. Current adapter uses MLflow's process-wide tracking destination; use separate processes for different stores, and do not mutate the SDK destination behind an active observer.
- [x] Store and read back a real local three-span trace; verify a synthetic API key is absent.
- [x] Run deterministic catalog fixtures at 10/100/1,000 entries and log measurements/artifact to MLflow.
- [ ] Add real provider failure/retry spans and explicit nested-agent parentage; current LLM event represents the successful aggregate call including retries, and failed requests have only the run error.
- [ ] Add real transport integration and held-out model task datasets; no live-model quality claim yet.

Files: `nova/mlflow_observability.py`, `nova/observability.py`, `nova/config.py`, timing hooks in `nova/agent.py` and `nova/tool_executor.py`, `nova/evaluation.py`, and corresponding tests.

Baseline: synthetic schema estimates were 410 / 4,100 / 41,900 tokens for 10 / 100 / 1,000 tools. Tail-skill lookup and prompt budget checks passed at each size. These are local tokenizer estimates, not provider billing or model task-success measurements.

### 3. Budgeted activation, metadata cache, and deferred connections — ✅ Implemented

- [x] Introduce `nova/tool_catalog.py` and a per-agent selected view, with a virtual `discover_tools` tool routed through the normal executor and policy path.
- [x] Exact identifier resolution first, then deterministic lexical search; activate complete schemas only and retain selected tools until budget pressure requires eviction. Core tools stay pinned.
- [x] Enforce selected-schema eligibility and normal policy during dispatch; tests cover removal, oversized schemas, denied unselected calls, MCP approval after activation, next-request prompt/API parity, and both same-response activation/eviction cases. The current response uses its frozen offered set, intersected with live availability. Catalog results are labeled untrusted.
- [x] Compare full and selected schema cost and exact-name tail-tool reachability using the baseline runner. Before the deferred-server schema extension, at 1,000 synthetic tools, three pinned fixture tools plus discovery and the requested tool used 276 estimated tokens versus 41,900 for the full catalog. These are schema-only measurements; live task success and total cost including extra discovery turns remain unmeasured.
- [x] Add metadata caches: bounded immutable skill metadata keyed by file identity/size/mtime/ctime; per-agent MCP schema snapshots keyed by client catalog revision; lexical tool-search terms rebuilt on catalog changes.
- [x] Invalidate on skill edits, renames, deletion, atomic replacement, server configuration replacement, removal, reconnect, and explicit `McpClient.refresh_server(name)`. Returned metadata is isolated from caller mutation.
- [x] Serialize MCP refresh/configuration changes with in-flight calls, including transport lookup under the lifecycle lock.
- [x] Add opt-in deferred MCP connections using configured server metadata, explicit server selection, fresh schema discovery, and serialized first use. Full-catalog mode keeps eager connections.

Ruling: deferred discovery uses trusted configuration names/descriptions instead of persistent remote tool schemas. This avoids stale schemas and requires no disk cache identity or TTL policy. The tradeoff is an extra server-discovery step and dependence on useful configured descriptions; tool-level search cannot see an unconnected server's capabilities.

Configuration: `tool_catalog.enabled` defaults to true in loaded configuration; `schema_max_tokens` defaults to 4096. Disable it for full-catalog comparison. The API schema budget includes discovery itself. Startup fails clearly if pinned core schemas cannot fit. Small catalogs retain every tool, plus the discovery schema overhead. Direct callers supplying a standalone config dictionary must include this section to enable selection.

The normal core is `read_file`, `list_files`, `search_files`, `terminal`, `skills_list`, and `skill_view`, where available. A discovery query activates up to five matches; exact identifier lookup activates only that tool. Under budget pressure, older non-core selections are evicted; schemas are never truncated. Selection does not confer permission. Successful LLM telemetry includes schema-token estimates and offered tool count.

Retrieval-quality cases beyond exact names are implemented below. Next: caller-contract implementation and live task evaluation. Preserve the full-catalog benchmark as a control. Do not claim model reliability from these deterministic tests.

### 4. Caller contract and end-to-end eval — 📋 Subsequent milestone

Implement SPEC-006's shared run results and machine CLI/SDK, then evaluate real caller tasks, artifacts, permissions, and cancellation. Keep tuning and held-out datasets distinct. Set success/cost regression thresholds from measured baselines. Decide Langfuse retirement after MLflow captures useful real failures and retries.

## Deferred MCP Connections

Set `deferred: true` and a useful `description` on an MCP server in trusted configuration. With `tool_catalog.enabled: true`, startup skips that server. Existing servers remain eager by default; disabling the tool catalog connects every configured server so tools remain reachable without discovery.

1. Call `discover_tools` with a task query such as `{"query": "climate"}`. Alongside tool matches, `servers` contains up to five matching configured names, bounded descriptions, and connection state; `total_servers` reports the match count. Exact server names resolve directly. Search does not connect or expose commands, endpoints, environment variables, or headers.
2. Select a returned server with `{"query": "forecast", "server": "weather"}`. Only that exact configured server connects, and its fresh catalog enters normal budgeted selection. `server_connection.status` is `connected`, `unavailable`, or `unknown`.
3. Call an activated tool on the next model request. Connection and activation do not grant execution permission; confirmation and frozen offered-tool eligibility still apply.

Failed connections publish no tools and may be retried explicitly. Connections are retained until refresh, removal, or shutdown; simultaneous selection is serialized and starts one transport. Configuration replacement invalidates the previous catalog. Server search is lexical over configured names and descriptions and does not use cached remote schemas. Resource URI enumeration and unsolicited remote list-change notifications remain outside this increment.

Validation includes a real stdio fixture proving zero startup/search processes, selected-only connection, one initialization on repeated/concurrent use, successful permitted calls, denial before approval or before the next model request, offline recovery after configuration replacement, process cleanup, and exact lookup among 1,000 configured servers.

## Cache Validation

The cache is process-local; it is not persistent or a background watcher. Each skill discovery still scans directory entries and file stat metadata, so warm discovery remains linear in file count. Metadata has a 4,096-entry LRU bound and full skill bodies are loaded fresh. MCP server-side changes require explicit refresh or reconnect; unsolicited list-change notifications are not implemented by this work. Injected MCP clients without an integer `catalog_revision` are refreshed without caching.

A local 1,000-skill fixture measured 126.3 ms cold versus 16.7 ms warm, with identical metadata. Timings are one local measurement, not a performance guarantee. Suite version 3 and measurements are stored in local MLflow and `/tmp/nova-mlflow-validation/catalog-cache.json`.

Tests include warm-cache parse avoidance, caller-mutation isolation, edits with restored mtime, renames/deletes/replacements, MCP config replacement/removal, changed schemas, and a real stdio refresh that verifies the old process exits. A concurrent regression verifies refresh waits until an active call finishes.

## Retrieval Quality Baseline

✅ Implemented in `nova/evaluation.py` with regressions in `tests/test_evaluation.py`. The fixed Python case data is versioned separately as retrieval dataset 1. Suite version 4 adds retrieval reports; the default baseline-only command preserves suite version 3 and its report shape.

Eight scripted cases are divided into four tuning and four held-out evaluation cases: exact identifier, natural-language weather request, ambiguous search names, a two-step issue workflow, documentation lookup, warehouse lookup, a weather paraphrase, and an absent capability. Synthetic archive-search distractors fill catalogs to 10, 100, and 1,000 entries. Each case starts with a fresh catalog. The reserved held-out split has been run to establish this baseline; it is not evidence of performance on unseen real-world tasks. No ranking changes were made after inspecting its results.

The runner uses top-one discovery and records ranked matches, actual selected schemas, missing required tools, budget-blocked matches, discovery-call counts, and schema-token snapshots. Multi-step availability includes tools required by earlier steps. The full-catalog control offers identical fixture definitions at every checkpoint and measures availability, not model selection or execution.

| Measurement | Status | Result |
|-------------|--------|--------|
| Tuning cases | ✅ Measured | All four retrieve their target tools; the issue workflow retains both required schemas. |
| Held-out positive cases | 🟡 Gap observed | Documentation and warehouse queries succeed; “Will I need an umbrella tomorrow?” misses the weather tool. |
| Absent capability | ✅ Measured | “transcribe audio” returns no match. |
| Large-catalog availability | 🟡 Gap observed | At 100 and 1,000 tools, the paraphrase leaves the weather schema unavailable. At 10 tools, all schemas fit, so availability remains complete despite the retrieval miss. |
| Schema budgets | ✅ Passing | No selected-schema budget violations in the measured fixtures. |
| Local MLflow | ✅ Read back | Stored and reloaded 105 metrics and the exact JSON artifact in `/tmp/nova-retrieval-validation/tracking.db`, run `a194d744eca848b998c8cc4ef846efde`. |

Scoring conventions: positive cases use recall and precision against expected identifiers; empty expected results score 1 only when discovery abstains. Consequently, held-out macro recall is 0.75, while positive-only recall is 2/3. MLflow groups remain separate by catalog size, split, and variant. Each logged metric is an unweighted mean over cases: `budget_violations` is mean violations per case, and `peak_schema_tokens` is mean per-case peak. Inspect per-case JSON for totals or maxima; separate dashboard sum/max metrics are deferred.

`schema_snapshot_tokens` sums the initial schema estimate and each post-discovery checkpoint. The full-catalog control uses the same number of checkpoints. These estimates exclude queries, discovery results, conversation history, retries, and provider framing/billing; they are not total task cost. The script executes no capability tools and does not score permissions, arguments, artifacts, or model task completion.

Quality misses remain in the report without failing the process while this baseline is being established. Existing deterministic baseline checks and any schema-budget violation produce a nonzero exit. Regression tests verify that removing search results lowers scores and that a retrieved-but-oversized schema is reported as unavailable rather than truncated.

Read-only review found no critical or important issues. It identified the aggregation conventions above; they are documented here rather than changing recorded metric semantics. Live task quality, arguments, billing, and production acceptance remain subsequent work. Actual MLflow persistence was independently verified by artifact and metric readback.

## Running Locally

Install with `uv sync --all-extras`. For local storage outside the repository:

```bash
mkdir -p /tmp/nova-mlflow
cd /tmp/nova-mlflow
/Users/markcastillo/git/nova-agent/.venv/bin/mlflow server --host 127.0.0.1 --port 5000 --backend-store-uri sqlite:///mlflow.db
```

Set `observability.enabled: true`, `provider: mlflow`, and `mlflow.tracking_uri: http://127.0.0.1:5000` in trusted global config or an explicitly supplied config. Automatic project config cannot enable or redirect tracing. Keep content capture off unless needed.

Run from the repository for an offline baseline, or supply the local server to persist it:

```bash
uv run python -m nova.evaluation --sizes 10 100 1000 --output /tmp/catalog-baseline.json
uv run python -m nova.evaluation --tracking-uri http://127.0.0.1:5000
uv run python -m nova.evaluation --suite retrieval --split heldout --sizes 10 100 1000 --output /tmp/retrieval-results.json
uv run python -m nova.evaluation --suite all --tracking-uri http://127.0.0.1:5000
```

The baseline runner creates synthetic skills in a temporary directory; retrieval fixtures are in memory. Neither suite makes model calls. Retrieval requires catalog sizes from 10 through 10,000. The runner logs deterministic checks and scale/retrieval metrics using MLflow experiment tracking; `mlflow.genai.evaluate` and model judges are follow-on work. SQLite direct-URI runs are also supported; run those outside the repository so default artifact directories stay outside source control.

Last verification: 1,419 passed in the full suite (85.76% coverage), including catalog selection, cache invalidation, deferred connection, concurrent lifecycle, and real stdio integration cases. Deferred-connection review found no actionable blockers. Lint, formatting, and mypy passed. Local baseline artifacts are in `/tmp/nova-mlflow-validation/`; the final outside-repository tracking database is `local.db`. No persistent MLflow server is running.

Retrieval increment verification (November 8): 1,434 passed in the full suite with 86.16% coverage. Lint, formatting, mypy, and whitespace checks passed. All three catalog sizes ran through both baseline and retrieval suites; real local MLflow metric/artifact readback matched the emitted report. This increment changes only `nova/evaluation.py`, `tests/test_evaluation.py`, and this specification, preserving prior work. All changes remain local and uncommitted on `feat/catalog-evals`; no persistent server was started. Next action: implement SPEC-006's shared caller contract, then use it for live task evaluations. Retrieval ranking, richer failure/retry tracing, and Langfuse retirement remain open.

Verification: `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy nova/`, `uv run pytest`. Run the real local trace readback separately; mocked adapter tests alone do not establish persistence.

## Related Documentation

| Document | Purpose |
|----------|---------|
| [Callable Agent Plan](SPEC-006-CALLABLE_AGENT_PLAN.md) | CLI/SDK contract evaluated by this work |
| [Harness Engineering](SPEC-001-HARNESS_ENGINEERING.md) | Existing trace and verification model |
| [MCP Integration](GUIDE-007-MCP_INTEGRATION.md) | Current transports and configuration |
| [MLflow Self Hosting](https://mlflow.org/docs/latest/self-hosting/) | Local SQLite and artifact storage |
| [MLflow Custom Scorers](https://mlflow.org/docs/latest/genai/eval-monitor/scorers/custom/) | Offline evaluation and online limitations |
| [MLflow OpenTelemetry](https://mlflow.org/docs/latest/genai/tracing/opentelemetry/) | Trace ingestion/export interoperability |
