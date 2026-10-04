"""Model metadata loaded from an OpenAI-compatible provider endpoint."""

from dataclasses import dataclass
from typing import Any

# Conservative fallback when the provider does not report a context window.
# A too-large default silently disables compaction and produces hard API 400s
# on small-context models; 128k is safe for the vast majority of models.
# Users with larger models (e.g. 1M-token Gemini) can raise this per-run via
# the ``llm.context_window`` config override without changing the safe default.
DEFAULT_CONTEXT_WINDOW = 128_000


@dataclass(frozen=True)
class ModelMetadata:
    context_window: int | None = None
    input_price_per_million: float | None = None
    output_price_per_million: float | None = None
    cache_read_price_per_million: float | None = None
    cache_write_price_per_million: float | None = None


_MODEL_METADATA: dict[str, ModelMetadata] = {}


def _value(item: Any, key: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def _price(pricing: Any, name: str) -> float | None:
    try:
        value = float(_value(pricing, name))
        return value * 1_000_000 if value >= 0 else None
    except (TypeError, ValueError):
        return None


def load_provider_metadata(client: Any) -> dict[str, ModelMetadata]:
    """Load model context and pricing metadata from ``client.models.list()``.

    Metadata is optional because some compatible providers do not implement the
    models endpoint or return incomplete records. In either case, callers retain
    safe defaults and the error is intentionally non-fatal.
    """
    _MODEL_METADATA.clear()
    try:
        response = client.models.list()
        records = _value(response, "data", []) or []
    except Exception:
        return {}

    loaded: dict[str, ModelMetadata] = {}
    for record in records:
        model_id = _value(record, "id")
        if not isinstance(model_id, str) or not model_id:
            continue

        context = _value(record, "context_length")
        pricing = _value(record, "pricing", {}) or {}
        try:
            context_value = int(context) if context is not None and int(context) > 0 else None
        except (TypeError, ValueError):
            context_value = None

        loaded[model_id] = ModelMetadata(
            context_window=context_value,
            input_price_per_million=_price(pricing, "prompt"),
            output_price_per_million=_price(pricing, "completion"),
            cache_read_price_per_million=_price(pricing, "cache_read"),
            cache_write_price_per_million=_price(pricing, "cache_write"),
        )

    _MODEL_METADATA.update(loaded)
    return loaded


def get_model_metadata(model: str) -> ModelMetadata | None:
    """Return exact or partial-match metadata for a model identifier.

    Matching order: exact id, then the segment after the last ``/`` (provider
    prefix stripped), then the longest substring match. Longest-match avoids a
    short id like ``gpt-4`` incorrectly resolving to ``gpt-4o-mini``.
    """
    if model in _MODEL_METADATA:
        return _MODEL_METADATA[model]

    model_lower = model.lower()
    suffix = model_lower.rsplit("/", 1)[-1]

    best_key: str | None = None
    for key in _MODEL_METADATA:
        key_lower = key.lower()
        if key_lower.rsplit("/", 1)[-1] == suffix:
            return _MODEL_METADATA[key]
        if (key_lower in model_lower or model_lower in key_lower) and (
            best_key is None or len(key_lower) > len(best_key.lower())
        ):
            best_key = key
    return _MODEL_METADATA[best_key] if best_key is not None else None


def get_model_context_window(model: str, override: int | None = None) -> int:
    """Return the effective context window for a model.

    Priority: an explicit ``override`` (from ``llm.context_window`` config),
    then provider-reported metadata, then ``DEFAULT_CONTEXT_WINDOW``. A
    positive override wins so users can opt into large windows (e.g. 1M)
    even when the provider reports a smaller or missing value.
    """
    if override and override > 0:
        return override
    metadata = get_model_metadata(model)
    return (
        metadata.context_window if metadata and metadata.context_window else DEFAULT_CONTEXT_WINDOW
    )
