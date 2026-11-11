"""Self-contained HTML report.

One file, no dependencies, no network requests -- so it can be committed to a
repo, attached to a ticket, or opened from a USB stick in five years and still
render. Every number on the page comes from the canonical payload, so the page
and the JSON output can never disagree.
"""

from __future__ import annotations

import html
import json

from blackbox.analysis import Analysis
from blackbox.detectors.base import HIGH, LOW, MEDIUM

_CSS = """
:root {
  color-scheme: light dark;
  --bg: #fbfbfa;
  --panel: #ffffff;
  --border: #e4e2dd;
  --ink: #1b1a18;
  --muted: #6c6862;
  --accent: #2f6f4f;
  --high: #b4452f;
  --med: #b07d26;
  --low: #5a7a8c;
  --bar: #cfd8d2;
  --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #17181a;
    --panel: #1e2023;
    --border: #32353a;
    --ink: #e8e6e3;
    --muted: #9b968e;
    --accent: #7fc4a0;
    --high: #e8836a;
    --med: #e0b457;
    --low: #92b4c8;
    --bar: #3a4047;
  }
}
:root[data-theme="dark"] {
  --bg: #17181a;
  --panel: #1e2023;
  --border: #32353a;
  --ink: #e8e6e3;
  --muted: #9b968e;
  --accent: #7fc4a0;
  --high: #e8836a;
  --med: #e0b457;
  --low: #92b4c8;
  --bar: #3a4047;
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 0 16px 72px;
  background: var(--bg); color: var(--ink);
  font: 15px/1.6 ui-sans-serif, -apple-system, "Segoe UI", Roboto, sans-serif;
}
.wrap { max-width: 980px; margin: 0 auto; }
header { padding: 40px 0 24px; border-bottom: 1px solid var(--border); }
h1 { margin: 0 0 6px; font-size: 25px; letter-spacing: -0.015em; font-weight: 650; }
.sub { color: var(--muted); font-family: var(--mono); font-size: 12.5px; word-break: break-all; }
h2 {
  margin: 40px 0 14px; font-size: 12px; font-weight: 650;
  text-transform: uppercase; letter-spacing: 0.1em; color: var(--muted);
}
.tiles { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 22px; }
.tile {
  flex: 1 1 128px; background: var(--panel); border: 1px solid var(--border);
  border-radius: 9px; padding: 13px 15px;
}
.tile .v { font-size: 22px; font-weight: 640; font-family: var(--mono); letter-spacing: -0.02em; }
.tile .k { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.07em; margin-top: 3px; }
.panel {
  background: var(--panel); border: 1px solid var(--border);
  border-radius: 9px; padding: 4px 16px; margin-bottom: 12px;
}
table { width: 100%; border-collapse: collapse; font-family: var(--mono); font-size: 12.5px; }
th {
  text-align: right; padding: 11px 7px; color: var(--muted); font-weight: 500;
  border-bottom: 1px solid var(--border); font-size: 11px;
  text-transform: uppercase; letter-spacing: 0.05em; white-space: nowrap;
}
th:first-child, td:first-child { text-align: left; }
td { padding: 9px 7px; text-align: right; border-bottom: 1px solid var(--border); }
tr:last-child td { border-bottom: none; }
.tag {
  display: inline-block; font-size: 10px; padding: 1px 6px; border-radius: 4px;
  border: 1px solid var(--border); color: var(--muted); margin-left: 6px;
  font-family: var(--mono); text-transform: uppercase; letter-spacing: 0.04em;
}
.split { display: flex; height: 9px; border-radius: 5px; overflow: hidden; margin: 6px 0 2px; background: var(--bar); }
.split span { display: block; height: 100%; }
.legend { display: flex; flex-wrap: wrap; gap: 14px; font-size: 11.5px; color: var(--muted); font-family: var(--mono); margin-bottom: 14px; }
.legend i { display: inline-block; width: 9px; height: 9px; border-radius: 2px; margin-right: 5px; vertical-align: -1px; }
.bars { font-family: var(--mono); font-size: 12.5px; }
.bars .row { display: flex; align-items: center; gap: 10px; padding: 4px 0; }
.bars .lbl { flex: 0 0 180px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.bars .track { flex: 1; background: var(--bar); border-radius: 4px; height: 11px; }
.bars .fill { background: var(--accent); height: 100%; border-radius: 4px; }
.bars .num { flex: 0 0 52px; text-align: right; color: var(--muted); }
.finding { border-left: 3px solid var(--border); padding: 12px 0 12px 14px; margin: 0 0 4px; border-bottom: 1px solid var(--border); }
.finding:last-child { border-bottom: none; }
.finding.high { border-left-color: var(--high); }
.finding.medium { border-left-color: var(--med); }
.finding.low { border-left-color: var(--low); }
.finding .ttl { font-weight: 600; font-size: 14px; }
.finding .meta { font-family: var(--mono); font-size: 11px; color: var(--muted); margin-top: 3px; }
.finding .det { color: var(--muted); font-size: 13.5px; margin-top: 7px; }
.sev { font-family: var(--mono); font-size: 10px; text-transform: uppercase; letter-spacing: 0.08em; font-weight: 700; }
.sev.high { color: var(--high); } .sev.medium { color: var(--med); } .sev.low { color: var(--low); }
details.ev { margin-top: 8px; }
details.ev summary { cursor: pointer; font-family: var(--mono); font-size: 11px; color: var(--muted); }
details.ev pre {
  margin: 8px 0 0; padding: 10px; background: var(--bg); border: 1px solid var(--border);
  border-radius: 6px; font-size: 11px; overflow-x: auto; color: var(--muted);
}
.note { color: var(--muted); font-size: 12.5px; margin: 10px 0 0; }
.ok { color: var(--accent); }
footer {
  margin-top: 48px; padding-top: 18px; border-top: 1px solid var(--border);
  color: var(--muted); font-family: var(--mono); font-size: 11.5px; line-height: 1.8;
}
@media (max-width: 620px) {
  .bars .lbl { flex-basis: 108px; }
  th, td { padding: 8px 4px; font-size: 11.5px; }
  .tile { flex-basis: 100%; }
}
"""

_TIER_COLORS = {
    "output": "var(--high)",
    "cache writes": "var(--med)",
    "fresh input": "var(--accent)",
    "cache reads": "var(--low)",
}


def _esc(value) -> str:
    return html.escape(str(value), quote=True)


def _n(value: int) -> str:
    return f"{value:,}"


def _usd(value: float) -> str:
    """Never render real spend as $0.00."""
    if value and abs(value) < 0.01:
        return f"{value:.4f}"
    return f"{value:,.2f}"


def _dur(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}min"
    return f"{seconds / 3600:.1f}h"


def _spend_by_tier(analysis: Analysis) -> list[tuple[str, float]]:
    c = analysis.cost
    fresh = sum(mc.input_usd for mc in c.by_model)
    out = sum(mc.output_usd for mc in c.by_model)
    reads = sum(mc.cache_read_usd for mc in c.by_model)
    writes = sum(mc.cache_write_usd for mc in c.by_model)
    return [
        ("output", out),
        ("cache writes", writes),
        ("fresh input", fresh),
        ("cache reads", reads),
    ]


def render(analysis: Analysis) -> str:
    s = analysis.summary()
    c = analysis.cost
    p = analysis.payload()

    counts = {HIGH: 0, MEDIUM: 0, LOW: 0}
    for f in analysis.findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1

    parts: list[str] = []
    add = parts.append

    add("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">")
    add('<meta name="viewport" content="width=device-width, initial-scale=1">')
    add(f"<title>Black Box — {_esc(p['source'])}</title>")
    add(f"<style>{_CSS}</style></head><body><div class=\"wrap\">")

    # --- header -----------------------------------------------------------
    add("<header>")
    add("<h1>Agent Black Box</h1>")
    add(
        f'<div class="sub">{_esc(p["source"])} · adapter {_esc(analysis.trace.adapter)}'
        + (f' · {_esc(analysis.trace.meta.get("git_branch"))}' if analysis.trace.meta.get("git_branch") else "")
        + "</div>"
    )
    cost_label = f"${c.total_usd:,.2f}" + ("" if c.is_complete else "+")
    add('<div class="tiles">')
    for value, key in (
        (cost_label, "cost"),
        (_n(s["tool_calls"]), "tool calls"),
        (_n(s["tool_failures"]), "failed"),
        (_dur(s["active_seconds"]), "active time"),
        (str(len(analysis.findings)), "findings"),
    ):
        add(f'<div class="tile"><div class="v">{_esc(value)}</div><div class="k">{_esc(key)}</div></div>')
    add("</div></header>")

    # --- cost -------------------------------------------------------------
    add("<h2>Cost</h2><div class=\"panel\">")
    tiers = [(name, usd) for name, usd in _spend_by_tier(analysis) if usd > 0]
    tier_total = sum(usd for _, usd in tiers)
    if tier_total > 0:
        add('<div class="split">')
        for name, usd in tiers:
            pct = usd / tier_total * 100
            add(f'<span style="width:{pct:.3f}%;background:{_TIER_COLORS[name]}" title="{_esc(name)}"></span>')
        add("</div>")
        add('<div class="legend">')
        for name, usd in tiers:
            add(
                f'<span><i style="background:{_TIER_COLORS[name]}"></i>{_esc(name)} '
                f"${usd:,.2f} ({usd / tier_total * 100:.0f}%)</span>"
            )
        add("</div>")

    add("<table><thead><tr><th>model</th><th>msgs</th><th>output</th>")
    add("<th>cache read</th><th>5m writes</th><th>1h writes</th><th>usd</th></tr></thead><tbody>")
    for mc in c.by_model:
        tag = "" if mc.priced else '<span class="tag">unpriced</span>'
        add(
            f"<tr><td>{_esc(mc.model)}{tag}</td><td>{_n(mc.calls)}</td>"
            f"<td>{_n(mc.usage.output_tokens)}</td><td>{_n(mc.usage.cache_read_tokens)}</td>"
            f"<td>{_n(mc.usage.cache_write_5m_tokens)}</td>"
            f"<td>{_n(mc.usage.cache_write_1h_tokens)}</td>"
            f"<td>{_usd(mc.total_usd)}</td></tr>"
        )
    add("</tbody></table>")

    share = c.cache_write_1h_usd_share
    if share > 0:
        tail = (
            " Rebuilding a long 1-hour-cached prefix is the most common way a run "
            "costs more than its token count suggests."
            if share >= 0.25
            else ""
        )
        add(f'<p class="note"><strong>{share * 100:.1f}%</strong> of spend was 1-hour cache writes.{tail}</p>')
    if not c.is_complete:
        add(
            f'<p class="note">{_n(c.unpriced_tokens)} tokens ran on models missing from the '
            f"price table ({_esc(', '.join(c.unpriced_models))}), so the total is a floor, "
            f"not a figure.</p>"
        )
    add("</div>")

    # --- branches ---------------------------------------------------------
    if len(s["branches"]) > 1:
        add("<h2>Branches</h2><div class=\"panel\"><table><thead><tr>")
        add("<th>branch</th><th>events</th><th>tool calls</th><th>usd</th></tr></thead><tbody>")
        for b in s["branches"]:
            usd = c.by_branch.get(b["name"], 0.0)
            add(
                f'<tr><td>{_esc(b["name"])}</td><td>{_n(b["events"])}</td>'
                f'<td>{_n(b["tool_calls"])}</td><td>{_usd(usd)}</td></tr>'
            )
        add("</tbody></table>")
        add(
            '<p class="note">Subagent work is attributed to its own branch, so the main '
            "thread's cost is the main thread's alone.</p></div>"
        )

    # --- tools ------------------------------------------------------------
    if s["tools"]:
        top = s["tools"][:12]
        peak = max(e["calls"] for e in top)
        add('<h2>Tools</h2><div class="panel"><div class="bars">')
        for entry in top:
            pct = entry["calls"] / peak * 100
            add(
                f'<div class="row"><div class="lbl">{_esc(entry["tool"])}</div>'
                f'<div class="track"><div class="fill" style="width:{pct:.2f}%"></div></div>'
                f'<div class="num">{entry["calls"]}</div></div>'
            )
        add("</div></div>")

    # --- findings ---------------------------------------------------------
    add(
        f"<h2>Findings — {len(analysis.findings)} "
        f"({counts[HIGH]} high, {counts[MEDIUM]} medium, {counts[LOW]} low)</h2>"
    )
    add('<div class="panel">')
    if not analysis.findings:
        add('<p class="note ok">Nothing tripped a detector.</p>')
    for f in analysis.findings:
        branch = "" if f.branch == "main" else f'<span class="tag">{_esc(f.branch)}</span>'
        add(f'<div class="finding {_esc(f.severity)}">')
        add(f'<div class="ttl">{_esc(f.title)}{branch}</div>')
        add(
            f'<div class="meta"><span class="sev {_esc(f.severity)}">{_esc(f.severity)}</span>'
            f" · {_esc(f.detector)}</div>"
        )
        add(f'<div class="det">{_esc(f.detail)}</div>')
        metrics = json.dumps({k: f.metrics[k] for k in sorted(f.metrics)}, indent=2, default=str)
        add(
            '<details class="ev"><summary>evidence &amp; metrics</summary><pre>'
            + _esc(metrics)
            + "\n\nevents:\n"
            + _esc("\n".join(f.evidence))
            + "</pre></details>"
        )
        add("</div>")
    add("</div>")

    # --- footer -----------------------------------------------------------
    cfg = analysis.config
    add("<footer>")
    add(f"payload digest {_esc(analysis.digest())}<br>")
    add(
        f"thresholds: stall &ge; {cfg.stall_seconds:.0f}s · idle &ge; {cfg.idle_seconds:.0f}s · "
        f"retry &ge; {cfg.retry_min_repeats} in {cfg.retry_window} calls · "
        f"thrash &ge; {cfg.thrash_min_edits} writes<br>"
    )
    add(f"prices as of {_esc(c.pricing_as_of)} · last event {_esc(s['last_ts'] or 'unknown')}<br>")
    add(
        "every number above was derived without a model call, and replays byte-identical "
        "from the stored events"
    )
    if s["orphan_events"]:
        add(
            f"<br>{_n(s['orphan_events'])} events reference a parent outside this file "
            f"(resumed or forked session)"
        )
    add("</footer>")

    add("</div></body></html>")
    return "".join(parts)
