"""Tests for http_client tool."""

import json
import socket
from unittest.mock import patch

import pytest

from nova.tools.http_client import _http_delete, _http_get, _http_post, _http_put, _is_url_safe


class FakeResponse:
    """Minimal http.client.HTTPResponse stand-in."""

    def __init__(self, status=200, reason="OK", headers=(), body=b"{}"):
        self.status = status
        self.reason = reason
        self.headers = list(headers)
        self.body = body

    def getheaders(self):
        return list(self.headers)

    def getheader(self, name, default=None):
        for key, val in self.headers:
            if key.lower() == name.lower():
                return val
        return default

    def read(self):
        return self.body


class FakeConnection:
    """Minimal _PinnedHTTP(S)Connection stand-in."""

    def __init__(self):
        self.status = 200
        self.reason = "OK"
        self.headers = []
        self.body = b"{}"
        self.request_args = None
        self.exc = None

    def request(self, method, path, body=None, headers=None):
        self.request_args = (method, path, body, headers)
        if self.exc:
            raise self.exc

    def getresponse(self):
        return FakeResponse(self.status, self.reason, self.headers, self.body)

    def close(self):
        pass


@pytest.fixture
def mock_conn():
    """Fixture that fakes the pinned connection layer.

    Pins DNS to a fake public IP so tests run offline, and swaps both
    connection classes for a single FakeConnection.
    """
    conn = FakeConnection()
    conn.constructor_calls = []
    fake_info = [(socket.AF_INET, 1, 6, "", ("93.184.216.34", 0))]

    def make_conn(host, port, ip, timeout):
        conn.constructor_calls.append((host, port, ip, timeout))
        return conn

    with (
        patch("nova.tools.http_client.socket.getaddrinfo", return_value=fake_info),
        patch("nova.tools.http_client._PinnedHTTPConnection", side_effect=make_conn),
        patch("nova.tools.http_client._PinnedHTTPSConnection", side_effect=make_conn),
    ):
        yield conn


class TestHttpGet:
    """Tests for http_get tool."""

    def test_http_get_success(self, mock_conn):
        """Test successful GET request."""
        mock_conn.headers = [("Content-Type", "application/json")]
        mock_conn.body = b'{"status": "ok"}'

        result = _http_get({"url": "https://example.com/api"})
        assert "Status: 200 OK" in result
        assert "application/json" in result
        assert '"status": "ok"' in result

    def test_http_get_no_url(self):
        """Test GET without URL."""
        result = _http_get({"url": ""})
        assert "Error:" in result

    def test_http_get_invalid_url(self):
        """Test GET with invalid URL."""
        result = _http_get({"url": "not-a-url"})
        assert "Error:" in result

    def test_http_get_timeout(self, mock_conn):
        """Test GET timeout."""
        mock_conn.exc = TimeoutError("timed out")
        result = _http_get({"url": "https://example.com/api", "timeout": 30})
        assert "Error:" in result
        assert "timed out" in result.lower()

    def test_http_get_connection_error(self, mock_conn):
        """Test connection failure."""
        mock_conn.exc = ConnectionRefusedError("refused")
        result = _http_get({"url": "https://example.com/api"})
        assert "Error:" in result
        assert "Connection failed" in result

    def test_http_get_custom_headers(self, mock_conn):
        """Test GET with custom headers."""
        mock_conn.body = b"{}"
        result = _http_get(
            {"url": "https://example.com/api", "headers": {"Authorization": "Bearer token"}}
        )
        assert "Status: 200 OK" in result
        method, path, body, headers = mock_conn.request_args
        assert method == "GET"
        assert headers["Authorization"] == "Bearer token"

    def test_http_get_uses_pinned_ip(self, mock_conn):
        """The connection is pinned to the validated IP, not the hostname."""
        _http_get({"url": "http://example.com/path?q=1"})
        method, path, body, headers = mock_conn.request_args
        assert method == "GET"
        assert path == "/path?q=1"
        host, port, ip, timeout = mock_conn.constructor_calls[0]
        assert host == "example.com"
        assert ip == "93.184.216.34"
        assert port == 80

    def test_http_get_gzip_body(self, mock_conn):
        """Gzip-encoded responses are transparently decompressed."""
        import gzip

        mock_conn.headers = [("Content-Encoding", "gzip")]
        mock_conn.body = gzip.compress(b'{"ok": true}')
        result = _http_get({"url": "https://example.com/api"})
        assert '"ok": true' in result

    def test_http_get_headers_not_object(self, mock_conn):
        """Non-object headers must fail cleanly."""
        result = _http_get({"url": "https://example.com/api", "headers": ["nope"]})
        assert "Error:" in result
        assert "Headers" in result


class TestHttpPost:
    """Tests for http_post tool."""

    def test_http_post_success(self, mock_conn):
        """Test successful POST request."""
        mock_conn.status = 201
        mock_conn.reason = "Created"
        mock_conn.body = b'{"id": 123}'

        body = json.dumps({"name": "test"})
        result = _http_post({"url": "https://example.com/api", "body": body})
        assert "Status: 201 Created" in result
        method, path, request_body, headers = mock_conn.request_args
        assert method == "POST"
        assert json.loads(request_body) == {"name": "test"}
        assert headers["Content-Type"] == "application/json"

    def test_http_post_invalid_json_body(self, mock_conn):
        """Test POST with invalid JSON body."""
        result = _http_post({"url": "https://example.com/api", "body": "not json"})
        assert "Error:" in result
        assert "Invalid JSON" in result

    def test_http_post_empty_body(self, mock_conn):
        """Test POST with empty body."""
        mock_conn.body = b""
        result = _http_post({"url": "https://example.com/api", "body": ""})
        assert "Status: 200 OK" in result
        _, _, request_body, _ = mock_conn.request_args
        assert request_body is None


class TestHttpPut:
    """Tests for http_put tool."""

    def test_http_put_success(self, mock_conn):
        """Test successful PUT request."""
        mock_conn.body = b'{"updated": true}'
        body = json.dumps({"name": "updated"})
        result = _http_put({"url": "https://example.com/api/1", "body": body})
        assert "Status: 200 OK" in result
        method, _, _, _ = mock_conn.request_args
        assert method == "PUT"


class TestHttpDelete:
    """Tests for http_delete tool."""

    def test_http_delete_success(self, mock_conn):
        """Test successful DELETE request."""
        mock_conn.status = 204
        mock_conn.reason = "No Content"
        mock_conn.body = b""
        result = _http_delete({"url": "https://example.com/api/1"})
        assert "Status: 204 No Content" in result
        method, _, _, _ = mock_conn.request_args
        assert method == "DELETE"


class TestTimeoutValidation:
    """Tests for timeout validation."""

    def test_invalid_timeout_too_high(self, mock_conn):
        """Test timeout exceeding max."""
        result = _http_get({"url": "https://example.com/api", "timeout": 301})
        assert "Error:" in result
        assert "timeout" in result.lower()

    def test_invalid_timeout_negative(self):
        """Test negative timeout."""
        result = _http_get({"url": "https://example.com/api", "timeout": -1})
        assert "Error:" in result

    def test_invalid_timeout_string(self, mock_conn):
        """Test non-numeric timeout."""
        result = _http_get({"url": "https://example.com/api", "timeout": "abc"})
        assert "Error:" in result
        assert "timeout" in result.lower()


class TestSSRFProtection:
    """Tests for SSRF protection in _is_url_safe.

    The check must inspect every A record returned by getaddrinfo, not just
    the first one — that's what makes DNS rebinding attacks harder. A host
    that resolves to a mix of public and private IPs should be denied.
    """

    def test_blocks_hostname_resolving_to_private_ip(self):
        fake_info = [
            (2, 1, 6, "", ("10.0.0.5", 0)),
        ]
        with patch("nova.tools.http_client.socket.getaddrinfo", return_value=fake_info):
            ok, msg = _is_url_safe("https://evil.example.com/")
        assert ok is False
        assert "private" in msg.lower()

    def test_blocks_when_any_record_is_private(self):
        # Multi-IP host: first A record is public, second is link-local.
        # Old code that called gethostbyname() would have seen only the
        # first (public) record and let this through.
        fake_info = [
            (2, 1, 6, "", ("8.8.8.8", 0)),
            (2, 1, 6, "", ("169.254.169.254", 0)),  # AWS metadata
        ]
        with patch("nova.tools.http_client.socket.getaddrinfo", return_value=fake_info):
            ok, _ = _is_url_safe("https://rebind.example.com/")
        assert ok is False

    def test_blocks_aws_metadata_endpoint(self):
        # Direct hit, no DNS shenanigans
        ok, _ = _is_url_safe("https://169.254.169.254/latest/meta-data/")
        assert ok is False

    def test_blocks_loopback(self):
        ok, _ = _is_url_safe("http://127.0.0.1/admin")
        assert ok is False

    def test_blocks_non_http_scheme(self):
        ok, _ = _is_url_safe("file:///etc/passwd")
        assert ok is False

    def test_blocks_unresolvable_host(self):
        with patch(
            "nova.tools.http_client.socket.getaddrinfo",
            side_effect=OSError("no such host"),
        ):
            ok, msg = _is_url_safe("https://nope.invalid/")
        assert ok is False
        assert "unable to resolve" in msg.lower()

    def test_blocks_literal_private_ip(self):
        ok, msg = _is_url_safe("http://192.168.1.10/admin")
        assert ok is False
        assert "private" in msg.lower()
