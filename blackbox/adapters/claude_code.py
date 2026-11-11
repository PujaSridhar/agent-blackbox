"""Adapter for Claude Code session transcripts (``~/.claude/projects/**/*.jsonl``).

The format is undocumented and will drift. What this adapter relies on, in
rough order of how load-bearing it is:

* ``uuid`` / ``parentUuid`` -- the causal spine of the run
* ``isSidechain`` -- marks rows belonging to a subagent branch
* ``message.content[]`` -- ``tool_use`` blocks on assistant rows, ``tool_result``
  blocks on user rows, matched by ``tool_use_id``
* ``message.usage`` -- token counts, including the cache tiers
* ``timestamp`` -- advisory only; ordering always comes from line order

Anything it does not understand becomes a ``meta`` event rather than an error,
so a new row type added upstream costs fidelity, never a crash.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from blackbox.adapters.base import Trace
from blackbox.events import (
    ASSISTANT_MESSAGE,
    META,
    TOOL_CALL,
    TOOL_RESULT,
    USER_MESSAGE,
    Event,
    Usage,
    parse_ts,
)

NAME = "claude_code"

# Rows that exist for the client's own bookkeeping and carry no model work.
_BOOKKEEPING = {
    "queue-operation",
    "last-prompt",
    "custom-title",
    "file-history-snapshot",
    "atis-latch",
    "attachment",
}


def _usage(raw: Any) -> Usage:
    """Read a ``message.usage`` object, preferring the explicit cache split."""
    if not isinstance(raw, dict):
        return Usage()

    creation = raw.get("cache_creation")
    write_5m = 0
    write_1h = 0
    if isinstance(creation, dict):
        write_5m = int(creation.get("ephemeral_5m_input_tokens") or 0)
        write_1h = int(creation.get("ephemeral_1h_input_tokens") or 0)

    total_writes = int(raw.get("cache_creation_input_tokens") or 0)
    if total_writes and (write_5m + write_1h) == 0:
        # Older rows report only the total. Attribute it to the 5-minute tier,
        # which is the cheaper of the two, so an unknown split can only ever
        # understate the bill rather than inflate it.
        write_5m = total_writes

    details = raw.get("output_tokens_details")
    thinking = 0
    if isinstance(details, dict):
        thinking = int(details.get("thinking_tokens") or 0)

    return Usage(
        input_tokens=int(raw.get("input_tokens") or 0),
        output_tokens=int(raw.get("output_tokens") or 0),
        cache_read_tokens=int(raw.get("cache_read_input_tokens") or 0),
        cache_write_5m_tokens=write_5m,
        cache_write_1h_tokens=write_1h,
        thinking_tokens=thinking,
    )


def _result_text(content: Any) -> str:
    """Flatten a tool_result payload, which may be a string or a block list."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if isinstance(block.get("text"), str):
                    parts.append(block["text"])
                elif block.get("type"):
                    parts.append(f"[{block['type']}]")
        return "\n".join(parts)
    if content is None:
        return ""
    return str(content)


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b["text"]
            for b in content
            if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
        )
    return ""


def sniff(path: str) -> bool:
    """True when the first usable line looks like a Claude Code row."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for _ in range(20):
                line = fh.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    return False
                if not isinstance(row, dict):
                    return False
                if "sessionId" in row or row.get("type") in {"assistant", "user"}:
                    return True
    except OSError:
        return False
    return False


def parse(path: str) -> Trace:
    events: list[Event] = []
    # A row that carried tool results is represented by synthetic event uids
    # (``<uuid>:tr0``), so a later row whose parentUuid names that row would
    # dangle. This maps each source row uuid to the event that stands for it.
    representative: dict[str, str] = {}
    seq = 0
    skipped = 0
    session_id = None
    cwd = None
    git_branch = None
    versions: set[str] = set()

    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                continue
            if not isinstance(row, dict):
                skipped += 1
                continue

            session_id = session_id or row.get("sessionId")
            cwd = cwd or row.get("cwd")
            git_branch = git_branch or row.get("gitBranch")
            if isinstance(row.get("version"), str):
                versions.add(row["version"])

            raw_type = str(row.get("type") or "unknown")
            uid = row.get("uuid") or f"line{lineno}"
            parent = row.get("parentUuid")
            ts = parse_ts(row.get("timestamp"))
            sidechain = bool(row.get("isSidechain"))
            message = row.get("message") if isinstance(row.get("message"), dict) else {}
            model = message.get("model")
            content = message.get("content")

            tool_result_payload = row.get("toolUseResult")
            interrupted = bool(
                isinstance(tool_result_payload, dict) and tool_result_payload.get("interrupted")
            )

            if raw_type == "assistant":
                events.append(
                    Event(
                        uid=uid,
                        seq=seq,
                        kind=ASSISTANT_MESSAGE,
                        parent_uid=parent,
                        ts=ts,
                        is_sidechain=sidechain,
                        model=model,
                        usage=_usage(message.get("usage")),
                        text=_message_text(content),
                        raw_type=raw_type,
                        source_line=lineno,
                    )
                )
                seq += 1

                representative.setdefault(uid, uid)

                if isinstance(content, list):
                    for idx, block in enumerate(content):
                        if not isinstance(block, dict) or block.get("type") != "tool_use":
                            continue
                        tool_input = block.get("input")
                        events.append(
                            Event(
                                uid=f"{uid}:tc{idx}",
                                seq=seq,
                                kind=TOOL_CALL,
                                parent_uid=uid,
                                ts=ts,
                                is_sidechain=sidechain,
                                model=model,
                                tool_name=block.get("name"),
                                tool_use_id=block.get("id"),
                                tool_input=tool_input if isinstance(tool_input, dict) else {},
                                raw_type="tool_use",
                                source_line=lineno,
                            )
                        )
                        seq += 1
                continue

            if raw_type == "user":
                results = [
                    b
                    for b in (content if isinstance(content, list) else [])
                    if isinstance(b, dict) and b.get("type") == "tool_result"
                ]
                if results:
                    representative.setdefault(uid, f"{uid}:tr0")
                    for idx, block in enumerate(results):
                        is_error = block.get("is_error")
                        flags = ("interrupted",) if interrupted else ()
                        events.append(
                            Event(
                                uid=f"{uid}:tr{idx}",
                                seq=seq,
                                kind=TOOL_RESULT,
                                parent_uid=parent,
                                ts=ts,
                                is_sidechain=sidechain,
                                tool_use_id=block.get("tool_use_id"),
                                tool_ok=(not bool(is_error)) if is_error is not None else None,
                                tool_output=_result_text(block.get("content")),
                                flags=flags,
                                raw_type="tool_result",
                                source_line=lineno,
                            )
                        )
                        seq += 1
                    continue

                events.append(
                    Event(
                        uid=uid,
                        seq=seq,
                        kind=USER_MESSAGE,
                        parent_uid=parent,
                        ts=ts,
                        is_sidechain=sidechain,
                        text=_message_text(content),
                        raw_type=raw_type,
                        source_line=lineno,
                    )
                )
                seq += 1
                representative.setdefault(uid, uid)
                continue

            representative.setdefault(uid, uid)
            events.append(
                Event(
                    uid=uid,
                    seq=seq,
                    kind=META,
                    parent_uid=parent,
                    ts=ts,
                    is_sidechain=sidechain,
                    text=str(row.get("content") or "")[:2000],
                    raw_type=raw_type,
                    source_line=lineno,
                )
            )
            seq += 1

    events = _resolve_parents(events, representative)
    events = _link_results_to_calls(events)

    return Trace(
        events=events,
        source=path,
        adapter=NAME,
        skipped_lines=skipped,
        meta={
            "session_id": session_id,
            "cwd": cwd,
            "git_branch": git_branch,
            "client_versions": sorted(versions),
        },
    )


def _resolve_parents(events: list[Event], representative: dict[str, str]) -> list[Event]:
    """Point every parent edge at an event that actually exists.

    Without this, any row whose ``parentUuid`` names a row that carried tool
    results dangles -- the row's uuid is not an event uid, the synthetic
    ``<uuid>:tr0`` is. Those dangling edges read as orphans, and because a
    sidechain event with no visible parent starts a new branch, one subagent
    would be reported as several.
    """
    out: list[Event] = []
    for ev in events:
        if ev.parent_uid:
            resolved = representative.get(ev.parent_uid)
            if resolved and resolved != ev.parent_uid:
                ev = replace(ev, parent_uid=resolved)
        out.append(ev)
    return out


def _link_results_to_calls(events: list[Event]) -> list[Event]:
    """Re-parent each tool result onto the call it answers.

    In the raw file a result's ``parentUuid`` points at the assistant *message*.
    Pointing it at the specific ``tool_use`` instead is what makes the graph
    causal rather than merely chronological: with parallel tool calls in one
    message, the message-level link cannot say which result belongs to which
    call, and every per-call duration computed from it would be wrong.
    """
    call_uid_by_tool_use: dict[str, str] = {}
    for ev in events:
        if ev.kind == TOOL_CALL and ev.tool_use_id:
            call_uid_by_tool_use.setdefault(ev.tool_use_id, ev.uid)

    linked: list[Event] = []
    for ev in events:
        if ev.kind == TOOL_RESULT and ev.tool_use_id:
            call_uid = call_uid_by_tool_use.get(ev.tool_use_id)
            if call_uid:
                ev = replace(ev, parent_uid=call_uid)
        linked.append(ev)
    return linked
