"""Tests for slash command handlers."""

from unittest.mock import MagicMock, patch

import pytest

from nova.command_handlers import (
    _HANDLERS,
    dispatch_command,
    get_registered_commands,
)

# ─── Helpers ─────────────────────────────────────────────────────────────────

DISPLAY_MOD = "nova.command_handlers"


def _patch_display():
    """Suppress all terminal output in command handlers."""
    return patch(
        f"{DISPLAY_MOD}._cprint"
        if hasattr(__import__("nova.command_handlers", fromlist=[""]), "_cprint")
        else "nova.display._cprint"
    )


# ─── Registry ────────────────────────────────────────────────────────────────


def test_all_expected_commands_are_registered():
    expected = {
        "new",
        "reset",
        "history",
        "status",
        "st",
        "sessions",
        "model",
        "tools",
        "usage",
        "undo",
        "compact",
        "copy",
    }
    for cmd in expected:
        assert cmd in _HANDLERS, f"Command '{cmd}' missing from registry"


def test_dispatch_command_returns_true_for_known_command(agent):
    with patch("nova.display._cprint"):
        result = dispatch_command("status", agent, "")
    assert result is True


def test_dispatch_command_returns_false_for_unknown_command(agent):
    result = dispatch_command("nonexistent_xyz", agent, "")
    assert result is False


def test_dispatch_command_handles_handler_exception(agent):
    with (
        patch.dict(_HANDLERS, {"boom": MagicMock(side_effect=RuntimeError("oops"))}),
        patch("nova.display._cprint"),
    ):
        result = dispatch_command("boom", agent, "")
    assert result is True  # handler found, exception caught internally


def test_get_registered_commands_returns_set(agent):
    cmds = get_registered_commands()
    assert isinstance(cmds, set)
    assert len(cmds) > 0


def test_command_registries_do_not_drift():
    """Every dispatchable handler name/alias must be known to commands.py.

    commands.py is the source of truth for autocomplete and TUI resolution;
    a handler the TUI cannot resolve is unreachable in the chat loop.
    """
    from nova.commands import resolve_command

    # Handlers that are intentionally resolved dynamically (skills) are excluded;
    # only static @command_handler names/aliases are checked here.
    for name in _HANDLERS:
        assert resolve_command(name) is not None, (
            f"Handler '{name}' is not in commands.py COMMAND_REGISTRY (drift)"
        )


# ─── cmd_new ─────────────────────────────────────────────────────────────────


def test_cmd_new_creates_new_session(agent):
    old_session_id = agent.session_id
    agent.messages = [{"role": "user", "content": "old"}]
    with patch("nova.display._cprint"):
        dispatch_command("new", agent, "")
    assert agent.session_id is not None
    assert agent.session_id != old_session_id
    assert agent.messages == []


def test_cmd_reset_alias_works(agent):
    old_session_id = agent.session_id
    with patch("nova.display._cprint"):
        dispatch_command("reset", agent, "")
    assert agent.session_id != old_session_id


# ─── cmd_history ─────────────────────────────────────────────────────────────


def test_cmd_history_empty_messages(agent):
    agent.messages = []
    with patch("nova.display._cprint") as mock_print:
        dispatch_command("history", agent, "")
    mock_print.assert_not_called()


def test_cmd_history_shows_user_and_assistant_messages(agent):
    agent.messages = [
        {"role": "user", "content": "hello world"},
        {"role": "assistant", "content": "hi there"},
    ]
    printed = []
    with patch("nova.display._cprint", side_effect=lambda s: printed.append(s)):
        dispatch_command("history", agent, "")
    combined = "\n".join(printed)
    assert "hello world" in combined
    assert "hi there" in combined


def test_cmd_history_truncates_long_assistant_content(agent):
    long_content = "x" * 300
    agent.messages = [{"role": "assistant", "content": long_content}]
    printed = []
    with patch("nova.display._cprint", side_effect=lambda s: printed.append(s)):
        dispatch_command("history", agent, "")
    combined = "\n".join(printed)
    assert "…" in combined


def test_cmd_history_skips_tool_messages(agent):
    agent.messages = [
        {"role": "tool", "content": "tool result"},
    ]
    with patch("nova.display._cprint") as mock_print:
        dispatch_command("history", agent, "")
    mock_print.assert_not_called()


# ─── cmd_status ──────────────────────────────────────────────────────────────


def test_cmd_status_prints_session_info(agent):
    printed = []
    with patch("nova.display._cprint", side_effect=lambda s: printed.append(s)):
        dispatch_command("status", agent, "")
    combined = "\n".join(printed)
    assert agent.session_id in combined
    assert "test-model" in combined


def test_cmd_status_shows_delegation_disabled(agent):
    agent.config.pop("delegation", None)
    printed = []
    with patch("nova.display._cprint", side_effect=lambda s: printed.append(s)):
        dispatch_command("status", agent, "")
    assert any("disabled" in s for s in printed)


def test_cmd_status_shows_delegation_enabled(make_agent, delegation_config):
    a = make_agent(config=delegation_config)
    printed = []
    with patch("nova.display._cprint", side_effect=lambda s: printed.append(s)):
        dispatch_command("status", a, "")
    assert any("enabled" in s for s in printed)


def test_cmd_st_alias_works(agent):
    with patch("nova.display._cprint"):
        result = dispatch_command("st", agent, "")
    assert result is True


# ─── cmd_sessions ─────────────────────────────────────────────────────────────


def test_cmd_sessions_no_sessions(agent):
    agent.session_store.list_sessions = MagicMock(return_value=[])
    printed = []
    with patch("nova.display._cprint", side_effect=lambda s: printed.append(s)):
        dispatch_command("sessions", agent, "")
    assert any("No sessions" in s for s in printed)


def test_cmd_sessions_lists_sessions(agent):
    agent.session_store.list_sessions = MagicMock(
        return_value=[
            {"session_id": "abc-123", "created_at": "2026-01-01"},
        ]
    )
    printed = []
    with patch("nova.display._cprint", side_effect=lambda s: printed.append(s)):
        dispatch_command("sessions", agent, "")
    assert any("abc-123" in s for s in printed)


def test_cmd_sessions_uses_session_id_key(agent):
    agent.session_store.list_sessions = MagicMock(
        return_value=[{"session_id": "real-id", "created_at": "today"}]
    )
    printed = []
    with patch("nova.display._cprint", side_effect=lambda s: printed.append(s)):
        dispatch_command("sessions", agent, "")
    assert any("real-id" in s for s in printed)


def test_cmd_resume_loads_requested_session(agent):
    target = agent.session_store.create_session(model="other-model")
    agent.session_store.add_message(target, "user", "saved question")
    agent.session_store.add_message(target, "assistant", "saved answer")
    with patch("nova.display._cprint"):
        dispatch_command("resume", agent, target)
    assert agent.session_id == target
    assert agent.config["llm"]["model"] == "other-model"
    assert [m["content"] for m in agent.messages] == ["saved question", "saved answer"]


def test_cmd_resume_displays_loaded_history(agent):
    target = agent.session_store.create_session()
    agent.session_store.add_message(target, "user", "saved question")
    agent.session_store.add_message(target, "assistant", "saved answer")
    printed = []
    with patch("nova.display._cprint", side_effect=printed.append):
        dispatch_command("resume", agent, target)
    assert any("saved question" in message for message in printed)
    assert any("saved answer" in message for message in printed)


def test_cmd_resume_accepts_session_listing_row(agent):
    target = agent.session_store.create_session(model="other-model")
    agent.session_store.add_message(target, "user", "saved question")
    with patch("nova.display._cprint"):
        dispatch_command("resume", agent, f"{target} 2026-08-27T02:53:57")
    assert agent.session_id == target
    assert [m["content"] for m in agent.messages] == ["saved question"]


# ─── cmd_model ───────────────────────────────────────────────────────────────


def test_cmd_model_no_args_prints_current_model(agent):
    printed = []
    with patch("nova.display._cprint", side_effect=lambda s: printed.append(s)):
        dispatch_command("model", agent, "")
    assert any("test-model" in s for s in printed)


def test_cmd_model_with_args_switches_model(agent):
    with patch("nova.display._cprint"):
        dispatch_command("model", agent, "openai/gpt-4o")
    assert agent.config["llm"]["model"] == "openai/gpt-4o"


def test_cmd_model_trims_whitespace(agent):
    with patch("nova.display._cprint"):
        dispatch_command("model", agent, "  openai/gpt-4o  ")
    assert agent.config["llm"]["model"] == "openai/gpt-4o"


def test_cmd_skills_rejects_unknown_subcommand(agent):
    printed = []
    with patch("nova.display._cprint", side_effect=lambda s: printed.append(s)):
        dispatch_command("skills", agent, "unknown")
    assert any("Usage: /skills" in s for s in printed)


# ─── cmd_tools ───────────────────────────────────────────────────────────────


def test_cmd_tools_lists_available_tools(agent):
    printed = []
    with patch("nova.display._cprint", side_effect=lambda s: printed.append(s)):
        dispatch_command("tools", agent, "")
    assert any("Available tools" in s for s in printed)


def test_cmd_tools_prints_tool_names(agent):
    from nova.tools.registry import discover_builtin_tools

    discover_builtin_tools()
    printed = []
    with patch("nova.display._cprint", side_effect=lambda s: printed.append(s)):
        dispatch_command("tools", agent, "")
    assert len(printed) > 1  # header + at least one tool entry


# ─── cmd_usage ───────────────────────────────────────────────────────────────


def test_cmd_usage_without_cost_tracker(agent):
    agent.cost_tracker = None
    with patch("nova.display._cprint") as mock_print:
        dispatch_command("usage", agent, "")
    mock_print.assert_called()


def test_cmd_usage_with_cost_tracker(agent):
    mock_tracker = MagicMock()
    mock_tracker.format_summary.return_value = "Total cost: $0.01"
    agent.cost_tracker = mock_tracker
    printed = []
    with patch("nova.display._cprint", side_effect=lambda s: printed.append(s)):
        dispatch_command("usage", agent, "")
    assert any("$0.01" in s for s in printed)


# ─── cmd_undo ────────────────────────────────────────────────────────────────


def test_cmd_undo_removes_last_exchange(agent):
    agent.messages = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "response"},
        {"role": "user", "content": "second"},
        {"role": "assistant", "content": "response2"},
    ]
    with patch("nova.display._cprint"):
        dispatch_command("undo", agent, "")
    assert len(agent.messages) == 2
    assert agent.messages[-1]["content"] == "response"
    assert agent.session_store.get_messages(agent.session_id) == agent.messages


def test_cmd_compact_persists_trimmed_history(agent):
    agent.messages = [{"role": "user", "content": f"msg {i}"} for i in range(10)]
    for message in agent.messages:
        agent.session_store.add_message(agent.session_id, message["role"], message["content"])
    with patch("nova.display._cprint"):
        dispatch_command("compact", agent, "")
    assert agent.session_store.get_messages(agent.session_id) == agent.messages


def test_cmd_undo_does_nothing_when_empty(agent):
    agent.messages = []
    printed = []
    with patch("nova.display._cprint", side_effect=lambda s: printed.append(s)):
        dispatch_command("undo", agent, "")
    assert any("Nothing to undo" in s for s in printed)


def test_cmd_undo_does_nothing_with_one_message(agent):
    agent.messages = [{"role": "user", "content": "only one"}]
    printed = []
    with patch("nova.display._cprint", side_effect=lambda s: printed.append(s)):
        dispatch_command("undo", agent, "")
    assert len(agent.messages) == 1
    assert any("Nothing to undo" in s for s in printed)


# ─── cmd_compact ─────────────────────────────────────────────────────────────


def test_cmd_compact_trims_to_configured_recent_messages(agent):
    agent.messages = [{"role": "user", "content": f"msg {i}"} for i in range(10)]
    with patch("nova.display._cprint"):
        dispatch_command("compact", agent, "")
    assert len(agent.messages) == 6


def test_cmd_compact_leaves_short_history_unchanged(agent):
    agent.messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    with patch("nova.display._cprint"):
        dispatch_command("compact", agent, "")
    assert len(agent.messages) == 2


def test_cmd_compact_does_not_orphan_tool_results(agent):
    from nova.agent import _normalize_message_history

    agent.messages = [{"role": "user", "content": f"msg {i}"} for i in range(8)] + [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "c1", "function": {"name": "terminal", "arguments": "{}"}}],
        },
        {"role": "tool", "content": "out", "tool_call_id": "c1"},
    ]
    with patch("nova.display._cprint"):
        dispatch_command("compact", agent, "")
    assert agent.messages == _normalize_message_history(agent.messages)
    roles = [m["role"] for m in agent.messages]
    assert "tool" not in roles or "assistant" in roles


# ─── cmd_copy ────────────────────────────────────────────────────────────────


def test_cmd_copy_copies_last_assistant_message(agent):
    agent.messages = [
        {"role": "user", "content": "tell me something"},
        {"role": "assistant", "content": "something interesting"},
    ]
    with patch("subprocess.run") as mock_run, patch("nova.display._cprint"):
        dispatch_command("copy", agent, "")
    mock_run.assert_called_once()
    call_kwargs = mock_run.call_args
    assert b"something interesting" in call_kwargs[1]["input"]


def test_cmd_copy_no_assistant_message(agent):
    agent.messages = [{"role": "user", "content": "hello"}]
    printed = []
    with (
        patch("subprocess.run") as mock_run,
        patch("nova.display._cprint", side_effect=lambda s: printed.append(s)),
    ):
        dispatch_command("copy", agent, "")
    mock_run.assert_not_called()
    assert any("No response" in s for s in printed)


def test_cmd_copy_skips_empty_assistant_content(agent):
    agent.messages = [
        {"role": "assistant", "content": None},
        {"role": "assistant", "content": "real content"},
    ]
    with patch("subprocess.run") as mock_run, patch("nova.display._cprint"):
        dispatch_command("copy", agent, "")
    mock_run.assert_called_once()
    assert b"real content" in mock_run.call_args[1]["input"]


# ─── cmd_memory ──────────────────────────────────────────────────────────────


# ─── cmd_skills ──────────────────────────────────────────────────────────────


def test_cmd_skills_lists_available_skills(agent, tmp_path):
    """Test that /skills command lists available skills."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    skill_dir = skills_dir / "test-skill"
    skill_dir.mkdir()
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(
        "---\nname: test-skill\ncategory: general\ndescription: Test skill\n---\n\nContent"
    )

    agent.config["skills"] = {"directory": str(skills_dir)}
    printed = []
    with patch("nova.display._cprint", side_effect=lambda s: printed.append(s)):
        dispatch_command("skills", agent, "")
    combined = "\n".join(printed)
    assert "Available skills" in combined
    assert "test-skill" in combined


def test_cmd_skills_no_skills(agent, tmp_path):
    """Test /skills when no skills exist."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    agent.config["skills"] = {"directory": str(skills_dir)}
    printed = []
    with patch("nova.display._cprint", side_effect=lambda s: printed.append(s)):
        dispatch_command("skills", agent, "")
    assert any("No skills found" in s for s in printed)


# ─── Skill slash commands ─────────────────────────────────────────────────────


def test_dispatch_skill_command_returns_true(agent, tmp_path):
    """Test that a skill name can be dispatched as a command."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    skill_dir = skills_dir / "my-skill"
    skill_dir.mkdir()
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(
        "---\nname: my-skill\ncategory: general\ndescription: My skill\n---\n\nSkill content here"
    )

    agent.config["skills"] = {"directory": str(skills_dir)}
    with patch("nova.display._cprint"):
        result = dispatch_command("my-skill", agent, "")
    assert result is True


def test_dispatch_skill_command_displays_content(agent, tmp_path):
    """Test that skill content is displayed when loaded via slash command."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    skill_dir = skills_dir / "test-skill"
    skill_dir.mkdir()
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(
        "---\nname: test-skill\ncategory: general\ndescription: Test\n---\n\nMy special skill content"
    )

    agent.config["skills"] = {"directory": str(skills_dir)}
    printed = []
    with patch("nova.display._cprint", side_effect=lambda s: printed.append(s)):
        dispatch_command("test-skill", agent, "")
    combined = "\n".join(printed)
    assert "My special skill content" in combined
    assert "test-skill" in combined


def test_dispatch_nonexistent_skill_returns_false(agent, tmp_path):
    """Test that a nonexistent skill returns False."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    agent.config["skills"] = {"directory": str(skills_dir)}
    result = dispatch_command("nonexistent-skill", agent, "")
    assert result is False


def test_dispatch_skill_with_mixed_case_resolves(agent, tmp_path):
    """Test that skill matching is case-insensitive."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    skill_dir = skills_dir / "my-skill"
    skill_dir.mkdir()
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(
        "---\nname: my-skill\ncategory: general\ndescription: My skill\n---\n\nContent"
    )

    agent.config["skills"] = {"directory": str(skills_dir)}
    with patch("nova.display._cprint"):
        result = dispatch_command("My-Skill", agent, "")
    assert result is True


@pytest.mark.parametrize("model, expected", [("test-model", 1000000), ("small", 0)])
def test_model_switch_scopes_context_override(agent, model, expected):
    agent.config["llm"]["context_window"] = 1000000
    with patch("nova.display._cprint"):
        dispatch_command("model", agent, model)
    assert agent.config["llm"]["context_window"] == expected


@pytest.mark.parametrize("model, expected", [("test-model", 1000000), ("small", 0)])
def test_resume_scopes_context_override(agent, model, expected):
    agent.config["llm"]["context_window"] = 1000000
    session_id = agent.session_store.create_session(model=model)
    with patch("nova.display._cprint"):
        dispatch_command("resume", agent, session_id)
    assert agent.config["llm"]["model"] == model
    assert agent.config["llm"]["context_window"] == expected
