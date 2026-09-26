"""List-price cost of recorded usage. These are published prices, never bills."""

PRICE_SOURCE = "Vercel AI Gateway GET /v1/models list prices, fetched 2026-09-24/25 (<=272K-token tier)"

# USD per token.
PRICES = {
    "gpt-6-sol": {"input": 2e-6, "cache_read": 2e-7, "cache_write": 2.5e-6, "output": 1e-5},
    "gpt-6-luna": {"input": 1e-7, "cache_read": 1e-8, "cache_write": 1.25e-7, "output": 5e-7},
    "gpt-6-astra": {"input": 1e-5, "cache_read": 1e-6, "cache_write": 1.25e-5, "output": 5e-5},
    "claude-opus-5-5": {"input": 4e-6, "cache_read": 2e-7, "cache_write": 5e-6, "output": 2e-5},
    "claude-opus-5": {"input": 5e-6, "cache_read": 5e-7, "cache_write": 6.25e-6, "output": 2.5e-5},
    "claude-sonnet-5": {"input": 2e-6, "cache_read": 2e-7, "cache_write": 2.5e-6, "output": 1e-5},
    "claude-fable-5-1": {"input": 1e-5, "cache_read": 2.5e-7, "cache_write": 1.25e-5, "output": 5e-5},
    "claude-opus-4-8": {"input": 5e-6, "cache_read": 5e-7, "cache_write": 6.25e-6, "output": 2.5e-5},
    "claude-sonnet-4-6": {"input": 3e-6, "cache_read": 3e-7, "cache_write": 3.75e-6, "output": 1.5e-5},
    "claude-haiku-4-5": {"input": 1e-6, "cache_read": 1e-7, "cache_write": 1.25e-6, "output": 5e-6},
    "jev": {"input": 4.2e-8, "output": 0.0},
}

# Pi usage field -> price field. Pi reports reasoning inside `output`; never add it twice.
PI_FIELDS = {"input": "input", "cacheRead": "cache_read", "cacheWrite": "cache_write", "output": "output"}


def model_key(model, prices=PRICES):
    name = (model or "").lower().rsplit("/", 1)[-1]
    name = name.removeprefix("system.ai.").replace(".", "-")
    if name not in prices:
        raise KeyError(f"No list price for model {model!r}")
    return name


def usage_cost(usage, model, prices=PRICES):
    price = prices[model_key(model, prices)]
    return sum((usage.get(field) or 0) * price[key] for field, key in PI_FIELDS.items())


def jev_cost(usages, prices=PRICES):
    price = prices["jev"]
    return sum((u.get("input_tokens", u.get("inputTokens")) or 0) * price["input"]
               + (u.get("output_tokens", u.get("outputTokens")) or 0) * price["output"] for u in usages)


def cache_multipliers(model=None, prices=PRICES):
    """(read, write) prices relative to uncached input; Anthropic/OpenAI ratios when unknown."""
    try:
        price = prices[model_key(model, prices)]
        return price["cache_read"] / price["input"], price["cache_write"] / price["input"]
    except KeyError:
        return 0.1, 1.25
