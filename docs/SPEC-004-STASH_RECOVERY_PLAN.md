# SPEC-004: Recover Remaining Updater and Cache-Usage Fixes

**Status:** ✅ Integrated into main
**Last Updated:** October 2026
**Type:** SPEC (Implementation Plan)

## Implementation Outcome

Both selected fixes were implemented on `fix/updater-and-cache-usage` and merged
into `main` after the user authorized delivery. Regression
checks reproduced the original failures before the code changes. The focused
suite now passes 105 tests; the full suite passes 1302 tests with 84.68% coverage.
Ruff lint, formatting, and mypy also pass. Neither stash was applied wholesale or dropped;
stash retirement remains a separate cleanup action. The original audit and planned
sequence below document why only these changes were selected.

## Verified Scope

Verification on October 4, 2026 compared local `main` (`dd64c24`) with live GitHub `main` (`022dd86`). The only committed local difference is the cleanup handoff; application code is identical. Remaining named branches are already ancestors of `main`. Current code, relevant commit history, both stash diffs, and focused behavioral reproductions were reviewed.

| Work | Existing implementation | Remaining gap | Decision |
|------|-------------------------|---------------|----------|
| Update interpreter selection | ✅ `39e89e4` detects repository `.venv` and `venv`, and fails if neither exists | ✅ Now selects the running virtualenv before repository fallbacks | Implemented from `533f21d` with updater regressions |
| Cache usage accounting | ✅ `51193e3` handles top-level cache hit/read and write fields; cost tracking supports discounted cache pricing | ✅ Now extracts nested cached tokens with top-level precedence | Implemented from `023ec2d` with precedence and provider-path tests |
| Native Anthropic and protocol rename | ✅ Native Anthropic support was deliberately removed in `b1b296b` | 🔴 Stashed plumbing no longer fits the current provider layer | Do not restore |
| Generic request options | 📋 Optional feature, no current requirement established | 🔴 Stash can overwrite core payload fields | Defer; any future design needs an allowlist and budget consistency |
| Endpoint metadata cache | 📋 Multi-endpoint isolation could matter for future SDK use | 🔴 Stash still uses a global current endpoint and cannot select the originating endpoint during lookup | Do not port; design separately when needed |

These findings establish that the two selected fixes are not already implemented. They do not establish that the user's current installation uses an external virtualenv or receives nested cache usage on every request.

## Evidence

- `nova/cli.py::cmd_update` only searches `<repo>/.venv/bin/python` and `<repo>/venv/bin/python`. In mocked execution, an external-only virtualenv exits with status 1; when a repo virtualenv also exists, installation targets that environment instead of the running external interpreter. The stashed selection handles both cases. No actual pull or package installation was performed.
- `nova/cost_tracker.py::extract_usage_from_response` omits nested prompt details. A response with 100 prompt tokens and 90 nested cached tokens produces zero cache-read tokens today. The stashed extraction produces 90. Using synthetic prompt/cache prices, the estimate changes from 0.0002 to 0.000038; these are test values, not current provider prices.
- The installed SDK schema contains `PromptTokensDetails.cached_tokens`. Both streaming and non-streaming adapters retain usage dictionaries, so extraction is the missing step.
- History searches for `sys.executable` / `sys.base_prefix` in the updater and `prompt_tokens_details` / `cached_tokens` in cost extraction found no equivalent landed implementation. Existing cache tests cover top-level fields, not the nested format.
- Focused baseline: 77 tests passed across CLI, cost tracking, model metadata, and providers. Ruff passed. The previous full implementation baseline was 1274 passing tests; rerun the full suite during implementation.

## Implementation Sequence

### 1. Preserve the starting state

Create a focused branch from refreshed `main` when implementation is requested. Preserve the existing documentation edits. Record the immutable stash IDs above; do not rely on stash indices after any stash operation. Read changes with `git show` and port selected code rather than applying either stash wholesale. Retain both stashes until the selected changes are integrated and validated.

### 2. Recover external virtualenv selection

Files: `nova/cli.py`, `tests/test_cli.py`.

- If `sys.prefix != sys.base_prefix` and `Path(sys.executable)` exists, use that interpreter for the editable reinstall. Keep the executable path as supplied; resolving its symlink could select the underlying system interpreter.
- Otherwise preserve the existing repository `.venv`, then `venv` search order.
- Preserve the nonzero exit when no suitable interpreter exists and preserve installation-failure propagation.
- Keep existing Git update behavior and dependency extras unchanged in this focused fix.
- Add tests that mock subprocesses, executable existence, and interpreter state: external-only environment; external environment alongside a repo environment; repo `.venv` fallback; legacy `venv` fallback; no virtualenv; pip failure. Assert the exact interpreter used and that failures never print success. Never execute real Git update or pip installation in these tests.

Acceptance: an editable Nova installation launched from an external virtualenv updates that same environment; ordinary repository installations continue to work.

### 3. Recover nested cache-token extraction

Files: `nova/cost_tracker.py`, `tests/test_cost_tracker.py`; add provider-path assertions in `tests/test_providers.py` if needed.

- Keep existing nonzero top-level cache-read fields authoritative. If they provide no count, use `usage.prompt_tokens_details.cached_tokens` when present.
- Missing or null prompt details should produce zero cached tokens. A non-mapping details value should not introduce an attribute-error failure.
- Treat nested cached tokens as a subset of `prompt_tokens`; do not add them to the prompt total or to cache-write counts.
- Preserve existing cache-write accounting and provider-reported total cost handling.
- Add tests for nested-only counts, missing/null/empty details, top-level versus nested precedence, unchanged prompt totals, and cache-discounted estimates using synthetic metadata.
- Verify streaming and non-streaming usage reaches extraction intact, and verify that an explicit provider total remains the reported total even with nested cache counts.

Acceptance: a response with 100 prompt tokens and 90 cached tokens records 100 input tokens and 90 cache-read tokens; provider totals remain authoritative.

### 4. Review and validate

Review the final diff for interpreter targeting, cache-field precedence, duplicate token accounting, and unintended restoration of obsolete provider code. Update the relevant usage documentation if behavior needs clarification.

```bash
.venv/bin/pytest -q tests/test_cli.py tests/test_cost_tracker.py tests/test_model_metadata.py tests/test_providers.py --no-cov
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy nova/
.venv/bin/pytest
```

Run a mocked end-to-end updater check and synthetic streaming/non-streaming usage checks. No paid provider calls or real updater execution are required to establish these behaviors. Commit the two logical fixes separately after review. Merge/push only within the authorization given for implementation; this document authorizes neither.

### 5. Retire the saved work after integration

After merge verification, retain a recoverable archive of the mixed provider stash and record which pieces were intentionally deferred. Drop saved stashes only when cleanup is authorized and recovery is verified. No branch merge is needed for the obsolete pieces.

## Related Documentation

| Document | Purpose |
|----------|---------|
| [Cost Tracking Guide](GUIDE-005-COST_TRACKING.md) | Usage and cost-accounting behavior |
| [Customization Guide](GUIDE-003-CUSTOMIZING.md) | Model and provider configuration |
| [SDK Specification](SPEC-003-NOVA_SDK_PUBLIC_API.md) | Future multi-endpoint considerations |
| [Contribution Guide](../CONTRIBUTING.md) | Validation and contribution workflow |
| [Session Handoff](../HANDOFF.md) | Current work state and audit evidence |
