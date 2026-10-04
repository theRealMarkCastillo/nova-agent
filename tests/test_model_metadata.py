"""Tests for model metadata."""

from nova.model_metadata import (
    DEFAULT_CONTEXT_WINDOW,
    get_model_context_window,
    get_model_metadata,
    load_provider_metadata,
)


def test_default_context_window():
    assert DEFAULT_CONTEXT_WINDOW == 128_000


def test_exact_match():
    load_provider_metadata(
        _Client(
            [
                {"id": "anthropic/claude-sonnet-4-20250514", "context_length": 200_000},
                {"id": "openai/gpt-4o", "context_length": 128_000},
                {"id": "google/gemini-2.5-pro", "context_length": 1_000_000},
            ]
        )
    )
    assert get_model_context_window("anthropic/claude-sonnet-4-20250514") == 200_000
    assert get_model_context_window("openai/gpt-4o") == 128_000
    assert get_model_context_window("google/gemini-2.5-pro") == 1_000_000


def test_partial_match():
    load_provider_metadata(
        _Client([{"id": "anthropic/claude-sonnet-4", "context_length": 200_000}])
    )
    result = get_model_context_window("anthropic/claude-sonnet-4")
    assert result == 200_000


def test_unknown_model_default():
    assert get_model_context_window("unknown/model") == DEFAULT_CONTEXT_WINDOW
    assert get_model_context_window("some-random-model") == DEFAULT_CONTEXT_WINDOW


def test_override_beats_provider_reported():
    load_provider_metadata(_Client([{"id": "test/model", "context_length": 128_000}]))
    assert get_model_context_window("test/model", override=1_000_000) == 1_000_000


def test_override_beats_fallback():
    assert get_model_context_window("some-random-model", override=512_000) == 512_000


def test_override_zero_or_none_uses_defaults():
    load_provider_metadata(_Client([{"id": "test/model", "context_length": 200_000}]))
    assert get_model_context_window("test/model", override=0) == 200_000
    assert get_model_context_window("test/model", override=None) == 200_000
    assert get_model_context_window("some-random-model", override=0) == DEFAULT_CONTEXT_WINDOW


def test_load_provider_metadata_reads_pricing():
    load_provider_metadata(
        _Client(
            [
                {
                    "id": "test/model",
                    "context_length": 123_456,
                    "pricing": {"prompt": "0.000001", "completion": "0.000002"},
                }
            ]
        )
    )
    metadata = get_model_metadata("test/model")
    assert metadata is not None
    assert metadata.context_window == 123_456
    assert metadata.input_price_per_million == 1.0
    assert metadata.output_price_per_million == 2.0


class _Client:
    def __init__(self, data):
        self.models = self
        self.data = data

    def list(self):
        return {"data": self.data}


def test_partial_match_prefers_longest():
    load_provider_metadata(
        _Client(
            [
                {"id": "gpt-4", "context_length": 8_192},
                {"id": "gpt-4o-mini", "context_length": 128_000},
            ]
        )
    )
    # "gpt-4" is a substring of the query and is registered first — a naive
    # first-hit substring scan would wrongly return its metadata. The longer,
    # more specific "gpt-4o-mini" entry must win instead.
    metadata = get_model_metadata("openai/gpt-4o-mini")
    assert metadata is not None
    assert metadata.context_window == 128_000


def test_partial_match_prefers_longest_substring_without_exact_suffix():
    # Neither key is an exact suffix of the query (which has a trailing date
    # stamp), so this exercises the longest-substring tie-break specifically,
    # not the exact-suffix shortcut.
    load_provider_metadata(
        _Client(
            [
                {"id": "claude", "context_length": 8_192},
                {"id": "claude-3-opus", "context_length": 200_000},
            ]
        )
    )
    metadata = get_model_metadata("anthropic/claude-3-opus-20240229")
    assert metadata is not None
    assert metadata.context_window == 200_000


def test_suffix_match_ignores_provider_prefix():
    load_provider_metadata(_Client([{"id": "vendor-a/claude-opus", "context_length": 200_000}]))
    # Different provider prefix on the query side: the full id strings are
    # not substrings of one another at all, so this only matches through the
    # last-path-segment suffix comparison, not plain substring containment.
    metadata = get_model_metadata("vendor-b/claude-opus")
    assert metadata is not None
    assert metadata.context_window == 200_000
