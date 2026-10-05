# GUIDE-012: Session Management

**Status:** ✅ Active  
**Last Updated:** October 2026  
**Type:** GUIDE (Developer & User Reference)

> Nova Agent stores every conversation in a SQLite database with FTS5 full-text search. This guide covers how sessions work, how to manage them, and how to recover old conversations.

---

## Quick Start

| Goal | Command |
|------|---------|
| List recent sessions | `/sessions` |
| Resume a session | `/resume [id]` |
| Start fresh | `/new` or `/reset` |
| Show current session info | `/status` |
| Search past sessions | `/sessions` (FTS5-powered) |

---

## How Sessions Work

Every conversation with Nova is a **session** — a self-contained thread of messages with its own state.

### Session Lifecycle

```
Create → Active → Compact → Persisted / Resumed / Deleted
```

1. **Create** — A new session starts when you run `nova chat` or call `/new`. Each session gets a UUID.
2. **Active** — Messages flow in and out. The session accumulates history, token usage, and tool call records.
3. **Compact** — When the context approaches its budget, Nova deterministically compacts old tool output and removes complete old turns (see [GUIDE-011](GUIDE-011-CONTEXT_COMPRESSION.md)).
4. **Persisted** — The session is saved to disk. It remains available after the chat ends.
5. **Resumed** — You can pick up a persisted session later with `/resume`.

### Storage

Sessions are stored in a SQLite database at `~/.nova/sessions/` (end users) or `sessions/` (developer installs).

**Schema:**

```sql
CREATE TABLE sessions (
    session_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    model TEXT,
    system_prompt TEXT,
    title TEXT,
    message_count INTEGER DEFAULT 0
);

CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    idx INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    tool_calls TEXT,
    tool_call_id TEXT,
    reasoning_content TEXT,
    timestamp TEXT NOT NULL
);

-- FTS5 virtual table (trigram tokenizer) indexing each message.
CREATE VIRTUAL TABLE message_search USING fts5(
    message_id UNINDEXED, session_id UNINDEXED, idx UNINDEXED,
    role UNINDEXED, content, tokenize='trigram'
);
```

**FTS5 full-text search** enables searching session titles and content for keyword matching — no regex, no grep. Just human-readable queries.

`search_sessions` returns sessions where every query word appears in the title or in some user or assistant message; the words may appear in different messages. Sessions with more matching messages rank first, then more recent ones. Tool output is searchable with `search_messages` but does not make a session match.

> **Schema version 4 (October 2026):** databases from earlier versions are migrated automatically on first open. The migration drops the old `session_fts` and `session_search` tables, which re-indexed a whole session on every new message, so long sessions got progressively slower to write. No messages are lost. Older Nova versions can still read a migrated database but fail when creating a session in it, so update every installation that shares it.

> ⚠️ The **trigram tokenizer** only indexes terms of 3+ characters. Searches for shorter terms (e.g. `AI`, `go`, or a single letter) return no results. Use a more specific term of 3+ characters to match reliably.

---

## Session Commands

All commands work inside `nova chat`:

### `/sessions`

Lists your most recent sessions (default: last 10).

```
/sessions
/sessions 20     # show last 20
/sessions search "deploy"  # search by keyword
```

Output:
```
ID                  Title                        Date           Messages
──────────────────  ───────────────────────────  ────────────   ────────
a3f7b2c1-...        Deploy backend to prod       May 08, 10:15  47
e8d4f1a2-...        Debug memory module          May 07, 14:30  32
b9c2e5d3-...        Write release notes          May 06, 09:00  18
```

### `/resume [id]`

Continues a previous session. The full message history is loaded and the conversation picks up where it left off.

```
/resume a3f7b2c1-...
```

**When to resume:**
- You started a long task and want to continue later
- A session was interrupted (crash, network issue)
- You want to pick up a conversation from a different machine (if sessions are synced)

**When NOT to resume:**
- The session context is stale (days or weeks old) — start fresh instead
- The session has drifted and is producing poor results — start fresh

### `/new` / `/reset`

Starts a new session. The previous session is persisted (not deleted) and can be resumed later.

```
/new
```

This is the same as `/reset` — they're aliases.

### `/status`

Shows the current session's state:

```
Session: a3f7b2c1-...
Model: qwen/qwen3.6-flash
Tokens: 45,230 / 128,000 (35%)
Messages: 38
Cost: $0.002145
Delegation: 0 sub-agents spawned
```

---

## Session Configuration

Sessions use the model context window and token budgets for active context:

```yaml
microcompact:
  enabled: true
  keep_recent: 6         # protect recent messages from tool-output compaction
```

### Session-Specific Overrides

You can override the model for a single session without changing your global config:

```
/model anthropic/claude-3.5-sonnet
```

This sets the model for the current session only. Switching back:

```
/model qwen/qwen3.6-flash
```

---

## Session Storage Details

### Database Location

| Install type | Path |
|-------------|------|
| End user | `~/.nova/sessions/sessions.db` |
| Developer | `sessions/sessions.db` (in project root) |

### Maintenance

SQLite handles its own maintenance, but you can optimize the FTS5 index:

```sql
-- Run periodically to keep search fast
PRAGMA fts5_optimize;
```

Nova calls `PRAGMA optimize` automatically on session close.

### Deletion

Sessions are **never auto-deleted**. They persist indefinitely until you remove them manually. This is intentional — you might need to pick up a conversation from months ago.

Deleting a session removes its messages **and** purges them from the full-text search index (`message_search`), so deleted conversation content is not recoverable via `search_sessions` or `search_messages`.

To delete old sessions directly:

```bash
sqlite3 ~/.nova/sessions/sessions.db \
  "DELETE FROM messages       WHERE session_id IN (SELECT session_id FROM sessions WHERE created_at < '2026-01-01');
   DELETE FROM message_search WHERE session_id IN (SELECT session_id FROM sessions WHERE created_at < '2026-01-01');
   DELETE FROM sessions       WHERE created_at < '2026-01-01';"
```

---

## Troubleshooting

| Problem | Cause | Solution |
|---------|-------|----------|
| `/sessions` shows nothing | No sessions exist yet | Start a conversation first |
| `/resume` fails with "session not found" | Session was deleted or DB is corrupted | Check `~/.nova/sessions/sessions.db` |
| Session title is "Untitled" | First message didn't have enough context | Nova will update the title on the next meaningful exchange |
| Search returns no results | FTS5 index is stale | Run `PRAGMA optimize` or restart Nova |
| Database is huge (>100MB) | Lots of sessions with long histories | Sessions are never auto-deleted; delete old ones manually |
| Session messages appear out of order | Rare SQLite journal mode issue | Ensure `PRAGMA journal_mode=WAL` (Nova sets this on init) |

---

## Related Documentation

| Document | Purpose |
|----------|---------|
| [GUIDE-003 Customizing](GUIDE-003-CUSTOMIZING.md) | Config options for sessions |
| [GUIDE-011 Context Compression](GUIDE-011-CONTEXT_COMPRESSION.md) | How context is managed within sessions |
| [GUIDE-009 Using Nova](GUIDE-009-USING_NOVA.md) | Slash commands overview |
| [ADR-003 Tool System Review](ADR-003-TOOL_SYSTEM_REVIEW.md) | Session storage design decisions |
