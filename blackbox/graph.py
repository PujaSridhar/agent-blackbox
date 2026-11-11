"""The causal graph.

A trace file is a flat list of rows. This module turns it into the shape the
run actually had: a tree of events, with each subagent's work separated into
its own branch.

Why bother, when you could just count tool calls in file order? Because a flat
list cannot answer the two questions that make the rest of the report mean
anything:

* **Which result belongs to which call?** With parallel tool calls inside one
  assistant message, file order alone cannot say. Per-call durations computed
  from it are wrong, and "the agent stalled" becomes a coin flip.
* **Whose cost is this?** A subagent's tokens are billed to the run but are not
  the main thread's work. Smear them together and the expensive step looks
  cheap while the main thread looks bloated.

Branch names are assigned in order of first appearance, so the same trace
always produces the same names.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from blackbox.events import MAIN_BRANCH, TOOL_CALL, TOOL_RESULT, Event


@dataclass
class Branch:
    """One thread of execution: the main thread, or one subagent."""

    name: str
    root_uid: str | None
    event_count: int = 0
    tool_calls: int = 0
    first_seq: int = 0
    last_seq: int = 0

    @property
    def is_main(self) -> bool:
        return self.name == MAIN_BRANCH


@dataclass
class Graph:
    events: list[Event]
    by_uid: dict[str, Event] = field(default_factory=dict)
    children: dict[str, list[str]] = field(default_factory=dict)
    branches: list[Branch] = field(default_factory=list)
    roots: list[str] = field(default_factory=list)
    orphans: int = 0

    # -- lookups -----------------------------------------------------------

    def tool_calls(self, branch: str | None = None) -> list[Event]:
        return [
            e
            for e in self.events
            if e.kind == TOOL_CALL and (branch is None or e.branch == branch)
        ]

    def result_for(self, call: Event) -> Event | None:
        """The result answering ``call``, found through the re-parented edge."""
        for uid in self.children.get(call.uid, ()):
            child = self.by_uid.get(uid)
            if child is not None and child.kind == TOOL_RESULT:
                return child
        return None

    def branch_named(self, name: str) -> Branch | None:
        for b in self.branches:
            if b.name == name:
                return b
        return None


def build(events: list[Event]) -> Graph:
    """Assign branches and index the tree. ``events`` is not mutated."""
    ordered = sorted(events, key=lambda e: e.seq)
    by_uid = {e.uid: e for e in ordered}

    # --- branch assignment ------------------------------------------------
    # A sidechain event belongs to the branch of its nearest sidechain
    # ancestor. An event whose parent is absent or not a sidechain starts one.
    branch_of: dict[str, str] = {}
    branch_names: dict[str, str] = {}  # root uid -> display name
    next_index = 1

    for ev in ordered:
        if not ev.is_sidechain:
            branch_of[ev.uid] = MAIN_BRANCH
            continue

        parent = by_uid.get(ev.parent_uid) if ev.parent_uid else None
        if parent is not None and parent.is_sidechain:
            # Inherit the parent's branch. Parents precede children in seq
            # order in practice; if one does not, fall back to starting a
            # branch here rather than silently mislabelling the event.
            inherited = branch_of.get(parent.uid)
            if inherited and inherited != MAIN_BRANCH:
                branch_of[ev.uid] = inherited
                continue

        name = branch_names.get(ev.uid)
        if name is None:
            name = f"subagent-{next_index}"
            next_index += 1
            branch_names[ev.uid] = name
        branch_of[ev.uid] = name

    labelled = [replace(e, branch=branch_of.get(e.uid, MAIN_BRANCH)) for e in ordered]
    by_uid = {e.uid: e for e in labelled}

    # --- index ------------------------------------------------------------
    children: dict[str, list[str]] = {}
    roots: list[str] = []
    orphans = 0
    for ev in labelled:
        if ev.parent_uid is None:
            roots.append(ev.uid)
        elif ev.parent_uid in by_uid:
            children.setdefault(ev.parent_uid, []).append(ev.uid)
        else:
            orphans += 1
            roots.append(ev.uid)

    # --- branch summaries -------------------------------------------------
    summaries: dict[str, Branch] = {}
    for ev in labelled:
        b = summaries.get(ev.branch)
        if b is None:
            root = None if ev.branch == MAIN_BRANCH else ev.uid
            b = Branch(name=ev.branch, root_uid=root, first_seq=ev.seq, last_seq=ev.seq)
            summaries[ev.branch] = b
        b.event_count += 1
        b.last_seq = max(b.last_seq, ev.seq)
        if ev.kind == TOOL_CALL:
            b.tool_calls += 1

    branches = sorted(summaries.values(), key=lambda b: (not b.is_main, b.first_seq))

    return Graph(
        events=labelled,
        by_uid=by_uid,
        children=children,
        branches=branches,
        roots=roots,
        orphans=orphans,
    )
