"""What a tool call actually touches.

The detectors need to know, for an arbitrary tool, whether it read a file or
wrote one and which path it operated on. Keeping that knowledge in one small
table means adding a tool is a one-line change, and an unrecognised tool
degrades to "we don't know what this touched" rather than to a wrong answer.
"""

from __future__ import annotations

import re
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

# Progress bookkeeping: tools whose whole job is to be called again with the
# same payload. Advancing a task list with {"advance": true} ten times is the
# tool working exactly as designed, not an agent stuck in a loop.
BOOKKEEPING_NAME_HINTS = ("update_plan", "todowrite", "wrap_up", "mark_chapter", "set_status")


def repeats_by_design(tool_name: str | None, tool_input: dict[str, Any]) -> bool:
    """True when the same call recurring with identical input is expected.

    Two families qualify: tools that *observe* (a screenshot returns something
    new each time by definition) and tools that record *progress* (the payload
    repeats because the step pointer, not the argument, is what moves).
    Treating either as a retry loop was the largest source of false positives
    when this was first run across a real corpus.
    """
    if not tool_name:
        return False
    if tool_name in OBSERVATION_TOOLS:
        return True
    lowered = tool_name.lower()
    if any(hint in lowered for hint in OBSERVATION_NAME_HINTS):
        return True
    if any(hint in lowered for hint in BOOKKEEPING_NAME_HINTS):
        return True
    action = tool_input.get("action")
    return isinstance(action, str) and action in OBSERVATION_ACTIONS


# Kept as the older name used by callers that only care about observation.
is_observation = repeats_by_design


# Tools that exist to wait for a human. Time spent in one is the human reading
# and typing, not the agent hanging, so it must never be reported as a stall.
HUMAN_IN_THE_LOOP_TOOLS = frozenset(
    {"AskUserQuestion", "ExitPlanMode", "EnterPlanMode", "mcp__ccd_turn__wrap_up"}
)

# A declined tool call is a human saying no. It is not a tool failure, and
# clustering it with real errors buries the real errors.
_DECLINE_MARKERS = (
    "the user doesn't want to proceed",
    "the user does not want to proceed",
    "user rejected",
    "user declined",
    "tool use was rejected",
    "permission denied by user",
)


def is_human_in_the_loop(tool_name: str | None) -> bool:
    return tool_name in HUMAN_IN_THE_LOOP_TOOLS


def is_declined(output: str) -> bool:
    """True when a failed result is a human refusing, not the tool breaking."""
    head = output[:400].lower()
    return any(marker in head for marker in _DECLINE_MARKERS)


# The first meaningful token of a shell command, used to keep unrelated
# failures in separate clusters even when their error text collapses.
_SHELL_NOISE = frozenset(
    {"cd", "sudo", "env", "time", "nohup", "exec", "command", "builtin", "nice", "xargs", "then", "do"}
)

_RUNNERS = frozenset(
    {"python", "python3", "uv", "uvx", "npx", "poetry", "bun", "npm", "pnpm", "yarn", "cargo", "go"}
)

_SEGMENT_SPLIT = re.compile(r"&&|\|\||[;|\n]")


def program_hint(tool_name: str | None, tool_input: dict[str, Any]) -> str:
    """A short label for *what was run*, for grouping failures.

    Two `Exit code 1` failures from `pytest` and from `npm run build` are not
    the same problem. Without this, a stripped error signature can still make
    them look identical.

    Shell plumbing is skipped: `cd /repo && pytest` is a pytest failure, not a
    cd failure.
    """
    if tool_name != "Bash":
        return ""
    command = tool_input.get("command")
    if not isinstance(command, str):
        return ""

    fallback = ""
    for segment in _SEGMENT_SPLIT.split(command):
        for raw in segment.split():
            token = raw.strip("\\\"'`()$&|;")
            if not token or "=" in token or token.startswith("-"):
                continue
            name = token.split("/")[-1][:24]
            if name in _SHELL_NOISE:
                break  # skip this whole segment, it is plumbing
            # `python -m pytest` is a pytest failure, not a python failure.
            # Runners say nothing on their own, so take what they ran.
            if name in _RUNNERS:
                tokens = segment.split()
                for i, tok in enumerate(tokens):
                    if tok in {"-m", "run", "exec"} and i + 1 < len(tokens):
                        return f"{name} {tokens[i + 1].split('/')[-1]}"[:32]
                for tok in tokens[1:]:
                    if not tok.startswith("-") and "=" not in tok:
                        return f"{name} {tok.split('/')[-1]}"[:32]
            return name
        else:
            continue
    return fallback
