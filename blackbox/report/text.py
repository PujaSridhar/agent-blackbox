"""Terminal report."""

from __future__ import annotations

from blackbox.analysis import Analysis
from blackbox.detectors.base import HIGH, LOW, MEDIUM

_BADGE = {HIGH: "HIGH", MEDIUM: "MED ", LOW: "LOW "}


def _n(value: int) -> str:
    return f"{value:,}"


def _usd(value: float) -> str:
    """Never render real spend as $0.00 -- a cost tool that hides cost is useless."""
    if value and abs(value) < 0.01:
        return f"{value:.4f}"
    return f"{value:,.2f}"


def _dur(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.1f}min"
    return f"{seconds / 3600:.1f}h"


def render(analysis: Analysis, max_findings: int | None = 25) -> str:
    s = analysis.summary()
    c = analysis.cost
    out: list[str] = []
    add = out.append

    add("")
    add(f"AGENT BLACK BOX   {s['events']} events from {analysis.trace.source.split('/')[-1]}")
    add(
        f"  adapter {analysis.trace.adapter} · {_n(s['tool_calls'])} tool calls · "
        f"{s['tool_failures']} failed · {_dur(s['active_seconds'])} active "
        f"over {_dur(s['span_seconds'])}"
    )
    if s["subagent_branches"]:
        add(f"  {s['subagent_branches']} subagent branch(es) separated from the main thread")
    if s["orphan_events"]:
        add(
            f"  note: {s['orphan_events']} events reference a parent outside this file "
            f"(resumed or forked session)"
        )
    if analysis.trace.skipped_lines:
        add(f"  note: {analysis.trace.skipped_lines} unparseable line(s) skipped")
    add("")

    # --- cost -------------------------------------------------------------
    marker = "" if c.is_complete else "  (at least — some tokens unpriced)"
    add(f"COST  ${c.total_usd:,.2f}{marker}      rates as of {c.pricing_as_of}")
    add(
        f"  {'model':<22}{'msgs':>7}{'output':>11}{'cache read':>13}"
        f"{'1h writes':>12}{'usd':>11}"
    )
    for mc in c.by_model:
        flag = "" if mc.priced else "  [unpriced]"
        add(
            f"  {mc.model:<22}{mc.calls:>7}{_n(mc.usage.output_tokens):>11}"
            f"{_n(mc.usage.cache_read_tokens):>13}{_n(mc.usage.cache_write_1h_tokens):>12}"
            f"{_usd(mc.total_usd):>11}{flag}"
        )
    share = c.cache_write_1h_usd_share
    if share > 0:
        note = "  <- rebuilding a long cached prefix is where this run's money went" if share >= 0.25 else ""
        add(f"  {share * 100:.1f}% of spend was 1-hour cache writes{note}")
    if not c.is_complete:
        add(
            f"  unpriced: {', '.join(c.unpriced_models)} "
            f"({_n(c.unpriced_tokens)} tokens not in the price table)"
        )
    add("")

    if len(c.by_branch) > 1:
        add("SPEND BY BRANCH")
        for name, usd in sorted(c.by_branch.items(), key=lambda kv: (-kv[1], kv[0])):
            branch = analysis.graph.branch_named(name)
            calls = branch.tool_calls if branch else 0
            add(f"  {name:<16}${_usd(usd):>10}   {calls} tool calls")
        add("")

    if s["tools"]:
        add("TOOLS")
        line = "  "
        for entry in s["tools"][:14]:
            chunk = f"{entry['tool']} {entry['calls']}   "
            if len(line) + len(chunk) > 96:
                add(line.rstrip())
                line = "  "
            line += chunk
        add(line.rstrip())
        add("")

    # --- findings ---------------------------------------------------------
    counts = {HIGH: 0, MEDIUM: 0, LOW: 0}
    for f in analysis.findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1
    add(
        f"FINDINGS  {len(analysis.findings)} total "
        f"({counts[HIGH]} high, {counts[MEDIUM]} medium, {counts[LOW]} low)"
    )
    if not analysis.findings:
        add("  nothing tripped a detector.")
    shown = analysis.findings if max_findings is None else analysis.findings[:max_findings]
    for f in shown:
        branch = "" if f.branch == "main" else f"  [{f.branch}]"
        add(f"  [{_BADGE.get(f.severity, '?')}] {f.detector:<14} {f.title}{branch}")
        add(f"         {f.detail}")
    remaining = len(analysis.findings) - len(shown)
    if remaining > 0:
        add(f"  ... {remaining} more (use --max-findings all)")
    add("")
    add(f"  payload digest {analysis.digest()[:16]}  ·  detectors ran with no model calls")
    add("")
    return "\n".join(out)
