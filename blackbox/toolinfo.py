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


# How a call touches a file. ``read`` observes it, ``write`` replaces or
# creates it (so the agent knows the contents afterwards either way), ``edit``
# changes it in place (so the agent needs to have known them beforehand).
READ = "read"
WRITE = "write"
EDIT = "edit"

# Some tools touch several files in one call -- a patch tool is the obvious
# case. The adapter normalises those into this list so the detectors never
# have to know which agent produced the trace.
MULTI_FILE_KEY = "files"


def file_operations(tool_name: str | None, tool_input: dict[str, Any]) -> list[tuple[str, str]]:
    """Every (path, kind) this call touches.

    Returning a list rather than one path is what lets a patch-style tool --
    one call that adds two files and updates a third -- be understood by the
    same detectors that handle single-file edits.
    """
    if not tool_name:
        return []

    entries = tool_input.get(MULTI_FILE_KEY)
    if isinstance(entries, list):
        operations = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            path = entry.get("path")
            kind = entry.get("op")
            if isinstance(path, str) and path and kind in (READ, WRITE, EDIT):
                operations.append((path, kind))
        return operations

    path = None
    for key in PATH_KEYS.get(tool_name, ()):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            path = value
            break
    if path is None:
        return []

    if tool_name in READ_TOOLS:
        return [(path, READ)]
    if tool_name in EDIT_IN_PLACE_TOOLS:
        return [(path, EDIT)]
    if tool_name in WRITE_TOOLS:
        return [(path, WRITE)]
    return []


def target_path(tool_name: str | None, tool_input: dict[str, Any]) -> str | None:
    """The first file path a call operates on, or ``None``."""
    operations = file_operations(tool_name, tool_input)
    return operations[0][0] if operations else None


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


# Agents read files through the shell as often as through a file tool --
# `sed -n '1,200p' src/app.js` is a read by any sensible reading. Missing that
# made every later edit of such a file look blind: on the Codex corpus, 84 of
# 84 blind-edit findings had a shell read of the same file beforehand.
#
# These paths deliberately do NOT become canonical read operations. A shell
# read returns a slice, formatted however the command chose, so hashing it and
# comparing against a later read would invent stale-read findings. They are
# used only to answer "has this agent seen this file at all?".
SHELL_READ_COMMANDS = frozenset(
    {"cat", "head", "tail", "sed", "nl", "less", "more", "bat", "view", "rg", "grep", "awk"}
)

_SHELL_SPLIT = re.compile(r"&&|\|\||[;|\n]")


# A filename, possibly with directories. Deliberately excludes things that
# are arguments rather than paths: sed scripts (`1,200p`), line numbers, and
# anything with shell metacharacters.
_PATHLIKE = re.compile(r"[A-Za-z_][A-Za-z0-9._\-/]*\Z")

# These take a pattern before the filenames, which must not be read as a path.
_PATTERN_FIRST = frozenset({"rg", "grep", "awk"})


def _looks_like_path(token: str) -> bool:
    if not token or token.startswith("-"):
        return False
    if token in {".", "..", "*"}:
        return False
    return bool(_PATHLIKE.match(token))


def shell_read_paths(tool_name: str | None, tool_input: dict[str, Any]) -> list[str]:
    """File paths a shell command appears to read.

    Deliberately permissive: over-counting a read only ever suppresses a
    blind-edit report, while under-counting invents one.
    """
    if tool_name not in {"Bash", "exec_command"}:
        return []
    command = tool_input.get("command") or tool_input.get("cmd")
    if not isinstance(command, str):
        return []

    paths: list[str] = []
    for segment in _SHELL_SPLIT.split(command):
        tokens = segment.split()
        if not tokens:
            continue
        program = None
        for token in tokens:
            cleaned = token.strip("\\\"'`()$&|;")
            if not cleaned or "=" in cleaned or cleaned.startswith("-"):
                continue
            program = cleaned.split("/")[-1]
            break
        if program not in SHELL_READ_COMMANDS:
            continue
        skip_pattern = program in _PATTERN_FIRST
        started = False
        for token in tokens:
            cleaned = token.strip("\\\"'`()$&|;<>")
            if not started:
                # Skip forward to just past the command name itself.
                started = cleaned.split("/")[-1] == program
                continue
            if cleaned.startswith("-") or "=" in cleaned:
                continue
            if skip_pattern:
                skip_pattern = False  # that was the search pattern, not a file
                continue
            if _looks_like_path(cleaned):
                paths.append(cleaned)
    return paths


def paths_overlap(a: str, b: str) -> bool:
    """True when two paths plausibly name the same file.

    A shell command usually names a file relative to the working directory
    while a patch names it absolutely, so the comparison is on trailing path
    components rather than on the strings.
    """
    left = [part for part in a.split("/") if part and part != "."]
    right = [part for part in b.split("/") if part and part != "."]
    if not left or not right:
        return False
    shorter, longer = (left, right) if len(left) <= len(right) else (right, left)
    return longer[-len(shorter):] == shorter
