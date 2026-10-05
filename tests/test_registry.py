"""Tests for the tool registry."""

from nova.tools.registry import discover_builtin_tools, registry


def test_discover_builtin_tools():
    discover_builtin_tools()
    assert "terminal" in registry.all_tool_names
    assert "read_file" in registry.all_tool_names
    assert "write_file" in registry.all_tool_names
    assert "wiki" in registry.all_tool_names


def test_get_definitions():
    discover_builtin_tools()
    tools = registry.get_definitions()
    assert len(tools) > 0
    for tool in tools:
        assert "type" in tool
        assert tool["type"] == "function"
        assert "function" in tool
        assert "name" in tool["function"]
        assert "parameters" in tool["function"]


def test_get_tool_summary_list():
    discover_builtin_tools()
    summary = registry.get_tool_summary_list()
    assert "terminal" in summary
    assert "read_file" in summary


def test_tool_summary_grouped_by_toolset():
    discover_builtin_tools()
    summary = registry.get_tool_summary_list()
    # Tools are grouped under per-toolset headers so the model can scan by domain.
    assert "### Files" in summary
    assert "### Git" in summary
    assert "### Sessions" in summary
    # Group headers come before the tools they contain.
    assert summary.index("### Files") < summary.index("read_file:")


def test_dispatch_unknown_tool():
    result = registry.dispatch("nonexistent_tool", {})
    assert "Error" in result
    assert "nonexistent_tool" in result


def test_builtin_read_only_tools_are_declared_at_registration():
    discover_builtin_tools({"delegation": {"enabled": True}})
    builtin = {
        name for name in registry.all_tool_names if not name.startswith(("web_", "mcp__", "test_"))
    }
    read_only = {name for name in builtin if registry.get_tool(name).is_read_only}

    assert read_only == {
        "read_file",
        "list_files",
        "search_files",
        "search_sessions",
        "search_messages",
        "read_session",
        "http_get",
        "skills_list",
        "skill_view",
        "skill_export",
        "task_status",
        "task_list",
        "task_output",
        "git_status",
        "git_log",
        "git_diff",
        "git_blame",
        "git_show",
    }
    assert {"terminal", "write_file", "patch_file", "wiki", "delegate_task"} <= builtin - read_only
