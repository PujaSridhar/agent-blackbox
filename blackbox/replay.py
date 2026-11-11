"""Deterministic replay.

Re-derives the entire report from the stored canonical events -- no trace file,
no network, no model call -- and must produce byte-identical output.

This is the load-bearing claim of the whole tool. An analysis of an agent run
that cannot itself be re-verified is just another opinion; one that reproduces
exactly, from a store you can also query by hand, is evidence. The thresholds
are read back out of the store rather than re-defaulted, so a replay cannot
quietly disagree with the original pass because a default moved.
"""

from __future__ import annotations

import json

from blackbox import store
from blackbox.analysis import Analysis, analyze_events
from blackbox.detectors.base import Config


def config_from_meta(meta: dict[str, str]) -> Config:
    raw = meta.get("config")
    if not raw:
        return Config()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return Config()
    allowed = {f for f in Config.__dataclass_fields__}
    return Config(**{k: v for k, v in data.items() if k in allowed})


def replay(db_path: str) -> Analysis:
    """Rebuild the analysis from a stored run. Zero model calls."""
    from blackbox import pricing

    events = store.read_events(db_path)
    meta = store.read_meta(db_path)

    # Rates supplied at analysis time are part of the input, so a replay has
    # to use the same ones or it would price the same run differently.
    try:
        overrides = json.loads(meta.get("price_overrides") or "{}")
    except json.JSONDecodeError:
        overrides = {}
    applied = pricing.set_overrides(overrides)

    trace_meta = {}
    for key, value in meta.items():
        if key.startswith("trace."):
            try:
                trace_meta[key[len("trace.") :]] = json.loads(value)
            except json.JSONDecodeError:
                trace_meta[key[len("trace.") :]] = value

    return analyze_events(
        events,
        source=meta.get("source", db_path),
        adapter=meta.get("adapter", "unknown"),
        meta=trace_meta,
        config=config_from_meta(meta),
        skipped_lines=int(meta.get("skipped_lines") or 0),
        price_overrides=applied,
    )
