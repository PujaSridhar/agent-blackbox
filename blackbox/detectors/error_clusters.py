"""Group failed tool calls by what actually went wrong.

A run with 88 failures is not 88 problems. Error text is normalised -- paths,
numbers, hex ids and quoted fragments removed -- so the same underlying failure
collapses into one cluster with a count, which is the form a human can act on.
"""

from __future__ import annotations

import re

from blackbox.detectors.base import HIGH, LOW, MEDIUM, Config, Finding
from blackbox.events import TOOL_RESULT
from blackbox.graph import Graph

NAME = "error_cluster"

_PATH = re.compile(r"(/[\w.\-]+){2,}")
_NUM = re.compile(r"\b\d+\b")
_HEX = re.compile(r"\b[0-9a-f]{8,}\b", re.I)
_QUOTED = re.compile(r"'[^']{0,80}'|\"[^\"]{0,80}\"")
_WS = re.compile(r"\s+")


def signature(text: str) -> str:
    """Collapse an error message to its invariant shape."""
    first = ""
    for line in text.splitlines():
        line = line.strip()
        if line:
            first = line
            break
    if not first:
        return "(empty error)"
    s = _QUOTED.sub("'_'", first)
    s = _PATH.sub("<path>", s)
    s = _HEX.sub("<hex>", s)
    s = _NUM.sub("<n>", s)
    s = _WS.sub(" ", s).strip()
    return s[:120]


def run(graph: Graph, config: Config) -> list[Finding]:
    clusters: dict[tuple[str, str, str], list[str]] = {}
    seqs: dict[tuple[str, str, str], int] = {}

    for ev in graph.events:
        if ev.kind != TOOL_RESULT or ev.tool_ok is not False:
            continue
        parent = graph.by_uid.get(ev.parent_uid) if ev.parent_uid else None
        tool_name = parent.tool_name if parent is not None else None
        key = (ev.branch, tool_name or "?", signature(ev.tool_output))
        clusters.setdefault(key, []).append(ev.uid)
        seqs.setdefault(key, ev.seq)

    findings: list[Finding] = []
    for key in sorted(clusters):
        branch, tool_name, sig = key
        uids = clusters[key]
        if len(uids) < config.error_cluster_min:
            continue
        severity = HIGH if len(uids) >= 10 else MEDIUM if len(uids) >= 4 else LOW
        findings.append(
            Finding(
                detector=NAME,
                severity=severity,
                title=f"{tool_name} failed {len(uids)}x: {sig[:60]}",
                detail=f"{len(uids)} {tool_name} calls failed with the same error shape: {sig}",
                key=f"{tool_name}:{sig}",
                branch=branch,
                count=len(uids),
                evidence=tuple(uids[:20]),
                metrics={
                    "failures": len(uids),
                    "tool": tool_name,
                    "signature": sig,
                    "first_seq": seqs[key],
                },
            )
        )
    return findings
