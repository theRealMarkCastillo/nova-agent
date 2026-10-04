# GUIDE-009: Using Nova Effectively

**Status:** ✅ Active  
**Last Updated:** October 2026
**Type:** GUIDE (User Reference)

> Practical patterns for getting consistent, high-quality results from Nova Agent day-to-day.

---

## Quick Reference

| Goal | Pattern |
|------|---------|
| Get specific output | Include file paths, function names, and constraints in the task |
| Long multi-step task | Break into checkpoints — verify each before continuing |
| Repeated workflow | Save it to a skill or `NOVA.md` |
| Preserve important facts | Save to the wiki (`Core/` for always-context, elsewhere for reference) |
| Confused or drifting session | `/compact` or `/new` |
| Sensitive environment | Set `permissions.mode: "ask"` |

---

## Writing Good Task Descriptions

The single biggest lever on output quality. Be specific, scoped, and concrete.

### Include what, where, and constraints

```
# ❌ Vague
"Fix the tests"

# ✅ Specific
"The test test_session_truncation in tests/test_session.py is failing because
the session store mock doesn't implement get_recent(). Add that method to the
mock — signature should match SessionStore.get_recent(limit: int) -> list[dict]."
```

### Include format expectations

```
# ❌ Ambiguous
"Summarize the permission system"

# ✅ Scoped
"Give me a 5-bullet summary of how the permission cascade works in
nova/permissions.py, in the order each check runs."
```

### Scope to one concern per task

Large multi-step tasks drift. Give nova one clear goal, verify the result, then continue.

```
# ❌ Too broad
"Refactor the agent, improve the tests, and update the docs"

# ✅ Scoped
"Refactor _execute_tool_calls_parallel in nova/agent.py to reduce nesting.
Don't change behavior. Don't touch anything outside that function."
```

---

## Session Management

### Continue a session when

- Ongoing work on the same feature or bug
- Earlier context (tool results, file reads) is still relevant
- Nova has built up state you want to reference

### Reset (`/new` or `nova reset`) when

- Switching to a completely different task
- The conversation has drifted and nova seems confused
- You want a fresh perspective uninfluenced by prior context

### Compact (`/compact`) when

- Session is long but you want to continue the same work
- Nova is repeating itself or losing track of earlier decisions
- Responses are getting slower or less precise

### Use `nova ask` instead of `nova chat` for

- One-shot questions with no follow-up needed
- Quick lookups: "What does `_truncate` in `nova/context.py` do?"
- Scripted or automated queries

---

## Memory Hygiene (Wiki)

Wiki memory persists across all sessions. The vault lives at `~/.nova/wiki/` and is openable in Obsidian. See [GUIDE-013-MEMORY_SYSTEM](GUIDE-013-MEMORY_SYSTEM.md) for the full reference.

### Where to save

- **`Core/<topic>`** — always-in-context. Use sparingly; every line costs tokens every turn.
  - Personal preferences: `"I prefer seeing the full diff before summaries"`
  - Identity / environment: `"User: Mark, macOS, Python 3.13"`
- **`People/`, `Projects/`, `Facts/`, `Concepts/`** — searchable reference.
  - Project conventions not in `NOVA.md`
  - Discovered tool quirks
  - Domain knowledge worth keeping

### Don't save

- Task progress (`"I was working on feature X"`) — sessions track this
- Temporary state (`"Current branch is feat/thing"`) — will be stale next session
- Anything already in `NOVA.md` or `AGENTS.md` — redundant
- Facts with implicit timestamps that will become incorrect

### Browse and maintain

Open `~/.nova/wiki/` in Obsidian for graph view, link navigation, and inline editing. Periodically ask the agent to run `wiki maintenance` — it returns a report of duplicates, orphans, and stale notes to review.

---

## Context Files (`NOVA.md` and `AGENTS.md`)

Loaded on every session. Use for project-specific conventions the agent should always follow. Keep them concise — they count against your token budget on every turn.

```bash
cp config/NOVA.md.example /path/to/project/NOVA.md
cp config/AGENTS.md.example /path/to/project/AGENTS.md
```

Good candidates for `NOVA.md`:
- Naming conventions and code style rules for this project
- Which commands to use (e.g., always use `.venv/bin/pytest`, not `pytest`)
- Architecture rules (e.g., "tools live in `nova/tools/`, never in `nova/`)
- Things the agent keeps getting wrong

---

## Skills

Nova scans the skills index on every turn and loads relevant ones automatically. The description drives loading — mention the domain and nova will pick it up:

```
# Implicit — nova loads python-coding automatically
"write a function that parses YAML frontmatter with type hints"

# Explicit — force-load when nova misses it
"load the nova-development skill, then add a new tool for HTTP requests"
```

If nova isn't following a skill's conventions, ask it to load the skill explicitly before continuing.

---

## Working With Tools

### Prefer targeted tools over terminal for common operations

```
# ❌ Harder to verify, no line numbers
"run: grep -n 'def _truncate' nova/"

# ✅ Better
"search nova/ for _truncate"
```

### Search before reading large files

```
"search nova/ for _execute_tool_calls, then read the relevant section"
```

### Always review before accepting destructive operations

In `ask` mode, nova will prompt. In `auto` mode, watch for:
- `write_file` on existing files
- `terminal` with `rm`, `git reset --hard`, `drop table`
- `patch_file` on files you haven't read first

Run `git diff` after any agentic session that touches files:

```bash
git diff        # unstaged changes
git diff HEAD   # everything since last commit
```

---

## Common Patterns

### Code → Test → Verify loop

```
1. "Read nova/tools/my_tool.py lines 1-50"
2. "Add a test for the error path when path is missing"
3. "Run pytest tests/test_tools.py -v and show the output"
4. "Run the full CI check"
```

### Incremental refactor

```
1. "Search nova/ for all call sites of _old_function"
2. "Rename _old_function to _new_name in nova/agent.py only"
3. "Update the remaining call sites one file at a time"
4. "Run the full CI check after each file"
```

### Debugging a failing test

```
1. "Run pytest tests/test_agent.py::test_failing -v — show full output"
2. "Read nova/agent.py lines around where the error occurs"
3. "Here's my hypothesis: [your theory]. Does the code support that?"
```

### Exploring an unfamiliar codebase

```
1. "Read NOVA.md and AGENTS.md to understand the architecture"
2. "Search nova/ for the entry point of the permission system"
3. "Read nova/permissions.py and explain how the cascade works"
```

---

## Cost and Token Hygiene

- Use `/usage` to check token consumption for the current session
- Use a cheaper model for simple tasks (`/model qwen/qwen3.6-flash`)
- Lower `budgets.conversation_turn_limit` if active context grows too large
- Use `nova ask` instead of `nova chat` for one-shot queries — no session overhead
- Compact long sessions before switching to a new subtask

---

## Updating Nova

For a Git-based installation, run `nova update` from the environment where you use
Nova. The updater reinstalls dependencies into the virtualenv running Nova,
including virtualenvs outside the repository. Without a running virtualenv, it
checks the repository's `.venv` and then `venv`. It prints the selected interpreter
and exits with an error if no virtualenv is available or installation fails.

## Related Documentation

| Document | Purpose |
|----------|---------|
| [Customizing Nova](GUIDE-003-CUSTOMIZING.md) | Config, SOUL.md, model selection, token budgets |
| [Permissions](GUIDE-008-PERMISSIONS.md) | Control what nova can and can't do |
| [Background Tasks](GUIDE-004-BACKGROUND_TASKS.md) | Long-running commands without blocking chat |
| [Cost Tracking](GUIDE-005-COST_TRACKING.md) | Monitor token usage and dollar cost |
| [README](../README.md) | Installation, slash commands, full feature overview |
