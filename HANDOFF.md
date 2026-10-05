# ACTIVE WORKSTREAM HANDOFF

**Status:** 🟡 In Progress
**Last Updated:** October 2026
**Type:** REPORT (Session Handoff)

- **Workstream:** Code/design review fixes and improvement plan ([SPEC-005](docs/SPEC-005-REVIEW_IMPROVEMENT_PLAN.md))
- **Active Branch:** `main` (changes are uncommitted in the working tree, per user request to fix directly on this branch)
- **Current Status:** IN_PROGRESS

## 1. Ground Truth & State

A code and design review of `main` at `863bc26` produced the findings recorded in SPEC-005. The user asked for the high-value bugs to be fixed directly on this branch and for an improvement plan. Phase 0 is implemented and verified. Nothing has been committed or pushed.

Exact files touched:
- `nova/retry.py`: new `classify_exception` classifies OpenAI SDK, httpx, and builtin transport errors by type before message patterns (H2). `retry_with_backoff` uses it.
- `nova/permissions.py`: a matching `allow: true` path rule no longer short-circuits when a command is present, so `terminal` keeps command-deny and confirmation checks (H3).
- `nova/tools/file_ops.py`: shared `_atomic_write` preserves existing file mode, writes through symlinks, and applies the import-time umask to new files (M1).
- `tests/test_retry.py`, `tests/test_permissions.py`, `tests/test_file_ops.py`: 12 regression tests; 11 failed before the fixes (`APITimeoutError` already matched its message text).
- `docs/GUIDE-008-PERMISSIONS.md`, `docs/GUIDE-014-RETRY_AND_ERROR_HANDLING.md`: user-facing behavior.
- `docs/SPEC-005-REVIEW_IMPROVEMENT_PLAN.md` (new), `docs/DOCUMENTATION_INDEX.md`: plan and index.
- `HANDOFF.md`: this file.

Validation:
- `.venv/bin/ruff check .`: passed.
- `.venv/bin/ruff format --check .`: passed.
- `.venv/bin/mypy nova/`: passed, 43 source files.
- `.venv/bin/pytest -q`: 1314 passed, 84.77% coverage.
- `git diff --check`: flags only the existing two-space Markdown line breaks on re-dated metadata lines.

Retained history from the previous workstream (still applies): stashes `stash@{0}` (`533f21d`) and `stash@{1}` (`023ec2d`) remain intact pending authorized cleanup; the archived worktree bundle remains under `.git/cleanup-archives/`.

## 2. Active Hypothesis & Blockers

No blockers. Phase 0 changes await the user's review and a decision on committing (suggested: three commits, `fix: retry SDK transport errors by exception type`, `fix: keep confirmation for commands under path allow rules`, `fix: preserve file mode and symlinks on atomic writes`, plus `docs: add SPEC-005 review improvement plan`).

## 3. Immediate Next Action (Start Here)

1. Run `.venv/bin/pytest -q` to confirm 1314 passing.
2. After the user approves commits, start SPEC-005 Phase 1 with M2 (`nova/context.py` `_CONTEXT_THREAT_PATTERNS`), adding a regression test that a context file containing `"é"` and `&#x26;` loads unmodified.

## Related Documentation

| Document | Purpose |
|----------|---------|
| [SPEC-005 Review Improvement Plan](docs/SPEC-005-REVIEW_IMPROVEMENT_PLAN.md) | Findings, phases, and acceptance checks |
| [GUIDE-008 Permissions](docs/GUIDE-008-PERMISSIONS.md) | Path-rule semantics |
| [GUIDE-014 Retry and Error Handling](docs/GUIDE-014-RETRY_AND_ERROR_HANDLING.md) | Exception classification |
| [Contribution Guide](CONTRIBUTING.md) | Development and validation workflow |
