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
from blackbox.describe import evidence_detail
from blackbox.detectors.base import Config, Finding
from blackbox.toolinfo import is_human_in_the_loop
from blackbox.events import TOOL_CALL, TOOL_RESULT as _TR  # noqa: F401
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
        # A declined call is a human saying no, not a tool breaking. Counting
        # it as a failure overstates how badly the run went.
        failures = sum(
            1
            for e in events
            if e.kind == TOOL_RESULT and e.tool_ok is False and "declined" not in e.flags
        )
        declined = sum(1 for e in events if "declined" in e.flags)

        # Time the run spent waiting on a person. Stalls deliberately exclude
        # it, so it is reported here instead of vanishing.
        human_wait = 0.0
        for call in self.graph.tool_calls():
            if not is_human_in_the_loop(call.tool_name):
                continue
            result = self.graph.result_for(call)
            if result is not None and call.ts and result.ts:
                gap = (result.ts - call.ts).total_seconds()
                if gap > 0:
                    human_wait += gap

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
            "declined_calls": declined,
            "human_wait_seconds": round(human_wait, 2),
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


    # -- cost attribution --------------------------------------------------

    # Findings whose implicated turns represent work that did not advance the
    # task. Polling is excluded (it is normal), and context_bloat is excluded
    # because its figure is a downstream *estimate* -- mixing it with measured
    # spend would launder an estimate into a hard number.
    WASTE_DETECTORS = ("retry_loop", "thrash", "error_cluster")

    def _issuing_turn(self, uid: str) -> Event | None:
        """Walk from a piece of evidence to the model turn that paid for it."""
        node = self.graph.by_uid.get(uid)
        if node is None:
            return None
        for _ in range(3):  # result -> call -> assistant message
            if not node.usage.is_empty():
                return node
            if not node.parent_uid:
                return None
            parent = self.graph.by_uid.get(node.parent_uid)
            if parent is None:
                return None
            node = parent
        return node if not node.usage.is_empty() else None

    def _turns_behind(self, finding: Finding) -> set[str]:
        turns = set()
        for uid in finding.evidence:
            turn = self._issuing_turn(uid)
            if turn is not None:
                turns.add(turn.uid)
        return turns

    def finding_usd(self, finding: Finding) -> float:
        """What the model turns behind this finding cost.

        This is the *direct* cost of the turns that issued the implicated
        calls. It deliberately excludes the downstream cost of carrying their
        results in context, which cannot be attributed exactly -- so every
        figure here is a floor, never an estimate dressed up as a total.
        """
        return sum(
            cost_mod.event_usd(self.graph.by_uid[uid]) for uid in sorted(self._turns_behind(finding))
        )

    def wasted(self) -> dict[str, Any]:
        """Run-level waste: the union of turns behind the waste findings.

        A union, not a sum: one turn can be implicated by several findings at
        once (a failing call that was also retried), and adding the findings
        up would bill that turn twice.
        """
        turns: set[str] = set()
        per_detector: dict[str, set[str]] = {}
        for finding in self.findings:
            if finding.detector not in self.WASTE_DETECTORS:
                continue
            if finding.detector == "retry_loop" and finding.metrics.get("kind") == "expected_repeat":
                continue
            behind = self._turns_behind(finding)
            turns |= behind
            per_detector.setdefault(finding.detector, set()).update(behind)

        total = sum(cost_mod.event_usd(self.graph.by_uid[uid]) for uid in sorted(turns))
        return {
            "usd": round(total, 6),
            "share_of_run": round(total / self.cost.total_usd, 6) if self.cost.total_usd else 0.0,
            "turns": len(turns),
            "by_detector": {
                name: round(
                    sum(cost_mod.event_usd(self.graph.by_uid[uid]) for uid in sorted(uids)), 6
                )
                for name, uids in sorted(per_detector.items())
            },
            "is_floor": True,
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
            "wasted": self.wasted(),
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
            "findings": [self._finding_payload(f) for f in self.findings],
        }

    def _finding_payload(self, finding: Finding) -> dict[str, Any]:
        payload = finding.to_dict()
        payload["direct_usd"] = round(self.finding_usd(finding), 6)
        payload["evidence_detail"] = [
            evidence_detail(self.graph.by_uid[uid])
            for uid in finding.evidence
            if uid in self.graph.by_uid
        ][:12]
        return payload

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
