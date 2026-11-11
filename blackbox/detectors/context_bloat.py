"""Find the tool results that made the rest of the run expensive.

Agent cost compounds in a way that is almost invisible turn by turn. A tool
result is not paid for once: it sits in the context window and is re-sent with
every later turn. A 20,000-token log dumped at step 40 of a 400-turn session
is billed roughly 360 more times as cached input before the run ends.

So the expensive moment is rarely the expensive-looking turn. This detector
walks the graph for large tool results and estimates what each one cost
*downstream*: its size multiplied by the model turns that followed it in the
same branch, priced at the cache-read rate.

Two honesty constraints, both surfaced in the finding text:

* Token counts are **estimated** from character length (~4 chars/token). This
  module does not tokenise, and a wrong-looking precise number would be worse
  than an openly approximate one.
* Where a trace records a compaction boundary, counting stops there: a result
  dumped before the context was summarised is not re-sent afterwards. Where a
  trace records no such boundary, the figure is an upper bound on a context
  that is never cleared, and silent context editing would lower it.

Each downstream turn is priced at **its own** model's cache-read rate, not at
one rate for the whole run. A run that hands reading to a cheap subagent and
reasons on an expensive model is exactly the case where a single blended rate
is wrong in both directions, and it is also the case where someone is most
likely to be looking.
"""

from __future__ import annotations

from collections import Counter

from blackbox.detectors.base import HIGH, LOW, MEDIUM, Config, Finding
from blackbox.events import ASSISTANT_MESSAGE, TOOL_RESULT
from blackbox.graph import Graph
from blackbox.pricing import lookup

NAME = "context_bloat"

CHARS_PER_TOKEN = 4


def _dominant_model(graph: Graph) -> str | None:
    counts = Counter(e.model for e in graph.events if e.model and not e.usage.is_empty())
    return counts.most_common(1)[0][0] if counts else None


def run(graph: Graph, config: Config) -> list[Finding]:
    findings: list[Finding] = []

    for branch in graph.branches:
        branch_events = [e for e in graph.events if e.branch == branch.name]
        # (seq, cache-read rate) for every billable turn in this branch. A turn
        # on an unpriced model contributes nothing rather than a guessed rate,
        # which makes the estimate a floor instead of a fiction.
        turns: list[tuple[int, float]] = []
        unpriced_turns = 0
        compaction_seqs = sorted(
            e.seq for e in branch_events if "compaction" in e.flags
        )
        for event in branch_events:
            if event.kind != ASSISTANT_MESSAGE or event.usage.is_empty():
                continue
            price = lookup(event.model)
            if price is None:
                unpriced_turns += 1
                continue
            turns.append((event.seq, price.read_rate()))
        if not turns:
            continue

        for ev in branch_events:
            if ev.kind != TOOL_RESULT or not ev.tool_output:
                continue
            est_tokens = len(ev.tool_output) // CHARS_PER_TOKEN
            if est_tokens < config.bloat_min_tokens:
                continue

            # A result stops being re-sent once the context is compacted.
            horizon = next((s for s in compaction_seqs if s > ev.seq), None)
            downstream = [
                rate
                for seq, rate in turns
                if seq > ev.seq and (horizon is None or seq < horizon)
            ]
            downstream_turns = len(downstream)
            if downstream_turns <= 0:
                continue

            # Each later turn re-reads this result at that turn's own rate.
            est_usd = est_tokens / 1e6 * sum(downstream)
            if est_usd < config.bloat_min_usd:
                continue

            call = graph.by_uid.get(ev.parent_uid) if ev.parent_uid else None
            tool = call.tool_name if call is not None else "tool"

            severity = HIGH if est_usd >= config.bloat_min_usd * 10 else (
                MEDIUM if est_usd >= config.bloat_min_usd * 3 else LOW
            )

            findings.append(
                Finding(
                    detector=NAME,
                    severity=severity,
                    title=(
                        f"{tool} result added ~{est_tokens:,} tokens to context, "
                        f"~${est_usd:,.2f} downstream"
                    ),
                    detail=(
                        f"This {tool} result is roughly {est_tokens:,} tokens and landed "
                        f"with {downstream_turns} model turns still to come, so it was "
                        f"re-sent as cached input about that many times — an estimated "
                        f"${est_usd:,.2f} after the fact, each turn priced at its own "
                        f"model's cache-read rate. Token counts are estimated from length, "
                        f"and context editing or compaction would lower the real figure, "
                        f"so treat this as an upper bound."
                    ),
                    key=f"{ev.seq:08d}",
                    branch=branch.name,
                    evidence=((call.uid,) if call is not None else ()) + (ev.uid,),
                    metrics={
                        "kind": "context_bloat",
                        "tool": tool,
                        "est_tokens": est_tokens,
                        "downstream_turns": downstream_turns,
                        "est_downstream_usd": round(est_usd, 4),
                        "unpriced_downstream_turns": unpriced_turns,
                        "compaction_bounded": horizon is not None,
                        "seq": ev.seq,
                        "estimated": True,
                    },
                )
            )

    return findings
