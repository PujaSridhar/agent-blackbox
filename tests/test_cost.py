from __future__ import annotations

from blackbox.events import Usage
from blackbox.pricing import lookup


def test_cache_write_tiers_are_priced_differently():
    price = lookup("claude-opus-5")
    assert price.write_1h_rate() > price.write_5m_rate() > price.input
    assert price.write_5m_rate() == price.input * 1.25
    assert price.write_1h_rate() == price.input * 2.0


def test_cache_read_is_far_cheaper_than_input():
    price = lookup("claude-opus-5")
    assert price.read_rate() == price.input * 0.1


def test_published_cache_read_overrides_the_default():
    # Opus 5.5 publishes $0.20, which is not 0.1x of its $4.00 input rate.
    price = lookup("claude-opus-5-5")
    assert price.read_rate() == 0.20
    assert price.read_rate() != price.input * 0.1


def test_unknown_model_is_not_guessed():
    assert lookup("some-future-model-9") is None


def test_unpriced_tokens_make_the_total_a_floor(analysis):
    cost = analysis.cost
    assert not cost.is_complete
    assert "some-future-model-9" in cost.unpriced_models
    assert cost.unpriced_tokens > 0


def test_spend_is_attributed_per_branch(analysis):
    by_branch = analysis.cost.by_branch
    assert "main" in by_branch and "subagent-1" in by_branch
    # The subagent ran on a cheaper model: its spend is real but small, and it
    # must not be folded into the main thread.
    assert by_branch["subagent-1"] > 0
    assert by_branch["main"] > by_branch["subagent-1"]
    assert abs(sum(by_branch.values()) - analysis.cost.total_usd) < 1e-9


def test_one_hour_cache_share_is_reported(analysis):
    assert 0.0 < analysis.cost.cache_write_1h_usd_share <= 1.0


def test_usage_adds_componentwise():
    a = Usage(input_tokens=1, output_tokens=2, cache_read_tokens=3, cache_write_1h_tokens=4)
    b = Usage(input_tokens=10, output_tokens=20, cache_read_tokens=30, cache_write_1h_tokens=40)
    total = a + b
    assert (total.input_tokens, total.output_tokens) == (11, 22)
    assert total.cache_read_tokens == 33
    assert total.cache_write_1h_tokens == 44
