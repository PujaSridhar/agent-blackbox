"""Human-readable labels for events.

A finding whose evidence reads ``e9ffa5aa-1c18-477f-9b2f-40eda0d86718:tc0`` is
not actionable: nobody can tell what that was without grepping the raw trace.
The same finding showing ``step 412 · Bash: bun test`` can be acted on
immediately.

Labels are derived from stored event fields only, so they survive a replay and
never change the payload digest for a given input.
"""

from __future__ import annotations

from typing import Any

from blackbox.events import (
    ASSISTANT_MESSAGE,
    META,
    TOOL_CALL,
    TOOL_RESULT,
    USER_MESSAGE,
    Event,
)

# The argument worth showing, per tool, in preference order.
_HEADLINE_ARGS = (
    "command",
    "file_path",
    "notebook_path",
    "path",
    "url",
    "query",
    "pattern",
    "prompt",
    "action",
    "skill",
)

_MAX = 110


def _clean(value: Any, limit: int = _MAX) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def headline_arg(tool_input: dict[str, Any]) -> str:
    """The most informative single argument of a tool call."""
    for key in _HEADLINE_ARGS:
        value = tool_input.get(key)
        if isinstance(value, (str, int, float)) and str(value).strip():
            return _clean(value)
    for key in sorted(tool_input):
        value = tool_input[key]
        if isinstance(value, (str, int, float)) and str(value).strip():
            return _clean(value)
    return ""


def describe(event: Event) -> str:
    """A one-line, human-readable summary of what an event was."""
    if event.kind == TOOL_CALL:
        arg = headline_arg(event.tool_input)
        name = event.tool_name or "tool"
        return f"{name}: {arg}" if arg else name

    if event.kind == TOOL_RESULT:
        if "declined" in event.flags:
            return "result: declined by the user"
        if "interrupted" in event.flags:
            return "result: interrupted"
        first = next((line.strip() for line in event.tool_output.splitlines() if line.strip()), "")
        prefix = "error" if event.tool_ok is False else "result"
        return f"{prefix}: {_clean(first)}" if first else f"{prefix}: (empty)"

    if event.kind == ASSISTANT_MESSAGE:
        text = _clean(event.text, 90)
        model = event.model or "assistant"
        return f"{model}: {text}" if text else f"{model} turn"

    if event.kind == USER_MESSAGE:
        text = _clean(event.text, 90)
        return f"user: {text}" if text else "user turn"

    if event.kind == META:
        return f"({event.raw_type})"

    return event.kind


def evidence_detail(event: Event, usd: float | None = None) -> dict[str, Any]:
    """Structured, renderable evidence for one event."""
    detail: dict[str, Any] = {
        "uid": event.uid,
        "seq": event.seq,
        "source_line": event.source_line,
        "ts": event.ts.isoformat() if event.ts else None,
        "tool": event.tool_name,
        "summary": describe(event),
    }
    if usd is not None:
        detail["usd"] = round(usd, 6)
    return detail
