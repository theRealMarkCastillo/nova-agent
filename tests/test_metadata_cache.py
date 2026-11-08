import os
from unittest.mock import patch

import pytest

from nova.skills import discover_skills


def write_skill(root, name, description):
    directory = root / name
    directory.mkdir(exist_ok=True)
    path = directory / "SKILL.md"
    path.write_text(f"---\nname: {name}\ndescription: {description}\n---\nBody")
    return path


def test_skill_cache_avoids_reparse_and_isolates_returned_metadata(tmp_path):
    import nova.skills as module

    write_skill(tmp_path, "one", "before")
    with patch("nova.skills.parse_frontmatter", wraps=module.parse_frontmatter) as parse:
        first = discover_skills(tmp_path)
        first[0]["description"] = "caller mutation"
        second = discover_skills(tmp_path)
        assert second[0]["description"] == "before"
        assert parse.call_count == 1


def test_skill_cache_invalidates_edits_even_with_restored_mtime(tmp_path):
    path = write_skill(tmp_path, "one", "before")
    original = path.stat()
    assert discover_skills(tmp_path)[0]["description"] == "before"
    path.write_text(path.read_text().replace("before", "after!"))
    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
    assert discover_skills(tmp_path)[0]["description"] == "after!"


def test_skill_cache_tracks_rename_delete_and_replacement(tmp_path):
    path = write_skill(tmp_path, "one", "before")
    assert len(discover_skills(tmp_path)) == 1
    path.parent.rename(tmp_path / "renamed")
    assert "/renamed/" in discover_skills(tmp_path)[0]["path"]
    (tmp_path / "renamed" / "SKILL.md").unlink()
    assert discover_skills(tmp_path) == []
    write_skill(tmp_path, "renamed", "replacement")
    assert discover_skills(tmp_path)[0]["description"] == "replacement"


def test_mcp_revision_invalidates_agent_cache_and_config(
    minimal_config, mock_session_store, mock_openai_client
):
    from unittest.mock import MagicMock

    from nova.agent import NovaAgent
    from nova.mcp_client import McpClient, McpStdioConfig

    transport = MagicMock()
    current = {"name": "old", "description": "old description", "inputSchema": {"type": "object"}}

    def respond(method, params=None):
        return (
            {"result": {"tools": [dict(current)]}}
            if method == "tools/list"
            else {"result": {"resources": []}}
        )

    transport.send_request.side_effect = respond
    client = McpClient()
    client.add_server_named("fixture", McpStdioConfig(command="fixture"))
    with patch("nova.mcp_client._StdioTransport", return_value=transport):
        assert client.connect_all() == ["fixture"]
        with patch.object(client, "list_tools", wraps=client.list_tools) as listing:
            agent = NovaAgent(
                config=minimal_config,
                openai_client=mock_openai_client,
                session_store=mock_session_store,
                mcp_client=client,
            )
            first_count = listing.call_count
            agent._refresh_mcp_tools()
            agent._refresh_mcp_tools()
            agent._get_tool_definitions()
            agent._get_tool_definitions()
            assert listing.call_count == first_count
        try:
            current["name"] = "new"
            assert client.refresh_server("fixture")
            names = {d["function"]["name"] for d in agent._get_tool_definitions()}
            assert "mcp__fixture__new" in names
            assert "mcp__fixture__old" not in names
            client.add_server_named("fixture", McpStdioConfig(command="replacement"))
            assert agent._mcp_tool_info("mcp__fixture__new") is None
            assert client.connect_all() == ["fixture"]
            assert agent._mcp_tool_info("mcp__fixture__new") is not None
            client.remove_server("fixture")
            assert agent._mcp_tool_info("mcp__fixture__new") is None
        finally:
            agent.close()
            client.disconnect_all()


def test_tool_search_index_invalidates_on_metadata_change():
    from nova.tool_catalog import ToolCatalog

    definition = {
        "type": "function",
        "function": {"name": "lookup", "description": "oldtopic", "parameters": {"type": "object"}},
    }
    catalog = ToolCatalog([definition], max_tokens=1000, core_names=[])
    assert catalog.discover("oldtopic")["matches"]
    definition["function"]["description"] = "newtopic"
    catalog.update([definition])
    assert not catalog.discover("oldtopic")["matches"]
    assert catalog.discover("newtopic")["matches"]


def test_mcp_metadata_snapshots_cannot_mutate_cache():
    from nova.mcp_client import McpClient, McpToolInfo

    client = McpClient()
    client._tools = [McpToolInfo("server", "tool", input_schema={"type": "object"})]
    snapshot = client.list_tools()
    snapshot[0].input_schema["type"] = "string"
    assert client.list_tools()[0].input_schema["type"] == "object"


def test_real_mcp_refresh_invalidates_schema_and_kills_old_process(
    tmp_path, minimal_config, mock_session_store, mock_openai_client
):
    import json
    import sys

    from nova.agent import NovaAgent
    from nova.mcp_client import McpClient, McpStdioConfig

    data = tmp_path / "tools.json"
    data.write_text(json.dumps([{"name": "old", "inputSchema": {"type": "object"}}]))
    server = tmp_path / "server.py"
    server.write_text("""import json, sys
from pathlib import Path
for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    method = request["method"]
    if method == "initialize":
        result = {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "fixture", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": json.loads(Path(sys.argv[1]).read_text())}
    else:
        result = {"resources": []}
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
""")
    client = McpClient()
    client.add_server_named(
        "fixture", McpStdioConfig(command=sys.executable, args=[str(server), str(data)])
    )
    agent = NovaAgent(
        config=minimal_config,
        openai_client=mock_openai_client,
        session_store=mock_session_store,
        mcp_client=client,
    )
    old_process = client._transports["fixture"]._proc
    try:
        assert agent._mcp_tool_info("mcp__fixture__old") is not None
        data.write_text(
            json.dumps(
                [
                    {
                        "name": "new",
                        "description": "Updated",
                        "inputSchema": {"type": "object", "required": ["value"]},
                    }
                ]
            )
        )
        assert client.refresh_server("fixture")
        assert old_process.poll() is not None
        definitions = {d["function"]["name"]: d["function"] for d in agent._get_tool_definitions()}
        assert "mcp__fixture__old" not in definitions
        assert definitions["mcp__fixture__new"]["parameters"]["required"] == ["value"]
        client.remove_server("fixture")
        assert agent._mcp_tool_info("mcp__fixture__new") is None
    finally:
        agent.close()
        client.disconnect_all()


def test_mcp_refresh_waits_for_inflight_call():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from unittest.mock import MagicMock

    from nova.mcp_client import McpClient, McpStdioConfig

    started, release, refresh_entered = Event(), Event(), Event()
    old = MagicMock()
    new = MagicMock()

    def old_request(method, params=None):
        started.set()
        assert release.wait(3)
        return {"result": {"content": [{"type": "text", "text": "done"}]}}

    old.send_request.side_effect = old_request
    new.send_request.return_value = {"result": {"tools": [], "resources": []}}
    client = McpClient()
    client.add_server_named("server", McpStdioConfig(command="fixture"))
    client._transports["server"] = old
    client._connected.add("server")

    def refresh():
        refresh_entered.set()
        return client.refresh_server("server")

    with (
        patch("nova.mcp_client._StdioTransport", return_value=new),
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        active = pool.submit(client.call_tool, "server", "tool", {})
        assert started.wait(3)
        pending = pool.submit(refresh)
        assert refresh_entered.wait(3)
        try:
            # Waiting on the lifecycle operation must not close the active transport.
            from concurrent.futures import TimeoutError

            with pytest.raises(TimeoutError):
                pending.result(timeout=0.1)
            old.disconnect.assert_not_called()
        finally:
            release.set()
        assert "done" in active.result(timeout=3)
        assert pending.result(timeout=3)
    old.disconnect.assert_called_once()
    client.disconnect_all()
