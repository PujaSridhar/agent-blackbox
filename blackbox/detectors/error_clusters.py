"""Group failed tool calls by what actually went wrong.

A run with 88 failures is not 88 problems. Error text is normalised -- paths,
numbers, hex ids and quoted fragments removed -- so the same underlying failure
collapses into one cluster with a count, which is the form a human can act on.

Three rules keep the clusters meaningful, each added after the naive version
produced one useless bucket on real traces:

* **A bare exit-code line is not an error message.** Shell failures arrive as
  ``Exit code 1`` followed by the real output. Taking line one collapses every
  unrelated shell failure in the run into ``Exit code <n>``.
* **The error is usually at the end.** Output is scanned from the bottom for a
  line that actually reads like a failure, falling back to the first content
  line when nothing does.
* **What ran is part of the identity.** A ``pytest`` failure and an ``npm``
  failure are different problems even when their text normalises the same, so
  the program is part of the cluster key.

Declined tool calls -- a human saying no -- are excluded entirely. They are
not failures, and they are numerous enough to drown real ones.
"""

from __future__ import annotations

import re

from blackbox.detectors.base import HIGH, LOW, MEDIUM, Config, Finding
from blackbox.events import TOOL_RESULT
from blackbox.graph import Graph
from blackbox.toolinfo import program_hint

NAME = "error_cluster"

_EXIT_CODE = re.compile(r"^(exit code|command exited with code|exit status)\s*\d+\s*$", re.I)
_ERRORISH = re.compile(
    r"(error|exception|failed|failure|fatal|traceback|no such|not found|cannot|can't"
    r"|unable|denied|refused|invalid|unexpected|missing|assert|bad |timed out)",
    re.I,
)
_PATH = re.compile(r"(/[\w.\-]+){2,}")
_NUM = re.compile(r"\b\d+\b")
_HEX = re.compile(r"\b[0-9a-f]{8,}\b", re.I)
_QUOTED = re.compile(r"'[^']{0,80}'|\"[^\"]{0,80}\"")
_WS = re.compile(r"\s+")


def signature(text: str) -> str:
    """Collapse an error message to its invariant shape."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return "(empty error)"

    start = 1 if _EXIT_CODE.match(lines[0]) else 0
    body = lines[start:] or lines

    # The useful line is usually the last one that reads like a failure.
    chosen = body[0]
    for line in reversed(body):
        if _ERRORISH.search(line):
            chosen = line
            break

    s = _QUOTED.sub("'_'", chosen)
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
        if "declined" in ev.flags:
            continue  # a human said no; that is not a tool failure
        parent = graph.by_uid.get(ev.parent_uid) if ev.parent_uid else None
        tool_name = parent.tool_name if parent is not None else None
        program = program_hint(tool_name, parent.tool_input if parent is not None else {})
        label = f"{tool_name or '?'}({program})" if program else (tool_name or "?")
        key = (ev.branch, label, signature(ev.tool_output))
        clusters.setdefault(key, []).append(ev.uid)
        seqs.setdefault(key, ev.seq)

    findings: list[Finding] = []
    for key in sorted(clusters):
        branch, label, sig = key
        uids = clusters[key]
        if len(uids) < config.error_cluster_min:
            continue
        severity = HIGH if len(uids) >= 10 else MEDIUM if len(uids) >= 4 else LOW
        findings.append(
            Finding(
                detector=NAME,
                severity=severity,
                title=f"{label} failed {len(uids)}x: {sig[:60]}",
                detail=f"{len(uids)} {label} calls failed with the same error shape: {sig}",
                key=f"{label}:{sig}",
                branch=branch,
                count=len(uids),
                evidence=tuple(uids),
                metrics={
                    "failures": len(uids),
                    "tool": label,
                    "signature": sig,
                    "first_seq": seqs[key],
                },
            )
        )
    return findings
