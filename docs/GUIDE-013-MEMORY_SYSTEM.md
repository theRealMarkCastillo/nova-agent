# GUIDE-013: Memory System (Obsidian-Compatible Wiki)

**Status:** ✅ Active
**Last Updated:** October 2026
**Type:** GUIDE (Developer & User Reference)

> Nova's memory is a persistent wiki of Obsidian-compatible markdown notes. Notes survive across sessions, link to each other with `[[wikilinks]]`, and can be browsed directly in Obsidian.

---

## Quick Start

### For Users (in chat)

```
User: Remember that I prefer Python 3.12 for projects.
Assistant: [wiki(action="write", title="Core/Preferences", content="Prefers Python 3.12 for projects")]
Assistant: Saved to Core/Preferences — this will be in every prompt going forward.

User: What do you know about my preferences?
Assistant: [wiki(action="search", query="preferences")]
Assistant: Found Core/Preferences: "Prefers Python 3.12 for projects."
```

### For Developers (programmatic)

```python
from pathlib import Path
from nova.wiki_memory import WikiMemory

wiki = WikiMemory(Path("~/.nova/wiki").expanduser(), max_prompt_notes=10)

# Write a note (creates Projects/nova.md with YAML frontmatter)
wiki.write("Projects/nova", "Agent framework for personal use.", tags=["agent"])

# Append to an existing note
wiki.append("Projects/nova", "Uses OpenRouter for LLM calls.")

# Read a note
note = wiki.read("Projects/nova")
# {"frontmatter": {...}, "content": "..."}

# Full-text search
results = wiki.search("OpenRouter")

# List notes (optionally filter by tag)
notes = wiki.list_notes(tag="agent")

# Maintenance — read-only analysis report
report = wiki.maintenance(stale_days=90)
```

---

## How Memory Works

The vault lives at `~/.nova/wiki/` by default and is a regular Obsidian vault — open it in Obsidian directly to browse, edit, and visualize the link graph.

### Vault layout

```
~/.nova/wiki/
  Core/                    # Always-in-context (full content auto-injected)
    Preferences.md
    Identity.md
  People/
    Mark.md
  Projects/
    nova-agent.md
  Facts/
    python-tooling.md
  Concepts/
    react-hooks.md
```

### Note format

Each note is a markdown file with YAML frontmatter:

```markdown
---
title: Projects/nova
tags: [agent, python]
created: 2026-05-11T10:00:00
modified: 2026-05-11T14:32:00
---

Nova is a personal AI agent framework.
Built on [[OpenRouter]] for model access.
Related: [[Projects/skills-system]], [[Facts/python-tooling]]
```

- **`[[wikilinks]]`** connect notes (Obsidian resolves by basename)
- **`#tags`** add cross-cutting context
- **Folder prefix in title** organizes notes (e.g., `People/Mark` → `~/.nova/wiki/People/Mark.md`)

---

## The `Core/` Convention

Notes in `Core/` are **special**: their **full content** is injected into every system prompt, no tool call required. Notes elsewhere only appear as an index (title + first line + tags).

| Folder | Injection | Use for |
|---|---|---|
| `Core/` | Full content, every prompt | Identity, preferences, environment, constants |
| Everything else | Title-only index of 10 most recent | Reference knowledge, project state, people |

**Keep `Core/` short.** Every line there costs tokens on every turn. Use it for facts the agent should *always* know without searching.

**Changes that reach every prompt need confirmation, even in `auto` mode.** These are: writing, appending to, or patching a `Core/` or pinned note; pinning a note; renaming a note into `Core/`; and vault-wide `replace`. They prompt even when `wiki` is in `allowed_tools`. Without an interactive confirmation (for example a script with no TTY), they are denied. Ordinary notes are unaffected.

**Injected notes are scanned.** `Core/` notes and notes with `inject: true` are checked for prompt-injection patterns each time the prompt is built. A flagged note is replaced by a `[BLOCKED: …]` placeholder in the prompt; the file itself is left unchanged so you can review it. This keeps an instruction that the agent was tricked into saving from persisting into every later session. It is a pattern check, not a guarantee, so review what gets written to `Core/`.

---

## Operations

| Action | Description |
|---|---|
| `write` | Create or overwrite a note (preserves `created` date if file exists) |
| `append` | Add content to an existing note (or create if absent) |
| `read` | Fetch a note's content |
| `search` | Case-insensitive full-text search across the vault |
| `list` | List notes by most recent, optionally filtered by tag |
| `delete` | Remove a note |
| `maintenance` | Read-only report: duplicate candidates, orphans, stale notes |

All operations are exposed through a single `wiki` tool — see [Built-in Tools](#built-in-tools).

Mutating operations are protected by both an in-process lock and a vault-local advisory lock, so separate Nova processes do not concurrently overwrite the same note. Read-only lookups and missing-note mutations do not create empty directories. Atomic replacement keeps note writes from exposing partially written files.

---

## Maintenance

Memory rots if nobody prunes it. The `wiki maintenance` action returns a read-only report so the agent (or you) can decide what to act on:

```python
report = wiki.maintenance(stale_days=90)
# {
#   "total_notes": 42,
#   "duplicate_candidates": [
#     {"titles": ["People/Mark", "Projects/Mark"], "paths": [...]}
#   ],
#   "orphans": [{"title": "Lonely Note", "path": "..."}],
#   "stale":   [{"title": "Old Project", "days_old": 184, "path": "..."}],
#   "tag_counts": {"python": 12, "api": 5, ...}
# }
```

**Detection heuristics:**
- **Duplicate candidates** — same basename (`People/Mark` vs `Projects/Mark`), one title contains the other, or ≥60% word overlap
- **Orphans** — notes with no `[[wikilinks]]` pointing in and none pointing out
- **Stale** — modified date older than `stale_days` (default 90)
- **Tag counts** — surfaces typos / near-duplicate tags (`python` vs `Python`)

**Policy: maintenance is suggest-only.** No automatic deletion. The agent appends corrections, merges via explicit `write`+`delete`, or asks the user.

---

## Memory vs. Context vs. Skills

| System | Purpose | Lifetime | Source |
|---|---|---|---|
| **Context** | Current conversation state | Single session (compressed over time) | Conversation flow |
| **Wiki memory** | Cross-session knowledge | Indefinite | Agent's `wiki` tool calls |
| **Skills** | Domain instructions | Indefinite | SKILL.md files |

### When to use each

- **Context** — "Let's debug this function together" → ephemeral, stays in the conversation
- **Wiki** — "Remember that I use a MacBook Pro" → persists across sessions
- **Skills** — "Write Python following PEP 8" → reusable instructions loaded on demand

---

## Configuration

```yaml
wiki:
  enabled: true              # On by default
  vault_path: "~/.nova/wiki" # Or point at an existing Obsidian vault
  max_prompt_notes: 10       # How many recent notes appear in the index
```

### End users

Enabled out of the box at `~/.nova/wiki/`. No config needed.

### Pointing at an existing Obsidian vault

```yaml
wiki:
  vault_path: "~/Documents/ObsidianVault/Nova"
```

Nova will write its notes into a subfolder structure (`Core/`, `People/`, etc.) inside that vault.

### Developers

```python
from pathlib import Path
from nova.wiki_memory import WikiMemory

wiki = WikiMemory(Path("/tmp/test-vault"), max_prompt_notes=5)
```

The `WikiMemory` instance is injectable into `NovaAgent` via `wiki_memory_store=` for testing.

---

## Best Practices

### For Users

1. **Curate `Core/`** — every line lives in every prompt. Keep it tight.
2. **Folder taxonomy** — `Core/`, `People/`, `Projects/`, `Facts/`, `Concepts/` cover most cases. Don't sprawl new folders.
3. **Link aggressively** — use `[[wikilinks]]`. The graph is your map.
4. **Run `wiki maintenance`** periodically to surface dedup and orphan candidates.
5. **Open it in Obsidian** — the graph view is genuinely useful.

### For Developers

1. **Atomic writes** — `WikiMemory` uses `mkstemp` + `os.replace` for crash-safe writes
2. **Path traversal protection** — titles with `..` or absolute paths are rejected
3. **YAML safety** — corrupt frontmatter degrades to empty dict, never crashes
4. **Search is linear** — fine for thousands of notes. If you need millions, add an index.
5. **Concurrency** — `WikiMemory` is not thread-safe. Wrap with a lock for multi-threaded use.

---

## Troubleshooting

| Problem | Cause | Solution |
|---|---|---|
| Notes not appearing in prompt | Wiki disabled | Set `wiki.enabled: true` in `~/.nova/config.yaml` |
| Core notes not auto-injected | Note isn't under `Core/` folder | Use title prefix `Core/<name>` when writing |
| "path traversal" error | Title contains `..` or absolute path | Use a relative title without `..` |
| Vault not visible in Obsidian | Obsidian needs to "open vault" on the directory | File → Open vault → select `~/.nova/wiki/` |
| Maintenance reports too many duplicates | Heuristic is permissive by design | Treat as candidates to review, not certainties |

---

## Related Documentation

| Document | Purpose |
|---|---|
| [GUIDE-003 Customizing](GUIDE-003-CUSTOMIZING.md) | Config options including wiki |
| [GUIDE-011 Context Compression](GUIDE-011-CONTEXT_COMPRESSION.md) | How context differs from memory |
| [GUIDE-012 Session Management](GUIDE-012-SESSION_MANAGEMENT.md) | Session storage (separate from memory) |
| [GUIDE-009 Using Nova](GUIDE-009-USING_NOVA.md) | Wiki tool usage in chat |
