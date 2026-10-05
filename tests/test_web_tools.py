"""Tests for the Firecrawl-backed web tools.

Uses real SDK response types (not MagicMocks) so field access is validated
against the actual Firecrawl schema. No network calls are made.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from firecrawl.v2.types import (
    CrawlErrorsResponse,
    CrawlJob,
    CrawlResponse,
    Document,
    DocumentMetadata,
    LinkResult,
    MapData,
    SearchData,
    SearchResultNews,
    SearchResultWeb,
)
from firecrawl.v2.utils.error_handler import (
    BadRequestError,
    FirecrawlError,
    PaymentRequiredError,
    RateLimitError,
    UnauthorizedError,
)

from nova.tools import firecrawl_client as fc
from nova.tools.registry import ToolRegistry, registry
from nova.tools.web import (
    _web_crawl,
    _web_dev_search,
    _web_extract,
    _web_map,
    _web_parse,
    _web_scrape,
    _web_search,
    _web_usage,
    register_web_tools,
)

CONFIG = {
    "web": {"firecrawl_api_key": "fc-test-key", "timeout_seconds": 30},
    "budgets": {"tool_result_max_chars": 8000},
}


@pytest.fixture(autouse=True)
def _registered():
    """Registration is config-gated, so register explicitly for these tests."""
    register_web_tools(CONFIG)


def _document(
    url: str, markdown: str = "# Hello\n\nBody text.", title: str = "Example"
) -> Document:
    return Document(
        markdown=markdown,
        metadata=DocumentMetadata(title=title, source_url=url, status_code=200),
    )


@pytest.fixture
def client():
    """A mock Firecrawl client injected in place of the real one."""
    mock = MagicMock()
    with patch("nova.tools.web.build_client", return_value=mock):
        yield mock


# ── Availability and configuration ──────────────────────────────────────────


def test_missing_api_key_returns_actionable_error():
    result = _web_search({"query": "python"}, config={"web": {}})
    assert result.startswith("Error:")
    assert "FIRECRAWL_API_KEY" in result


def test_missing_sdk_returns_install_hint():
    with (
        patch.dict("sys.modules", {"firecrawl": None}),
        pytest.raises(fc.FirecrawlUnavailable) as exc,
    ):
        fc.build_client(CONFIG)
    assert "nova-agent[web]" in str(exc.value)


def test_build_client_passes_key_and_timeout():
    with patch("firecrawl.Firecrawl") as ctor:
        fc.build_client(CONFIG)
    ctor.assert_called_once_with(api_key="fc-test-key", timeout=30.0)


def test_malformed_web_config_does_not_crash():
    assert fc.get_api_key({"web": "nonsense"}) == ""
    assert fc.get_timeout({"web": None}) == 30
    assert fc.get_timeout({"web": {"timeout_seconds": "abc"}}) == 30
    assert fc.get_timeout({"web": {"timeout_seconds": 9999}}) == 300
    assert fc.get_max_chars({"budgets": {"tool_result_max_chars": "x"}}) == 8000


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/test",
        "http://10.20.30.40/test",
        "http://172.16.0.1/test",
        "http://172.31.255.255/test",
        "http://192.168.1.1/test",
        "http://[::1]/test",
    ],
)
def test_validate_url_rejects_private_addresses(url):
    assert "cannot reach private" in fc.validate_url(url)


def test_max_chars_respects_configured_budget():
    assert fc.get_max_chars({"budgets": {"tool_result_max_chars": 2000}}) == 2000


# ── Error translation ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (UnauthorizedError("nope"), "API key"),
        (PaymentRequiredError("nope"), "credits"),
        (RateLimitError("nope"), "rate limit"),
    ],
)
def test_error_translation_is_actionable(exc, expected, client):
    client.search.side_effect = exc
    result = _web_search({"query": "python"}, config=CONFIG)
    assert result.startswith("Error:")
    assert expected.lower() in result.lower()


def test_error_translation_never_leaks_api_key(client):
    client.search.side_effect = UnauthorizedError("bad key fc-test-key leaked")
    result = _web_search({"query": "python"}, config=CONFIG)
    assert "fc-test-key" not in result


def test_firecrawl_error_message_is_surfaced(client):
    """Base FirecrawlError carries actionable detail (e.g. 'already completed')."""
    client.cancel_crawl.side_effect = FirecrawlError(
        "Unexpected error during cancel crawl: Status code 409. Crawl is already completed"
    )
    result = _web_crawl({"action": "cancel", "job_id": "j"}, config=CONFIG)
    assert "already completed" in result


def test_bad_request_message_is_surfaced(client):
    client.get_crawl_status.side_effect = BadRequestError(
        "Bad Request: Invalid job ID format. Job ID must be a valid UUID."
    )
    result = _web_crawl({"action": "status", "job_id": "nope"}, config=CONFIG)
    assert "valid UUID" in result


def test_firecrawl_error_message_is_redacted(client):
    client.search.side_effect = FirecrawlError("failed with Bearer fc-test-key-secret")
    result = _web_search({"query": "x"}, config=CONFIG)
    assert "fc-test-key-secret" not in result
    assert "<redacted>" in result


def test_unknown_exception_is_contained(client):
    client.search.side_effect = RuntimeError("boom internals")
    result = _web_search({"query": "python"}, config=CONFIG)
    assert result == "Error: Firecrawl request failed (RuntimeError)."


# ── web_search ──────────────────────────────────────────────────────────────


def test_web_search_formats_results(client):
    client.search.return_value = SearchData(
        web=[SearchResultWeb(url="https://python.org", title="Python", description="The language")]
    )
    result = _web_search({"query": "python", "limit": 3}, config=CONFIG)
    assert "Python" in result
    assert "https://python.org" in result
    assert "The language" in result
    client.search.assert_called_once_with("python", limit=3, sources=["web"])


def test_web_search_labels_content_as_untrusted(client):
    client.search.return_value = SearchData(
        web=[SearchResultWeb(url="https://x.dev", title="X", description="d")]
    )
    result = _web_search({"query": "x"}, config=CONFIG)
    assert fc.UNTRUSTED_HEADER in result


def test_web_search_clamps_limit_and_filters_sources(client):
    client.search.return_value = SearchData(web=[])
    _web_search({"query": "x", "limit": 999, "sources": ["web", "bogus"]}, config=CONFIG)
    assert client.search.call_args.kwargs["limit"] == 20
    assert client.search.call_args.kwargs["sources"] == ["web"]


def test_web_search_passes_domain_filters(client):
    client.search.return_value = SearchData(web=[])
    _web_search(
        {"query": "x", "include_domains": ["a.com"], "exclude_domains": ["b.com"]},
        config=CONFIG,
    )
    assert client.search.call_args.kwargs["include_domains"] == ["a.com"]
    assert client.search.call_args.kwargs["exclude_domains"] == ["b.com"]


def test_web_search_scrape_content_sets_scrape_options(client):
    client.search.return_value = SearchData(web=[])
    _web_search({"query": "x", "scrape_content": True}, config=CONFIG)
    assert client.search.call_args.kwargs["scrape_options"].formats == ["markdown"]


def test_web_search_empty_query():
    assert _web_search({"query": "  "}, config=CONFIG).startswith("Error:")


def test_web_search_no_results(client):
    client.search.return_value = SearchData()
    assert "No results found" in _web_search({"query": "zzz"}, config=CONFIG)


def test_web_search_includes_news_group(client):
    client.search.return_value = SearchData(
        news=[SearchResultNews(url="https://news.dev/a", title="Headline", snippet="d")]
    )
    result = _web_search({"query": "x", "sources": ["news"]}, config=CONFIG)
    assert "News" in result
    assert "Headline" in result


# ── web_scrape ──────────────────────────────────────────────────────────────


def test_web_scrape_single_url(client):
    client.scrape.return_value = _document("https://example.com")
    result = _web_scrape({"urls": ["https://example.com"]}, config=CONFIG)
    assert "Example" in result
    assert "Source: https://example.com" in result
    assert "Body text." in result
    assert client.scrape.call_args.kwargs["formats"] == ["markdown"]
    assert client.scrape.call_args.kwargs["only_main_content"] is True


def test_web_scrape_accepts_singular_url_key(client):
    client.scrape.return_value = _document("https://example.com")
    result = _web_scrape({"url": "https://example.com"}, config=CONFIG)
    assert "Body text." in result


def test_web_scrape_batches_multiple_urls(client):
    client.batch_scrape.return_value = CrawlJob(
        status="completed",
        total=2,
        completed=2,
        credits_used=2,
        data=[_document("https://a.dev"), _document("https://b.dev")],
    )
    result = _web_scrape({"urls": ["https://a.dev", "https://b.dev"]}, config=CONFIG)
    assert "https://a.dev" in result
    assert "https://b.dev" in result
    assert client.batch_scrape.call_args.kwargs["wait_timeout"] <= 120


def test_web_scrape_rejects_too_many_urls():
    result = _web_scrape({"urls": [f"https://x{i}.dev" for i in range(11)]}, config=CONFIG)
    assert "Too many URLs" in result


def test_web_scrape_requires_urls():
    assert _web_scrape({}, config=CONFIG).startswith("Error:")


def test_web_scrape_filters_disallowed_formats(client):
    client.scrape.return_value = _document("https://example.com")
    _web_scrape(
        {"urls": ["https://example.com"], "formats": ["screenshot", "markdown"]},
        config=CONFIG,
    )
    assert client.scrape.call_args.kwargs["formats"] == ["markdown"]


def test_web_scrape_truncates_to_budget(client):
    client.scrape.return_value = _document("https://example.com", markdown="A" * 50000)
    result = _web_scrape({"urls": ["https://example.com"]}, config=CONFIG)
    assert len(result) < 12000
    assert "truncated" in result


def test_web_scrape_reports_scrape_error_field(client):
    """Firecrawl reports per-page failures on metadata.error, not on Document."""
    client.scrape.return_value = Document(
        metadata=DocumentMetadata(
            source_url="https://example.com", status_code=403, error="blocked"
        ),
    )
    result = _web_scrape({"urls": ["https://example.com"]}, config=CONFIG)
    assert "403" in result
    assert "blocked" in result


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/file",
        "file:///etc/passwd",
        "http://localhost:8080",
        "http://127.0.0.1/admin",
        "http://169.254.169.254/latest/meta-data/",
        "http://192.168.1.1",
        "http://10.0.0.5",
    ],
)
def test_web_scrape_rejects_unreachable_targets(url):
    result = _web_scrape({"urls": [url]}, config=CONFIG)
    assert result.startswith("Error:")


def test_web_scrape_allows_bare_domain(client):
    client.scrape.return_value = _document("https://example.com")
    assert not _web_scrape({"urls": ["example.com"]}, config=CONFIG).startswith("Error:")


# ── web_map ─────────────────────────────────────────────────────────────────


def test_web_map_lists_links(client):
    client.map.return_value = MapData(
        links=[
            LinkResult(url="https://d.dev/a", title="A", description=""),
            LinkResult(url="https://d.dev/b", title=None, description=""),
        ]
    )
    result = _web_map({"url": "https://d.dev", "limit": 10}, config=CONFIG)
    assert "https://d.dev/a — A" in result
    assert "https://d.dev/b" in result
    assert client.map.call_args.kwargs["limit"] == 10


def test_web_map_passes_search_term(client):
    client.map.return_value = MapData(links=[])
    _web_map({"url": "https://d.dev", "search": "pricing"}, config=CONFIG)
    assert client.map.call_args.kwargs["search"] == "pricing"


def test_web_map_no_results(client):
    client.map.return_value = MapData(links=[])
    assert "No URLs found" in _web_map({"url": "https://d.dev"}, config=CONFIG)


def test_web_map_validates_url():
    assert _web_map({"url": ""}, config=CONFIG).startswith("Error:")


# ── web_crawl ───────────────────────────────────────────────────────────────


def test_web_crawl_start_returns_job_id(client):
    client.start_crawl.return_value = CrawlResponse(id="job-123", url="https://d.dev")
    result = _web_crawl({"action": "start", "url": "https://d.dev", "limit": 5}, config=CONFIG)
    assert "job-123" in result
    assert client.start_crawl.call_args.kwargs["limit"] == 5


def test_web_crawl_start_is_non_blocking(client):
    """start must not call the blocking crawl() waiter."""
    client.start_crawl.return_value = CrawlResponse(id="j", url="https://d.dev")
    _web_crawl({"action": "start", "url": "https://d.dev"}, config=CONFIG)
    client.crawl.assert_not_called()


def test_web_crawl_start_passes_path_filters(client):
    client.start_crawl.return_value = CrawlResponse(id="j", url="https://d.dev")
    _web_crawl(
        {
            "action": "start",
            "url": "https://d.dev",
            "include_paths": ["/docs/.*"],
            "exclude_paths": ["/blog/.*"],
            "max_discovery_depth": 3,
        },
        config=CONFIG,
    )
    kwargs = client.start_crawl.call_args.kwargs
    assert kwargs["include_paths"] == ["/docs/.*"]
    assert kwargs["exclude_paths"] == ["/blog/.*"]
    assert kwargs["max_discovery_depth"] == 3


def test_web_crawl_status_reports_progress(client):
    client.get_crawl_status.return_value = CrawlJob(
        status="scraping", total=10, completed=3, credits_used=3, data=[]
    )
    result = _web_crawl({"action": "status", "job_id": "job-1"}, config=CONFIG)
    assert "status: scraping" in result
    assert "3/10" in result
    assert "poll again" in result.lower()


def test_web_crawl_status_disables_auto_pagination(client):
    client.get_crawl_status.return_value = CrawlJob(
        status="completed", total=1, completed=1, credits_used=1, data=[]
    )
    _web_crawl({"action": "status", "job_id": "job-1"}, config=CONFIG)
    pagination = client.get_crawl_status.call_args.kwargs["pagination_config"]
    assert pagination.auto_paginate is False


def test_web_crawl_status_omits_body_by_default(client):
    client.get_crawl_status.return_value = CrawlJob(
        status="completed",
        total=1,
        completed=1,
        credits_used=1,
        data=[_document("https://d.dev/a", markdown="SECRET BODY")],
    )
    result = _web_crawl({"action": "status", "job_id": "j"}, config=CONFIG)
    assert "https://d.dev/a" in result
    assert "SECRET BODY" not in result


def test_web_crawl_status_includes_body_on_request(client):
    client.get_crawl_status.return_value = CrawlJob(
        status="completed",
        total=1,
        completed=1,
        credits_used=1,
        data=[_document("https://d.dev/a", markdown="PAGE BODY")],
    )
    result = _web_crawl({"action": "status", "job_id": "j", "include_content": True}, config=CONFIG)
    assert "PAGE BODY" in result


def test_web_crawl_cancel(client):
    client.cancel_crawl.return_value = True
    assert "cancelled" in _web_crawl({"action": "cancel", "job_id": "j"}, config=CONFIG)


def test_web_crawl_cancel_failure(client):
    client.cancel_crawl.return_value = False
    assert "Could not cancel" in _web_crawl({"action": "cancel", "job_id": "j"}, config=CONFIG)


def test_web_crawl_errors(client):
    client.get_crawl_errors.return_value = CrawlErrorsResponse(
        errors=[], robots_blocked=["https://d.dev/private"]
    )
    result = _web_crawl({"action": "errors", "job_id": "j"}, config=CONFIG)
    assert "robots.txt" in result
    assert "https://d.dev/private" in result


def test_web_crawl_errors_none(client):
    client.get_crawl_errors.return_value = CrawlErrorsResponse(errors=[], robots_blocked=[])
    assert "no errors" in _web_crawl({"action": "errors", "job_id": "j"}, config=CONFIG)


def test_web_crawl_rejects_bad_action():
    assert _web_crawl({"action": "explode"}, config=CONFIG).startswith("Error:")


@pytest.mark.parametrize("action", ["status", "cancel", "errors"])
def test_web_crawl_requires_job_id(action):
    result = _web_crawl({"action": action}, config=CONFIG)
    assert "job_id is required" in result


# ── web_extract ─────────────────────────────────────────────────────────────


def test_web_extract_start(client):
    client.start_extract.return_value = {"id": "ext-1"}
    result = _web_extract(
        {"action": "start", "urls": ["https://d.dev"], "prompt": "get pricing"},
        config=CONFIG,
    )
    assert "ext-1" in result
    assert client.start_extract.call_args.kwargs["prompt"] == "get pricing"


def test_web_extract_start_accepts_wildcard_urls(client):
    client.start_extract.return_value = {"id": "ext-1"}
    result = _web_extract(
        {"action": "start", "urls": ["https://d.dev/*"], "prompt": "x"}, config=CONFIG
    )
    assert not result.startswith("Error:")


def test_web_extract_start_passes_schema(client):
    client.start_extract.return_value = {"id": "ext-1"}
    schema = {"type": "object", "properties": {"price": {"type": "number"}}}
    _web_extract({"action": "start", "urls": ["https://d.dev"], "schema": schema}, config=CONFIG)
    assert client.start_extract.call_args.kwargs["schema"] == schema


def test_web_extract_requires_prompt_or_schema(client):
    result = _web_extract({"action": "start", "urls": ["https://d.dev"]}, config=CONFIG)
    assert "prompt and/or a schema" in result


def test_web_extract_requires_urls():
    assert _web_extract({"action": "start"}, config=CONFIG).startswith("Error:")


def test_web_extract_status_renders_json(client):
    client.get_extract_status.return_value = {
        "status": "completed",
        "data": {"price": 42},
    }
    result = _web_extract({"action": "status", "job_id": "ext-1"}, config=CONFIG)
    assert '"price": 42' in result


def test_web_extract_status_processing(client):
    client.get_extract_status.return_value = {"status": "processing", "data": None}
    result = _web_extract({"action": "status", "job_id": "ext-1"}, config=CONFIG)
    assert "poll again" in result.lower()


def test_web_extract_rejects_bad_action():
    assert _web_extract({"action": "nope"}, config=CONFIG).startswith("Error:")


# ── web_parse ───────────────────────────────────────────────────────────────


def test_web_parse_uploads_and_returns_markdown(tmp_path: Path, client):
    target = tmp_path / "doc.pdf"
    target.write_bytes(b"%PDF-1.4 fake")
    client.parse.return_value = Document(markdown="# Parsed\n\ntext")
    result = _web_parse({"path": str(target)}, config=CONFIG)
    assert "# Parsed" in result
    assert client.parse.call_args.kwargs["filename"] == "doc.pdf"


def test_web_parse_missing_file():
    assert _web_parse({"path": "/nope/missing.pdf"}, config=CONFIG).startswith("Error:")


def test_web_parse_rejects_unsupported_suffix(tmp_path: Path):
    target = tmp_path / "binary.bin"
    target.write_bytes(b"\x00\x01")
    result = _web_parse({"path": str(target)}, config=CONFIG)
    assert "Unsupported file type" in result


def test_web_parse_rejects_oversized_file(tmp_path: Path, monkeypatch):
    target = tmp_path / "big.pdf"
    target.write_bytes(b"x")
    monkeypatch.setattr("nova.tools.web._MAX_PARSE_BYTES", 0)
    assert "too large" in _web_parse({"path": str(target)}, config=CONFIG).lower()


def test_web_parse_requires_path():
    assert _web_parse({}, config=CONFIG).startswith("Error:")


def test_web_parse_is_not_read_only():
    """Uploading local file contents to a third party must require confirmation."""
    assert registry.get_tool("web_parse").is_read_only is False


def test_web_parse_denies_path_outside_workspace(tmp_path: Path):
    """web_parse must not exfiltrate files outside the configured workspace."""
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.pdf"
    secret.write_bytes(b"%PDF-1.4 secret")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = _web_parse(
        {"path": str(secret)},
        config=CONFIG,
        workspace=str(workspace),
    )
    assert result.startswith("Error:")
    assert "workspace" in result.lower()


def test_web_parse_denies_sensitive_dotfile(tmp_path: Path):
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    key = ssh_dir / "id_rsa.pdf"
    key.write_bytes(b"%PDF-1.4 key")

    result = _web_parse({"path": str(key)}, config=CONFIG, workspace=str(tmp_path))
    assert result.startswith("Error:")
    assert "sensitive" in result.lower()


# ── web_dev_search ──────────────────────────────────────────────────────────


def test_web_dev_search_formats_results(client):
    response = MagicMock()
    response.results = [
        {
            "title": "retry helper",
            "repo": "encode/httpx",
            "url": "https://github.com/encode/httpx",
            "type": "doc",
            "content": "def retry(): ...",
        }
    ]
    client.developer_search.return_value = response
    result = _web_dev_search({"query": "httpx retry", "limit": 3}, config=CONFIG)
    assert "retry helper" in result
    assert "encode/httpx" in result
    assert "def retry()" in result
    assert client.developer_search.call_args.kwargs["k"] == 3


def test_web_dev_search_passes_filters(client):
    response = MagicMock()
    response.results = []
    client.developer_search.return_value = response
    _web_dev_search(
        {
            "query": "x",
            "types": ["issue", "bogus"],
            "repos": ["a/b"],
            "language": "python",
            "min_stars": 100,
        },
        config=CONFIG,
    )
    kwargs = client.developer_search.call_args.kwargs
    assert kwargs["types"] == ["issue"]
    assert kwargs["repos"] == ["a/b"]
    assert kwargs["language"] == "python"
    assert kwargs["min_stars"] == 100


def test_web_dev_search_requires_query():
    assert _web_dev_search({"query": ""}, config=CONFIG).startswith("Error:")


def test_web_dev_search_no_results(client):
    response = MagicMock()
    response.results = []
    client.developer_search.return_value = response
    assert "No developer search results" in _web_dev_search({"query": "zzz"}, config=CONFIG)


# ── web_usage ───────────────────────────────────────────────────────────────


def test_web_usage_reports_all_three_metrics(client):
    client.get_credit_usage.return_value = {"remaining_credits": 500}
    client.get_token_usage.return_value = {"remaining_tokens": 1000}
    client.get_concurrency.return_value = {"concurrency": 1, "max_concurrency": 5}
    result = _web_usage({}, config=CONFIG)
    assert "remaining_credits=500" in result
    assert "remaining_tokens=1000" in result
    assert "max_concurrency=5" in result


def test_web_usage_degrades_per_metric(client):
    client.get_credit_usage.side_effect = RuntimeError("down")
    client.get_token_usage.return_value = {"remaining_tokens": 7}
    client.get_concurrency.return_value = {}
    result = _web_usage({}, config=CONFIG)
    assert "credits: unavailable" in result
    assert "remaining_tokens=7" in result
    assert "concurrency: unavailable" in result


def test_web_usage_without_key():
    assert _web_usage({}, config={"web": {}}).startswith("Error:")


# ── Registration ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name",
    [
        "web_search",
        "web_scrape",
        "web_map",
        "web_crawl",
        "web_extract",
        "web_parse",
        "web_dev_search",
        "web_usage",
    ],
)
def test_tool_is_registered(name):
    entry = registry.get_tool(name)
    assert entry is not None
    assert entry.toolset == "web"
    assert entry.schema["name"] == name


def test_read_only_web_tools_are_parallel_eligible():
    from nova.tools.web import _TOOLS

    read_only = {name: flag for name, _schema, _handler, _emoji, flag in _TOOLS}
    for name in ("web_search", "web_scrape", "web_map"):
        assert read_only[name] is True, f"{name} missing from parallel dispatch set"
    # Credit-spending, job-mutating tools must not be treated as read-only:
    # they require confirmation in ask-mode and are excluded from parallel fan-out.
    assert read_only["web_crawl"] is False
    assert read_only["web_extract"] is False
    assert read_only.get("web_parse", False) is False


def test_credit_spending_web_tools_are_not_read_only():
    for name in ("web_crawl", "web_extract", "web_parse"):
        entry = registry.get_tool(name)
        assert entry is not None
        assert entry.is_read_only is False, f"{name} must not be read-only"


def test_schemas_declare_required_fields():
    for name in ("web_search", "web_scrape", "web_map", "web_crawl", "web_extract"):
        schema = registry.get_tool(name).schema
        assert schema["parameters"]["required"], f"{name} has no required fields"


# ── Config gating ───────────────────────────────────────────────────────────


def _register_into_fresh(config):
    """Register into an isolated registry and return the tool names."""
    fresh = ToolRegistry()
    with patch("nova.tools.web.registry", fresh):
        register_web_tools(config)
    return set(fresh._tools)


def test_no_api_key_skips_registration():
    """Eight schemas cost ~1.7k tokens; don't charge agents that can't use them."""
    assert _register_into_fresh({"web": {"firecrawl_api_key": ""}}) == set()


def test_disabled_skips_registration():
    assert _register_into_fresh({"web": {"enabled": False, "firecrawl_api_key": "fc-k"}}) == set()


def test_missing_sdk_skips_registration():
    with patch("nova.tools.web.is_available", return_value=False):
        assert _register_into_fresh(CONFIG) == set()


def test_configured_key_registers_all_tools():
    assert len(_register_into_fresh(CONFIG)) == 8


def test_registration_is_idempotent():
    fresh = ToolRegistry()
    with patch("nova.tools.web.registry", fresh):
        register_web_tools(CONFIG)
        register_web_tools(CONFIG)
    assert len(fresh._tools) == 8
