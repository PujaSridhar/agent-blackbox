"""Analysis across many runs.

One session answers "what happened here". A corpus answers the questions
people actually have: what does this agent get wrong *habitually*, what is the
standing cost of those habits, and is any of it getting better.

Everything here is a reduction over per-run :class:`~blackbox.analysis.Analysis`
objects, so an aggregate can never disagree with the individual reports.
"""

from __future__ import annotations

import os
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from blackbox.analysis import Analysis, analyze
from blackbox.detectors.base import Config
from blackbox.events import Usage


@dataclass
class Corpus:
    runs: list[tuple[str, Analysis]] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)

    @property
    def total_usd(self) -> float:
        return sum(a.cost.total_usd for _, a in self.runs)

    @property
    def total_usage(self) -> Usage:
        total = Usage()
        for _, a in self.runs:
            total = total + a.cost.total_usage
        return total

    def summary(self) -> dict[str, Any]:
        tool_calls = sum(a.summary()["tool_calls"] for _, a in self.runs)
        failures = sum(a.summary()["tool_failures"] for _, a in self.runs)
        active = sum(a.summary()["active_seconds"] for _, a in self.runs)
        findings = sum(len(a.findings) for _, a in self.runs)

        wasted = sum(a.wasted()["usd"] for _, a in self.runs)
        usage = self.total_usage

        # What fraction of billed input was served from cache. Low means the
        # prefix is being rebuilt; high means the cache is doing its job.
        billed_input = usage.billed_input_tokens
        cache_hit = usage.cache_read_tokens / billed_input if billed_input else 0.0

        detector_counts: Counter[str] = Counter()
        detector_usd: dict[str, float] = defaultdict(float)
        thrashed: dict[str, int] = defaultdict(int)
        loops: list[tuple[int, str, str]] = []
        bloat: list[tuple[float, int, str, str]] = []

        for source, analysis in self.runs:
            name = os.path.basename(source)
            for finding in analysis.findings:
                detector_counts[finding.detector] += 1
                detector_usd[finding.detector] += analysis.finding_usd(finding)
                metrics = finding.metrics
                if finding.detector == "thrash":
                    thrashed[metrics["path"]] += metrics["writes"]
                elif finding.detector == "retry_loop" and metrics.get("kind") == "retry_loop":
                    loops.append((metrics["repeats"], metrics.get("tool", "?"), name))
                elif finding.detector == "context_bloat":
                    bloat.append(
                        (metrics["est_downstream_usd"], metrics["est_tokens"], metrics.get("tool", "?"), name)
                    )

        return {
            "runs": len(self.runs),
            "unreadable": len(self.failed),
            "total_usd": round(self.total_usd, 4),
            "wasted_usd": round(wasted, 4),
            "wasted_share": round(wasted / self.total_usd, 6) if self.total_usd else 0.0,
            "tool_calls": tool_calls,
            "tool_failures": failures,
            "failure_rate": round(failures / tool_calls, 6) if tool_calls else 0.0,
            "active_seconds": round(active, 2),
            "findings": findings,
            "cache_hit_rate": round(cache_hit, 6),
            "cache_write_1h_tokens": usage.cache_write_1h_tokens,
            "tokens": usage.total_tokens,
            "by_detector": {k: detector_counts[k] for k in sorted(detector_counts)},
            "usd_by_detector": {k: round(detector_usd[k], 4) for k in sorted(detector_usd)},
            "top_thrashed": [
                {"path": path, "writes": writes}
                for path, writes in sorted(thrashed.items(), key=lambda kv: (-kv[1], kv[0]))[:8]
            ],
            "worst_loops": [
                {"repeats": repeats, "tool": tool, "run": run}
                for repeats, tool, run in sorted(loops, reverse=True)[:8]
            ],
            "worst_bloat": [
                {"est_usd": usd, "est_tokens": tokens, "tool": tool, "run": run}
                for usd, tokens, tool, run in sorted(bloat, reverse=True)[:8]
            ],
        }


def load(paths: list[str], config: Config | None = None) -> Corpus:
    """Analyse many traces, carrying on past the ones that cannot be read."""
    corpus = Corpus()
    for path in paths:
        try:
            corpus.runs.append((path, analyze(path, config=config)))
        except Exception as exc:  # a corpus scan must not die on one bad file
            corpus.failed.append((path, f"{type(exc).__name__}: {exc}"))
    return corpus
