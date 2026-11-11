"""Detect files the agent kept rewriting *in a burst*.

Repeated writes to one file usually mean the agent could not settle: each edit
fixes what the last one broke. But the signal is density, not lifetime count.
Touching ``README.md`` three times across a six-hour build is ordinary
iterative work; touching it four times inside twenty tool calls is thrashing.

The first version of this detector counted writes across the whole session with
no window, and duly reported every substantial file in a long build. Like
:mod:`blackbox.detectors.retry_loops`, it now clusters by proximity and reports
each burst separately, with the span it occupied so the reader can judge.
"""

from __future__ import annotations

from blackbox.detectors.base import HIGH, LOW, MEDIUM, Config, Finding
from blackbox.events import TOOL_CALL
from blackbox.graph import Graph
from blackbox.toolinfo import is_write, target_path

NAME = "thrash"


def run(graph: Graph, config: Config) -> list[Finding]:
    findings: list[Finding] = []

    for branch in graph.branches:
        calls = graph.tool_calls(branch.name)
        positions: dict[str, list[int]] = {}
        for index, call in enumerate(calls):
            if not is_write(call.tool_name):
                continue
            path = target_path(call.tool_name, call.tool_input)
            if path:
                positions.setdefault(path, []).append(index)

        for path in sorted(positions):
            for cluster in _cluster(positions[path], config.thrash_window):
                if len(cluster) < config.thrash_min_edits:
                    continue
                events = [calls[i] for i in cluster]
                span = cluster[-1] - cluster[0] + 1
                severity = HIGH if len(cluster) >= 8 else MEDIUM if len(cluster) >= 5 else LOW
                findings.append(
                    Finding(
                        detector=NAME,
                        severity=severity,
                        title=f"{_short(path)} written {len(cluster)}x in {span} tool calls",
                        detail=(
                            f"{path} was written {len(cluster)} times inside a span of "
                            f"{span} tool calls. Edits that tightly packed usually mean "
                            f"each change was fixing the last one."
                        ),
                        key=f"{path}:{events[0].seq:08d}",
                        branch=branch.name,
                        count=len(cluster),
                        evidence=tuple(e.uid for e in events),
                        metrics={
                            "path": path,
                            "writes": len(cluster),
                            "window_span": span,
                            "first_seq": events[0].seq,
                            "last_seq": events[-1].seq,
                        },
                    )
                )
    return findings


def _cluster(indices: list[int], window: int) -> list[list[int]]:
    """Split positions into runs where each write is within ``window`` of the last."""
    if not indices:
        return []
    clusters: list[list[int]] = []
    current = [indices[0]]
    for index in indices[1:]:
        if index - current[-1] <= window:
            current.append(index)
        else:
            clusters.append(current)
            current = [index]
    clusters.append(current)
    return clusters


def _short(path: str, keep: int = 2) -> str:
    parts = [p for p in path.split("/") if p]
    return "/".join(parts[-keep:]) if parts else path
