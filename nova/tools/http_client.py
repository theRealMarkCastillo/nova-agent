"""HTTP client tool — make GET, POST, PUT, DELETE requests.

Supports JSON payloads, custom headers, and timeout enforcement.
Integrates with permission system to control external URLs.

Connections are pinned to a pre-resolved, validated IP address so the
destination cannot change between validation and connect time (DNS
rebinding). The original hostname is still used for the Host header and
TLS SNI/certificate validation.
"""

from __future__ import annotations

import contextlib
import gzip
import http.client
import ipaddress
import json
import logging
import socket
import ssl
import urllib.parse
import zlib
from typing import Any

from nova.tools.registry import registry

logger = logging.getLogger(__name__)

HTTP_GET_SCHEMA = {
    "name": "http_get",
    "description": "Make an HTTP GET request and return the response body.",
    "parameters": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The URL to request (must start with http:// or https://).",
            },
            "headers": {
                "type": "object",
                "description": 'Optional custom headers (e.g., {"Authorization": "Bearer token"}).',
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds (default: 30, max: 300).",
                "default": 30,
            },
        },
        "required": ["url"],
    },
}

HTTP_POST_SCHEMA = {
    "name": "http_post",
    "description": "Make an HTTP POST request with optional JSON body and return the response.",
    "parameters": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The URL to request (must start with http:// or https://).",
            },
            "body": {
                "type": "string",
                "description": 'JSON body as a string (e.g., \'{"key": "value"}\'). If empty, sends empty body.',
            },
            "headers": {
                "type": "object",
                "description": "Optional custom headers. Content-Type defaults to application/json if not set.",
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds (default: 30, max: 300).",
                "default": 30,
            },
        },
        "required": ["url"],
    },
}

HTTP_PUT_SCHEMA = {
    "name": "http_put",
    "description": "Make an HTTP PUT request with optional JSON body and return the response.",
    "parameters": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The URL to request (must start with http:// or https://).",
            },
            "body": {
                "type": "string",
                "description": "JSON body as a string. If empty, sends empty body.",
            },
            "headers": {
                "type": "object",
                "description": "Optional custom headers.",
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds (default: 30, max: 300).",
                "default": 30,
            },
        },
        "required": ["url"],
    },
}

HTTP_DELETE_SCHEMA = {
    "name": "http_delete",
    "description": "Make an HTTP DELETE request and return the response.",
    "parameters": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The URL to request (must start with http:// or https://).",
            },
            "headers": {
                "type": "object",
                "description": "Optional custom headers.",
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds (default: 30, max: 300).",
                "default": 30,
            },
        },
        "required": ["url"],
    },
}

_MAX_RESPONSE_CHARS = 10000

# URL schemes we allow
_ALLOWED_SCHEMES = {"http", "https"}

# Hostnames/paths we always block
_BLOCKED_HOSTS = frozenset(
    {
        "localhost",
        "127.0.0.1",
        "::1",
        "0.0.0.0",
        "metadata.google.internal",
        "169.254.169.254",  # AWS metadata
        "169.254.170.2",  # AWS credentials
        "metadata.ec2.internal",
        "kubernetes.default",
        "metadata.azure.com",
    }
)


# RFC 6598 carrier-grade NAT space; ipaddress does not treat it as private.
_SHARED_ADDRESS_SPACE = ipaddress.ip_network("100.64.0.0/10")


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """HTTP connection whose TCP connect targets a pre-validated IP.

    The Host header still uses the original hostname; only the connect
    is pinned, closing the DNS-rebinding window between validation and
    connection.
    """

    def __init__(self, host: str, port: int, ip: str, timeout: int):
        super().__init__(host, port, timeout=timeout)
        self._pin_ip = ip

    def connect(self) -> None:
        self.sock = socket.create_connection((self._pin_ip, self.port), timeout=self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection pinned to a pre-validated IP.

    TLS SNI and certificate validation still use the original hostname,
    so secure connections keep working while the TCP destination is pinned.
    """

    def __init__(self, host: str, port: int, ip: str, timeout: int):
        self._ctx = ssl.create_default_context()
        super().__init__(host, port, timeout=timeout, context=self._ctx)
        self._pin_ip = ip

    def connect(self) -> None:
        self.sock = socket.create_connection((self._pin_ip, self.port), timeout=self.timeout)
        self.sock = self._ctx.wrap_socket(self.sock, server_hostname=self.host)


def _resolve_pinned_ip(host: str) -> tuple[bool, str, str]:
    """Resolve a host once and validate every address it maps to.

    Returns (ok, error_message, pinned_ip). The caller must connect only
    to the returned IP — the connection is pinned to it, so DNS rebinding
    between this validation and connect time cannot redirect the request.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, OSError) as e:
        return False, f"URL denied: unable to resolve host — {e}", ""
    if not infos:
        return False, "URL denied: unable to resolve host — no addresses", ""
    for info in infos:
        addr_str = info[4][0]
        try:
            addr = ipaddress.ip_address(addr_str)
        except ValueError:
            return False, f"URL denied: {addr_str} is not a valid IP", ""
        # Older 3.12 releases do not apply IPv4 rules to IPv4-mapped IPv6.
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
            addr = addr.ipv4_mapped
        if addr in _SHARED_ADDRESS_SPACE:
            return False, f"URL denied: {host} resolves to shared address {addr_str}", ""
        if addr.is_private:
            return False, f"URL denied: {host} resolves to private address {addr_str}", ""
        if addr.is_loopback:
            return False, f"URL denied: {host} resolves to loopback address {addr_str}", ""
        if addr.is_link_local:
            return False, f"URL denied: {host} resolves to link-local address {addr_str}", ""
        if addr.is_reserved:
            return False, f"URL denied: {host} resolves to reserved address {addr_str}", ""
    # Prefer IPv4 for the pinned address: deterministic, and avoids
    # IPv6-routing surprises on hosts with both A and AAAA records.
    ipv4 = [i for i in infos if i[0] == socket.AF_INET]
    pinned = (ipv4 or infos)[0][4][0]
    if not isinstance(pinned, str):
        return False, "URL denied: invalid address", ""
    return True, "", pinned


_EMPTY_PARSE = urllib.parse.ParseResult("", "", "", "", "", "")


def _prepare_url(url: str) -> tuple[bool, str, urllib.parse.ParseResult, str]:
    """Validate, parse, and SSRF-check a URL.

    Returns (ok, error, parsed_url, pinned_ip). The pinned IP is the only
    address the request may connect to.
    """
    valid, msg = _validate_url(url)
    if not valid:
        return False, msg, _EMPTY_PARSE, ""
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError as e:
        return False, f"Invalid URL: {e}", _EMPTY_PARSE, ""
    host = (parsed.hostname or "").lower()
    if not host:
        return False, "URL denied: missing host", _EMPTY_PARSE, ""
    if host in _BLOCKED_HOSTS:
        return False, f"URL denied: {host} is a reserved address", _EMPTY_PARSE, ""
    ok, msg, ip = _resolve_pinned_ip(host)
    if not ok:
        return False, msg, _EMPTY_PARSE, ""
    return True, "", parsed, ip


def _is_url_safe(url: str) -> tuple[bool, str]:
    """Validate URL format, scheme, and target safety.

    Blocks:
    - Non http/https schemes
    - IPv4 private ranges (10.x, 172.16-31.x, 192.168.x)
    - Link-local (169.254.x)
    - Unspecified (0.0.0.0)
    - IPv6 loopback/unspecified
    - Well-known SSRF hosts (AWS/Azure/GCP metadata endpoints)

    Every address a host maps to is inspected — a single gethostbyname()
    call would see only the first A record. The request is additionally
    pinned to the validated address (see _PinnedHTTPConnection), so the
    destination cannot change between this check and connect time.
    """
    ok, msg, _, _ = _prepare_url(url)
    return ok, msg


def _validate_url(url: str) -> tuple[bool, str]:
    """Validate URL format and scheme."""
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        return False, "URL must start with http:// or https://"
    return True, ""


def _coerce_timeout(raw: Any) -> int:
    """Coerce a timeout arg to an int; 0 signals invalid (rejected by validation)."""
    if isinstance(raw, bool):
        return 0
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 0
    return value


def _validate_timeout(timeout: int) -> tuple[bool, str]:
    """Validate timeout value."""
    if not isinstance(timeout, int) or timeout <= 0 or timeout > 300:
        return False, "Timeout must be between 1 and 300 seconds"
    return True, ""


def _decode_body(data: bytes, content_encoding: str) -> str:
    """Decode a response body, transparently decompressing gzip/deflate."""
    if content_encoding.lower() == "gzip":
        data = gzip.decompress(data)
    elif content_encoding.lower() == "deflate":
        data = zlib.decompress(data)
    return data.decode("utf-8", errors="replace")


def _truncate_response(text: str, max_chars: int = _MAX_RESPONSE_CHARS) -> str:
    """Truncate response to fit within budget."""
    if len(text) <= max_chars:
        return text
    head = int(max_chars * 0.7)
    tail = int(max_chars * 0.2)
    return f"{text[:head]}\n\n[...{len(text) - head - tail:,} chars truncated...]\n\n{text[-tail:]}"


def _make_request(
    method: str,
    url: str,
    body: str | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 30,
) -> str:
    """Make an HTTP request and return formatted response."""
    # Validate URL + SSRF safety check (also yields the pinned IP)
    ok, msg, parsed, ip = _prepare_url(url)
    if not ok:
        return f"Error: {msg}"

    # Validate timeout
    valid, msg = _validate_timeout(timeout)
    if not valid:
        return f"Error: {msg}"

    # Parse headers
    if headers is None:
        headers = {}
    elif not isinstance(headers, dict):
        return "Error: Headers must be a JSON object"
    headers = dict(headers)

    # Default User-Agent
    if "User-Agent" not in headers:
        headers["User-Agent"] = "Nova-Agent/1.0"

    # Parse body for POST/PUT
    json_body = None
    if body:
        if not isinstance(body, str):
            return "Error: Body must be a JSON string"
        try:
            json_body = json.loads(body)
        except json.JSONDecodeError:
            return f"Error: Invalid JSON in body: {body}"

    # Add Content-Type header if not set and we have a body
    if json_body is not None and "Content-Type" not in headers:
        headers["Content-Type"] = "application/json"

    scheme = parsed.scheme.lower()
    host = parsed.hostname or ""
    try:
        port = parsed.port or (443 if scheme == "https" else 80)
    except ValueError:
        return "Error: URL has an invalid port"
    path = parsed.path or "/"
    if parsed.query:
        path += f"?{parsed.query}"

    request_body = json.dumps(json_body).encode("utf-8") if json_body is not None else None

    logger.info("HTTP %s to %s (timeout=%ds)", method, url[:100], timeout)

    conn_cls = _PinnedHTTPSConnection if scheme == "https" else _PinnedHTTPConnection
    conn = conn_cls(host, port, ip, timeout)
    try:
        conn.request(method, path, body=request_body, headers=headers)
        resp = conn.getresponse()

        logger.info("HTTP %s response: %d", method, resp.status)

        body_text = _decode_body(resp.read(), resp.getheader("Content-Encoding", ""))

        # Collect response
        status_line = f"Status: {resp.status} {resp.reason}"
        headers_str = "\nHeaders:"
        for key, val in resp.getheaders():
            headers_str += f"\n  {key}: {val}"

        # Try to parse as JSON; fallback to raw text
        # Try to parse as JSON; fallback to raw text
        with contextlib.suppress(json.JSONDecodeError, ValueError):
            body_text = json.dumps(json.loads(body_text), indent=2)
        body_text = _truncate_response(body_text)

        body_text = _truncate_response(body_text)

        return f"{status_line}{headers_str}\n\nBody:\n{body_text}"

    except TimeoutError:
        return f"Error: Request timed out after {timeout}s"
    except ssl.SSLError as e:
        return f"Error: TLS connection failed: {e}"
    except (OSError, http.client.HTTPException) as e:
        return f"Error: Connection failed: {e}"
    except Exception as e:
        logger.error("HTTP request unexpected error: %s", e)
        return f"Error: HTTP request failed: {e}"
    finally:
        conn.close()


def _http_get(args: dict[str, Any], **kwargs: Any) -> str:
    """Handler for http_get."""
    url = args.get("url", "")
    headers = args.get("headers", {})
    timeout = _coerce_timeout(args.get("timeout", 30))
    return _make_request("GET", url, headers=headers, timeout=timeout)


def _http_post(args: dict[str, Any], **kwargs: Any) -> str:
    """Handler for http_post."""
    url = args.get("url", "")
    body = args.get("body", "")
    headers = args.get("headers", {})
    timeout = _coerce_timeout(args.get("timeout", 30))
    return _make_request("POST", url, body=body, headers=headers, timeout=timeout)


def _http_put(args: dict[str, Any], **kwargs: Any) -> str:
    """Handler for http_put."""
    url = args.get("url", "")
    body = args.get("body", "")
    headers = args.get("headers", {})
    timeout = _coerce_timeout(args.get("timeout", 30))
    return _make_request("PUT", url, body=body, headers=headers, timeout=timeout)


def _http_delete(args: dict[str, Any], **kwargs: Any) -> str:
    """Handler for http_delete."""
    url = args.get("url", "")
    headers = args.get("headers", {})
    timeout = _coerce_timeout(args.get("timeout", 30))
    return _make_request("DELETE", url, headers=headers, timeout=timeout)


registry.register(
    name="http_get",
    toolset="http",
    schema=HTTP_GET_SCHEMA,
    handler=_http_get,
    emoji="📡",
)

registry.register(
    name="http_post",
    toolset="http",
    schema=HTTP_POST_SCHEMA,
    handler=_http_post,
    emoji="📡",
)

registry.register(
    name="http_put",
    toolset="http",
    schema=HTTP_PUT_SCHEMA,
    handler=_http_put,
    emoji="📡",
)

registry.register(
    name="http_delete",
    toolset="http",
    schema=HTTP_DELETE_SCHEMA,
    handler=_http_delete,
    emoji="📡",
)
