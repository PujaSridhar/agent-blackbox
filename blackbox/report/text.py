"""Terminal report."""

from __future__ import annotations

from blackbox.analysis import Analysis
from blackbox.describe import describe
from blackbox.detectors.base import HIGH, LOW, MEDIUM

_BADGE = {HIGH: "HIGH", MEDIUM: "MED ", LOW: "LOW "}


def _n(value: int) -> str:
    return f"{value:,}"


def _tail(text: str, width: int) -> str:
    """Keep the end of a long path, marking what was dropped."""
    return text if len(text) <= width else "…" + text[-(width - 1):]


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
    extra = []
    if s.get("human_wait_seconds"):
        extra.append(f"{_dur(s['human_wait_seconds'])} waiting on you")
    if s.get("declined_calls"):
        extra.append(f"{s['declined_calls']} call(s) you declined")
    if extra:
        add("  " + " · ".join(extra))
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

    waste = analysis.wasted()
    if waste["usd"] > 0:
        parts = ", ".join(
            f"{name.replace('_', ' ')} ${value:,.2f}"
            for name, value in sorted(waste["by_detector"].items(), key=lambda kv: -kv[1])
        )
        add(
            f"  at least ${waste['usd']:,.2f} ({waste['share_of_run'] * 100:.1f}%) went to work "
            f"that did not advance the task"
        )
        add(f"    {parts}")
        add("    a floor: it counts the turns that issued these calls, not the context they left behind")
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
        usd = analysis.finding_usd(f)
        price = f"  ${_usd(usd)}" if usd > 0 else ""
        add(f"  [{_BADGE.get(f.severity, '?')}] {f.detector:<14} {f.title}{branch}{price}")
        add(f"         {f.detail}")
        for ev_uid in f.evidence[:2]:
            event = analysis.graph.by_uid.get(ev_uid)
            if event is not None:
                add(f"         · step {event.seq} line {event.source_line}  {describe(event)}")
    remaining = len(analysis.findings) - len(shown)
    if remaining > 0:
        add(f"  ... {remaining} more (use --max-findings all)")
    add("")
    add(f"  payload digest {analysis.digest()[:16]}  ·  detectors ran with no model calls")
    add("")
    return "\n".join(out)


# --- corpus ---------------------------------------------------------------


def render_corpus(corpus, title: str = "ACROSS ALL RUNS") -> str:
    """Summary across many traces."""
    s = corpus.summary()
    out: list[str] = []
    add = out.append

    add("")
    add(f"AGENT BLACK BOX   {title}")
    add(
        f"  {s['runs']} run(s) · {_n(s['tool_calls'])} tool calls · "
        f"{_n(s['findings'])} findings · {_dur(s['active_seconds'])} active"
    )
    if s["unreadable"]:
        add(f"  note: {s['unreadable']} file(s) could not be read")
    add("")

    add(f"SPEND  ${s['total_usd']:,.2f} across {s['runs']} run(s)")
    if s["wasted_usd"] > 0:
        add(
            f"  at least ${s['wasted_usd']:,.2f} ({s['wasted_share'] * 100:.1f}%) on work "
            f"that did not advance the task"
        )
    add(
        f"  cache hit rate {s['cache_hit_rate'] * 100:.1f}% of billed input · "
        f"{_n(s['cache_write_1h_tokens'])} tokens of 1-hour cache writes"
    )
    add(f"  tool failure rate {s['failure_rate'] * 100:.1f}% ({_n(s['tool_failures'])} failed)")
    add("")

    if s["by_detector"]:
        add("FINDINGS BY TYPE")
        for name in sorted(s["by_detector"], key=lambda k: -s["by_detector"][k]):
            usd = s["usd_by_detector"].get(name, 0.0)
            money = f"   ${_usd(usd)}" if usd > 0 else ""
            add(f"  {name:<16}{s['by_detector'][name]:>5}{money}")
        add("")

    if s["top_thrashed"]:
        add("MOST REWRITTEN FILES")
        for entry in s["top_thrashed"][:6]:
            add(f"  {entry['writes']:>3}x  {_tail(entry['path'], 72)}")
        add("")

    if s["worst_loops"]:
        add("WORST RETRY LOOPS")
        for entry in s["worst_loops"][:5]:
            add(f"  {entry['repeats']:>3}x  {entry['tool'][:40]:<40} {entry['run'][:20]}")
        add("")

    if s["worst_bloat"]:
        add("MOST EXPENSIVE CONTEXT ADDITIONS  (estimated)")
        for entry in s["worst_bloat"][:5]:
            add(
                f"  ~${entry['est_usd']:>8,.2f}  {_n(entry['est_tokens']):>9} tok  "
                f"{entry['tool'][:30]:<30} {entry['run'][:18]}"
            )
        add("")

    return "\n".join(out)


# --- diff -----------------------------------------------------------------


def _fmt(value: float, unit: str) -> str:
    if unit == "usd":
        return f"${_usd(value)}"
    if unit == "rate":
        return f"{value * 100:.1f}%"
    if unit == "duration":
        return _dur(value)
    return f"{value:,.0f}" if abs(value - round(value)) < 0.05 else f"{value:,.1f}"


def _delta(value: float, unit: str) -> str:
    sign = "+" if value > 0 else ""
    if unit == "rate":
        return f"{sign}{value * 100:.1f}pp"
    if unit == "usd":
        return f"{sign}${_usd(abs(value))}" if value >= 0 else f"-${_usd(abs(value))}"
    if unit == "duration":
        return f"{sign}{_dur(abs(value))}"
    return f"{sign}{value:,.0f}" if abs(value - round(value)) < 0.05 else f"{sign}{value:,.1f}"


def render_diff(result: dict, before_label: str, after_label: str) -> str:
    out: list[str] = []
    add = out.append

    add("")
    add("AGENT BLACK BOX   diff")
    add(f"  before  {before_label}  ({result['runs']['before']} run(s))")
    add(f"  after   {after_label}  ({result['runs']['after']} run(s))")
    if result["uneven"]:
        add("  run counts differ, so totals are also shown per run and the verdict follows those")
    add("")

    header = f"  {'':<20}{'before':>12}{'after':>12}{'change':>12}   "
    add(header)
    for row in result["metrics"]:
        before = _fmt(row["before"], row["unit"])
        after = _fmt(row["after"], row["unit"])
        change = _delta(row["delta"], row["unit"])
        verdict = f"  {row['verdict']}" if row["verdict"] else ""
        add(f"  {row['label']:<20}{before:>12}{after:>12}{change:>12}{verdict}")
        if result["uneven"] and "before_per_run" in row:
            per = (
                f"{_fmt(row['before_per_run'], row['unit'])} -> "
                f"{_fmt(row['after_per_run'], row['unit'])} per run"
            )
            add(f"  {'':<20}{per:>36}")
    add("")

    if result["by_detector"]:
        add("FINDINGS BY TYPE")
        add(f"  {'':<20}{'before':>12}{'after':>12}{'change':>12}")
        for row in result["by_detector"]:
            verdict = f"  {row['verdict']}" if row["verdict"] else ""
            add(
                f"  {row['detector']:<20}{row['before']:>12,}{row['after']:>12,}"
                f"{_delta(row['delta'], 'count'):>12}{verdict}"
            )
        add("")

    if result["unpriced"]["before"] or result["unpriced"]["after"]:
        add("  note: one or both sides ran on models with no price available,")
        add("        so cost comparisons are incomplete. Supply rates with --prices.")
        add("")

    return "\n".join(out)
