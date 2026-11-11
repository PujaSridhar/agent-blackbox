"""Detect calls that took a long time to come back -- and say which kind.

The measurement is wall-clock between a tool call being issued and its result
arriving, read off the re-parented graph edge so parallel calls are attributed
correctly.

A long gap has two very different causes, and conflating them makes the whole
report untrustworthy:

* a **stall** -- the tool itself ran long (a hung test runner, a slow network
  call). Bounded by what a tool plausibly does.
* an **idle gap** -- the session was paused: waiting on a human approval, or
  simply left overnight. These can run to hours and say nothing about the
  agent's behaviour.

Gaps past ``idle_seconds`` are reported as idle gaps at low severity and never
counted as stalls. Calls the user interrupted are excluded entirely.
"""

from __future__ import annotations

from blackbox.detectors.base import HIGH, LOW, MEDIUM, Config, Finding
from blackbox.graph import Graph

NAME = "stall"


def run(graph: Graph, config: Config) -> list[Finding]:
    findings: list[Finding] = []

    for call in graph.tool_calls():
        result = graph.result_for(call)
        if result is None or call.ts is None or result.ts is None:
            continue
        if "interrupted" in result.flags:
            continue

        seconds = (result.ts - call.ts).total_seconds()
        if seconds < config.stall_seconds:
            continue

        tool = call.tool_name or "tool"

        if seconds >= config.idle_seconds:
            findings.append(
                Finding(
                    detector=NAME,
                    severity=LOW,
                    title=f"idle gap of {_human(seconds)} at {tool}",
                    detail=(
                        f"{_human(seconds)} passed between this {tool} call and its "
                        f"result -- far longer than any tool runs. The session was "
                        f"almost certainly paused here, waiting on a human or simply "
                        f"left running. Counted as idle time, not as a stall."
                    ),
                    key=f"idle:{call.seq:08d}",
                    branch=call.branch,
                    evidence=(call.uid, result.uid),
                    metrics={
                        "seconds": round(seconds, 2),
                        "tool": tool,
                        "seq": call.seq,
                        "kind": "idle_gap",
                    },
                )
            )
            continue

        if seconds >= config.stall_seconds * 5:
            severity = HIGH
        elif seconds >= config.stall_seconds * 2:
            severity = MEDIUM
        else:
            severity = LOW

        findings.append(
            Finding(
                detector=NAME,
                severity=severity,
                title=f"{tool} took {_human(seconds)} to return",
                detail=(
                    f"{_human(seconds)} of wall-clock passed between this {tool} call "
                    f"and its result. The gap includes any wait for human approval, so "
                    f"treat it as an upper bound on execution time."
                ),
                key=f"stall:{call.seq:08d}",
                branch=call.branch,
                evidence=(call.uid, result.uid),
                metrics={
                    "seconds": round(seconds, 2),
                    "tool": tool,
                    "seq": call.seq,
                    "kind": "stall",
                },
            )
        )

    return findings


def _human(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.1f}min"
    return f"{seconds / 3600:.1f}h"
