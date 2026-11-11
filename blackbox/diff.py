"""Comparing two sets of runs.

The question people actually have when they change an agent is not "what did
this run cost" but "did that change help". Nobody can answer it today, so
prompt and tooling changes get made on vibes: you tweak the system prompt, run
it again, and decide it feels better.

A diff makes the question answerable. Because everything upstream is
deterministic, the same traces always produce the same comparison, so a
difference is a difference in the agent, never in the measurement.

Run counts are allowed to differ -- in practice "before" is one run and
"after" is three -- so every rate is compared directly and every total is also
reported per run, which is the only honest way to line up uneven sides.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from blackbox.aggregate import Corpus

LOWER = "lower"
HIGHER = "higher"
NEUTRAL = "neutral"

USD = "usd"
RATE = "rate"
COUNT = "count"
DURATION = "duration"


@dataclass(frozen=True)
class Metric:
    key: str
    label: str
    unit: str
    better: str
    per_run: bool = False


# Ordered as they are read: money first, then what caused it, then volume.
METRICS: tuple[Metric, ...] = (
    Metric("total_usd", "cost", USD, LOWER, per_run=True),
    Metric("wasted_usd", "wasted", USD, LOWER, per_run=True),
    Metric("wasted_share", "wasted share", RATE, LOWER),
    Metric("cache_hit_rate", "cache hit rate", RATE, HIGHER),
    Metric("failure_rate", "tool failure rate", RATE, LOWER),
    Metric("findings", "findings", COUNT, LOWER, per_run=True),
    Metric("tool_calls", "tool calls", COUNT, NEUTRAL, per_run=True),
    Metric("active_seconds", "active time", DURATION, NEUTRAL, per_run=True),
)


def _verdict(metric: Metric, delta: float) -> str:
    if abs(delta) < 1e-9 or metric.better == NEUTRAL:
        return ""
    improved = delta < 0 if metric.better == LOWER else delta > 0
    return "better" if improved else "worse"


def compare(before: Corpus, after: Corpus) -> dict[str, Any]:
    """Line up two corpora metric by metric."""
    left = before.summary()
    right = after.summary()
    left_runs = max(left["runs"], 1)
    right_runs = max(right["runs"], 1)

    rows = []
    for metric in METRICS:
        a = float(left.get(metric.key) or 0.0)
        b = float(right.get(metric.key) or 0.0)
        delta = b - a
        row: dict[str, Any] = {
            "key": metric.key,
            "label": metric.label,
            "unit": metric.unit,
            "before": a,
            "after": b,
            "delta": delta,
            "pct_change": (delta / a) if a else None,
            "verdict": _verdict(metric, delta),
        }
        if metric.per_run:
            per_a = a / left_runs
            per_b = b / right_runs
            row["before_per_run"] = per_a
            row["after_per_run"] = per_b
            row["delta_per_run"] = per_b - per_a
            # With uneven sides the per-run figure is the comparable one, so
            # the verdict follows it rather than the raw total.
            if left_runs != right_runs:
                row["verdict"] = _verdict(metric, per_b - per_a)
        rows.append(row)

    detectors = sorted(set(left["by_detector"]) | set(right["by_detector"]))
    by_detector = []
    for name in detectors:
        a = left["by_detector"].get(name, 0)
        b = right["by_detector"].get(name, 0)
        by_detector.append(
            {
                "detector": name,
                "before": a,
                "after": b,
                "delta": b - a,
                "before_per_run": a / left_runs,
                "after_per_run": b / right_runs,
                "verdict": _verdict(Metric(name, name, COUNT, LOWER), (b / right_runs) - (a / left_runs)),
            }
        )

    return {
        "runs": {"before": left["runs"], "after": right["runs"]},
        "uneven": left["runs"] != right["runs"],
        "metrics": rows,
        "by_detector": by_detector,
        "unpriced": {
            "before": left["total_usd"] == 0 and left["tokens"] > 0,
            "after": right["total_usd"] == 0 and right["tokens"] > 0,
        },
    }
