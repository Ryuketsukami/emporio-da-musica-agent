"""Dollar cost of model calls, from the per-million-token prices in config/pricing.toml."""

import dataclasses
import tomllib
from pathlib import Path

from src.config import PRICING_PATH

RATES = ("input", "output", "cache_write", "cache_read")


def load_prices(path: Path = PRICING_PATH) -> dict[str, dict[str, float]]:
    with path.open("rb") as f:
        table = tomllib.load(f)
    for model, entry in table.items():
        missing = [rate for rate in RATES if rate not in entry]
        if missing:
            raise ValueError(f"{path.name}: {model} has no {', '.join(missing)}")
    return {model: {rate: float(entry[rate]) for rate in RATES} for model, entry in table.items()}


def call_cost(model: str | None, usage, prices: dict[str, dict[str, float]]) -> float | None:
    """US dollars for one call, or None when the model has no price entry."""
    if model is None:
        return None
    entry = prices.get(model) or next((p for name, p in prices.items() if model.startswith(name + "-")), None)
    if entry is None:
        return None
    return sum(getattr(usage, rate) * entry[rate] for rate in RATES) / 1_000_000


def add_usage(a, b):
    return dataclasses.replace(a, **{rate: getattr(a, rate) + getattr(b, rate) for rate in RATES})


def add_cost(a: float | None, b: float | None) -> float | None:
    """A sum with an unpriced part is unknown, not low."""
    return None if a is None or b is None else a + b
