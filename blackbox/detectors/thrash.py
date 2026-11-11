"""Detect files the agent kept rewriting.

Repeated writes to one file usually mean the agent could not settle on a
solution: each edit fixes what the last one broke. The count is the signal,
and the evidence lets you read the sequence back.
"""

from __future__ import annotations

from blackbox.detectors.base import HIGH, LOW, MEDIUM, Config, Finding
from blackbox.events import TOOL_CALL
from blackbox.graph import Graph
from blackbox.toolinfo import is_write, target_path

NAME = "thrash"


def run(graph: Graph, config: Config) -> list[Finding]:
    counts: dict[tuple[str, str], list[str]] = {}
    first_seq: dict[tuple[str, str], int] = {}

    for ev in graph.events:
        if ev.kind != TOOL_CALL or not is_write(ev.tool_name):
            continue
        path = target_path(ev.tool_name, ev.tool_input)
        if not path:
            continue
        key = (ev.branch, path)
        counts.setdefault(key, []).append(ev.uid)
        first_seq.setdefault(key, ev.seq)

    findings: list[Finding] = []
    for key in sorted(counts):
        branch, path = key
        uids = counts[key]
        if len(uids) < config.thrash_min_edits:
            continue
        severity = HIGH if len(uids) >= 10 else MEDIUM if len(uids) >= 5 else LOW
        findings.append(
            Finding(
                detector=NAME,
                severity=severity,
                title=f"{_short(path)} written {len(uids)}x",
                detail=(
                    f"{path} was written {len(uids)} times in this run. Repeated edits to "
                    f"one file usually mean each change was fixing the last one."
                ),
                key=f"{path}",
                branch=branch,
                count=len(uids),
                evidence=tuple(uids[:20]),
                metrics={"path": path, "writes": len(uids), "first_seq": first_seq[key]},
            )
        )
    return findings


def _short(path: str, keep: int = 2) -> str:
    parts = [p for p in path.split("/") if p]
    return "/".join(parts[-keep:]) if parts else path
