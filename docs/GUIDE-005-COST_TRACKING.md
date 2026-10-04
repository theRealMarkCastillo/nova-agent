# Cost Tracking

**Status:** ✅ Active  
**Last Updated:** October 2026
**Type:** GUIDE (Feature Reference)

> Nova Agent tracks token usage and estimates dollar costs for each session, helping you monitor spending across different models.

## Quick Start

Cost tracking is enabled by default. View your session usage with:

```
/usage
```

Output:
```
Context used: 12,450 / 128,000 tokens (9%)
Tokens: 45,230 total (32,100 in, 13,130 out) | Cost: $0.002145 ($0.000963 in, $0.001182 out)
```

## Configuration

```yaml
cost_tracking:
  enabled: true    # Set to false to disable
```

## How Costs Are Calculated

Costs are estimated using per-model pricing tables. When OpenRouter returns actual cost data in response headers, those values are used instead.

### Default Pricing (per 1M tokens)

| Model | Input | Output |
|-------|-------|--------|
| `qwen/qwen3.6-flash` | $0.03 | $0.09 |
| `qwen/qwen3.5-flash` | $0.03 | $0.09 |
| `qwen/qwen3-235b-a22b` | $0.10 | $0.30 |
| `openai/gpt-4o-mini` | $0.15 | $0.60 |
| `openai/gpt-4o` | $2.50 | $10.00 |
| `openai/o3-mini` | $1.10 | $4.40 |
| `anthropic/claude-3.5-haiku` | $0.80 | $4.00 |
| `anthropic/claude-3.5-sonnet` | $3.00 | $15.00 |
| `anthropic/claude-sonnet-4-20250514` | $3.00 | $15.00 |
| `google/gemini-2.5-flash` | $0.15 | $0.60 |
| `google/gemini-2.5-pro` | $1.25 | $10.00 |
| `meta-llama/llama-3.3-70b-instruct` | $0.12 | $0.30 |
| `meta-llama/llama-4-maverick` | $0.20 | $0.80 |

Unknown models use a default rate of $0.10/1M input tokens and $0.30/1M output tokens.

## Programmatic API

```python
from nova.cost_tracker import CostTracker

tracker = CostTracker(model="qwen/qwen3.6-flash")

# Add usage from an API response
tracker.add_usage(input_tokens=1000, output_tokens=500)

# Or with explicit costs
tracker.add_usage(
    input_tokens=1000,
    output_tokens=500,
    input_cost=0.00003,
    output_cost=0.000045,
)

# View totals
print(tracker.total.input_tokens)  # 2000
print(tracker.total.output_tokens)  # 1000
print(tracker.total.total_cost)  # 0.00015

# Human-readable summary
print(tracker.format_summary())
# "Tokens: 3,000 total (2,000 in, 1,000 out) | Cost: $0.000150 ($0.000060 in, $0.000090 out)"

# Reset for a new session
tracker.reset()
```

## Extracting Usage from API Responses

```python
from nova.cost_tracker import extract_usage_from_response

response = {
    "usage": {
        "prompt_tokens": 1000,
        "completion_tokens": 500,
        "cost": 0.000075,
    }
}

usage = extract_usage_from_response(response)
# {"input_tokens": 1000, "output_tokens": 500, "output_cost": 0.000075}
```

---

Nested `usage.prompt_tokens_details.cached_tokens` counts are recognized in both
streaming and non-streaming responses. Existing nonzero top-level cache-read
counts take precedence. Cached tokens remain part of the input-token total;
they are not added again. When provider metadata supplies cache pricing, these
counts contribute to the discounted cost estimate. A provider-reported total
cost remains authoritative. Missing or malformed nested cache details are ignored.

## Related Documentation

| Document | Purpose |
|----------|---------|
| [Customizing Nova](GUIDE-003-CUSTOMIZING.md) | Token budget configuration |
| [Hooks](GUIDE-006-HOOKS.md) | `EVENT_POST_LLM_CALL` for per-call cost monitoring |
| [README](../README.md) | Supported models overview |
```
