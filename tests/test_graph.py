from __future__ import annotations

from blackbox import graph as graph_mod
from blackbox.adapters import registry
from blackbox.events import MAIN_BRANCH, TOOL_CALL


def test_subagent_work_is_one_branch(analysis):
    names = [b.name for b in analysis.graph.branches]
    assert names[0] == MAIN_BRANCH
    assert names.count("subagent-1") == 1
    # Three sidechain tool calls, one branch -- not three.
    assert sum(1 for b in analysis.graph.branches if not b.is_main) == 1
    assert analysis.graph.branch_named("subagent-1").tool_calls == 3


def test_no_orphans_in_a_self_contained_trace(analysis):
    assert analysis.graph.orphans == 0


def test_result_for_resolves_the_matching_call(analysis):
    calls = analysis.graph.tool_calls()
    assert calls
    for call in calls:
        result = analysis.graph.result_for(call)
        if result is not None:
            assert result.tool_use_id == call.tool_use_id


def test_branch_names_are_stable_across_rebuilds(fixture_path):
    events = registry.load(fixture_path).events
    first = {e.uid: e.branch for e in graph_mod.build(events).events}
    second = {e.uid: e.branch for e in graph_mod.build(list(reversed(events))).events}
    # build() sorts by seq, so input order cannot change the labelling.
    assert first == second


def test_build_does_not_mutate_its_input(fixture_path):
    events = registry.load(fixture_path).events
    before = [e.branch for e in events]
    graph_mod.build(events)
    assert [e.branch for e in events] == before
