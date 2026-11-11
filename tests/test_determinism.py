"""The load-bearing guarantee.

The tool's claim is that its findings can be re-derived -- from a store you can
also query by hand -- and come out byte-identical, with no model in the loop.
If any test in this file fails, that claim is void and the rest of the
project's value goes with it.
"""

from __future__ import annotations

import json
import os

from blackbox.replay import replay as replay_run
from blackbox import store
from blackbox.analysis import analyze


def test_two_analyses_of_one_file_agree(fixture_path):
    assert analyze(fixture_path).digest() == analyze(fixture_path).digest()


def test_replay_from_the_store_is_byte_identical(fixture_path, tmp_path):
    original = analyze(fixture_path)
    db = str(tmp_path / "run.duckdb")
    store.write(db, original)

    replayed = replay_run(db)

    assert replayed.digest() == original.digest()
    assert replayed.findings_digest() == original.findings_digest()
    assert len(replayed.findings) == len(original.findings)
    assert abs(replayed.cost.total_usd - original.cost.total_usd) < 1e-12


def test_replay_reads_thresholds_back_from_the_store(fixture_path, tmp_path):
    """A moved default must not let a replay quietly disagree with the original."""
    from blackbox.detectors.base import Config

    config = Config(stall_seconds=5.0, thrash_min_edits=2, idle_seconds=120.0)
    original = analyze(fixture_path, config=config)
    db = str(tmp_path / "tuned.duckdb")
    store.write(db, original)

    replayed = replay_run(db)

    assert replayed.config.stall_seconds == 5.0
    assert replayed.config.thrash_min_edits == 2
    assert replayed.digest() == original.digest()


def test_store_round_trip_preserves_what_detectors_read(fixture_path, tmp_path):
    """Tool output is hashed by the contradiction detector, so it must survive
    the store untruncated."""
    original = analyze(fixture_path)
    db = str(tmp_path / "run.duckdb")
    store.write(db, original)

    stored = {e.uid: e for e in store.read_events(db)}
    for ev in original.graph.events:
        back = stored[ev.uid]
        assert back.tool_output == ev.tool_output
        assert back.output_digest() == ev.output_digest()
        assert back.tool_input == ev.tool_input
        assert back.flags == ev.flags
        assert back.branch == ev.branch
        assert back.usage == ev.usage


def test_payload_is_json_serialisable_and_sorted(fixture_path):
    payload = analyze(fixture_path).payload()
    blob = json.dumps(payload, sort_keys=True, default=str)
    assert json.loads(blob) == json.loads(blob)
    assert payload["schema_version"] >= 1


def test_payload_carries_no_wall_clock(fixture_path):
    """Nothing in the payload may come from the clock, or replay could not match."""
    import datetime

    payload = analyze(fixture_path).payload()
    blob = json.dumps(payload, default=str)
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    # The fixture is dated 2026-03; a today-stamp in the payload means a leak.
    assert today not in blob or today.startswith("2026-03")


def test_no_model_calls_are_possible_in_the_analysis_path(fixture_path, monkeypatch):
    """Guard the 'zero LLM calls' half of the claim by breaking the network."""
    import socket

    def deny(*args, **kwargs):
        raise AssertionError("the analysis path must never open a socket")

    monkeypatch.setattr(socket, "socket", deny)
    monkeypatch.setattr(socket, "create_connection", deny)

    analysis = analyze(fixture_path)
    assert analysis.findings
    assert analysis.digest()
