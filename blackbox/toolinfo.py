"""What a tool call actually touches.

The detectors need to know, for an arbitrary tool, whether it read a file or
wrote one and which path it operated on. Keeping that knowledge in one small
table means adding a tool is a one-line change, and an unrecognised tool
degrades to "we don't know what this touched" rather than to a wrong answer.
"""

from __future__ import annotations

from typing import Any

# tool name -> the input key holding the path it operates on
PATH_KEYS: dict[str, tuple[str, ...]] = {
    "Read": ("file_path",),
    "Edit": ("file_path",),
    "Write": ("file_path",),
    "NotebookEdit": ("notebook_path",),
}

READ_TOOLS = frozenset({"Read"})
WRITE_TOOLS = frozenset({"Edit", "Write", "NotebookEdit"})

# Tools that edit *existing* content, so operating without having read the file
# is suspicious. ``Write`` is excluded on purpose: creating a new file without
# reading it first is the normal case, and flagging it would bury the signal.
EDIT_IN_PLACE_TOOLS = frozenset({"Edit", "NotebookEdit"})


def target_path(tool_name: str | None, tool_input: dict[str, Any]) -> str | None:
    """Return the file path a call operates on, or ``None`` if not applicable."""
    if not tool_name:
        return None
    for key in PATH_KEYS.get(tool_name, ()):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def is_read(tool_name: str | None) -> bool:
    return tool_name in READ_TOOLS


def is_write(tool_name: str | None) -> bool:
    return tool_name in WRITE_TOOLS


def is_edit_in_place(tool_name: str | None) -> bool:
    return tool_name in EDIT_IN_PLACE_TOOLS


# Tools whose job is to *observe*. Calling one repeatedly with identical input
# is polling -- watching a screen, re-reading a log -- and is normal behaviour.
# Calling an *acting* tool repeatedly with identical input is a retry loop.
# Conflating the two was the single biggest source of false positives when
# this was first run against real traces.
OBSERVATION_TOOLS = frozenset(
    {"Read", "Glob", "Grep", "ToolSearch", "WebFetch", "WebSearch", "NotebookRead"}
)

# Substrings that mark an MCP tool as an observer regardless of its server.
OBSERVATION_NAME_HINTS = (
    "read_page",
    "get_page_text",
    "read_terminal",
    "read_console",
    "read_network",
    "screenshot",
    "tabs_context",
    "list_",
    "get_status",
)

# For multiplexed tools (one name, many actions) the action decides.
OBSERVATION_ACTIONS = frozenset({"screenshot", "wait", "scroll", "zoom", "hover", "read"})


def is_observation(tool_name: str | None, tool_input: dict[str, Any]) -> bool:
    """True when repeating this exact call is polling rather than retrying."""
    if not tool_name:
        return False
    if tool_name in OBSERVATION_TOOLS:
        return True
    lowered = tool_name.lower()
    if any(hint in lowered for hint in OBSERVATION_NAME_HINTS):
        return True
    action = tool_input.get("action")
    return isinstance(action, str) and action in OBSERVATION_ACTIONS
