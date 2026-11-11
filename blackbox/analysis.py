"""The analysis pipeline: trace -> graph -> cost -> findings -> payload.

The payload is the tool's actual output contract. It contains no wall-clock
reads, no absolute ordering that depends on dict iteration, and no floating
point that is not rounded at the boundary -- so two runs over the same input
serialise to identical bytes. :func:`Analysis.digest` is that guarantee made
checkable, and ``tests/test_determinism.py`` enforces it.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from blackbox import cost as cost_mod
from blackbox import detectors as detectors_mod
from blackbox import graph as graph_mod
from blackbox.adapters import registry
from blackbox.adapters.base import Trace
from blackbox.detectors.base import Config, Finding
from blackbox.events import (
    ASSISTANT_MESSAGE,
    TOOL_CALL,
    TOOL_RESULT,
    USER_MESSAGE,
    Event,
)

SCHEMA_VERSION = 1


@dataclass
class Analysis:
    trace: Trace
    graph: graph_mod.Graph
    cost: cost_mod.CostReport
    findings: list[Finding]
    config: Config = field(default_factory=Config)

    # -- summary -----------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        events = self.graph.events
        kinds = Counter(e.kind for e in events)
        tools = Counter(e.tool_name or "?" for e in events if e.kind == TOOL_CALL)
        failures = sum(1 for e in events if e.kind == TOOL_RESULT and e.tool_ok is False)

        stamped = sorted(e.ts for e in events if e.ts is not None)
        first_ts = stamped[0].isoformat() if stamped else None
        last_ts = stamped[-1].isoformat() if stamped else None
        span = round((stamped[-1] - stamped[0]).total_seconds(), 2) if len(stamped) > 1 else 0.0

        # Span alone is misleading for a session resumed over days: it counts
        # the nights. Active time sums only the gaps short enough to be work.
        active = 0.0
        for earlier, later in zip(stamped, stamped[1:]):
            gap = (later - earlier).total_seconds()
            if 0 <= gap <= self.config.idle_seconds:
                active += gap

        return {
            "events": len(events),
            "user_messages": kinds.get(USER_MESSAGE, 0),
            "assistant_messages": kinds.get(ASSISTANT_MESSAGE, 0),
            "tool_calls": kinds.get(TOOL_CALL, 0),
            "tool_results": kinds.get(TOOL_RESULT, 0),
            "tool_failures": failures,
            "orphan_events": self.graph.orphans,
            "skipped_lines": self.trace.skipped_lines,
            "models": sorted({e.model for e in events if e.model}),
            "branches": [
                {
                    "name": b.name,
                    "events": b.event_count,
                    "tool_calls": b.tool_calls,
                    "first_seq": b.first_seq,
                    "last_seq": b.last_seq,
                }
                for b in self.graph.branches
            ],
            "subagent_branches": sum(1 for b in self.graph.branches if not b.is_main),
            "tools": [
                {"tool": name, "calls": count}
                # -count then name: ties never reorder between runs.
                for name, count in sorted(tools.items(), key=lambda kv: (-kv[1], kv[0]))
            ],
            "first_ts": first_ts,
            "last_ts": last_ts,
            "span_seconds": span,
            "active_seconds": round(active, 2),
        }

    def cost_payload(self) -> dict[str, Any]:
        c = self.cost
        return {
            "total_usd": round(c.total_usd, 6),
            "complete": c.is_complete,
            "unpriced_models": sorted(c.unpriced_models),
            "unpriced_tokens": c.unpriced_tokens,
            "pricing_as_of": c.pricing_as_of,
            "cache_write_1h_usd_share": round(c.cache_write_1h_usd_share, 6),
            "totals": {
                "input_tokens": c.total_usage.input_tokens,
                "output_tokens": c.total_usage.output_tokens,
                "cache_read_tokens": c.total_usage.cache_read_tokens,
                "cache_write_5m_tokens": c.total_usage.cache_write_5m_tokens,
                "cache_write_1h_tokens": c.total_usage.cache_write_1h_tokens,
                "thinking_tokens": c.total_usage.thinking_tokens,
            },
            "by_model": [
                {
                    "model": mc.model,
                    "calls": mc.calls,
                    "priced": mc.priced,
                    "input_tokens": mc.usage.input_tokens,
                    "output_tokens": mc.usage.output_tokens,
                    "cache_read_tokens": mc.usage.cache_read_tokens,
                    "cache_write_5m_tokens": mc.usage.cache_write_5m_tokens,
                    "cache_write_1h_tokens": mc.usage.cache_write_1h_tokens,
                    "thinking_tokens": mc.usage.thinking_tokens,
                    "input_usd": round(mc.input_usd, 6),
                    "output_usd": round(mc.output_usd, 6),
                    "cache_read_usd": round(mc.cache_read_usd, 6),
                    "cache_write_usd": round(mc.cache_write_usd, 6),
                    "total_usd": round(mc.total_usd, 6),
                }
                for mc in c.by_model
            ],
            "by_branch": {k: round(v, 6) for k, v in sorted(c.by_branch.items())},
        }

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "source": os.path.basename(self.trace.source),
            "adapter": self.trace.adapter,
            "config": self.config.to_dict(),
            "trace_meta": {k: self.trace.meta[k] for k in sorted(self.trace.meta)},
            "summary": self.summary(),
            "cost": self.cost_payload(),
            "findings": [f.to_dict() for f in self.findings],
        }

    def digest(self) -> str:
        """SHA-256 over the canonical payload."""
        blob = json.dumps(self.payload(), sort_keys=True, default=str, ensure_ascii=True)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def findings_digest(self) -> str:
        blob = json.dumps(
            [f.to_dict() for f in self.findings], sort_keys=True, default=str, ensure_ascii=True
        )
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def analyze_events(
    events: list[Event],
    *,
    source: str,
    adapter: str,
    meta: dict[str, Any] | None = None,
    config: Config | None = None,
    skipped_lines: int = 0,
) -> Analysis:
    """Run the pipeline over already-parsed events (the replay entry point)."""
    config = config or Config()
    g = graph_mod.build(events)
    c = cost_mod.compute(g.events)
    findings = detectors_mod.run_all(g, config)
    trace = Trace(
        events=g.events,
        source=source,
        adapter=adapter,
        meta=meta or {},
        skipped_lines=skipped_lines,
    )
    return Analysis(trace=trace, graph=g, cost=c, findings=findings, config=config)


def analyze(path: str, config: Config | None = None, adapter: str | None = None) -> Analysis:
    """Parse and analyse a trace file."""
    trace = registry.load(path, adapter=adapter)
    return analyze_events(
        trace.events,
        source=trace.source,
        adapter=trace.adapter,
        meta=trace.meta,
        config=config,
        skipped_lines=trace.skipped_lines,
    )
