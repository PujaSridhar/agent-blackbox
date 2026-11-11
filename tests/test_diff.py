"""Comparing two sets of runs.

The point of a diff is to answer "did that change help" without anyone having
to trust a feeling. These tests pin the two things that would make it
untrustworthy: a verdict that points the wrong way, and uneven run counts
being compared as if they were even.
"""

from __future__ import annotations

import json
import shutil

import pytest

from blackbox import aggregate, diff
from blackbox.cli import main


@pytest.fixture
def two_sides(tmp_path, fixture_path):
    before = tmp_path / "before"
    after = tmp_path / "after"
    before.mkdir()
    after.mkdir()
    shutil.copy(fixture_path, before / "run.jsonl")
    shutil.copy(fixture_path, after / "run.jsonl")
    return str(before), str(after)


def _row(result, key):
    return next(r for r in result["metrics"] if r["key"] == key)


def test_identical_sides_show_no_change(two_sides):
    before, after = two_sides
    result = diff.compare(
        aggregate.load([f"{before}/run.jsonl"]), aggregate.load([f"{after}/run.jsonl"])
    )
    for row in result["metrics"]:
        assert row["delta"] == pytest.approx(0.0)
        assert row["verdict"] == ""
    assert not result["uneven"]


def test_verdicts_follow_the_direction_that_is_actually_better():
    """Cost going down is better; cache hit rate going down is worse. A diff
    that gets this backwards is worse than no diff."""
    cost = diff.Metric("total_usd", "cost", diff.USD, diff.LOWER)
    cache = diff.Metric("cache_hit_rate", "cache", diff.RATE, diff.HIGHER)
    calls = diff.Metric("tool_calls", "calls", diff.COUNT, diff.NEUTRAL)

    assert diff._verdict(cost, -1.0) == "better"
    assert diff._verdict(cost, +1.0) == "worse"
    assert diff._verdict(cache, +0.1) == "better"
    assert diff._verdict(cache, -0.1) == "worse"
    assert diff._verdict(calls, +100) == ""
    assert diff._verdict(cost, 0.0) == ""


def test_uneven_run_counts_are_judged_per_run(tmp_path, fixture_path):
    """Three runs cost more than one in total and exactly the same per run.
    Calling that a regression would make the tool useless for the case it
    exists for: one run before a change, several after."""
    before = tmp_path / "before"
    after = tmp_path / "after"
    before.mkdir()
    after.mkdir()
    shutil.copy(fixture_path, before / "a.jsonl")
    for name in ("a.jsonl", "b.jsonl", "c.jsonl"):
        shutil.copy(fixture_path, after / name)

    result = diff.compare(
        aggregate.load([str(before / "a.jsonl")]),
        aggregate.load([str(after / n) for n in ("a.jsonl", "b.jsonl", "c.jsonl")]),
    )

    assert result["uneven"]
    cost = _row(result, "total_usd")
    assert cost["delta"] > 0                      # three runs cost more in total
    assert cost["delta_per_run"] == pytest.approx(0.0)
    assert cost["verdict"] == ""                  # ...but nothing got worse


def test_detector_counts_are_compared(two_sides, fixture_path, tmp_path):
    before, after = two_sides
    result = diff.compare(
        aggregate.load([f"{before}/run.jsonl"]), aggregate.load([f"{after}/run.jsonl"])
    )
    names = {row["detector"] for row in result["by_detector"]}
    assert "retry_loop" in names and "thrash" in names
    assert all(row["delta"] == 0 for row in result["by_detector"])


def test_diff_is_deterministic(two_sides):
    before, after = two_sides
    first = diff.compare(
        aggregate.load([f"{before}/run.jsonl"]), aggregate.load([f"{after}/run.jsonl"])
    )
    second = diff.compare(
        aggregate.load([f"{before}/run.jsonl"]), aggregate.load([f"{after}/run.jsonl"])
    )
    assert json.dumps(first, sort_keys=True, default=str) == json.dumps(
        second, sort_keys=True, default=str
    )


# --- through the CLI ------------------------------------------------------

def test_cli_diff_renders(two_sides, capsys):
    before, after = two_sides
    assert main(["diff", before, after]) == 0
    out = capsys.readouterr().out
    assert "diff" in out
    assert "cost" in out
    assert "FINDINGS BY TYPE" in out


def test_cli_diff_across_formats(fixture_path, capsys, tmp_path):
    """A Claude Code run against a Codex run: different vendors, one model."""
    codex = tmp_path / "codex.jsonl"
    from tests.fixtures import make_codex_fixture

    make_codex_fixture.write(str(codex))

    assert main(["diff", fixture_path, str(codex)]) == 0
    out = capsys.readouterr().out
    assert "cost" in out
    # Codex rates are not shipped, so the comparison says so rather than
    # quietly reporting a drop to zero.
    assert "no price available" in out


def test_cli_diff_needs_two_sides(fixture_path, capsys):
    assert main(["diff", fixture_path]) == 2
    assert "usage: blackbox diff" in capsys.readouterr().err


def test_cli_diff_missing_side_is_an_error(fixture_path, capsys, tmp_path):
    assert main(["diff", fixture_path, str(tmp_path / "nothing")]) == 1
    assert "no traces found" in capsys.readouterr().err


def test_cli_diff_json(two_sides, tmp_path, capsys):
    before, after = two_sides
    out = tmp_path / "diff.json"
    assert main(["diff", before, after, "--quiet", "--json", str(out)]) == 0
    payload = json.loads(out.read_text())
    assert payload["runs"] == {"before": 1, "after": 1}
    assert payload["metrics"]
