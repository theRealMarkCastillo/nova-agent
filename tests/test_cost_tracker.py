"""Tests for the cost tracker."""

from unittest.mock import patch

import pytest

from nova.cost_tracker import CostTracker, UsageSnapshot, extract_usage_from_response
from nova.model_metadata import ModelMetadata, load_provider_metadata

# ── UsageSnapshot ───────────────────────────────────────────────────────────


def test_usage_snapshot_totals():
    snap = UsageSnapshot(input_tokens=1000, output_tokens=500, input_cost=0.001, output_cost=0.002)
    assert snap.total_tokens == 1500
    assert snap.total_cost == 0.003


def test_usage_snapshot_defaults():
    snap = UsageSnapshot()
    assert snap.total_tokens == 0
    assert snap.total_cost == 0.0


# ── CostTracker — Basic Usage ───────────────────────────────────────────────


def test_add_usage_tokens_only():
    tracker = CostTracker(model="qwen/qwen3.6-flash")
    tracker.add_usage(input_tokens=1000, output_tokens=500)
    assert tracker.total.input_tokens == 1000
    assert tracker.total.output_tokens == 500


def test_add_usage_with_costs():
    tracker = CostTracker(model="qwen/qwen3.6-flash")
    tracker.add_usage(
        input_tokens=1000, output_tokens=500, input_cost=0.00003, output_cost=0.000045
    )
    assert tracker.total.input_cost == 0.00003
    assert tracker.total.output_cost == 0.000045


def test_add_usage_cumulative():
    tracker = CostTracker(model="qwen/qwen3.6-flash")
    tracker.add_usage(input_tokens=1000, output_tokens=500)
    tracker.add_usage(input_tokens=2000, output_tokens=1000)
    assert tracker.total.input_tokens == 3000
    assert tracker.total.output_tokens == 1500


def test_add_usage_cumulative_costs():
    tracker = CostTracker(model="qwen/qwen3.6-flash")
    tracker.add_usage(
        input_tokens=1000, output_tokens=500, input_cost=0.00003, output_cost=0.000045
    )
    tracker.add_usage(
        input_tokens=2000, output_tokens=1000, input_cost=0.00006, output_cost=0.00009
    )
    assert abs(tracker.total.input_cost - 0.00009) < 1e-10
    assert abs(tracker.total.output_cost - 0.000135) < 1e-10


def test_cost_estimation_from_model_pricing():
    tracker = CostTracker(model="qwen/qwen3.6-flash")
    tracker.add_usage(input_tokens=1_000_000, output_tokens=1_000_000)
    # Unknown provider metadata uses the documented fallback.
    assert abs(tracker.total.input_cost - 0.10) < 1e-10
    assert abs(tracker.total.output_cost - 0.30) < 1e-10


def test_cost_estimation_unknown_model_uses_default():
    tracker = CostTracker(model="unknown/model")
    tracker.add_usage(input_tokens=1_000_000, output_tokens=1_000_000)
    # Default: $0.10/1M input, $0.30/1M output
    assert abs(tracker.total.input_cost - 0.10) < 1e-10
    assert abs(tracker.total.output_cost - 0.30) < 1e-10


def test_cost_estimation_uses_provider_pricing():
    class Client:
        models = None

        def __init__(self):
            self.models = self

        def list(self):
            return {
                "data": [
                    {
                        "id": "provider/model",
                        "pricing": {"prompt": "0.000003", "completion": "0.000007"},
                    }
                ]
            }

    load_provider_metadata(Client())
    tracker = CostTracker(model="provider/model")
    tracker.add_usage(input_tokens=1_000_000, output_tokens=1_000_000)
    assert tracker.total.input_cost == 3.0
    assert tracker.total.output_cost == 7.0


def test_reset_tracker():
    tracker = CostTracker(model="qwen/qwen3.6-flash")
    tracker.add_usage(input_tokens=1000, output_tokens=500, input_cost=0.001, output_cost=0.002)
    tracker.reset()
    assert tracker.total.input_tokens == 0
    assert tracker.total.output_tokens == 0
    assert tracker.total.input_cost == 0.0
    assert tracker.total.output_cost == 0.0


def test_format_summary():
    tracker = CostTracker(model="qwen/qwen3.6-flash")
    tracker.add_usage(
        input_tokens=1000, output_tokens=500, input_cost=0.00003, output_cost=0.000045
    )
    summary = tracker.format_summary()
    assert "1,500" in summary
    assert "1,000" in summary
    assert "500" in summary
    assert "$" in summary


def test_format_summary_no_cost():
    tracker = CostTracker(model="qwen/qwen3.6-flash")
    tracker.add_usage(input_tokens=1000, output_tokens=500, input_cost=0.0, output_cost=0.0)
    summary = tracker.format_summary()
    assert "Tokens:" in summary
    assert "Cost:" not in summary


# ── extract_usage_from_response ─────────────────────────────────────────────


def test_extract_usage_from_response():
    response = {
        "usage": {
            "prompt_tokens": 1000,
            "completion_tokens": 500,
        }
    }
    usage = extract_usage_from_response(response)
    assert usage["input_tokens"] == 1000
    assert usage["output_tokens"] == 500


def test_extract_usage_from_response_with_cost():
    response = {
        "usage": {
            "prompt_tokens": 1000,
            "completion_tokens": 500,
            "cost": 0.000075,
        }
    }
    usage = extract_usage_from_response(response)
    assert usage["input_tokens"] == 1000
    assert usage["output_tokens"] == 500
    assert usage["input_cost"] == 0.000075
    assert usage["output_cost"] == 0.0


def test_extract_usage_from_response_empty():
    response = {}
    usage = extract_usage_from_response(response)
    assert usage["input_tokens"] == 0
    assert usage["output_tokens"] == 0


def test_extract_usage_from_response_none_usage_does_not_crash():
    """Providers that omit streamed usage must not crash cost tracking."""
    usage = extract_usage_from_response({"choices": [], "usage": None})
    assert usage["input_tokens"] == 0
    assert usage["output_tokens"] == 0


def test_extract_usage_from_response_with_cache_tokens():
    usage = extract_usage_from_response(
        {
            "usage": {
                "prompt_cache_hit_tokens": 90,
                "prompt_cache_miss_tokens": 10,
                "completion_tokens": 5,
            }
        }
    )
    assert usage["input_tokens"] == 100
    assert usage["cache_read_tokens"] == 90
    assert usage["cache_write_tokens"] == 10


def test_cache_tokens_are_included_in_cost_summary():
    tracker = CostTracker(model="unknown/model")
    tracker.add_usage(input_tokens=100, output_tokens=10, cache_read_tokens=90)
    assert tracker.total.cache_read_tokens == 90
    assert "Cache: 90 read" in tracker.format_summary()


@pytest.mark.parametrize(
    "fields, expected",
    [
        ({}, 90),
        ({"cache_read_input_tokens": 30}, 30),
        ({"prompt_cache_hit_tokens": 40}, 40),
        ({"cache_read_input_tokens": 30, "prompt_cache_hit_tokens": 40}, 30),
        ({"cache_read_input_tokens": 0}, 90),
        ({"cache_read_input_tokens": 0, "prompt_cache_hit_tokens": 40}, 40),
        ({"prompt_cache_hit_tokens": 0}, 90),
    ],
)
def test_nested_cached_tokens_preserve_totals_and_precedence(fields, expected):
    usage = extract_usage_from_response(
        {
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 5,
                "prompt_tokens_details": {"cached_tokens": 90},
                "cache_creation_input_tokens": 10,
                **fields,
            }
        }
    )
    assert usage["input_tokens"] == 100
    assert usage["output_tokens"] == 5
    assert usage["cache_read_tokens"] == expected
    assert usage["cache_write_tokens"] == 10


@pytest.mark.parametrize(
    "details",
    [
        None,
        {},
        [],
        "invalid",
        {"cached_tokens": None},
        {"cached_tokens": "90"},
        {"cached_tokens": -1},
        {"cached_tokens": True},
    ],
)
def test_missing_or_malformed_prompt_details(details):
    usage = extract_usage_from_response(
        {"usage": {"prompt_tokens": 100, "prompt_tokens_details": details}}
    )
    assert usage["cache_read_tokens"] == 0
    assert usage["input_tokens"] == 100


@pytest.mark.parametrize("reported_cost", [None, 0.0, 0.00005])
def test_nested_cache_usage_cost_accounting(reported_cost):
    response = {
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 5,
            "prompt_tokens_details": {"cached_tokens": 90},
        }
    }
    if reported_cost is not None:
        response["usage"]["cost"] = reported_cost
    metadata = ModelMetadata(
        input_price_per_million=2, output_price_per_million=4, cache_read_price_per_million=0.2
    )
    tracker = CostTracker(model="test-model")
    with patch("nova.cost_tracker.get_model_metadata", return_value=metadata):
        tracker.add_usage(**extract_usage_from_response(response))
    assert tracker.total.input_tokens == 100
    assert tracker.total.cache_read_tokens == 90
    assert tracker.total.total_tokens == 105
    expected = reported_cost if reported_cost is not None else 0.000058
    assert tracker.total.total_cost == pytest.approx(expected)
