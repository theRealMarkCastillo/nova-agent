import pytest

from nova.tokens import estimate_total_request_tokens
from nova.tool_catalog import ToolCatalog


def definitions(count):
    return [
        {
            "type": "function",
            "function": {
                "name": f"tool_{i}",
                "description": f"Capability number {i}",
                "parameters": {"type": "object", "properties": {"value": {"type": "string"}}},
            },
        }
        for i in range(count)
    ]


def test_large_catalog_bounds_schemas_and_finds_tail():
    catalog = ToolCatalog(definitions(1000), max_tokens=800, core_names=[])
    assert estimate_total_request_tokens([], tools=catalog.definitions()) <= 800
    result = catalog.discover("tool_999", limit=1)
    assert result["matches"][0]["name"] == "tool_999"
    assert catalog.is_active("tool_999")
    assert estimate_total_request_tokens([], tools=catalog.definitions()) <= 800


def test_small_catalog_keeps_all_tools():
    catalog = ToolCatalog(definitions(3), max_tokens=1000, core_names=[])
    assert all(catalog.is_active(f"tool_{i}") for i in range(3))


def test_removed_tools_cannot_remain_active():
    catalog = ToolCatalog(definitions(100), max_tokens=800, core_names=[])
    catalog.discover("tool_99", limit=1)
    catalog.update(definitions(3))
    assert not catalog.is_active("tool_99")


def test_unfit_schema_is_never_truncated():
    tools = definitions(1)
    tools[0]["function"]["parameters"]["description"] = "massive " * 5000
    catalog = ToolCatalog(tools, max_tokens=800, core_names=[])
    result = catalog.discover("tool_0", limit=1)
    assert result["matches"][0]["active"] is False
    assert result["matches"][0]["reason"] == "schema_budget"


def test_reserved_tools_must_fit():
    with pytest.raises(ValueError, match="budget"):
        ToolCatalog(definitions(1), max_tokens=1, core_names=["tool_0"])


def test_agent_discovery_activation_and_dispatch_policy(
    minimal_config, mock_session_store, mock_openai_client
):
    import json
    from unittest.mock import MagicMock

    from nova.agent import NovaAgent
    from nova.mcp_client import McpClient, McpToolInfo

    minimal_config["tool_catalog"] = {"enabled": True, "schema_max_tokens": 4096}
    minimal_config["permissions"] = {"mode": "ask"}
    mcp = McpClient()
    mcp._tools = [
        McpToolInfo(server_name="fixture", name=f"tool_{i}", description=f"Capability {i}")
        for i in range(1000)
    ]
    mcp.call_tool = MagicMock(return_value="worked")
    agent = NovaAgent(
        config=minimal_config,
        openai_client=mock_openai_client,
        session_store=mock_session_store,
        mcp_client=mcp,
    )

    def call(name, arguments=None):
        return {"id": "call", "function": {"name": name, "arguments": json.dumps(arguments or {})}}

    try:
        target = "mcp__fixture__tool_999"
        assert target not in agent._system_prompt
        assert agent.tool_executor.run(call(target)).status == "denied"
        discovery = agent.tool_executor.run(call("discover_tools", {"query": target, "limit": 1}))
        assert discovery.status == "completed", discovery.content
        assert target in agent._system_prompt
        assert target in {item["function"]["name"] for item in agent._get_tool_definitions()}
        # Activation is not approval: MCP still requires confirmation.
        assert agent.tool_executor.run(call(target)).status == "denied"
        mcp.call_tool.assert_not_called()
        agent._confirmation_callback = lambda *_: True
        assert agent.tool_executor.run(call(target)).status == "completed"
        mcp.call_tool.assert_called_once()
    finally:
        agent.close()


def test_agent_next_request_gets_activated_schema(
    minimal_config, mock_session_store, mock_openai_client
):
    import json
    from unittest.mock import MagicMock

    from nova.agent import NovaAgent
    from nova.mcp_client import McpClient, McpToolInfo

    minimal_config["tool_catalog"] = {"enabled": True, "schema_max_tokens": 4096}
    mcp = McpClient()
    mcp._tools = [McpToolInfo(server_name="fixture", name=f"tool_{i}") for i in range(1000)]
    target = "mcp__fixture__tool_999"
    agent = NovaAgent(
        config=minimal_config,
        openai_client=mock_openai_client,
        session_store=mock_session_store,
        mcp_client=mcp,
    )
    calls = []

    def response(messages, *, tools, **kwargs):
        names = {item["function"]["name"] for item in tools}
        calls.append(names)
        if len(calls) == 1:
            assert target not in names
            return {
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "search",
                                    "function": {
                                        "name": "discover_tools",
                                        "arguments": json.dumps({"query": target, "limit": 1}),
                                    },
                                }
                            ]
                        }
                    }
                ]
            }
        assert target in names
        assert target in messages[0]["content"]
        return {"choices": [{"message": {"content": "done"}}]}

    agent._call_llm = MagicMock(side_effect=response)
    try:
        assert agent.run("Use the last tool", stream=False) == "done"
        assert len(calls) == 2
    finally:
        agent.close()


def test_discovery_does_not_allow_unadvertised_same_batch_call(
    minimal_config, mock_session_store, mock_openai_client
):
    import json
    from unittest.mock import MagicMock

    from nova.agent import NovaAgent
    from nova.mcp_client import McpClient, McpToolInfo

    minimal_config["tool_catalog"] = {"enabled": True, "schema_max_tokens": 4096}
    mcp = McpClient()
    mcp._tools = [McpToolInfo(server_name="fixture", name=f"tool_{i}") for i in range(1000)]
    mcp.call_tool = MagicMock(return_value="worked")
    agent = NovaAgent(
        config=minimal_config,
        openai_client=mock_openai_client,
        session_store=mock_session_store,
        mcp_client=mcp,
        confirmation_callback=lambda *_: True,
    )
    target = "mcp__fixture__tool_999"
    agent._offered_tool_names = {item["function"]["name"] for item in agent._get_tool_definitions()}

    def call(name, args):
        return {"id": name, "function": {"name": name, "arguments": json.dumps(args)}}

    try:
        results = agent.tool_executor.run_batch(
            [call("discover_tools", {"query": target, "limit": 1}), call(target, {})]
        )
        assert results[0].status == "completed"
        assert results[1].status == "denied"
        mcp.call_tool.assert_not_called()
    finally:
        agent.close()


def test_discovery_eviction_preserves_previously_offered_batch_call(
    minimal_config, mock_session_store, mock_openai_client
):
    import json
    from unittest.mock import MagicMock

    from nova.agent import NovaAgent
    from nova.mcp_client import McpClient, McpToolInfo
    from nova.tool_catalog import DEFAULT_CORE

    minimal_config["tool_catalog"] = {"enabled": True, "schema_max_tokens": 4096}
    mcp = McpClient()
    mcp._tools = [McpToolInfo(server_name="fixture", name=name) for name in ("old", "new")]
    mcp.call_tool = MagicMock(return_value="worked")
    agent = NovaAgent(
        config=minimal_config,
        openai_client=mock_openai_client,
        session_store=mock_session_store,
        mcp_client=mcp,
        confirmation_callback=lambda *_: True,
    )
    old, new = "mcp__fixture__old", "mcp__fixture__new"
    # Replace the budget with exactly core + one extra tool.
    all_defs = agent._all_tool_definitions()
    core = [d for d in all_defs if d["function"]["name"] in DEFAULT_CORE]
    from nova.tool_catalog import DISCOVERY_SCHEMA

    old_def = next(d for d in all_defs if d["function"]["name"] == old)
    budget = estimate_total_request_tokens([], tools=[*core, DISCOVERY_SCHEMA, old_def])
    agent._tool_catalog = ToolCatalog(all_defs, max_tokens=budget, core_names=list(DEFAULT_CORE))
    agent._tool_catalog.discover(old, limit=1)
    agent._offered_tool_names = {d["function"]["name"] for d in agent._tool_catalog.definitions()}

    def call(name, args):
        return {"id": name, "function": {"name": name, "arguments": json.dumps(args)}}

    try:
        results = agent.tool_executor.run_batch(
            [call("discover_tools", {"query": new, "limit": 1}), call(old, {})]
        )
        assert not agent._tool_catalog.is_active(old)
        assert results[0].status == "completed"
        assert results[1].status == "completed", results[1].content
        mcp.call_tool.assert_called_once_with("fixture", "old", {})
    finally:
        agent.close()
