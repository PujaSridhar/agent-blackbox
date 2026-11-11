"""The second adapter.

A canonical model designed against one vendor's logs has not been shown to be
canonical. These tests exist to check the claim: the Codex rollout format
shares no surface detail with Claude Code transcripts, and the graph, the cost
engine and every detector run on it unchanged.
"""

from __future__ import annotations

import pathlib

import pytest

from blackbox.adapters import claude_code, codex, registry
from blackbox.analysis import analyze
from blackbox.detectors.base import Config
from blackbox.events import ASSISTANT_MESSAGE, TOOL_CALL, TOOL_RESULT

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "codex.jsonl"


@pytest.fixture(scope="module")
def codex_path() -> str:
    if not FIXTURE.exists():
        from tests.fixtures import make_codex_fixture

        make_codex_fixture.write(str(FIXTURE))
    return str(FIXTURE)


@pytest.fixture(scope="module")
def codex_analysis(codex_path):
    return analyze(codex_path)


# --- format detection -----------------------------------------------------

def test_the_right_adapter_is_chosen(codex_path, fixture_path):
    assert codex.sniff(codex_path)
    assert not claude_code.sniff(codex_path)
    assert registry.load(codex_path).adapter == "codex"
    # ...and the Claude fixture is still claimed by the Claude adapter.
    assert registry.load(fixture_path).adapter == "claude_code"


# --- the two things that are easy to get wrong ----------------------------

def test_cumulative_usage_is_differenced_not_summed(codex_analysis):
    """Codex reports a running total. The fixture repeats one reading, which
    is what makes naive summing of per-request usage over-count."""
    usage = codex_analysis.cost.total_usage
    # 400 + 300 + 250 + 12 x 80
    assert usage.output_tokens == 1_910
    # 9,000 + 7,500 + 5,000 + 12 x 1,800
    assert usage.cache_read_tokens == 43_100


def test_cached_tokens_are_removed_from_fresh_input(codex_analysis):
    """OpenAI counts cached tokens inside input_tokens; double counting them
    would inflate the fresh-input figure that gets priced at the full rate."""
    usage = codex_analysis.cost.total_usage
    # (12,000-9,000) + (8,000-7,500) + (6,000-5,000) + 12 x (2,000-1,800)
    assert usage.input_tokens == 6_900
    assert usage.cache_write_5m_tokens == 0
    assert usage.cache_write_1h_tokens == 0


def test_a_repeated_reading_adds_nothing(codex_analysis):
    turns = [
        e for e in codex_analysis.graph.events
        if e.kind == ASSISTANT_MESSAGE and not e.usage.is_empty()
    ]
    assert len(turns) == 15  # 16 readings, one of them a duplicate


# --- the canonical model holding up ---------------------------------------

def test_results_link_to_their_calls(codex_analysis):
    calls = [e for e in codex_analysis.graph.events if e.kind == TOOL_CALL]
    results = [e for e in codex_analysis.graph.events if e.kind == TOOL_RESULT]
    assert calls and results
    call_uids = {e.uid for e in calls}
    assert all(r.parent_uid in call_uids for r in results)


def test_one_patch_call_is_many_file_operations(codex_analysis):
    """apply_patch edits several files at once. Flattening it to one path
    would make thrash and contradiction blind on every Codex trace."""
    from blackbox import toolinfo

    patches = [
        e for e in codex_analysis.graph.events
        if e.kind == TOOL_CALL and e.tool_name == "apply_patch"
    ]
    assert patches
    first = toolinfo.file_operations("apply_patch", patches[0].tool_input)
    assert len(first) == 3
    kinds = dict((path.split("/")[-1], kind) for path, kind in first)
    assert kinds["terminal.js"] == toolinfo.WRITE   # *** Add File
    assert kinds["package.json"] == toolinfo.EDIT   # *** Update File


def test_detectors_run_unchanged_on_codex(codex_analysis):
    found = {f.detector for f in codex_analysis.findings}
    assert "retry_loop" in found
    assert "error_cluster" in found
    assert "contradiction" in found


def test_the_planted_retry_loop_is_the_only_one(codex_analysis):
    """Repeated *identical* calls are the loop. The fixture's repeated patches
    differ in body, so they are thrash without also being a loop."""
    loops = [f for f in codex_analysis.findings if f.detector == "retry_loop"]
    assert len(loops) == 1
    assert loops[0].count == 4
    assert loops[0].metrics["tool"] == "exec_command"


def test_repeated_patches_to_one_file_are_thrash(codex_analysis):
    thrash = [f for f in codex_analysis.findings if f.detector == "thrash"]
    assert len(thrash) == 1
    assert thrash[0].metrics["path"].endswith("src/terminal.js")
    # Four updates plus the patch that created it.
    assert thrash[0].metrics["writes"] == 5


def test_exit_codes_mark_failure(codex_analysis):
    assert codex_analysis.summary()["tool_failures"] == 4
    clusters = [f for f in codex_analysis.findings if f.detector == "error_cluster"]
    assert len(clusters) == 1
    assert "TS2345" in clusters[0].metrics["signature"]


def test_a_file_the_patch_created_is_not_a_blind_edit(codex_analysis):
    blind = {
        f.metrics["path"]
        for f in codex_analysis.findings
        if f.metrics.get("kind") == "blind_write"
    }
    # terminal.js was added by a patch, then updated four times.
    assert "/repo/codex-project/src/terminal.js" not in blind
    # package.json was only ever updated in place, never created or read.
    assert "/repo/codex-project/package.json" in blind


# --- compaction -----------------------------------------------------------

def test_compaction_is_recorded(codex_analysis):
    marked = [e for e in codex_analysis.graph.events if "compaction" in e.flags]
    assert len(marked) == 1


def test_context_bloat_stops_at_a_compaction_boundary(codex_path):
    """A result dumped before the context is summarised is not re-sent after
    it, so counting past the boundary would overstate the cost."""
    tuned = analyze(codex_path, config=Config(bloat_min_usd=0.0, bloat_min_tokens=1_000,
                                              ), price_overrides={"gpt-5.4": {"input": 1.0, "output": 8.0}})
    bloat = [f for f in tuned.findings if f.detector == "context_bloat"]
    assert bloat
    finding = max(bloat, key=lambda f: f.metrics["est_tokens"])
    assert finding.metrics["compaction_bounded"] is True
    # Six turns before the boundary, not the twelve that follow the result.
    assert finding.metrics["downstream_turns"] == 6


def test_a_file_read_through_the_shell_is_not_a_blind_edit(codex_analysis):
    """`sed -n '1,200p' file` is a read. Ignoring it reported 84 of 84 Codex
    blind edits wrongly, because this agent inspects files with the shell."""
    blind = {
        f.metrics["path"]
        for f in codex_analysis.findings
        if f.metrics.get("kind") == "blind_write"
    }
    assert "/repo/codex-project/src/components/Viewer.jsx" not in blind
    # The genuine case survives: package.json was never seen by any route.
    assert "/repo/codex-project/package.json" in blind


def test_shell_reads_are_recognised_but_not_treated_as_file_reads():
    """They answer 'has the agent seen this?' only. A shell read returns a
    slice, so hashing it would invent stale-read findings."""
    from blackbox import toolinfo

    found = toolinfo.shell_read_paths("exec_command", {"cmd": "sed -n '1,200p' src/app.js"})
    assert found == ["src/app.js"]
    assert toolinfo.file_operations("exec_command", {"cmd": "cat src/app.js"}) == []

    assert toolinfo.shell_read_paths("exec_command", {"cmd": "rm -rf build"}) == []
    assert toolinfo.shell_read_paths("Bash", {"command": "cd /repo && cat Makefile"}) == ["Makefile"]


def test_relative_and_absolute_paths_are_matched():
    from blackbox.toolinfo import paths_overlap

    assert paths_overlap("/repo/src/app.js", "src/app.js")
    assert paths_overlap("src/app.js", "/repo/src/app.js")
    assert not paths_overlap("/repo/src/app.js", "src/other.js")
    # A shared basename under different directories is still a match only
    # when the trailing components line up.
    assert not paths_overlap("/repo/a/app.js", "/repo/b/app.js")
    assert paths_overlap("/repo/a/app.js", "a/app.js")
