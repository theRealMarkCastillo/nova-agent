import json
import sys

import pytest

from nova.agent import NovaAgent
from nova.mcp_client import build_mcp_client


@pytest.fixture
def deferred_config(tmp_path, minimal_config):
    server = tmp_path / "server.py"
    events = tmp_path / "events.jsonl"
    server.write_text("""import json, sys
from pathlib import Path
events = Path(sys.argv[1])
for line in sys.stdin:
    request = json.loads(line)
    with events.open("a") as output:
        output.write(json.dumps(request) + "\\n")
    if "id" not in request:
        continue
    method = request["method"]
    if method == "initialize":
        result = {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "fixture", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "forecast", "description": "Weather forecast", "inputSchema": {"type": "object"}}]}
    elif method == "tools/call":
        result = {"content": [{"type": "text", "text": "Sunny"}]}
    else:
        result = {"resources": []}
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
""")
    minimal_config["tool_catalog"] = {"enabled": True, "schema_max_tokens": 4096}
    minimal_config["mcp"] = {
        "servers": {
            "weather": {
                "command": sys.executable,
                "args": [str(server), str(events)],
                "deferred": True,
                "description": "Weather and climate forecasts",
                "env": {"FIXTURE_SECRET": "not-for-catalog"},
            },
            "offline": {
                "command": str(tmp_path / "missing-executable"),
                "deferred": True,
                "description": "Unavailable fixture",
            },
        }
    }
    return minimal_config, events


def tool_call(name, **arguments):
    return {"id": name, "function": {"name": name, "arguments": json.dumps(arguments)}}


def test_deferred_discovery_connects_only_selected_server_and_preserves_policy(
    deferred_config, mock_openai_client, mock_session_store
):
    config, events = deferred_config
    config["permissions"] = {"mode": "ask"}
    agent = NovaAgent(
        config=config, openai_client=mock_openai_client, session_store=mock_session_store
    )
    try:
        assert not events.exists()
        assert not agent.mcp_client.connected_servers
        result = agent.tool_executor.run(tool_call("discover_tools", query="climate"))
        assert result.status == "completed", result.content
        assert '"name": "weather"' in result.content
        assert "not-for-catalog" not in result.content
        assert not events.exists()
        result = agent.tool_executor.run(
            tool_call("discover_tools", query="forecast", server="weather")
        )
        assert result.status == "completed", result.content
        assert agent.mcp_client.connected_servers == {"weather"}
        assert "mcp__weather__forecast" in agent._system_prompt
        assert agent.tool_executor.run(tool_call("mcp__weather__forecast")).status == "denied"
        agent._confirmation_callback = lambda *_: True
        assert "Sunny" in agent.tool_executor.run(tool_call("mcp__weather__forecast")).content
        agent.tool_executor.run(tool_call("discover_tools", query="forecast", server="weather"))
        requests = [json.loads(line) for line in events.read_text().splitlines()]
        assert sum(r["method"] == "initialize" for r in requests) == 1
        assert sum(r["method"] == "tools/call" for r in requests) == 1
        process = agent.mcp_client._transports["weather"]._proc
    finally:
        agent.close()
    assert process.poll() is not None


def test_deferred_offline_and_invalid_requests_do_not_publish_tools(
    deferred_config, mock_openai_client, mock_session_store
):
    config, events = deferred_config
    agent = NovaAgent(
        config=config, openai_client=mock_openai_client, session_store=mock_session_store
    )
    try:
        for arguments in (
            {"query": "", "server": "weather"},
            {"query": "forecast", "limit": 0, "server": "weather"},
        ):
            assert (
                agent.tool_executor.run(tool_call("discover_tools", **arguments)).status
                != "completed"
            )
        assert not events.exists()
        for server, status in (("offline", "unavailable"), ("unknown", "unknown")):
            result = agent.tool_executor.run(
                tool_call("discover_tools", query="forecast", server=server)
            )
            assert result.status == "completed", result.content
            assert f'"status": "{status}"' in result.content
            assert not agent.mcp_client.connected_servers
            assert not agent.mcp_client.list_tools()
    finally:
        agent.close()


def test_full_catalog_mode_connects_deferred_servers(
    deferred_config, mock_openai_client, mock_session_store
):
    config, events = deferred_config
    config["tool_catalog"]["enabled"] = False
    agent = NovaAgent(
        config=config, openai_client=mock_openai_client, session_store=mock_session_store
    )
    try:
        assert events.exists()
        assert "weather" in agent.mcp_client.connected_servers
    finally:
        agent.close()


def test_deferred_server_search_is_bounded_exact_and_tracks_configuration(deferred_config):
    config, events = deferred_config
    client = build_mcp_client(config)
    from nova.mcp_client import McpStdioConfig

    for i in range(1000):
        client.add_server_named(
            f"server_{i}",
            McpStdioConfig(command="unused", deferred=True, description="shared topic"),
        )
    results = client.search_servers("shared", limit=5)
    assert results["total_servers"] == 1000
    assert len(results["servers"]) == 5
    assert client.search_servers("server_999")["servers"][0]["name"] == "server_999"
    client.add_server_named(
        "server_999", McpStdioConfig(command="new", deferred=True, description="replacement")
    )
    assert client.search_servers("shared")["total_servers"] == 999
    client.remove_server("server_999")
    assert client.search_servers("replacement")["servers"] == []
    assert not events.exists()


@pytest.mark.parametrize("field,value", [("deferred", "true"), ("description", 42)])
def test_deferred_config_rejects_invalid_metadata(deferred_config, field, value):
    import copy

    from nova.config import DEFAULT_CONFIG, ConfigError, _validate_config

    config = copy.deepcopy(DEFAULT_CONFIG)
    config["mcp"] = deferred_config[0]["mcp"]
    config["mcp"]["servers"]["weather"][field] = value
    with pytest.raises(ConfigError, match=field):
        _validate_config(config)


def test_explicit_retry_recovers_after_server_config_replacement(deferred_config):
    from nova.mcp_client import McpStdioConfig

    config, events = deferred_config
    client = build_mcp_client(config)
    assert client.connect_server("offline") == "unavailable"
    assert client.list_tools() == []
    fixture = config["mcp"]["servers"]["weather"]
    client.add_server_named(
        "offline", McpStdioConfig(command=fixture["command"], args=fixture["args"], deferred=True)
    )
    try:
        assert client.connect_server("offline") == "connected"
        assert "Sunny" in client.call_tool("offline", "forecast", {})
        assert events.exists()
        client.remove_server("offline")
        assert client.connect_server("offline") == "unknown"
        assert client.list_tools() == []
    finally:
        client.disconnect_all()


def test_concurrent_first_use_starts_one_process(deferred_config):
    from concurrent.futures import ThreadPoolExecutor

    config, events = deferred_config
    client = build_mcp_client(config)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(client.connect_server, ["weather", "weather"]))
        assert results == ["connected", "connected"]
        requests = [json.loads(line) for line in events.read_text().splitlines()]
        assert sum(r["method"] == "initialize" for r in requests) == 1
        assert len(client.list_tools()) == 1
    finally:
        client.disconnect_all()


def test_deferred_connection_requires_next_model_request_before_dispatch(
    deferred_config, mock_openai_client, mock_session_store
):
    config, events = deferred_config
    agent = NovaAgent(
        config=config,
        openai_client=mock_openai_client,
        session_store=mock_session_store,
        confirmation_callback=lambda *_: True,
    )
    try:
        agent._offered_tool_names = {
            item["function"]["name"] for item in agent._get_tool_definitions()
        }
        results = agent.tool_executor.run_batch(
            [
                tool_call("discover_tools", query="forecast", server="weather"),
                tool_call("mcp__weather__forecast"),
            ]
        )
        assert results[0].status == "completed"
        assert results[1].status == "denied"
        requests = [json.loads(line) for line in events.read_text().splitlines()]
        assert all(r["method"] != "tools/call" for r in requests)
        agent._offered_tool_names = {
            item["function"]["name"] for item in agent._get_tool_definitions()
        }
        assert "Sunny" in agent.tool_executor.run(tool_call("mcp__weather__forecast")).content
    finally:
        agent.close()
