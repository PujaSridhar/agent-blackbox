"""Model pricing, in US dollars per million tokens.

Rates verified against Anthropic's published pricing on 2026-11-10. They are
data, not logic: when prices move, edit this table and nothing else.

Two cache tiers are priced separately because they are billed separately:

* a **cache read** is far cheaper than fresh input -- 0.1x the input rate by
  default, with per-model overrides where Anthropic publishes one;
* a **cache write** costs *more* than fresh input, and a 1-hour write costs
  more than a 5-minute one (2.0x vs 1.25x the input rate).

That last line is why a run can look cheap per token and still be expensive:
rebuilding a long 1-hour-cached prefix is the single most effective way to
burn money without noticing.
"""

from __future__ import annotations

from dataclasses import dataclass

PRICING_AS_OF = "2026-11-10"

CACHE_READ_MULTIPLIER = 0.1
CACHE_WRITE_5M_MULTIPLIER = 1.25
CACHE_WRITE_1H_MULTIPLIER = 2.0


@dataclass(frozen=True)
class Price:
    """Dollars per million tokens for one model."""

    input: float
    output: float
    cache_read: float | None = None
    cache_write_5m: float | None = None
    cache_write_1h: float | None = None

    def read_rate(self) -> float:
        if self.cache_read is not None:
            return self.cache_read
        return self.input * CACHE_READ_MULTIPLIER

    def write_5m_rate(self) -> float:
        if self.cache_write_5m is not None:
            return self.cache_write_5m
        return self.input * CACHE_WRITE_5M_MULTIPLIER

    def write_1h_rate(self) -> float:
        if self.cache_write_1h is not None:
            return self.cache_write_1h
        return self.input * CACHE_WRITE_1H_MULTIPLIER


# Explicit cache-read rates below are the ones Anthropic publishes directly;
# every other model falls back to the 0.1x default.
PRICES: dict[str, Price] = {
    "claude-fable-5-2": Price(input=8.00, output=40.00, cache_read=0.20),
    "claude-mythos-5-2": Price(input=8.00, output=40.00, cache_read=0.20),
    "claude-fable-5-1": Price(input=10.00, output=50.00),
    "claude-mythos-5-1": Price(input=10.00, output=50.00),
    "claude-fable-5": Price(input=10.00, output=50.00),
    "claude-opus-5-5": Price(input=4.00, output=20.00, cache_read=0.20),
    "claude-opus-5": Price(input=5.00, output=25.00),
    "claude-opus-4-8": Price(input=5.00, output=25.00),
    "claude-opus-4-7": Price(input=5.00, output=25.00),
    "claude-opus-4-6": Price(input=5.00, output=25.00),
    "claude-sonnet-5-5": Price(input=2.00, output=10.00, cache_read=0.10),
    "claude-sonnet-5": Price(input=2.00, output=10.00),
    "claude-sonnet-4-6": Price(input=3.00, output=15.00),
    "claude-haiku-5-5": Price(input=0.10, output=0.50),
    "claude-haiku-4-5": Price(input=1.00, output=5.00),
}

# Rows the harness writes for its own bookkeeping. They carry no model call,
# so they cost nothing -- priced at zero rather than reported as unknown.
FREE_MODELS = {"<synthetic>"}


# Rates supplied at runtime, for models this table does not ship. Kept
# separate from PRICES so it is always clear which numbers came from the user
# and which were verified here.
OVERRIDES: dict[str, Price] = {}


def set_overrides(mapping: dict[str, dict[str, float]] | None) -> dict[str, dict[str, float]]:
    """Install user-supplied rates. Returns what was applied, for the record.

    This exists because guessing is not an option. Rates for models outside
    this table -- another vendor's, or one released after it was written --
    have to come from whoever is running the tool, and then travel with the
    stored run so a replay prices it identically.
    """
    OVERRIDES.clear()
    applied: dict[str, dict[str, float]] = {}
    for model, entry in sorted((mapping or {}).items()):
        if not isinstance(entry, dict):
            continue
        try:
            price = Price(
                input=float(entry["input"]),
                output=float(entry["output"]),
                cache_read=_opt(entry.get("cache_read")),
                cache_write_5m=_opt(entry.get("cache_write_5m")),
                cache_write_1h=_opt(entry.get("cache_write_1h")),
            )
        except (KeyError, TypeError, ValueError):
            continue
        OVERRIDES[model] = price
        applied[model] = {
            "input": price.input,
            "output": price.output,
            "cache_read": price.cache_read,
            "cache_write_5m": price.cache_write_5m,
            "cache_write_1h": price.cache_write_1h,
        }
    return applied


def _opt(value) -> float | None:
    return None if value is None else float(value)


def lookup(model: str | None) -> Price | None:
    """Return the price for ``model``, or ``None`` when it is not in the table.

    An unknown model is never guessed at. The report counts its tokens and
    says plainly that part of the bill could not be priced -- a wrong number
    is worse than a missing one.
    """
    if not model:
        return None
    if model in OVERRIDES:
        return OVERRIDES[model]
    if model in FREE_MODELS:
        return Price(input=0.0, output=0.0, cache_read=0.0, cache_write_5m=0.0, cache_write_1h=0.0)
    return PRICES.get(model)
