"""Edge repair shared by every adapter.

Vendors record a tool result next to the *message* that issued the call, not
next to the call itself. With several tool calls in one message that edge
cannot say which result answers which call, so every per-call duration derived
from it is a guess. Both adapters re-point the edge using the vendor's own
call id, which is the one thing that is unambiguous.
"""

from __future__ import annotations

from dataclasses import replace

from blackbox.events import TOOL_CALL, TOOL_RESULT, Event


def link_results_to_calls(events: list[Event]) -> list[Event]:
    """Re-parent each tool result onto the call it answers."""
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
