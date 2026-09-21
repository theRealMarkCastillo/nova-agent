"""Tests for session storage."""

import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from nova.session import SessionStore


def test_create_and_get_session():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "test.db"
        store = SessionStore(db)

        sid = store.create_session(model="test-model", title="Test Session")
        assert sid is not None

        info = store.get_session_info(sid)
        assert info is not None
        assert info["model"] == "test-model"
        assert info["title"] == "Test Session"
        assert info["message_count"] == 0


def test_add_and_get_messages():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "test.db"
        store = SessionStore(db)

        sid = store.create_session()
        store.add_message(sid, "user", "Hello")
        store.add_message(sid, "assistant", "Hi there!")

        msgs = store.get_messages(sid)
        assert len(msgs) == 2
        assert msgs[0]["role"] == "user"
        assert msgs[0]["content"] == "Hello"
        assert msgs[1]["role"] == "assistant"


def test_replace_messages_rebuilds_message_count_and_search_content():
    with tempfile.TemporaryDirectory() as tmp:
        store = SessionStore(Path(tmp) / "test.db")
        sid = store.create_session()
        store.add_message(sid, "user", "old")
        store.add_message(sid, "assistant", "answer")

        store.replace_messages(sid, [{"role": "user", "content": "new"}])

        assert store.get_messages(sid) == [{"role": "user", "content": "new"}]
        assert store.get_session_info(sid)["message_count"] == 1
        assert store.search_sessions("new")[0]["session_id"] == sid
        assert store.search_sessions("old") == []


def test_replace_messages_does_not_duplicate_session_search_row():
    with tempfile.TemporaryDirectory() as tmp:
        store = SessionStore(Path(tmp) / "test.db")
        sid = store.create_session()
        store.replace_messages(sid, [{"role": "user", "content": "hello world"}])

        with store._connection() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM session_search WHERE session_id = ?", (sid,)
            ).fetchone()[0]
        assert count == 1
        # Search must return the session exactly once (no duplicate rows).
        results = store.search_sessions("hello")
        assert [r["session_id"] for r in results].count(sid) == 1


def test_delete_session_purges_message_search_index():
    with tempfile.TemporaryDirectory() as tmp:
        store = SessionStore(Path(tmp) / "test.db")
        sid = store.create_session()
        store.add_message(sid, "user", "sensitive secret content")

        assert store.delete_session(sid) is True

        with store._connection() as conn:
            leaked = conn.execute(
                "SELECT COUNT(*) FROM message_search WHERE session_id = ?", (sid,)
            ).fetchone()[0]
        assert leaked == 0


def test_get_messages_with_limit():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "test.db"
        store = SessionStore(db)

        sid = store.create_session()
        for i in range(10):
            store.add_message(sid, "user", f"Message {i}")

        # Get last 3 messages
        msgs = store.get_messages(sid, limit=3)
        assert len(msgs) == 3
        assert msgs[0]["content"] == "Message 7"
        assert msgs[2]["content"] == "Message 9"


def test_list_sessions():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "test.db"
        store = SessionStore(db)

        store.create_session(title="Session A")
        store.create_session(title="Session B")

        sessions = store.list_sessions()
        assert len(sessions) == 2
        titles = {s["title"] for s in sessions}
        assert titles == {"Session A", "Session B"}


def test_update_system_prompt():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "test.db"
        store = SessionStore(db)

        sid = store.create_session()
        store.update_system_prompt(sid, "You are a test agent.")

        info = store.get_session_info(sid)
        assert info["system_prompt"] == "You are a test agent."


def test_update_title_updates_session_and_search_index():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "test.db"
        store = SessionStore(db)

        sid = store.create_session(title="Old title")
        store.update_title(sid, "New title")

        info = store.get_session_info(sid)
        assert info["title"] == "New title"
        assert store.search_sessions("New title")[0]["session_id"] == sid
        assert store.search_sessions("Old title") == []


def test_delete_session_exists():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "test.db"
        store = SessionStore(db)

        sid = store.create_session(title="To Delete")
        store.add_message(sid, "user", "Test message")

        assert store.delete_session(sid) is True

        info = store.get_session_info(sid)
        assert info is None

        msgs = store.get_messages(sid)
        assert len(msgs) == 0


def test_delete_session_not_found():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "test.db"
        store = SessionStore(db)

        assert store.delete_session("nonexistent") is False


def test_prune_sessions():
    import sqlite3
    from datetime import datetime, timedelta

    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "test.db"
        store = SessionStore(db)

        # Create old session by directly updating timestamps
        sid_old = store.create_session(title="Old Session")
        store.add_message(sid_old, "user", "old message")

        sid_new = store.create_session(title="New Session")
        store.add_message(sid_new, "user", "new message")

        # Manually set old session to 40 days ago
        cutoff = (datetime.now() - timedelta(days=40)).isoformat()
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute(
                "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
                (cutoff, sid_old),
            )

        # Prune sessions older than 30 days
        deleted_count = store.prune_sessions(30)
        assert deleted_count == 1

        # Old session should be gone
        assert store.get_session_info(sid_old) is None

        # New session should still exist
        assert store.get_session_info(sid_new) is not None


def test_prune_sessions_zero_deleted():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "test.db"
        store = SessionStore(db)

        store.create_session(title="Recent")

        deleted_count = store.prune_sessions(30)
        assert deleted_count == 0


def test_search_sessions():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "test.db"
        store = SessionStore(db)

        sid1 = store.create_session(title="Python Workshop")
        store.add_message(sid1, "user", "How to learn Python")
        store.add_message(sid1, "assistant", "Python is great for beginners")

        sid2 = store.create_session(title="JavaScript Course")
        store.add_message(sid2, "user", "JavaScript syntax")
        store.add_message(sid2, "assistant", "JS is different from Python")

        sid3 = store.create_session(title="Random Chat")
        store.add_message(sid3, "user", "Hello world")

        # Search for Python
        results = store.search_sessions("Python")
        session_ids = [r["session_id"] for r in results]
        assert sid1 in session_ids
        assert sid2 in session_ids

        # Search for JavaScript
        results = store.search_sessions("JavaScript")
        session_ids = [r["session_id"] for r in results]
        assert sid2 in session_ids


def test_search_sessions_multi_term_and():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "test.db"
        store = SessionStore(db)

        # Words appear non-adjacent — phrase search would miss this
        sid = store.create_session(title="Founding Discussion")
        store.add_message(sid, "user", "We are defining the founding team structure")
        store.add_message(sid, "assistant", "Great, here are the key roles we need to fill")

        results = store.search_sessions("founding roles")
        assert any(r["session_id"] == sid for r in results)


def test_search_sessions_prefix_on_last_term():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "test.db"
        store = SessionStore(db)

        sid = store.create_session(title="Architecture")
        store.add_message(sid, "user", "How do we handle database migrations")

        results = store.search_sessions("database migr")
        assert any(r["session_id"] == sid for r in results)


def test_search_sessions_empty_query():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "test.db"
        store = SessionStore(db)

        sid = store.create_session(title="Test")
        store.add_message(sid, "user", "content")

        results = store.search_sessions("")
        assert isinstance(results, list)


def test_search_sessions_invalid_syntax_returns_empty():
    """FTS5 operator characters must degrade to no results, not raise."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "test.db"
        store = SessionStore(db)

        sid = store.create_session(title="Test")
        store.add_message(sid, "user", "searchable content")

        for bad_query in ("foo AND (", "content:", "^-", "(("):
            results = store.search_sessions(bad_query)
            assert isinstance(results, list)
            assert all(r["session_id"] != sid or bad_query == "" for r in results)


def test_add_message_with_tool_calls():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "test.db"
        store = SessionStore(db)

        sid = store.create_session()
        tool_calls = [{"id": "1", "function": "test_func", "args": {}}]

        idx = store.add_message(sid, "assistant", "Calling function", tool_calls=tool_calls)
        assert idx == 0

        msgs = store.get_messages(sid)
        assert len(msgs) == 1
    assert msgs[0]["tool_calls"] == tool_calls


def test_tool_call_id_round_trips():
    with tempfile.TemporaryDirectory() as tmp:
        store = SessionStore(Path(tmp) / "sessions.db")
        sid = store.create_session()
        store.add_message(sid, "tool", "result", tool_call_id="call_123")
        assert store.get_messages(sid)[0]["tool_call_id"] == "call_123"


def test_add_message_indexes():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "test.db"
        store = SessionStore(db)

        sid = store.create_session()

        idx0 = store.add_message(sid, "user", "First")
        idx1 = store.add_message(sid, "assistant", "Second")
        idx2 = store.add_message(sid, "user", "Third")

        assert idx0 == 0
        assert idx1 == 1
        assert idx2 == 2

        msgs = store.get_messages(sid)
        assert len(msgs) == 3


def test_create_session_custom_id():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "test.db"
        store = SessionStore(db)

        custom_id = "custom_session_123"
        sid = store.create_session(session_id=custom_id, title="Custom")

        assert sid == custom_id
        info = store.get_session_info(sid)
        assert info["session_id"] == custom_id


def test_get_session_info_not_found():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "test.db"
        store = SessionStore(db)

        info = store.get_session_info("nonexistent")
        assert info is None


def test_list_sessions_limit():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "test.db"
        store = SessionStore(db)

        for i in range(25):
            store.create_session(title=f"Session {i}")

        sessions = store.list_sessions(limit=10)
        assert len(sessions) == 10


def test_list_sessions_empty():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "test.db"
        store = SessionStore(db)

        sessions = store.list_sessions()
        assert len(sessions) == 0


def test_session_message_count_increments():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "test.db"
        store = SessionStore(db)

        sid = store.create_session()
        assert store.get_session_info(sid)["message_count"] == 0

        store.add_message(sid, "user", "msg1")
        assert store.get_session_info(sid)["message_count"] == 1

        store.add_message(sid, "assistant", "msg2")
        assert store.get_session_info(sid)["message_count"] == 2


def test_reasoning_content_persists():
    """reasoning_content must round-trip through add_message/get_messages.

    Without this, DeepSeek-style thinking models lose their chain of thought
    on session resume, which makes the next LLM call incoherent.
    """
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "test.db"
        store = SessionStore(db)

        sid = store.create_session()
        store.add_message(
            sid,
            "assistant",
            "",
            tool_calls=[
                {"id": "1", "type": "function", "function": {"name": "x", "arguments": "{}"}}
            ],
            reasoning_content="step 1: think about the problem",
        )

        msgs = store.get_messages(sid)
        assert msgs[0]["reasoning_content"] == "step 1: think about the problem"
        assert msgs[0]["tool_calls"][0]["function"]["name"] == "x"


def test_reasoning_content_omitted_when_none():
    """If reasoning_content is None it must not appear in loaded messages.

    Loading sessions added before this column existed, or assistant messages
    without reasoning, must not include an empty field that confuses callers.
    """
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "test.db"
        store = SessionStore(db)

        sid = store.create_session()
        store.add_message(sid, "assistant", "regular reply")

        msgs = store.get_messages(sid)
        assert "reasoning_content" not in msgs[0]


def test_legacy_database_migrates_reasoning_content_column():
    """Existing databases must gain columns added after their initial schema."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "test.db"
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.executescript(
                """
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
                    timestamp TEXT NOT NULL
                );
                CREATE TABLE session_fts (
                    session_id TEXT PRIMARY KEY,
                    title TEXT,
                    content TEXT
                );
                """
            )

        store = SessionStore(db)
        sid = store.create_session()
        store.add_message(sid, "assistant", "reply", reasoning_content="thinking")

        assert store.get_messages(sid)[0]["reasoning_content"] == "thinking"
