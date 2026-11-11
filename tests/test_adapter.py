"""The adapter is the only part that touches a vendor format, so it is the part
most likely to break when that format drifts. These tests pin the behaviour
that everything downstream assumes."""

from __future__ import annotations

from blackbox.adapters import claude_code, registry
from blackbox.events import META, TOOL_CALL, TOOL_RESULT


def test_sniffs_claude_code(fixture_path):
    assert claude_code.sniff(fixture_path)


def test_unknown_row_types_degrade_to_meta(fixture_path):
    trace = registry.load(fixture_path)
    raw_types = {e.raw_type for e in trace.events if e.kind == META}
    # An unfamiliar row must not crash the parse and must not be dropped.
    assert "brand-new-row-type" in raw_types
    assert trace.skipped_lines == 0


def test_every_result_is_linked_to_its_call(fixture_path):
    trace = registry.load(fixture_path)
    calls = {e.uid for e in trace.events if e.kind == TOOL_CALL}
    results = [e for e in trace.events if e.kind == TOOL_RESULT]
    assert results
    assert all(e.parent_uid in calls for e in results)


def test_no_dangling_parents(fixture_path):
    """A parent edge pointing at a row that carried tool results must resolve.

    Regression: tool-result rows are represented by synthetic uids, so an
    unresolved edge read as an orphan -- which also split one subagent into
    several branches.
    """
    trace = registry.load(fixture_path)
    uids = {e.uid for e in trace.events}
    dangling = [e for e in trace.events if e.parent_uid and e.parent_uid not in uids]
    assert dangling == []


def test_cache_tiers_are_parsed_apart(fixture_path):
    trace = registry.load(fixture_path)
    with_1h = [e for e in trace.events if e.usage.cache_write_1h_tokens]
    assert with_1h, "fixture should contain 1-hour cache writes"
    ev = with_1h[0]
    assert ev.usage.cache_write_1h_tokens == 5_000
    assert ev.usage.cache_write_5m_tokens == 0
    assert ev.usage.cache_read_tokens == 20_000


def test_total_only_cache_writes_land_in_the_cheaper_tier():
    """An older row reporting only a total must not be priced as a 1-hour write."""
    usage = claude_code._usage(
        {"cache_creation_input_tokens": 1000, "input_tokens": 1, "output_tokens": 2}
    )
    assert usage.cache_write_5m_tokens == 1000
    assert usage.cache_write_1h_tokens == 0


def test_interrupted_calls_are_flagged(fixture_path):
    trace = registry.load(fixture_path)
    interrupted = [e for e in trace.events if "interrupted" in e.flags]
    assert len(interrupted) == 1
