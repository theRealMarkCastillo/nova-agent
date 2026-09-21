"""Tests for CLI argument parsing and command implementations."""

import getpass
import sys
import tempfile
import warnings
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nova.cli import (
    cmd_acp,
    cmd_ask,
    cmd_chat,
    cmd_reset,
    cmd_sessions,
    cmd_setup,
    main,
)

# ─── Argument Parsing Tests ──────────────────────────────────────────────────


def test_cli_no_args_prints_help(capsys):
    """Test that running with no args prints help and exits."""
    with patch.object(sys, "argv", ["nova"]):
        try:
            main()
        except SystemExit as e:
            assert e.code == 1

    captured = capsys.readouterr()
    assert "usage" in captured.out.lower() or "nova" in captured.out.lower()


def test_cli_chat_command_parsing():
    """Test that 'chat' command is parsed correctly."""
    with patch("nova.cli.cmd_chat") as mock_chat:
        with patch.object(sys, "argv", ["nova", "chat"]):
            main()
        mock_chat.assert_called_once()


@pytest.mark.parametrize("flag", ["-c", "--continue"])
def test_cli_chat_continue_command_parsing(flag):
    with patch("nova.cli.cmd_chat") as mock_chat:
        with patch.object(sys, "argv", ["nova", "chat", flag]):
            main()
        args = mock_chat.call_args.args[0]
        assert args.continue_session is True


def test_cli_acp_command_parsing():
    with patch("nova.cli.cmd_acp") as mock_acp:
        with patch.object(sys, "argv", ["nova", "acp"]):
            main()
        mock_acp.assert_called_once()


def test_cmd_acp_runs_server_without_writing_stdout(capsys):
    args = MagicMock()
    with (
        patch("nova.cli.load_config", return_value={"test": "config"}),
        patch("nova.acp_server.run_acp_server") as run_server,
        patch("nova.cli.asyncio.run") as asyncio_run,
    ):
        cmd_acp(args)

    run_server.assert_called_once_with({"test": "config"})
    asyncio_run.assert_called_once()
    asyncio_run.call_args.args[0].close()
    assert capsys.readouterr().out == ""


def test_cli_acp_interrupt_keeps_stdout_protocol_only(capsys):
    with (
        patch("nova.cli.cmd_acp", side_effect=KeyboardInterrupt),
        patch.object(sys, "argv", ["nova", "acp"]),
        pytest.raises(SystemExit, match="130"),
    ):
        main()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Interrupted" in captured.err


def test_cli_chat_with_session():
    """Test that 'chat --session <id>' passes session arg."""
    with patch("nova.cli.cmd_chat") as mock_chat:
        with patch.object(sys, "argv", ["nova", "chat", "--session", "abc123"]):
            main()
        args = mock_chat.call_args[0][0]
        assert args.session == "abc123"


def test_cli_ask_command_parsing():
    """Test that 'ask' command is parsed correctly."""
    with patch("nova.cli.cmd_ask") as mock_ask:
        with patch.object(sys, "argv", ["nova", "ask", "what is python?"]):
            main()
        args = mock_ask.call_args[0][0]
        assert args.question == "what is python?"


def test_cli_sessions_command_parsing():
    """Test that 'sessions' command is parsed correctly."""
    with patch("nova.cli.cmd_sessions") as mock_sessions:
        with patch.object(sys, "argv", ["nova", "sessions"]):
            main()
        args = mock_sessions.call_args[0][0]
        assert args.limit == 20  # default


def test_cli_sessions_with_limit():
    """Test that 'sessions --limit 5' passes limit arg."""
    with patch("nova.cli.cmd_sessions") as mock_sessions:
        with patch.object(sys, "argv", ["nova", "sessions", "--limit", "5"]):
            main()
        args = mock_sessions.call_args[0][0]
        assert args.limit == 5


def test_cli_reset_command_parsing():
    """Test that 'reset' command is parsed correctly."""
    with patch("nova.cli.cmd_reset") as mock_reset:
        with patch.object(sys, "argv", ["nova", "reset"]):
            main()
        mock_reset.assert_called_once()


def test_cli_reset_with_session():
    """Test that 'reset --session <id>' passes session_id arg."""
    with patch("nova.cli.cmd_reset") as mock_reset:
        with patch.object(sys, "argv", ["nova", "reset", "--session", "xyz789"]):
            main()
        args = mock_reset.call_args[0][0]
        assert args.session_id == "xyz789"


# ─── Command Implementation Tests ────────────────────────────────────────────


class TestCmdChat:
    """Tests for cmd_chat command."""

    def test_cmd_chat_starts_agent(self):
        """Test that cmd_chat creates and runs agent via _chat_loop."""
        mock_agent = MagicMock()
        args = MagicMock(session=None, continue_session=False)

        with (
            patch("nova.cli.load_config") as mock_config,
            patch("nova.cli.NovaAgent", return_value=mock_agent) as mock_agent_cls,
            patch("nova.cli._chat_loop") as mock_chat_loop,
        ):
            mock_config.return_value = {"test": "config"}
            cmd_chat(args)

        mock_agent_cls.assert_called_once_with(
            config={"test": "config"},
            session_id=None,
        )
        mock_chat_loop.assert_called_once_with(mock_agent)

    def test_cmd_chat_with_session_id(self):
        """Test that cmd_chat passes session_id to agent."""
        mock_agent = MagicMock()
        args = MagicMock(session="session-123", continue_session=False)

        with (
            patch("nova.cli.load_config") as mock_config,
            patch("nova.cli.NovaAgent", return_value=mock_agent),
            patch("nova.cli._chat_loop") as mock_chat_loop,
        ):
            mock_config.return_value = {"test": "config"}
            cmd_chat(args)

        mock_chat_loop.assert_called_once_with(mock_agent)

    def test_cmd_chat_handles_agent_exception(self):
        """Test that cmd_chat propagates _chat_loop errors."""
        args = MagicMock(session=None, continue_session=False)

        with (
            patch("nova.cli.load_config"),
            patch("nova.cli.NovaAgent"),
            patch("nova.cli._chat_loop", side_effect=KeyboardInterrupt()),
            pytest.raises(KeyboardInterrupt),
        ):
            cmd_chat(args)

    def test_cmd_chat_continues_most_recent_session(self):
        mock_agent = MagicMock()
        args = MagicMock(session=None, continue_session=True)
        mock_store = MagicMock()
        mock_store.list_sessions.return_value = [{"session_id": "latest-session"}]

        with (
            patch("nova.cli.load_config", return_value={"session": {"directory": "/tmp"}}),
            patch("nova.cli.NovaAgent", return_value=mock_agent) as mock_agent_cls,
            patch("nova.cli._chat_loop"),
            patch("nova.session.SessionStore", return_value=mock_store),
        ):
            cmd_chat(args)

        mock_store.list_sessions.assert_called_once_with(limit=1)
        mock_agent_cls.assert_called_once_with(
            config={"session": {"directory": "/tmp"}},
            session_id="latest-session",
        )

    def test_cmd_chat_continue_starts_new_when_no_sessions(self):
        mock_agent = MagicMock()
        args = MagicMock(session=None, continue_session=True)
        mock_store = MagicMock()
        mock_store.list_sessions.return_value = []

        with (
            patch("nova.cli.load_config", return_value={"session": {"directory": "/tmp"}}),
            patch("nova.cli.NovaAgent", return_value=mock_agent) as mock_agent_cls,
            patch("nova.cli._chat_loop"),
            patch("nova.session.SessionStore", return_value=mock_store),
        ):
            cmd_chat(args)

        mock_agent_cls.assert_called_once_with(
            config={"session": {"directory": "/tmp"}},
            session_id=None,
        )


class TestCmdAsk:
    """Tests for cmd_ask command."""

    def test_cmd_ask_runs_question(self, capsys):
        """Test that cmd_ask runs agent and prints response."""
        mock_agent = MagicMock()
        mock_agent.run.return_value = "Answer to the question"
        args = MagicMock(question="What is 2+2?")

        with (
            patch("nova.cli.load_config") as mock_config,
            patch("nova.cli.NovaAgent", return_value=mock_agent),
        ):
            mock_config.return_value = {"test": "config"}
            cmd_ask(args)

        mock_agent.run.assert_called_once_with("What is 2+2?", stream=False)
        captured = capsys.readouterr()
        assert "Answer to the question" in captured.out

    def test_cmd_ask_with_multiword_question(self, capsys):
        """Test that cmd_ask handles multiword questions."""
        mock_agent = MagicMock()
        mock_agent.run.return_value = "42"
        question = "What is the meaning of life, the universe, and everything?"
        args = MagicMock(question=question)

        with patch("nova.cli.load_config"), patch("nova.cli.NovaAgent", return_value=mock_agent):
            cmd_ask(args)

        mock_agent.run.assert_called_once_with(question, stream=False)

    def test_cmd_ask_handles_agent_error(self):
        """Test that cmd_ask propagates agent errors."""
        mock_agent = MagicMock()
        mock_agent.run.side_effect = RuntimeError("Agent failed")
        args = MagicMock(question="What?")

        with (
            patch("nova.cli.load_config"),
            patch("nova.cli.NovaAgent", return_value=mock_agent),
            pytest.raises(RuntimeError),
        ):
            cmd_ask(args)


class TestCmdSessions:
    """Tests for cmd_sessions command."""

    def test_cmd_sessions_list_empty(self, capsys):
        """Test listing sessions when none exist."""
        args = MagicMock(prune=None, limit=20)

        with (
            patch("nova.cli.load_config") as mock_config,
            patch("nova.session.SessionStore") as mock_store_cls,
        ):
            mock_config.return_value = {"session": {"directory": "/tmp"}}
            mock_store = MagicMock()
            mock_store.list_sessions.return_value = []
            mock_store_cls.return_value = mock_store

            cmd_sessions(args)

        captured = capsys.readouterr()
        assert "No sessions found" in captured.out

    def test_cmd_sessions_list_with_data(self, capsys):
        """Test listing sessions with data."""
        args = MagicMock(prune=None, limit=5)
        sessions = [
            {
                "session_id": "sess1",
                "title": "First session",
                "message_count": 10,
                "updated_at": "2026-05-03T10:30:00",
            },
            {
                "session_id": "sess2",
                "title": None,  # untitled
                "message_count": 5,
                "updated_at": "2026-05-02T14:22:00",
            },
        ]

        with (
            patch("nova.cli.load_config") as mock_config,
            patch("nova.session.SessionStore") as mock_store_cls,
        ):
            mock_config.return_value = {"session": {"directory": "/tmp"}}
            mock_store = MagicMock()
            mock_store.list_sessions.return_value = sessions
            mock_store_cls.return_value = mock_store

            cmd_sessions(args)

        captured = capsys.readouterr()
        assert "sess1" in captured.out
        assert "First session" in captured.out
        assert "10" in captured.out
        assert "(untitled)" in captured.out  # untitled session
        mock_store.list_sessions.assert_called_once_with(limit=5)

    def test_cmd_sessions_list_with_custom_limit(self):
        """Test that list_sessions is called with correct limit."""
        args = MagicMock(prune=None, limit=42)

        with (
            patch("nova.cli.load_config") as mock_config,
            patch("nova.session.SessionStore") as mock_store_cls,
        ):
            mock_config.return_value = {"session": {"directory": "/tmp"}}
            mock_store = MagicMock()
            mock_store.list_sessions.return_value = []
            mock_store_cls.return_value = mock_store

            cmd_sessions(args)

        mock_store.list_sessions.assert_called_once_with(limit=42)

    def test_cmd_sessions_prune(self, capsys):
        """Test pruning sessions older than N days."""
        args = MagicMock(prune=30, limit=20)

        with (
            patch("nova.cli.load_config") as mock_config,
            patch("nova.session.SessionStore") as mock_store_cls,
        ):
            mock_config.return_value = {"session": {"directory": "/tmp"}}
            mock_store = MagicMock()
            mock_store.prune_sessions.return_value = 5
            mock_store_cls.return_value = mock_store

            cmd_sessions(args)

        mock_store.prune_sessions.assert_called_once_with(older_than_days=30)
        captured = capsys.readouterr()
        assert "Pruned 5 session(s) older than 30 day(s)" in captured.out

    def test_cmd_sessions_prune_zero_sessions(self, capsys):
        """Test pruning when no sessions are older than threshold."""
        args = MagicMock(prune=1, limit=20)

        with (
            patch("nova.cli.load_config") as mock_config,
            patch("nova.session.SessionStore") as mock_store_cls,
        ):
            mock_config.return_value = {"session": {"directory": "/tmp"}}
            mock_store = MagicMock()
            mock_store.prune_sessions.return_value = 0
            mock_store_cls.return_value = mock_store

            cmd_sessions(args)

        captured = capsys.readouterr()
        assert "Pruned 0 session(s)" in captured.out


class TestCmdReset:
    """Tests for cmd_reset command."""

    def test_cmd_reset_no_session_id_prints_help(self, capsys):
        """Test reset without session_id prints instructions."""
        args = MagicMock(session_id=None)

        with (
            patch("nova.cli.load_config") as mock_config,
            patch("nova.session.SessionStore") as mock_store_cls,
        ):
            mock_config.return_value = {"session": {"directory": "/tmp"}}
            mock_store_cls.return_value = MagicMock()

            cmd_reset(args)

        captured = capsys.readouterr()
        assert "nova reset --session" in captured.out
        assert "nova sessions" in captured.out

    def test_cmd_reset_deletes_session(self, capsys):
        """Test that reset deletes the specified session."""
        args = MagicMock(session_id="sess-123")

        with (
            patch("nova.cli.load_config") as mock_config,
            patch("nova.session.SessionStore") as mock_store_cls,
        ):
            mock_config.return_value = {"session": {"directory": "/tmp"}}
            mock_store = MagicMock()
            mock_store.delete_session.return_value = True
            mock_store_cls.return_value = mock_store

            cmd_reset(args)

        mock_store.delete_session.assert_called_once_with("sess-123")
        captured = capsys.readouterr()
        assert "Deleted session sess-123" in captured.out

    def test_cmd_reset_session_not_found(self, capsys):
        """Test reset when session doesn't exist exits nonzero."""
        args = MagicMock(session_id="nonexistent")

        with (
            patch("nova.cli.load_config") as mock_config,
            patch("nova.session.SessionStore") as mock_store_cls,
        ):
            mock_config.return_value = {"session": {"directory": "/tmp"}}
            mock_store = MagicMock()
            mock_store.delete_session.return_value = False
            mock_store_cls.return_value = mock_store

            with pytest.raises(SystemExit) as exc_info:
                cmd_reset(args)

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "Session not found: nonexistent" in captured.out


class TestCmdSetup:
    """Tests for cmd_setup command."""

    def test_cmd_setup_with_env_key(self, capsys):
        """Test setup when API key is in environment."""
        args = MagicMock()
        tmpdir = tempfile.mkdtemp()

        with (
            patch("nova.cli.ensure_nova_home") as mock_ensure,
            patch.dict("os.environ", {"LLM_API_KEY": "env-api-key"}),
            patch("builtins.input", return_value="1"),
        ):  # select default model
            mock_ensure.return_value = Path(tmpdir)
            cmd_setup(args)

        captured = capsys.readouterr()
        assert "API key found in environment" in captured.out
        assert "Setup complete!" in captured.out

    def test_cmd_setup_with_existing_config(self, capsys):
        """Test setup when config already exists."""
        args = MagicMock()
        tmpdir = tempfile.mkdtemp()
        config_path = Path(tmpdir) / "config.yaml"

        # Write existing config
        import yaml

        existing = {
            "llm": {
                "api_key": "existing-key",
                "model": "anthropic/claude-sonnet-4",
            }
        }
        with open(config_path, "w") as f:
            yaml.dump(existing, f)

        with (
            patch("nova.cli.ensure_nova_home") as mock_ensure,
            patch.dict("os.environ", {}, clear=True),
            patch("builtins.input", side_effect=["N"]),
        ):  # don't change model
            mock_ensure.return_value = Path(tmpdir)
            cmd_setup(args)

        captured = capsys.readouterr()
        assert "API key already configured" in captured.out

    def test_cmd_setup_missing_api_key_exit(self, capsys):
        """Test setup exits when no API key provided."""
        args = MagicMock()
        tmpdir = tempfile.mkdtemp()

        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=getpass.GetPassWarning)
            with (
                patch("nova.cli.ensure_nova_home") as mock_ensure,
                patch.dict("os.environ", {}, clear=True),
                patch("builtins.input", return_value=""),
            ):  # no API key
                mock_ensure.return_value = Path(tmpdir)
                with pytest.raises(SystemExit) as exc_info:
                    cmd_setup(args)
                assert exc_info.value.code == 1

    def test_cmd_setup_with_custom_model(self, capsys):
        """Test setup with custom model selection."""
        args = MagicMock()
        tmpdir = tempfile.mkdtemp()

        with (
            patch("nova.cli.ensure_nova_home") as mock_ensure,
            patch.dict("os.environ", {"LLM_API_KEY": "test-key"}),
            patch("builtins.input", side_effect=["5", "custom/model"]),
        ):
            mock_ensure.return_value = Path(tmpdir)
            cmd_setup(args)

        captured = capsys.readouterr()
        assert "Setup complete!" in captured.out

    def test_cmd_setup_predefined_model_choices(self, capsys):
        """Test setup with predefined model choices."""
        args = MagicMock()
        tmpdir = tempfile.mkdtemp()

        with (
            patch("nova.cli.ensure_nova_home") as mock_ensure,
            patch.dict("os.environ", {"LLM_API_KEY": "test-key"}),
            patch("builtins.input", return_value="2"),
        ):  # choice 2: opus
            mock_ensure.return_value = Path(tmpdir)
            cmd_setup(args)

        captured = capsys.readouterr()
        assert "Setup complete!" in captured.out
