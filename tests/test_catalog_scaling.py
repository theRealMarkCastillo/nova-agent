from unittest.mock import MagicMock, patch

from nova.mcp_client import McpClient, McpStdioConfig
from nova.skills import build_skills_prompt
from nova.tools.skills_tool import _skills_list


def test_mcp_discovers_tools_after_first_page():
    transport = MagicMock()
    transport.connect.return_value = True

    def respond(method, params=None):
        if method == "tools/list":
            if params and params.get("cursor") == "page-2":
                return {"result": {"tools": [{"name": "last_tool"}]}}
            return {"result": {"tools": [{"name": "first_tool"}], "nextCursor": "page-2"}}
        return {"result": {"resources": []}}

    transport.send_request.side_effect = respond
    client = McpClient()
    client.add_server_named("catalog", McpStdioConfig(command="fixture"))
    with patch("nova.mcp_client._StdioTransport", return_value=transport):
        try:
            assert client.connect_all() == ["catalog"]
            assert {tool.name for tool in client.list_tools()} == {"first_tool", "last_tool"}
        finally:
            client.disconnect_all()


def test_skills_list_exposes_overflow_or_continuation(tmp_path):
    for index in range(51):
        name = f"skill-{index:03}"
        directory = tmp_path / name
        directory.mkdir()
        (directory / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Test skill\n---\nBody", encoding="utf-8"
        )
    output = _skills_list({}, config={"skills": {"directory": str(tmp_path)}})
    assert "skill-050" in output or "next_cursor" in output


def test_skill_index_respects_budget_including_marker():
    skills = [{"name": "example", "category": "general", "description": "x" * 2000}]
    assert len(build_skills_prompt(skills, max_chars=500)) <= 500


def test_skill_pages_reach_last_entry_and_search(tmp_path):
    import json

    for index in range(60):
        directory = tmp_path / f"skill-{index:03}"
        directory.mkdir()
        (directory / "SKILL.md").write_text(
            f"---\nname: skill-{index:03}\ndescription: topic {index}\n---\nBody"
        )
    config = {"skills": {"directory": str(tmp_path)}}
    cursor = 0
    pages = []
    while cursor is not None:
        result = json.loads(_skills_list({"cursor": cursor}, config=config))
        pages.append(json.dumps(result["skills"]))
        cursor = result["next_cursor"]
    assert len(pages) == 3
    assert "skill-059" in pages[-1]
    result = json.loads(_skills_list({"query": "skill-059"}, config=config))
    assert result["total"] == 1
    assert "skill-059" in json.dumps(result["skills"])


def test_mcp_cursor_loop_discards_partial_catalog():
    transport = MagicMock()
    transport.send_request.return_value = {
        "result": {"tools": [{"name": "first"}], "nextCursor": "same"}
    }
    client = McpClient()
    client.add_server_named("catalog", McpStdioConfig(command="fixture"))
    with patch("nova.mcp_client._StdioTransport", return_value=transport):
        assert client.connect_all() == []
    assert client.list_tools() == []
    assert transport.send_request.call_count == 2
    transport.disconnect.assert_called_once()


def test_skill_budget_preserves_complete_xml():
    import xml.etree.ElementTree as ET

    skills = [
        {"name": f'skill-{i}"', "category": "a&b", "description": "x < y " * 20} for i in range(100)
    ]
    for budget in (0, 10, 100, 500, 2000):
        output = build_skills_prompt(skills, max_chars=budget)
        assert len(output) <= budget
        if output:
            ET.fromstring(output)


def test_long_skill_metadata_does_not_skip_page_entries(tmp_path):
    import json

    for i in range(50):
        name = f"skill-{i:03}-" + "x" * 150
        folder = tmp_path / name
        folder.mkdir()
        (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {'y' * 200}\n---\nBody")
    result = json.loads(
        _skills_list({"limit": 50}, config={"skills": {"directory": str(tmp_path)}})
    )
    assert len(result["skills"]) == 50


def test_real_stdio_catalog_pagination_and_cleanup(tmp_path):
    import sys

    server = tmp_path / "server.py"
    server.write_text("""import json, sys
for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    method = request["method"]
    if method == "initialize":
        result = {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "fixture", "version": "1"}}
    elif method in ("tools/list", "resources/list"):
        second = request.get("params", {}).get("cursor") == "next"
        key = "tools" if method == "tools/list" else "resources"
        name = "last" if second else "first"
        result = {key: [{"name": name, "uri": "fixture://" + name}]}
        if not second:
            result["nextCursor"] = "next"
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
""")
    client = McpClient()
    client.add_server_named("fixture", McpStdioConfig(command=sys.executable, args=[str(server)]))
    process = None
    try:
        assert client.connect_all() == ["fixture"]
        process = client._transports["fixture"]._proc
        assert {tool.name for tool in client.list_tools()} == {"first", "last"}
        assert {resource.name for resource in client.list_resources()} == {"first", "last"}
    finally:
        client.disconnect_all()
    assert process is not None and process.poll() is not None
