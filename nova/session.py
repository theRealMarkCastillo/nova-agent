"""SQLite session storage with FTS5 search.

Stores conversation sessions with message history, system prompts, and metadata.
"""

import json
import logging
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


class SessionStore:
    """SQLite-backed session storage."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.chmod(0o700)
        self._init_db()
        self.db_path.chmod(0o600)

    @contextmanager
    def _connection(self):
        """Open a short-lived SQLite connection with production-safe defaults."""
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def close(self) -> None:
        """Merge FTS5 index segments for efficient future searches."""
        if not self.db_path.exists():
            return
        try:
            with self._connection() as conn:
                conn.execute("PRAGMA optimize")
        except sqlite3.OperationalError:
            pass

    def _init_db(self):
        """Initialize database schema."""
        with self._connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    model TEXT,
                    system_prompt TEXT,
                    title TEXT,
                    message_count INTEGER DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    idx INTEGER NOT NULL,
                    role TEXT NOT NULL,
                     content TEXT NOT NULL,
                     tool_calls TEXT,
                     tool_call_id TEXT,
                     reasoning_content TEXT,
                    timestamp TEXT NOT NULL,
                    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
                );

                CREATE INDEX IF NOT EXISTS idx_messages_session
                    ON messages(session_id, idx);

                CREATE INDEX IF NOT EXISTS idx_sessions_updated_at
                    ON sessions(updated_at);
            """)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
            if "tool_call_id" not in columns:
                conn.execute("ALTER TABLE messages ADD COLUMN tool_call_id TEXT")
            if "reasoning_content" not in columns:
                conn.execute("ALTER TABLE messages ADD COLUMN reasoning_content TEXT")
            self._migrate_search_indexes(conn)

    def _migrate_search_indexes(self, conn: sqlite3.Connection) -> None:
        """Bring the full-text indexes to the current schema (user_version 4).

        Version 3 added ``message_search``, a trigram index of each message.
        Version 4 removed ``session_fts``/``session_search``, which appended
        every message to one row per session and re-indexed the whole session
        on each write. Session search now queries ``message_search`` instead,
        so dropping them loses nothing.
        """
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version < 3:
            conn.execute("DROP TABLE IF EXISTS message_search")
            conn.execute(
                "CREATE VIRTUAL TABLE message_search "
                "USING fts5(message_id UNINDEXED, session_id UNINDEXED, idx UNINDEXED, "
                "role UNINDEXED, content, tokenize='trigram')"
            )
            conn.execute(
                "INSERT INTO message_search(message_id, session_id, idx, role, content) "
                "SELECT id, session_id, idx, role, content FROM messages"
            )
        if version < 4:
            conn.execute("DROP TRIGGER IF EXISTS session_fts_insert")
            conn.execute("DROP TRIGGER IF EXISTS session_fts_update")
            conn.execute("DROP TABLE IF EXISTS session_search")
            conn.execute("DROP TABLE IF EXISTS session_fts")
            conn.execute("PRAGMA user_version = 4")

    def create_session(
        self,
        session_id: str | None = None,
        model: str | None = None,
        system_prompt: str | None = None,
        title: str | None = None,
    ) -> str:
        """Create a new session."""
        if session_id is None:
            session_id = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        if not session_id:
            raise ValueError("session_id must be non-empty")

        now = datetime.now().isoformat()
        with self._connection() as conn:
            conn.execute(
                "INSERT INTO sessions (session_id, created_at, updated_at, model, system_prompt, title, message_count) "
                "VALUES (?, ?, ?, ?, ?, ?, 0)",
                (session_id, now, now, model, system_prompt, title),
            )

        logger.info("Created session %s", session_id)
        return session_id

    def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        tool_calls: list | None = None,
        reasoning_content: str | None = None,
        tool_call_id: str | None = None,
    ) -> int:
        """Add a message to a session. Returns the message index."""
        if not session_id:
            raise ValueError("session_id must be non-empty")
        now = datetime.now().isoformat()
        tool_calls_json = json.dumps(tool_calls) if tool_calls else None

        with self._connection() as conn:
            # Atomic: get max idx and insert in one transaction to avoid TOCTOU race
            conn.execute("BEGIN IMMEDIATE")
            try:
                cursor = conn.execute(
                    "SELECT COALESCE(MAX(idx), -1) FROM messages WHERE session_id = ?",
                    (session_id,),
                )
                idx = cursor.fetchone()[0] + 1

                cursor = conn.execute(
                    "INSERT INTO messages "
                    "(session_id, idx, role, content, tool_calls, tool_call_id, reasoning_content, timestamp) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        session_id,
                        idx,
                        role,
                        content,
                        tool_calls_json,
                        tool_call_id,
                        reasoning_content,
                        now,
                    ),
                )
                conn.execute(
                    "INSERT INTO message_search(message_id, session_id, idx, role, content) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (cursor.lastrowid, session_id, idx, role, content),
                )
                conn.execute(
                    "UPDATE sessions SET updated_at = ?, message_count = message_count + 1 WHERE session_id = ?",
                    (now, session_id),
                )
            except Exception:
                conn.execute("ROLLBACK")
                raise

        return idx

    def get_messages(
        self,
        session_id: str,
        limit: int | None = None,
        around_idx: int | None = None,
        radius: int = 2,
    ) -> list[dict]:
        """Get all messages for a session, optionally limited."""
        with self._connection() as conn:
            if around_idx is not None:
                radius = max(0, min(int(radius), 20))
                query = (
                    "SELECT role, content, tool_calls, tool_call_id, reasoning_content FROM messages "
                    "WHERE session_id = ? AND idx BETWEEN ? AND ? ORDER BY idx"
                )
                cursor = conn.execute(query, (session_id, around_idx - radius, around_idx + radius))
            elif limit:
                # Use parameterized query to prevent SQL injection
                query = (
                    "SELECT role, content, tool_calls, tool_call_id, reasoning_content FROM messages "
                    "WHERE session_id = ? ORDER BY idx DESC LIMIT ?"
                )
                cursor = conn.execute(query, (session_id, limit))
            else:
                query = (
                    "SELECT role, content, tool_calls, tool_call_id, reasoning_content FROM messages "
                    "WHERE session_id = ? ORDER BY idx"
                )
                cursor = conn.execute(query, (session_id,))
            messages = []
            for row in cursor.fetchall():
                msg = {
                    "role": row[0],
                    "content": row[1],
                }
                if row[2]:
                    try:
                        tool_calls = json.loads(row[2])
                    except (TypeError, ValueError, json.JSONDecodeError):
                        logger.warning("Ignoring malformed tool_calls in session %s", session_id)
                    else:
                        if isinstance(tool_calls, list):
                            msg["tool_calls"] = tool_calls
                if row[3]:
                    msg["tool_call_id"] = row[3]
                if row[4]:
                    msg["reasoning_content"] = row[4]
                messages.append(msg)

            if limit:
                messages.reverse()

            return messages

    def get_session_info(self, session_id: str) -> dict | None:
        """Get session metadata."""
        with self._connection() as conn:
            cursor = conn.execute(
                "SELECT session_id, created_at, updated_at, model, system_prompt, title, message_count "
                "FROM sessions WHERE session_id = ?",
                (session_id,),
            )
            row = cursor.fetchone()
            if not row:
                return None

            return {
                "session_id": row[0],
                "created_at": row[1],
                "updated_at": row[2],
                "model": row[3],
                "system_prompt": row[4],
                "title": row[5],
                "message_count": row[6],
            }

    def update_system_prompt(self, session_id: str, system_prompt: str):
        """Update the system prompt for a session."""
        with self._connection() as conn:
            conn.execute(
                "UPDATE sessions SET system_prompt = ?, updated_at = ? WHERE session_id = ?",
                (system_prompt, datetime.now().isoformat(), session_id),
            )

    def update_title(self, session_id: str, title: str):
        """Update a session title."""
        now = datetime.now().isoformat()
        with self._connection() as conn:
            conn.execute(
                "UPDATE sessions SET title = ?, updated_at = ? WHERE session_id = ?",
                (title, now, session_id),
            )

    def replace_messages(self, session_id: str, messages: list[dict]) -> None:
        """Replace the persisted conversation messages for a session."""
        now = datetime.now().isoformat()
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM message_search WHERE session_id = ?", (session_id,))
            for idx, message in enumerate(messages):
                content = message.get("content") or ""
                role = message.get("role", "")
                tool_calls = message.get("tool_calls")
                cursor = conn.execute(
                    "INSERT INTO messages "
                    "(session_id, idx, role, content, tool_calls, tool_call_id, reasoning_content, timestamp) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        session_id,
                        idx,
                        role,
                        content,
                        json.dumps(tool_calls) if tool_calls else None,
                        message.get("tool_call_id"),
                        message.get("reasoning_content"),
                        now,
                    ),
                )
                conn.execute(
                    "INSERT INTO message_search(message_id, session_id, idx, role, content) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (cursor.lastrowid, session_id, idx, role, content),
                )
            conn.execute(
                "UPDATE sessions SET message_count = ?, updated_at = ? WHERE session_id = ?",
                (len(messages), now, session_id),
            )

    def list_sessions(self, limit: int = 20) -> list[dict]:
        """List recent sessions."""
        with self._connection() as conn:
            cursor = conn.execute(
                "SELECT session_id, created_at, updated_at, model, title, message_count "
                "FROM sessions ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            )
            return [
                {
                    "session_id": row[0],
                    "created_at": row[1],
                    "updated_at": row[2],
                    "model": row[3],
                    "title": row[4],
                    "message_count": row[5],
                }
                for row in cursor.fetchall()
            ]

    def delete_session(self, session_id: str) -> bool:
        """Delete a session and all its messages. Returns True if deleted."""
        with self._connection() as conn:
            cursor = conn.execute(
                "SELECT session_id FROM sessions WHERE session_id = ?", (session_id,)
            )
            if not cursor.fetchone():
                return False
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM message_search WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
        logger.info("Deleted session %s", session_id)
        return True

    def prune_sessions(self, older_than_days: int) -> int:
        """Delete sessions older than N days. Returns count deleted."""
        from datetime import timedelta

        cutoff = (datetime.now() - timedelta(days=older_than_days)).isoformat()
        with self._connection() as conn:
            cursor = conn.execute("SELECT session_id FROM sessions WHERE updated_at < ?", (cutoff,))
            old_ids = [row[0] for row in cursor.fetchall()]
            if old_ids:
                placeholders = ",".join("?" for _ in old_ids)
                conn.execute(f"DELETE FROM messages WHERE session_id IN ({placeholders})", old_ids)
                conn.execute(
                    f"DELETE FROM message_search WHERE session_id IN ({placeholders})", old_ids
                )
                conn.execute(f"DELETE FROM sessions WHERE session_id IN ({placeholders})", old_ids)
        logger.info("Pruned %d sessions older than %d days", len(old_ids), older_than_days)
        return len(old_ids)

    def search_sessions(self, query: str, limit: int = 10) -> list[dict]:
        """Find sessions where every query word appears in the title or a message.

        Words may appear in different messages. Sessions with more matching
        user/assistant messages rank first, then more recent ones.
        """
        # The trigram index cannot match words under 3 characters, so they are
        # ignored, as the index itself does within a multi-word query.
        tokens = [token for token in re.findall(r"\S+", query.strip()) if len(token) >= 3]
        if not tokens:
            return []
        # Quote each token so FTS operators and punctuation are data, not syntax.
        quoted = [f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens]
        message_match = (
            "SELECT session_id FROM message_search "
            "WHERE message_search MATCH ? AND role IN ('user', 'assistant')"
        )
        token_clause = (
            f"(instr(lower(coalesce(s.title, '')), lower(?)) > 0 "
            f"OR s.session_id IN ({message_match}))"
        )
        params: list[object] = [" OR ".join(quoted)]
        for token, phrase in zip(tokens, quoted, strict=True):
            params.extend([token, phrase])
        params.append(limit)
        sql = (
            "WITH hits AS ("
            "SELECT session_id, COUNT(*) AS matches FROM message_search "
            "WHERE message_search MATCH ? AND role IN ('user', 'assistant') "
            "GROUP BY session_id) "
            "SELECT s.session_id, s.title, s.updated_at, s.message_count "
            "FROM sessions s LEFT JOIN hits h ON h.session_id = s.session_id "
            f"WHERE {' AND '.join([token_clause] * len(tokens))} "
            "ORDER BY COALESCE(h.matches, 0) DESC, s.updated_at DESC LIMIT ?"
        )
        try:
            with self._connection() as conn:
                return [
                    {
                        "session_id": row[0],
                        "title": row[1],
                        "updated_at": row[2],
                        "message_count": row[3],
                    }
                    for row in conn.execute(sql, params).fetchall()
                ]
        except sqlite3.OperationalError as e:
            # FTS5 treats characters like ( ) : ^ - as query syntax; a hostile
            # or accidental operator string must degrade to no results.
            logger.warning("Session search rejected query %r: %s", query[:80], e)
            return []

    def search_messages(
        self, query: str, limit: int = 10, session_id: str | None = None
    ) -> list[dict]:
        """Search individual messages using the FTS5 trigram index."""
        tokens = re.findall(r"\S+", query.strip())
        if not tokens:
            return []
        fts_query = " ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)
        try:
            limit = max(1, min(int(limit), 50))
            with self._connection() as conn:
                sql = (
                    "SELECT m.message_id, m.session_id, m.idx, m.role, "
                    "snippet(message_search, 4, '[', ']', '...', 24), "
                    "s.title, s.updated_at "
                    "FROM message_search m JOIN sessions s ON s.session_id = m.session_id "
                    "WHERE message_search MATCH ?"
                )
                params: list[object] = [fts_query]
                if session_id:
                    sql += " AND m.session_id = ?"
                    params.append(session_id)
                sql += " ORDER BY rank LIMIT ?"
                params.append(limit)
                rows = conn.execute(sql, params).fetchall()
                return [
                    {
                        "message_id": row[0],
                        "session_id": row[1],
                        "idx": row[2],
                        "role": row[3],
                        "snippet": row[4],
                        "title": row[5],
                        "updated_at": row[6],
                    }
                    for row in rows
                ]
        except (sqlite3.OperationalError, ValueError, TypeError) as e:
            logger.warning("Message search rejected query %r: %s", fts_query[:80], e)
            return []
