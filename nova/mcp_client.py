"""MCP (Model Context Protocol) client — stdio, HTTP, and SSE transports.

Connects to external MCP servers, discovers tools and resources,
and exposes them as Nova-Agent tools.

Supports three transport types:
- **stdio** — Local subprocess (most common for local MCP servers)
- **http** — Streamable HTTP (POST-based JSON-RPC, newer MCP spec)
- **sse** — Server-Sent Events for server→client + HTTP POST for client→server
"""

import copy
import json
import logging
import re
import selectors
import subprocess
import threading
import time
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Union

import httpx

from nova import __version__
from nova.tasks import sanitize_environment

logger = logging.getLogger(__name__)

# JSON-RPC protocol version
MCP_PROTOCOL_VERSION = "2024-11-05"

# Default per-request timeout for stdio transports (seconds). A hung server
# must never be able to wedge the agent loop indefinitely.
_STDIO_REQUEST_TIMEOUT = 60.0

# Max bytes of MCP result content injected into the model context. External
# servers are untrusted: their output is truncated and labeled downstream.
MCP_RESULT_MAX_CHARS = 12000

# Transport type alias
Transport = Union["_StdioTransport", "_HttpTransport", "_SseTransport"]


@dataclass
class McpToolInfo:
    """Metadata for an MCP-discovered tool."""

    server_name: str
    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)


@dataclass
class McpResourceInfo:
    """Metadata for an MCP-discovered resource."""

    server_name: str
    name: str
    uri: str
    description: str = ""


@dataclass
class McpStdioConfig:
    """Configuration for an MCP server via stdio transport."""

    type: str = "stdio"
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    deferred: bool = False
    description: str = ""


@dataclass
class McpHttpConfig:
    """Configuration for an MCP server via Streamable HTTP transport."""

    type: str = "http"
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    timeout: float = 30.0
    deferred: bool = False
    description: str = ""


@dataclass
class McpSseConfig:
    """Configuration for an MCP server via SSE transport."""

    type: str = "sse"
    url: str = ""
    post_url: str = ""  # Defaults to url if not set
    headers: dict[str, str] = field(default_factory=dict)
    timeout: float = 30.0
    deferred: bool = False
    description: str = ""


McpServerConfig = McpStdioConfig | McpHttpConfig | McpSseConfig


class _StdioTransport:
    """Manages a subprocess-based MCP connection."""

    def __init__(self, config: McpStdioConfig) -> None:
        self.config = config
        self._proc: subprocess.Popen | None = None
        self._next_id: int = 0
        self._selector: selectors.BaseSelector | None = None

    def connect(self) -> bool:
        """Start the subprocess and initialize the MCP session."""
        try:
            # Sanitize the environment so MCP subprocesses do not inherit the
            # agent's API keys; the server's own env overrides sit on top.
            env = {**sanitize_environment(), **self.config.env}
            self._proc = subprocess.Popen(
                [self.config.command, *self.config.args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=env,
                text=True,
            )
            return self._initialize()
        except Exception as e:
            logger.error("Stdio transport connect failed: %s", e)
            self.disconnect()
            return False

    def disconnect(self) -> None:
        """Terminate the subprocess."""
        if self._selector is not None:
            with suppress(Exception):
                self._selector.close()
            self._selector = None
        if self._proc:
            try:
                if self._proc.stdin and not self._proc.stdin.closed:
                    self._proc.stdin.close()
                self._proc.terminate()
                self._proc.wait(timeout=3)
            except Exception:
                self._proc.kill()
            self._proc = None

    def send_request(self, method: str, params: dict[str, Any] | None = None) -> dict:
        """Send a JSON-RPC request and read the matching response."""
        if not self._proc or self._proc.poll() is not None:
            raise RuntimeError("Process is not running")
        request_id = self._next_id
        self._next_id += 1
        request = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params or {},
        }
        self._send(request)
        return self._read_response(request_id)

    def send_notification(self, method: str, params: dict[str, Any] | None = None) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        if not self._proc or not self._proc.stdin or self._proc.stdin.closed:
            return
        notification = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
        }
        self._send(notification)

    def _initialize(self) -> bool:
        """Run the MCP initialization handshake."""
        self._next_id = 0
        init_request = {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "nova-agent", "version": __version__},
            },
        }
        self._send(init_request)
        self._read_response(0)
        self.send_notification("notifications/initialized")
        return True

    def _send(self, message: dict) -> None:
        """Write a JSON-RPC message to stdin."""
        if not self._proc or not self._proc.stdin or self._proc.stdin.closed:
            raise RuntimeError("Stdin is closed")
        self._proc.stdin.write(json.dumps(message) + "\n")
        self._proc.stdin.flush()

    def _readline_with_deadline(self, deadline: float) -> str:
        """Read one line from stdout, raising on timeout.

        Uses a selector on the underlying fd so a silent server cannot block
        the agent forever. Falls back to a plain blocking readline when the
        stream has no real fileno (e.g. mocked in tests).
        """
        assert self._proc is not None and self._proc.stdout is not None
        stdout = self._proc.stdout
        try:
            fileno = stdout.fileno()
        except (OSError, ValueError, AttributeError):
            fileno = None

        if fileno is not None and isinstance(fileno, int):
            if self._selector is None:
                self._selector = selectors.DefaultSelector()
                self._selector.register(stdout, selectors.EVENT_READ)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("MCP server response timed out")
            if not self._selector.select(timeout=remaining):
                raise TimeoutError("MCP server response timed out")
        return stdout.readline()

    def _read_response(self, request_id: int) -> dict:
        """Read JSON-RPC messages from stdout until the matching id arrives.

        Skips notifications and unrelated messages, and enforces a wall-clock
        timeout so a hung or chatty server cannot wedge the agent loop.
        """
        if not self._proc or not self._proc.stdout or self._proc.stdout.closed:
            raise RuntimeError("Stdout is closed")
        deadline = time.monotonic() + _STDIO_REQUEST_TIMEOUT
        while True:
            line = self._readline_with_deadline(deadline)
            if not line:
                raise RuntimeError("Server closed connection")
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                # Non-JSON log line on stdout — ignore and keep reading.
                continue
            if not isinstance(message, dict):
                continue
            if "id" not in message:
                # Notification or request from the server — not our reply.
                continue
            if message.get("id") == request_id:
                return message
            # A response to a different request (should not happen with the
            # per-transport lock, but be defensive) — ignore it.


class _HttpTransport:
    """Manages a Streamable HTTP-based MCP connection.

    Uses POST for all requests. The server responds with JSON-RPC
    responses directly. Supports session IDs via response headers.
    """

    def __init__(self, config: McpHttpConfig) -> None:
        self.config = config
        headers = {"Accept": "application/json, text/event-stream"}
        headers.update(config.headers)
        self._client = httpx.Client(
            headers=headers,
            timeout=config.timeout,
        )
        self._next_id: int = 0
        self._session_id: str | None = None

    def connect(self) -> bool:
        """Initialize the MCP session via HTTP."""
        try:
            return self._initialize()
        except Exception as e:
            logger.error("HTTP transport connect failed: %s", e)
            self.disconnect()
            return False

    def disconnect(self) -> None:
        """Close the HTTP client."""
        self._client.close()

    @staticmethod
    def _response_json(response: httpx.Response) -> dict[str, Any]:
        """Decode a JSON or single-event SSE MCP response."""
        content_type = response.headers.get("content-type", "")
        if "text/event-stream" not in content_type:
            return response.json()

        for line in response.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        raise ValueError("MCP SSE response contained no data event")

    def send_request(self, method: str, params: dict[str, Any] | None = None) -> dict:
        """Send a JSON-RPC request via POST and return the response."""
        request_id = self._next_id
        self._next_id += 1
        body = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params or {},
        }
        headers = dict(self._client.headers)
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id

        response = self._client.post(self.config.url, json=body, headers=headers)
        response.raise_for_status()

        # Capture session ID if provided
        session_id = response.headers.get("Mcp-Session-Id")
        if session_id:
            self._session_id = session_id

        return self._response_json(response)

    def send_notification(self, method: str, params: dict[str, Any] | None = None) -> None:
        """Send a JSON-RPC notification via POST (no response expected)."""
        body = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
        }
        headers = dict(self._client.headers)
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id

        self._client.post(self.config.url, json=body, headers=headers)

    def _initialize(self) -> bool:
        """Run the MCP initialization handshake."""
        self._next_id = 0
        init_request = {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "nova-agent", "version": __version__},
            },
        }
        headers = dict(self._client.headers)
        response = self._client.post(self.config.url, json=init_request, headers=headers)
        response.raise_for_status()
        self._response_json(response)

        # Capture session ID
        session_id = response.headers.get("Mcp-Session-Id")
        if session_id:
            self._session_id = session_id

        # Send initialized notification
        self.send_notification("notifications/initialized")
        return True


class _SseTransport:
    """Manages an SSE-based MCP connection.

    Uses Server-Sent Events for server→client messages and HTTP POST
    for client→server messages. The SSE endpoint streams JSON-RPC
    responses; the POST endpoint sends requests.
    """

    def __init__(self, config: McpSseConfig) -> None:
        self.config = config
        self._client = httpx.Client(
            base_url=config.url,
            headers=config.headers,
            timeout=config.timeout,
        )
        self._post_url = config.post_url or config.url
        self._next_id: int = 0
        self._session_id: str | None = None
        self._sse_endpoint: str | None = None

    def connect(self) -> bool:
        """Initialize the MCP session via SSE."""
        try:
            return self._initialize()
        except Exception as e:
            logger.error("SSE transport connect failed: %s", e)
            self.disconnect()
            return False

    def disconnect(self) -> None:
        """Close the HTTP client."""
        self._client.close()

    def send_request(self, method: str, params: dict[str, Any] | None = None) -> dict:
        """Send a JSON-RPC request via POST, read response from SSE stream."""
        request_id = self._next_id
        self._next_id += 1
        body = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params or {},
        }
        headers = dict(self.config.headers)
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id

        # POST the request
        response = self._client.post(self._post_url, json=body, headers=headers)
        response.raise_for_status()

        # For SSE, the response comes back on the SSE stream.
        # We need to read from the SSE endpoint for the matching response.
        return self._read_sse_response(request_id)

    def send_notification(self, method: str, params: dict[str, Any] | None = None) -> None:
        """Send a JSON-RPC notification via POST."""
        body = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
        }
        headers = dict(self.config.headers)
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id

        self._client.post(self._post_url, json=body, headers=headers)

    def _initialize(self) -> bool:
        """Run the MCP initialization handshake via SSE."""
        self._next_id = 0

        # First, establish the SSE connection
        headers = dict(self.config.headers)
        headers["Accept"] = "text/event-stream"
        sse_response = self._client.get(
            "/",
            headers=headers,
            timeout=httpx.Timeout(5.0, connect=5.0),
            follow_redirects=True,
        )
        sse_response.raise_for_status()

        # Capture session ID and endpoint
        self._session_id = sse_response.headers.get("Mcp-Session-Id")
        endpoint = sse_response.headers.get("Endpoint")
        if endpoint:
            self._sse_endpoint = endpoint

        # Send initialize request via POST
        init_request = {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "nova-agent", "version": __version__},
            },
        }
        post_headers = dict(self.config.headers)
        if self._session_id:
            post_headers["Mcp-Session-Id"] = self._session_id

        post_response = self._client.post(
            self._post_url,
            json=init_request,
            headers=post_headers,
        )
        post_response.raise_for_status()

        # Read the initialize response from SSE
        self._read_sse_response(0)

        # Send initialized notification
        self.send_notification("notifications/initialized")
        return True

    def _read_sse_response(self, request_id: int) -> dict:
        """Read SSE events until we find the matching response."""
        # For simplicity, we make a separate GET request to read the SSE stream
        # In production, you'd maintain a persistent SSE connection with a queue
        headers = dict(self.config.headers)
        headers["Accept"] = "text/event-stream"
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id

        with self._client.stream("GET", "/", headers=headers) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if line.startswith("data: "):
                    data = json.loads(line[6:])
                    if data.get("id") == request_id:
                        return data
        raise RuntimeError("SSE response not found for request ID")


class McpClient:
    """Manages connections to MCP servers via stdio, HTTP, or SSE transports.

    Usage:
        client = McpClient()

        # Stdio server (local subprocess)
        client.add_server(McpStdioConfig(
            command="npx",
            args=["-y", "@modelcontextprotocol/server-filesystem", "/path/to/dir"],
        ))

        # HTTP server (Streamable HTTP)
        client.add_server(McpHttpConfig(
            url="https://api.example.com/mcp",
            headers={"Authorization": "Bearer token"},
        ))

        # SSE server
        client.add_server(McpSseConfig(
            url="https://api.example.com/sse",
            headers={"Authorization": "Bearer token"},
        ))

        tools = client.list_tools()
        result = client.call_tool("filesystem", "read_file", {"path": "test.txt"})
    """

    def __init__(self) -> None:
        self._server_configs: dict[str, McpServerConfig] = {}
        self._transports: dict[str, _StdioTransport | _HttpTransport | _SseTransport] = {}
        self._tools: list[McpToolInfo] = []
        self._resources: list[McpResourceInfo] = []
        self._connected: set[str] = set()
        self._call_lock = threading.RLock()
        self._catalog_revision = 0

    def add_server(self, config: McpServerConfig) -> None:
        """Register an MCP server configuration.

        Derives a name from the config so two HTTP/SSE servers don't collide
        on a default name. For unambiguous naming across multiple servers,
        prefer add_server_named().
        """
        if isinstance(config, McpStdioConfig):
            if not config.command:
                name = f"stdio-{len(self._server_configs)}"
            else:
                # Use the command's basename; same command + same args will
                # still collide — use add_server_named() to disambiguate.
                name = config.command.split("/")[-1] or config.command
        elif isinstance(config, McpHttpConfig):
            name = f"http:{config.url}" if config.url else f"http-{len(self._server_configs)}"
        elif isinstance(config, McpSseConfig):
            name = f"sse:{config.url}" if config.url else f"sse-{len(self._server_configs)}"
        else:
            name = f"{config.type}-{len(self._server_configs)}"
        self.add_server_named(name, config)

    @property
    def catalog_revision(self) -> int:
        with self._call_lock:
            return self._catalog_revision

    def add_server_named(self, name: str, config: McpServerConfig) -> None:
        with self._call_lock:
            if name in self._server_configs and self._server_configs[name] != config:
                self.remove_server(name)
            self._server_configs[name] = copy.deepcopy(config)
            logger.info("Registered MCP server: %s (type=%s)", name, config.type)

    def refresh_server(self, name: str) -> bool:
        with self._call_lock:
            config = self._server_configs.get(name)
            if config is None:
                return False
            self._disconnect_server(name)
            return self._connect_server(name, config)

    def remove_server(self, name: str) -> None:
        """Remove an MCP server and disconnect it."""
        with self._call_lock:
            self._server_configs.pop(name, None)
            self._disconnect_server(name)
            self._rebuild_tool_list()

    def connect_server(self, name: str) -> str:
        with self._call_lock:
            config = self._server_configs.get(name)
            if config is None:
                return "unknown"
            if name in self._connected or self._connect_server(name, config):
                return "connected"
            return "unavailable"

    def search_servers(self, query: str, *, limit: int = 5) -> dict[str, Any]:
        if not isinstance(query, str) or not query.strip() or len(query) > 1000:
            raise ValueError("query must contain 1 to 1000 characters")
        if type(limit) is not int or not 1 <= limit <= 5:
            raise ValueError("limit must be between 1 and 5")
        with self._call_lock:
            exact = query.strip() if query.strip() in self._server_configs else None
            terms = set(re.findall(r"[a-z0-9]+", query.casefold()))
            matches = []
            for name, config in self._server_configs.items():
                if exact is not None and name != exact:
                    continue
                words = set(re.findall(r"[a-z0-9]+", f"{name} {config.description}".casefold()))
                score = 10000 if name == exact else len(terms & words)
                if score:
                    matches.append((-score, name))
            return {
                "servers": [
                    {
                        "name": name,
                        "description": self._server_configs[name].description[:200],
                        "connected": name in self._connected,
                    }
                    for _, name in sorted(matches)[:limit]
                ],
                "total_servers": len(matches),
            }

    def connect_all(self, *, include_deferred: bool = True) -> list[str]:
        """Connect to all registered servers.

        Returns:
            List of successfully connected server names.
        """
        with self._call_lock:
            connected = []
            for name, config in self._server_configs.items():
                if name in self._connected or (config.deferred and not include_deferred):
                    continue
                if self._connect_server(name, config):
                    connected.append(name)
            return connected

    def disconnect_all(self) -> None:
        """Disconnect from all servers."""
        with self._call_lock:
            for name in list(self._connected):
                self._disconnect_server(name)
            self._tools.clear()
            self._resources.clear()
            self._catalog_revision += 1

    def list_tools(self) -> list[McpToolInfo]:
        """List all tools from connected servers."""
        with self._call_lock:
            return copy.deepcopy(self._tools)

    def list_resources(self) -> list[McpResourceInfo]:
        """List all resources from connected servers."""
        with self._call_lock:
            return copy.deepcopy(self._resources)

    def call_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> str:
        """Call an MCP tool on a specific server.

        Returns:
            Tool result as a string.
        """
        with self._call_lock:
            transport = self._transports.get(server_name)
            if not transport:
                return f"Error: MCP server '{server_name}' is not connected."

            try:
                with self._call_lock:
                    response = transport.send_request(
                        "tools/call",
                        {
                            "name": tool_name,
                            "arguments": arguments,
                        },
                    )
                return self._format_external(
                    self._extract_tool_result(response),
                    source=f"MCP tool {server_name}/{tool_name}",
                )
            except Exception as e:
                logger.error("MCP tool call failed (%s/%s): %s", server_name, tool_name, e)
                return f"Error: MCP tool call failed: {type(e).__name__}"

    def read_resource(self, server_name: str, uri: str) -> str:
        """Read an MCP resource from a specific server."""
        with self._call_lock:
            transport = self._transports.get(server_name)
            if not transport:
                return f"Error: MCP server '{server_name}' is not connected."

            try:
                with self._call_lock:
                    response = transport.send_request("resources/read", {"uri": uri})
                error = response.get("error")
                if isinstance(error, dict):
                    return (
                        f"Error: MCP resource read failed: {error.get('message', 'unknown error')}"
                    )
                result = response.get("result", {})
                contents = result.get("contents", [])
                if contents:
                    text = contents[0].get("text", contents[0].get("blob", "(binary)"))
                    return self._format_external(text, source=f"MCP resource {server_name} {uri}")
                return "(empty resource)"
            except Exception as e:
                logger.error("MCP resource read failed (%s/%s): %s", server_name, uri, e)
                return f"Error: MCP resource read failed: {type(e).__name__}"

    def is_connected(self, server_name: str) -> bool:
        """Check if a server is connected."""
        with self._call_lock:
            return server_name in self._connected

    @property
    def connected_servers(self) -> frozenset[str]:
        """Return the names of currently connected servers."""
        with self._call_lock:
            return frozenset(self._connected)

    def _list_pages(self, transport: Transport, method: str, key: str) -> list[dict]:
        entries: list[dict] = []
        cursor: str | None = None
        seen: set[str] = set()
        size = 0
        for _ in range(100):
            response = (
                transport.send_request(method, {"cursor": cursor})
                if cursor is not None
                else transport.send_request(method)
            )
            size += len(json.dumps(response).encode("utf-8"))
            if size > 8 * 1024 * 1024:
                raise ValueError("MCP catalog exceeds discovery byte limit")
            if response.get("error"):
                raise ValueError("MCP catalog request failed")
            result = response.get("result", {})
            page = result.get(key, [])
            if not isinstance(page, list) or not all(isinstance(item, dict) for item in page):
                raise ValueError("Malformed MCP catalog page")
            entries.extend(page)
            cursor = result.get("nextCursor")
            if cursor is None:
                return entries
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                raise ValueError("Invalid or repeated MCP catalog cursor")
            seen.add(cursor)
        raise ValueError("MCP catalog exceeds discovery page limit")

    def _connect_server(self, name: str, config: McpServerConfig) -> bool:
        """Connect to a single MCP server using the appropriate transport."""
        try:
            transport: Transport
            if isinstance(config, McpStdioConfig):
                transport = _StdioTransport(config)
            elif isinstance(config, McpHttpConfig):
                transport = _HttpTransport(config)
            elif isinstance(config, McpSseConfig):
                transport = _SseTransport(config)
            else:
                logger.error("Unknown server config type: %s", type(config))
                return False

            if not transport.connect():
                transport.disconnect()
                return False

            self._transports[name] = transport

            discovered_tools: list[McpToolInfo] = []
            discovered_resources: list[McpResourceInfo] = []

            # Discover tools
            for tool in self._list_pages(transport, "tools/list", "tools"):
                discovered_tools.append(
                    McpToolInfo(
                        server_name=name,
                        name=tool["name"],
                        description=tool.get("description", ""),
                        input_schema=tool.get("inputSchema", {}),
                    )
                )

            # Resource discovery is optional in MCP.
            try:
                resources = self._list_pages(transport, "resources/list", "resources")
            except Exception:
                resources = []
                logger.warning("MCP resource discovery unavailable or incomplete for %s", name)
            for resource in resources:
                discovered_resources.append(
                    McpResourceInfo(
                        server_name=name,
                        name=resource.get("name", resource.get("uri", "")),
                        uri=resource.get("uri", ""),
                        description=resource.get("description", ""),
                    )
                )

            self._tools.extend(discovered_tools)
            self._resources.extend(discovered_resources)

            self._connected.add(name)
            self._catalog_revision += 1
            logger.info(
                "Connected to MCP server '%s' (%s): %d tools, %d resources",
                name,
                config.type,
                len(self._tools),
                len(self._resources),
            )
            return True

        except Exception as e:
            logger.error("Failed to connect to MCP server '%s': %s", name, e)
            self._disconnect_server(name)
            return False

    def _disconnect_server(self, name: str) -> None:
        """Disconnect from a single server."""
        transport = self._transports.pop(name, None)
        try:
            if transport:
                transport.disconnect()
        finally:
            self._connected.discard(name)
            self._tools = [tool for tool in self._tools if tool.server_name != name]
            self._resources = [
                resource for resource in self._resources if resource.server_name != name
            ]
            self._catalog_revision += 1

    def _rebuild_tool_list(self) -> None:
        """Rebuild the tool list from connected servers."""
        self._tools = [t for t in self._tools if t.server_name in self._connected]
        self._resources = [r for r in self._resources if r.server_name in self._connected]

    @staticmethod
    def _format_external(text: str, source: str) -> str:
        """Truncate and label untrusted MCP output for safe context injection."""
        if text.startswith("Error:"):
            return text
        from nova.context import label_external_content, truncate_with_head_tail

        truncated = truncate_with_head_tail(text, MCP_RESULT_MAX_CHARS)
        return label_external_content(truncated, source=source)

    @staticmethod
    def _extract_tool_result(response: dict) -> str:
        """Extract text content from a tool call response.

        Surfaces JSON-RPC errors and tool-level ``isError`` results so the
        model never mistakes a failure for empty success.
        """
        error = response.get("error")
        if isinstance(error, dict):
            return f"Error: MCP tool call failed: {error.get('message', 'unknown error')}"

        result = response.get("result", {})
        content = result.get("content", [])
        if not content:
            return "(no output)"

        parts = []
        for item in content:
            if item.get("type") == "text":
                parts.append(item.get("text", ""))
            elif item.get("type") == "image":
                parts.append(f"[image: {item.get('mimeType', 'unknown')}]")
            else:
                parts.append(str(item))

        text = "\n".join(parts) if parts else "(no output)"
        if result.get("isError"):
            return f"Error: {text}"
        return text


def build_mcp_client(config: dict) -> McpClient:
    """Build an MCP client from Nova-Agent config.

    Config format:
        mcp:
          servers:
            filesystem:
              type: stdio
              command: npx
              args: ["-y", "@modelcontextprotocol/server-filesystem", "/path"]
            remote-api:
              type: http
              url: https://api.example.com/mcp
              headers:
                Authorization: "Bearer ${API_TOKEN}"
            sse-server:
              type: sse
              url: https://api.example.com/sse
              headers:
                Authorization: "Bearer ${API_TOKEN}"
    """
    client = McpClient()
    mcp_cfg = config.get("mcp", {})
    servers = mcp_cfg.get("servers", {})

    for name, server_cfg in servers.items():
        if not isinstance(server_cfg, dict):
            continue

        server_type = server_cfg.get("type", "stdio")

        if server_type == "stdio":
            command = server_cfg.get("command", "")
            if not command:
                logger.warning("MCP server '%s' has no command — skipping", name)
                continue
            client.add_server_named(
                name,
                McpStdioConfig(
                    command=command,
                    args=server_cfg.get("args", []),
                    env=server_cfg.get("env", {}),
                    deferred=server_cfg.get("deferred", False),
                    description=server_cfg.get("description", ""),
                ),
            )

        elif server_type == "http":
            url = server_cfg.get("url", "")
            if not url:
                logger.warning("MCP server '%s' has no url — skipping", name)
                continue
            client.add_server_named(
                name,
                McpHttpConfig(
                    url=url,
                    headers=server_cfg.get("headers", {}),
                    timeout=server_cfg.get("timeout", 30.0),
                    deferred=server_cfg.get("deferred", False),
                    description=server_cfg.get("description", ""),
                ),
            )

        elif server_type == "sse":
            url = server_cfg.get("url", "")
            if not url:
                logger.warning("MCP server '%s' has no url — skipping", name)
                continue
            client.add_server_named(
                name,
                McpSseConfig(
                    url=url,
                    post_url=server_cfg.get("post_url", ""),
                    headers=server_cfg.get("headers", {}),
                    timeout=server_cfg.get("timeout", 30.0),
                    deferred=server_cfg.get("deferred", False),
                    description=server_cfg.get("description", ""),
                ),
            )

        else:
            logger.warning("Unknown MCP server type '%s' for server '%s'", server_type, name)

    return client
