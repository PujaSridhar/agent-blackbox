"""Each detector has a case planted for it in the synthetic fixture, plus the
false positives that matter most: an interrupted call must not read as a stall,
an overnight pause must not read as a stall, and creating a new file must not
read as a blind edit."""

from __future__ import annotations

from blackbox import detectors
from blackbox.detectors.base import Config
from blackbox.detectors.error_clusters import signature


def _of(analysis, detector):
    return [f for f in analysis.findings if f.detector == detector]


# --- retry loops ----------------------------------------------------------

def test_retry_loop_is_detected(analysis):
    found = _of(analysis, "retry_loop")
    assert len(found) == 1
    assert found[0].count == 4
    assert found[0].metrics["tool"] == "Bash"


def test_identical_calls_far_apart_are_not_a_loop():
    """Running the same test command at the start and end of a long session is
    normal work, not a loop. Only repeats inside the window count."""
    from blackbox.detectors import retry_loops
    from blackbox.events import TOOL_CALL, Event
    from blackbox.graph import build

    events = []
    for seq in range(40):
        command = "bun test" if seq in (0, 20, 39) else f"echo step{seq}"
        events.append(
            Event(
                uid=f"c{seq}",
                seq=seq,
                kind=TOOL_CALL,
                tool_name="Bash",
                tool_input={"command": command},
            )
        )

    graph = build(events)
    assert retry_loops.run(graph, Config()) == []

    # Tighten nothing but the window, and the same three calls do cluster.
    assert retry_loops.run(graph, Config(retry_window=40))


def test_description_differences_do_not_hide_a_repeat():
    """Two calls doing the same work must collapse even if their prose differs."""
    from blackbox.events import TOOL_CALL, Event

    a = Event(uid="a", seq=0, kind=TOOL_CALL, tool_input={"command": "ls", "description": "one"})
    b = Event(uid="b", seq=1, kind=TOOL_CALL, tool_input={"command": "ls", "description": "two"})
    assert a.input_digest() == b.input_digest()


# --- stalls ---------------------------------------------------------------

def test_stall_is_detected(analysis):
    stalls = [f for f in _of(analysis, "stall") if f.metrics["kind"] == "stall"]
    assert len(stalls) == 1
    assert 239 < stalls[0].metrics["seconds"] < 241


def test_long_pause_is_an_idle_gap_not_a_stall(analysis):
    idle = [f for f in _of(analysis, "stall") if f.metrics["kind"] == "idle_gap"]
    assert len(idle) == 1
    assert idle[0].metrics["seconds"] > 3600
    assert idle[0].severity == "low"


def test_interrupted_call_is_never_reported(analysis):
    """The fixture interrupts a one-hour call; reporting it would be a lie."""
    reported = {round(f.metrics["seconds"]) for f in _of(analysis, "stall")}
    assert 3600 not in reported


# --- error clusters -------------------------------------------------------

def test_errors_cluster_by_normalised_shape(analysis):
    edit_clusters = [
        f for f in _of(analysis, "error_cluster") if f.metrics["tool"] == "Edit"
    ]
    assert len(edit_clusters) == 1, "three failures differing only by path are one problem"
    assert edit_clusters[0].count == 3


def test_signature_strips_volatile_detail():
    a = signature("could not open /repo/src/a/b.py at line 42 (id ab12cd34ef)")
    b = signature("could not open /repo/src/x/y.py at line 99 (id ff99aa11bb)")
    assert a == b


def test_signature_keeps_distinct_errors_distinct():
    assert signature("permission denied") != signature("connection refused")


# --- contradictions -------------------------------------------------------

def test_stale_read_is_detected(analysis):
    stale = [f for f in _of(analysis, "contradiction") if f.metrics["kind"] == "stale_read"]
    assert len(stale) == 1
    assert stale[0].severity == "high"
    assert stale[0].metrics["path"].endswith("auth.py")


def test_blind_in_place_edit_is_detected(analysis):
    blind = {
        f.metrics["path"]
        for f in _of(analysis, "contradiction")
        if f.metrics["kind"] == "blind_write"
    }
    assert "/repo/src/untouched.py" in blind


def test_creating_a_new_file_is_not_a_blind_edit(analysis):
    """thrashed.py is only ever written, never edited in place."""
    blind = {
        f.metrics["path"]
        for f in _of(analysis, "contradiction")
        if f.metrics["kind"] == "blind_write"
    }
    assert "/repo/src/thrashed.py" not in blind


# --- thrash ---------------------------------------------------------------

def test_thrash_is_detected(analysis):
    thrash = _of(analysis, "thrash")
    assert len(thrash) == 1
    assert thrash[0].metrics["path"] == "/repo/src/thrashed.py"
    assert thrash[0].metrics["writes"] == 5


def test_thrash_threshold_is_configurable(analysis, fixture_path):
    from blackbox.analysis import analyze

    assert _of(analyze(fixture_path, config=Config(thrash_min_edits=99)), "thrash") == []


# --- ordering -------------------------------------------------------------

def test_findings_are_ordered_severity_first(analysis):
    ranks = [f.rank for f in analysis.findings]
    assert ranks == sorted(ranks)


def test_registry_order_does_not_affect_output(analysis, monkeypatch, fixture_path):
    """Adding or reordering a detector must not reshuffle the report."""
    from blackbox.analysis import analyze

    reordered = tuple(reversed(detectors.DETECTORS))
    monkeypatch.setattr(detectors, "DETECTORS", reordered)
    again = analyze(fixture_path)
    assert again.findings_digest() == analysis.findings_digest()


# --- polling vs retrying --------------------------------------------------

def test_repeated_observation_is_polling_not_a_retry_loop():
    """Taking the same screenshot eight times is polling. Calling it a retry
    loop was the biggest false positive when first run on real traces."""
    from blackbox.detectors import retry_loops
    from blackbox.events import TOOL_CALL, Event
    from blackbox.graph import build

    events = [
        Event(
            uid=f"s{i}",
            seq=i,
            kind=TOOL_CALL,
            tool_name="mcp__Claude_Browser__computer",
            tool_input={"action": "screenshot"},
        )
        for i in range(8)
    ]
    found = retry_loops.run(build(events), Config())
    assert len(found) == 1
    assert found[0].metrics["kind"] == "expected_repeat"
    assert found[0].severity == "low"
    assert "expected by design" in found[0].detail


def test_repeated_action_is_still_a_retry_loop():
    from blackbox.detectors import retry_loops
    from blackbox.events import TOOL_CALL, Event
    from blackbox.graph import build

    events = [
        Event(
            uid=f"c{i}",
            seq=i,
            kind=TOOL_CALL,
            tool_name="mcp__Claude_Browser__computer",
            tool_input={"action": "left_click", "coordinate": [10, 20]},
        )
        for i in range(8)
    ]
    found = retry_loops.run(build(events), Config())
    assert len(found) == 1
    assert found[0].metrics["kind"] == "retry_loop"
    assert found[0].severity == "high"


def test_plain_file_reads_are_polling_not_retries():
    from blackbox.toolinfo import repeats_by_design

    assert repeats_by_design("Read", {"file_path": "/a.py"})
    assert repeats_by_design("mcp__terminal__read_terminal", {})
    assert not repeats_by_design("Edit", {"file_path": "/a.py"})
    assert not repeats_by_design("Bash", {"command": "rm -rf build"})


def test_progress_bookkeeping_is_not_a_retry_loop():
    """Advancing a task list with the same payload is the pointer moving, not
    the agent repeating itself."""
    from blackbox.toolinfo import repeats_by_design

    assert repeats_by_design("mcp__ccd_turn__update_plan", {"advance": True})
    assert repeats_by_design("TodoWrite", {})



# --- regressions: every one of these was a false positive on a real trace ---

def test_write_then_edit_is_not_a_blind_edit(analysis):
    """An agent that authored a file knows its contents as surely as if it had
    read them. All 17 blind-edit findings on the first real trace were this."""
    blind = {
        f.metrics["path"]
        for f in _of(analysis, "contradiction")
        if f.metrics["kind"] == "blind_write"
    }
    assert "/repo/src/authored.py" not in blind


def test_paging_through_a_file_is_not_a_stale_read(analysis):
    """Read takes offset/limit. Two regions of an unchanged file return
    different bytes; keyed by path alone that reads as tampering."""
    stale_paths = {
        f.metrics["path"]
        for f in _of(analysis, "contradiction")
        if f.metrics["kind"] == "stale_read"
    }
    assert "/repo/src/paged.py" not in stale_paths


def test_same_region_reread_is_still_a_stale_read(analysis):
    """The narrowing must not cost the real signal."""
    stale = [f for f in _of(analysis, "contradiction") if f.metrics["kind"] == "stale_read"]
    assert len(stale) == 1
    assert stale[0].metrics["path"].endswith("auth.py")
    assert stale[0].metrics["region_offset"] is None


def test_exit_code_header_is_not_an_error_signature(analysis):
    """Shell failures arrive as 'Exit code 1' then the real output. Taking line
    one collapsed seven unrelated failures into a single useless cluster."""
    from blackbox.detectors.error_clusters import signature

    sig = signature("Exit code 1\n=== test session starts ===\nAssertionError: expected 3")
    assert "exit code" not in sig.lower()
    assert "AssertionError" in sig

    assert signature("Exit code 1") == "Exit code <n>"  # nothing else to go on


def test_different_programs_do_not_share_a_cluster(analysis):
    """A pytest failure and an npm failure are different problems even when
    their normalised text would otherwise match."""
    labels = {f.metrics["tool"] for f in _of(analysis, "error_cluster")}
    assert "Bash(python pytest)" in labels
    assert "Bash(npm build)" in labels


def test_waiting_for_a_human_is_not_a_stall(analysis):
    """AskUserQuestion exists to wait for a person. Timing their typing and
    calling it an agent stall is noise."""
    tools = {f.metrics["tool"] for f in _of(analysis, "stall")}
    assert "AskUserQuestion" not in tools


def test_declined_tool_call_is_not_a_failure(analysis):
    """A human refusing a call is not the tool breaking."""
    assert analysis.summary()["tool_failures"] == 7  # 3 Edit + 2 pytest + 2 npm, no decline
    for finding in _of(analysis, "error_cluster"):
        assert "rm -rf" not in finding.detail


def test_declines_are_flagged_on_the_event(analysis):
    declined = [e for e in analysis.graph.events if "declined" in e.flags]
    assert len(declined) == 1


def test_spread_out_writes_are_not_thrash(analysis):
    """Four writes to README.md across a long session is iteration, not thrash."""
    paths = {f.metrics["path"] for f in _of(analysis, "thrash")}
    assert "/repo/README.md" not in paths


def test_dense_writes_are_still_thrash(analysis):
    thrash = _of(analysis, "thrash")
    assert len(thrash) == 1
    assert thrash[0].metrics["path"] == "/repo/src/thrashed.py"
    assert thrash[0].metrics["window_span"] <= Config().thrash_window


def test_program_hint_sees_past_shell_plumbing():
    from blackbox.toolinfo import program_hint

    assert program_hint("Bash", {"command": "cd /repo && python -m pytest -q"}) == "python pytest"
    assert program_hint("Bash", {"command": "cd a && cd b && ./deploy.sh"}) == "deploy.sh"
    assert program_hint("Edit", {"file_path": "/a.py"}) == ""


# --- context bloat --------------------------------------------------------

def test_context_bloat_is_detected_with_a_tuned_threshold(fixture_path):
    """The fixture's run is deliberately cheap, so the default dollar floor
    does not apply. The mechanism is what is under test here."""
    from blackbox.analysis import analyze

    tuned = analyze(fixture_path, config=Config(bloat_min_usd=0.01, bloat_min_tokens=2_000))
    bloat = [f for f in tuned.findings if f.detector == "context_bloat"]
    assert len(bloat) == 1
    finding = bloat[0]
    assert finding.metrics["est_tokens"] > 2_000
    assert finding.metrics["downstream_turns"] > 10
    assert finding.metrics["estimated"] is True


def test_context_bloat_respects_the_dollar_floor(analysis):
    """At default thresholds a cheap run reports nothing, rather than noise."""
    assert [f for f in analysis.findings if f.detector == "context_bloat"] == []


def test_context_bloat_is_silent_without_a_priced_model(fixture_path, monkeypatch):
    """No rate means no honest dollar figure, so the detector says nothing."""
    from blackbox.detectors import context_bloat

    monkeypatch.setattr(context_bloat, "lookup", lambda model: None)
    from blackbox.adapters import registry
    from blackbox.graph import build

    graph = build(registry.load(fixture_path).events)
    assert context_bloat.run(graph, Config(bloat_min_usd=0.0, bloat_min_tokens=0)) == []


def test_six_detectors_are_registered():
    names = {m.NAME for m in detectors.DETECTORS}
    assert names == {
        "retry_loop",
        "stall",
        "error_cluster",
        "contradiction",
        "thrash",
        "context_bloat",
    }
