"""Detect an agent repeating the same call and getting nowhere.

Identical inputs are matched on a digest of the call's *meaningful* arguments,
so two `Bash` calls running the same command count as repeats even when their
prose descriptions differ. Repeats only count when they fall within a sliding
window of tool calls -- running the same test command at the start and end of
a long session is normal; running it four times in six calls is a loop.

Repeating a call is not always retrying. Taking the same screenshot eight
times is polling, and advancing a task list with the same payload ten times is
bookkeeping: in both the input is identical by design. Those are reported,
because the count is still worth seeing, but at low severity and never as
"the agent learned nothing".
"""

from __future__ import annotations

from blackbox.detectors.base import HIGH, LOW, MEDIUM, Config, Finding
from blackbox.graph import Graph
from blackbox.toolinfo import repeats_by_design

NAME = "retry_loop"


def run(graph: Graph, config: Config) -> list[Finding]:
    findings: list[Finding] = []

    for branch in graph.branches:
        calls = graph.tool_calls(branch.name)
        positions: dict[tuple[str, str], list[int]] = {}
        for index, call in enumerate(calls):
            key = (call.tool_name or "?", call.input_digest())
            positions.setdefault(key, []).append(index)

        for key in sorted(positions):
            indices = positions[key]
            if len(indices) < config.retry_min_repeats:
                continue

            # Split into clusters of repeats that are close together.
            cluster = [indices[0]]
            clusters = []
            for idx in indices[1:]:
                if idx - cluster[-1] <= config.retry_window:
                    cluster.append(idx)
                else:
                    clusters.append(cluster)
                    cluster = [idx]
            clusters.append(cluster)

            tool_name, digest = key
            for cluster in clusters:
                if len(cluster) < config.retry_min_repeats:
                    continue
                events = [calls[i] for i in cluster]
                span = cluster[-1] - cluster[0] + 1
                expected = repeats_by_design(tool_name, events[0].tool_input)

                if expected:
                    severity = LOW
                    title = f"{tool_name} repeated {len(cluster)}x with identical input"
                    detail = (
                        f"The same {tool_name} call ran {len(cluster)} times within "
                        f"{span} tool calls. This tool either observes or records "
                        f"progress, so identical input is expected by design — it is "
                        f"not a retry loop."
                    )
                else:
                    severity = HIGH if len(cluster) >= 5 else MEDIUM
                    title = f"{tool_name} called {len(cluster)}x with identical input"
                    detail = (
                        f"The same {tool_name} call ran {len(cluster)} times within "
                        f"{span} tool calls. Identical input means the agent learned "
                        f"nothing between attempts."
                    )

                findings.append(
                    Finding(
                        detector=NAME,
                        severity=severity,
                        title=title,
                        detail=detail,
                        key=f"{tool_name}:{digest}:{events[0].seq}",
                        branch=branch.name,
                        count=len(cluster),
                        evidence=tuple(e.uid for e in events),
                        metrics={
                            "repeats": len(cluster),
                            "window_span": span,
                            "tool": tool_name,
                            "kind": "expected_repeat" if expected else "retry_loop",
                            "first_seq": events[0].seq,
                            "last_seq": events[-1].seq,
                        },
                    )
                )

    return findings
