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
    clusters = _of(analysis, "error_cluster")
    assert len(clusters) == 1, "three failures differing only by path are one problem"
    assert clusters[0].count == 3


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

def test_all_five_detectors_are_registered():
    names = {m.NAME for m in detectors.DETECTORS}
    assert names == {"retry_loop", "stall", "error_cluster", "contradiction", "thrash"}


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
    assert found[0].metrics["kind"] == "polling"
    assert found[0].severity == "low"
    assert "polling, not a retry loop" in found[0].detail


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
    from blackbox.toolinfo import is_observation

    assert is_observation("Read", {"file_path": "/a.py"})
    assert is_observation("mcp__terminal__read_terminal", {})
    assert not is_observation("Edit", {"file_path": "/a.py"})
    assert not is_observation("Bash", {"command": "rm -rf build"})
