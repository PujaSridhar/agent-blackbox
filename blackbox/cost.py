"""Cost attribution.

Tokens are counted per model and per branch, then priced. Three things this
does that a flat token total does not:

* **Separates the cache tiers.** A cache read is ~10x cheaper than fresh
  input; a cache write is *more* expensive, and a 1-hour write more again.
* **Attributes to branches.** Subagent spend is reported as its own line, so
  the main thread's cost is the main thread's cost.
* **Refuses to guess.** Tokens on a model missing from the price table are
  counted and reported as unpriced. The total then carries an explicit
  "at least" marker rather than a confident wrong number.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from blackbox.events import MAIN_BRANCH, Event, Usage
from blackbox.pricing import PRICING_AS_OF, lookup


@dataclass
class ModelCost:
    model: str
    usage: Usage
    calls: int = 0
    priced: bool = True

    input_usd: float = 0.0
    output_usd: float = 0.0
    cache_read_usd: float = 0.0
    cache_write_usd: float = 0.0

    @property
    def total_usd(self) -> float:
        return self.input_usd + self.output_usd + self.cache_read_usd + self.cache_write_usd


@dataclass
class CostReport:
    by_model: list[ModelCost] = field(default_factory=list)
    by_branch: dict[str, float] = field(default_factory=dict)
    tokens_by_branch: dict[str, Usage] = field(default_factory=dict)
    total_usd: float = 0.0
    unpriced_models: list[str] = field(default_factory=list)
    unpriced_tokens: int = 0
    pricing_as_of: str = PRICING_AS_OF

    @property
    def is_complete(self) -> bool:
        return not self.unpriced_models

    @property
    def total_usage(self) -> Usage:
        total = Usage()
        for mc in self.by_model:
            total = total + mc.usage
        return total

    @property
    def cache_write_1h_usd_share(self) -> float:
        """Fraction of spend going to 1-hour cache writes.

        High values mean a long cached prefix is being rebuilt repeatedly --
        the most common way an agent run costs more than it looks like it should.
        """
        if self.total_usd <= 0:
            return 0.0
        spend = 0.0
        for mc in self.by_model:
            price = lookup(mc.model)
            if price is None:
                continue
            spend += mc.usage.cache_write_1h_tokens / 1e6 * price.write_1h_rate()
        return spend / self.total_usd


def _price_model(model: str, usage: Usage, calls: int) -> ModelCost:
    price = lookup(model)
    if price is None:
        return ModelCost(model=model, usage=usage, calls=calls, priced=False)
    return ModelCost(
        model=model,
        usage=usage,
        calls=calls,
        priced=True,
        input_usd=usage.input_tokens / 1e6 * price.input,
        output_usd=usage.output_tokens / 1e6 * price.output,
        cache_read_usd=usage.cache_read_tokens / 1e6 * price.read_rate(),
        cache_write_usd=(
            usage.cache_write_5m_tokens / 1e6 * price.write_5m_rate()
            + usage.cache_write_1h_tokens / 1e6 * price.write_1h_rate()
        ),
    )


def compute(events: list[Event]) -> CostReport:
    per_model: dict[str, Usage] = {}
    per_model_calls: dict[str, int] = {}
    per_branch_usage: dict[str, Usage] = {}
    per_branch_usd: dict[str, float] = {}

    for ev in events:
        if ev.usage.is_empty():
            continue
        model = ev.model or "unknown"
        per_model[model] = per_model.get(model, Usage()) + ev.usage
        per_model_calls[model] = per_model_calls.get(model, 0) + 1

        branch = ev.branch or MAIN_BRANCH
        per_branch_usage[branch] = per_branch_usage.get(branch, Usage()) + ev.usage
        single = _price_model(model, ev.usage, 1)
        per_branch_usd[branch] = per_branch_usd.get(branch, 0.0) + single.total_usd

    by_model = [
        _price_model(model, per_model[model], per_model_calls[model])
        for model in sorted(per_model)
    ]

    unpriced = [mc.model for mc in by_model if not mc.priced]
    unpriced_tokens = sum(mc.usage.total_tokens for mc in by_model if not mc.priced)

    return CostReport(
        by_model=by_model,
        by_branch=dict(sorted(per_branch_usd.items())),
        tokens_by_branch=dict(sorted(per_branch_usage.items())),
        total_usd=sum(mc.total_usd for mc in by_model),
        unpriced_models=unpriced,
        unpriced_tokens=unpriced_tokens,
    )
