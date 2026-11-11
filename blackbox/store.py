"""The canonical event store.

Writing the parsed run to DuckDB buys two things:

* **SQL over an agent run.** ``select tool_name, count(*) from events ...`` is
  a better interface than any fixed dashboard, and it is the natural home for
  questions this tool's authors did not think of.
* **A replay substrate.** Once the canonical events are stored, the findings
  can be re-derived without the original trace file and without a model call.
  :mod:`blackbox.replay` does exactly that, and the determinism test asserts
  the result is byte-identical.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from blackbox.events import Event, Usage, parse_ts

if TYPE_CHECKING:  # pragma: no cover
    from blackbox.analysis import Analysis

EVENT_COLUMNS = (
    "uid",
    "seq",
    "kind",
    "parent_uid",
    "ts",
    "is_sidechain",
    "branch",
    "model",
    "tool_name",
    "tool_use_id",
    "tool_input",
    "tool_ok",
    "tool_output",
    "text",
    "flags",
    "raw_type",
    "source_line",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_5m_tokens",
    "cache_write_1h_tokens",
    "thinking_tokens",
)

_SCHEMA = """
create table events (
    uid text, seq bigint, kind text, parent_uid text, ts text,
    is_sidechain boolean, branch text, model text,
    tool_name text, tool_use_id text, tool_input text, tool_ok boolean,
    tool_output text, text text, flags text, raw_type text, source_line bigint,
    input_tokens bigint, output_tokens bigint, cache_read_tokens bigint,
    cache_write_5m_tokens bigint, cache_write_1h_tokens bigint, thinking_tokens bigint
);
create table findings (
    detector text, severity text, title text, detail text, key text,
    branch text, count bigint, evidence text, metrics text
);
create table cost_by_model (
    model text, calls bigint, priced boolean,
    input_tokens bigint, output_tokens bigint, cache_read_tokens bigint,
    cache_write_5m_tokens bigint, cache_write_1h_tokens bigint, thinking_tokens bigint,
    input_usd double, output_usd double, cache_read_usd double,
    cache_write_usd double, total_usd double
);
create table run_meta (key text, value text);
"""


def _connect(path: str):
    try:
        import duckdb
    except ModuleNotFoundError as exc:  # pragma: no cover
        raise RuntimeError(
            "duckdb is required for --db. Install it with: pip install duckdb"
        ) from exc
    return duckdb.connect(path)


def write(path: str, analysis: "Analysis") -> None:
    """Write events, findings and cost to a fresh DuckDB file at ``path``."""
    import os

    if os.path.exists(path):
        os.remove(path)

    con = _connect(path)
    try:
        con.execute(_SCHEMA)

        rows = [tuple(e.to_row()[c] for c in EVENT_COLUMNS) for e in analysis.graph.events]
        if rows:
            placeholders = ",".join("?" * len(EVENT_COLUMNS))
            con.executemany(f"insert into events values ({placeholders})", rows)

        finding_rows = [
            (
                f.detector,
                f.severity,
                f.title,
                f.detail,
                f.key,
                f.branch,
                f.count,
                json.dumps(list(f.evidence)),
                json.dumps({k: f.metrics[k] for k in sorted(f.metrics)}, default=str),
            )
            for f in analysis.findings
        ]
        if finding_rows:
            con.executemany("insert into findings values (?,?,?,?,?,?,?,?,?)", finding_rows)

        cost_rows = [
            (
                mc.model,
                mc.calls,
                mc.priced,
                mc.usage.input_tokens,
                mc.usage.output_tokens,
                mc.usage.cache_read_tokens,
                mc.usage.cache_write_5m_tokens,
                mc.usage.cache_write_1h_tokens,
                mc.usage.thinking_tokens,
                mc.input_usd,
                mc.output_usd,
                mc.cache_read_usd,
                mc.cache_write_usd,
                mc.total_usd,
            )
            for mc in analysis.cost.by_model
        ]
        if cost_rows:
            con.executemany(
                "insert into cost_by_model values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", cost_rows
            )

        meta_rows = [
            ("source", analysis.trace.source),
            ("adapter", analysis.trace.adapter),
            ("skipped_lines", str(analysis.trace.skipped_lines)),
            ("config", json.dumps(analysis.config.to_dict(), sort_keys=True)),
            ("pricing_as_of", analysis.cost.pricing_as_of),
        ]
        meta_rows += [
            (f"trace.{k}", json.dumps(v, sort_keys=True, default=str))
            for k, v in sorted(analysis.trace.meta.items())
        ]
        con.executemany("insert into run_meta values (?,?)", meta_rows)
    finally:
        con.close()


def read_events(path: str) -> list[Event]:
    """Reconstruct canonical events from a stored run.

    The round trip must be lossless for everything a detector reads, which is
    why the store keeps tool output in full and why ``flags`` survives as a
    list rather than a formatted string.
    """
    con = _connect(path)
    try:
        cols = ",".join(EVENT_COLUMNS)
        rows = con.execute(f"select {cols} from events order by seq").fetchall()
    finally:
        con.close()

    events: list[Event] = []
    for row in rows:
        d: dict[str, Any] = dict(zip(EVENT_COLUMNS, row))
        usage = Usage(
            input_tokens=int(d["input_tokens"] or 0),
            output_tokens=int(d["output_tokens"] or 0),
            cache_read_tokens=int(d["cache_read_tokens"] or 0),
            cache_write_5m_tokens=int(d["cache_write_5m_tokens"] or 0),
            cache_write_1h_tokens=int(d["cache_write_1h_tokens"] or 0),
            thinking_tokens=int(d["thinking_tokens"] or 0),
        )
        flags = tuple(f for f in (d["flags"] or "").split(",") if f)
        events.append(
            Event(
                uid=d["uid"],
                seq=int(d["seq"]),
                kind=d["kind"],
                parent_uid=d["parent_uid"],
                ts=parse_ts(d["ts"]),
                is_sidechain=bool(d["is_sidechain"]),
                branch=d["branch"],
                model=d["model"],
                usage=usage,
                tool_name=d["tool_name"],
                tool_use_id=d["tool_use_id"],
                tool_input=json.loads(d["tool_input"]) if d["tool_input"] else {},
                tool_ok=d["tool_ok"],
                tool_output=d["tool_output"] or "",
                text=d["text"] or "",
                flags=flags,
                raw_type=d["raw_type"] or "",
                source_line=int(d["source_line"] or 0),
            )
        )
    return events


def read_meta(path: str) -> dict[str, str]:
    con = _connect(path)
    try:
        rows = con.execute("select key, value from run_meta").fetchall()
    finally:
        con.close()
    return {k: v for k, v in rows}
