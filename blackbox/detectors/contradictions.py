"""Detect an agent acting on a stale or absent view of a file.

Two patterns, both of which produce confidently wrong edits:

* **Stale read** -- the same region of a file is read twice, the contents
  differ, and nothing in the run wrote to it in between. Something outside the
  agent moved under it (a build step, a formatter, another agent), so any
  reasoning from the first read was already void.
* **Blind edit** -- a file is edited in place with no prior read *and no prior
  write* in the same branch. The agent is changing text it has never seen.

Both rules were narrowed after running against real traces, where the naive
versions were wrong essentially every time:

* A ``Read`` can take ``offset`` and ``limit``. Reading lines 1-100 and then
  101-200 returns different bytes from an unchanged file, so comparisons are
  keyed by the region read, never by path alone. Without this the detector
  reports a file as changing underneath an agent that merely paged through it.
* ``Write`` establishes knowledge. An agent that created a file knows its
  contents as surely as if it had read them, so a later in-place edit is not
  blind. Without this, every file an agent builds from scratch and then tweaks
  is reported as a blind edit.
* **Reading through the shell counts.** ``sed -n '1,200p' src/app.js`` is a
  read. Ignoring it reported 84 of 84 Codex blind edits wrongly, because that
  agent inspects files with shell commands rather than a file tool.
"""

from __future__ import annotations

from dataclasses import dataclass

from blackbox.detectors.base import HIGH, MEDIUM, Config, Finding
from blackbox.events import TOOL_CALL
from blackbox.graph import Graph
from blackbox import toolinfo

NAME = "contradiction"

READ = toolinfo.READ
WRITE = toolinfo.WRITE
EDIT = toolinfo.EDIT


@dataclass(frozen=True)
class _Entry:
    seq: int
    kind: str
    digest: str
    uid: str
    region: tuple[object, object]


def _region(tool_input: dict) -> tuple[object, object]:
    """The slice of a file a read covers. ``(None, None)`` means the whole file."""
    return (tool_input.get("offset"), tool_input.get("limit"))


def run(graph: Graph, config: Config) -> list[Finding]:
    findings: list[Finding] = []

    for branch in graph.branches:
        history: dict[str, list[_Entry]] = {}
        # (seq, path) for every file the agent inspected through a shell.
        shell_seen: list[tuple[int, str]] = []

        for ev in graph.events:
            if ev.branch != branch.name or ev.kind != TOOL_CALL:
                continue

            for seen in toolinfo.shell_read_paths(ev.tool_name, ev.tool_input):
                shell_seen.append((ev.seq, seen))

            for path, kind in toolinfo.file_operations(ev.tool_name, ev.tool_input):
                if kind == READ:
                    result = graph.result_for(ev)
                    if result is None:
                        continue
                    history.setdefault(path, []).append(
                        _Entry(ev.seq, READ, result.output_digest(), ev.uid, _region(ev.tool_input))
                    )
                else:
                    history.setdefault(path, []).append(
                        _Entry(ev.seq, kind, "", ev.uid, (None, None))
                    )

        for path in sorted(history):
            entries = history[path]
            findings.extend(_stale_reads(path, entries, branch.name))
            findings.extend(_blind_edits(path, entries, branch.name, shell_seen))

    return findings


def _stale_reads(path: str, entries: list[_Entry], branch: str) -> list[Finding]:
    """A region re-read with different bytes, with no write in between."""
    found: list[Finding] = []
    last_read: dict[tuple[object, object], _Entry] = {}

    for entry in entries:
        if entry.kind in (WRITE, EDIT):
            # The run changed the file, so a differing re-read is expected.
            last_read.clear()
            continue

        previous = last_read.get(entry.region)
        if previous is not None and previous.digest != entry.digest:
            found.append(
                Finding(
                    detector=NAME,
                    severity=HIGH,
                    title=f"{_short(path)} changed between two reads with no write",
                    detail=(
                        f"{path} was read at step {previous.seq} and again at step "
                        f"{entry.seq} — the same region, different contents, and the run "
                        f"never wrote to it in between. Something outside the agent "
                        f"changed the file, so anything concluded from the first read "
                        f"was already stale."
                    ),
                    key=f"stale:{path}:{entry.seq:08d}",
                    branch=branch,
                    evidence=(previous.uid, entry.uid),
                    metrics={
                        "path": path,
                        "kind": "stale_read",
                        "first_seq": previous.seq,
                        "second_seq": entry.seq,
                        "region_offset": entry.region[0],
                        "region_limit": entry.region[1],
                    },
                )
            )
        last_read[entry.region] = entry

    return found


def _blind_edits(
    path: str,
    entries: list[_Entry],
    branch: str,
    shell_seen: list[tuple[int, str]],
) -> list[Finding]:
    """An in-place edit of a file the agent has not seen by any route."""
    knows = False
    for entry in entries:
        if not knows and any(
            seq < entry.seq and toolinfo.paths_overlap(path, seen) for seq, seen in shell_seen
        ):
            knows = True
        if entry.kind in (READ, WRITE):
            # Reading it or having authored it both count as knowing it.
            knows = True
            continue
        if entry.kind != EDIT:
            continue
        if knows:
            return []
        return [
            Finding(
                detector=NAME,
                severity=MEDIUM,
                title=f"{_short(path)} edited with no prior read",
                detail=(
                    f"{path} was edited in place at step {entry.seq} without being read "
                    f"or written anywhere in this branch, so the agent changed content "
                    f"it has never seen. A read in an earlier session would not appear "
                    f"in this trace."
                ),
                key=f"blind:{path}:{entry.seq:08d}",
                branch=branch,
                evidence=(entry.uid,),
                metrics={"path": path, "kind": "blind_write", "seq": entry.seq},
            )
        ]
    return []


def _short(path: str, keep: int = 2) -> str:
    parts = [p for p in path.split("/") if p]
    return "/".join(parts[-keep:]) if parts else path
