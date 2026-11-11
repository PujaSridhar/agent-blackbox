"""Detect an agent acting on a stale or absent view of a file.

Two patterns, both of which produce confidently wrong edits:

* **Stale read** -- the same file is read twice, the contents differ, and
  nothing in the run wrote to it in between. Something outside the agent moved
  under it (a build step, a formatter, another agent), so any reasoning from
  the first read was already void.
* **Blind edit** -- a file is edited *in place* with no prior read of it in
  the same branch. The agent is editing text it never looked at. Creating a
  file with ``Write`` does not count: that is the normal case.

Read digests come from the result payload, so a read whose result is missing
is skipped rather than assumed unchanged.
"""

from __future__ import annotations

from blackbox.detectors.base import HIGH, MEDIUM, Config, Finding
from blackbox.events import TOOL_CALL
from blackbox.graph import Graph
from blackbox.toolinfo import is_edit_in_place, is_read, is_write, target_path

NAME = "contradiction"


def run(graph: Graph, config: Config) -> list[Finding]:
    findings: list[Finding] = []

    for branch in graph.branches:
        # path -> list of (seq, kind, digest, uid) in order
        history: dict[str, list[tuple[int, str, str, str]]] = {}

        for ev in graph.events:
            if ev.branch != branch.name or ev.kind != TOOL_CALL:
                continue
            path = target_path(ev.tool_name, ev.tool_input)
            if not path:
                continue

            if is_read(ev.tool_name):
                result = graph.result_for(ev)
                if result is None:
                    continue
                history.setdefault(path, []).append(
                    (ev.seq, "read", result.output_digest(), ev.uid)
                )
            elif is_write(ev.tool_name):
                kind = "edit" if is_edit_in_place(ev.tool_name) else "write"
                history.setdefault(path, []).append((ev.seq, kind, "", ev.uid))

        for path in sorted(history):
            entries = history[path]

            # --- stale reads ---------------------------------------------
            last_read: tuple[int, str, str, str] | None = None
            wrote_since_read = False
            for entry in entries:
                _, kind, digest, _uid = entry
                if kind in ("write", "edit"):
                    wrote_since_read = True
                    continue
                if last_read is not None and not wrote_since_read:
                    if digest != last_read[2]:
                        findings.append(
                            Finding(
                                detector=NAME,
                                severity=HIGH,
                                title=f"{_short(path)} changed between two reads with no write",
                                detail=(
                                    f"{path} was read at step {last_read[0]} and again at "
                                    f"step {entry[0]} with different contents, and the run "
                                    f"never wrote to it in between. Something outside the "
                                    f"agent changed the file, so anything concluded from "
                                    f"the first read was already stale."
                                ),
                                key=f"stale:{path}:{entry[0]:08d}",
                                branch=branch.name,
                                evidence=(last_read[3], entry[3]),
                                metrics={
                                    "path": path,
                                    "kind": "stale_read",
                                    "first_seq": last_read[0],
                                    "second_seq": entry[0],
                                },
                            )
                        )
                last_read = entry
                wrote_since_read = False

            # --- blind edits ---------------------------------------------
            seen_read = False
            for seq, kind, _digest, uid in entries:
                if kind == "read":
                    seen_read = True
                    continue
                if kind != "edit":
                    # A plain Write may legitimately create the file.
                    continue
                if not seen_read:
                    findings.append(
                        Finding(
                            detector=NAME,
                            severity=MEDIUM,
                            title=f"{_short(path)} edited with no prior read",
                            detail=(
                                f"{path} was edited in place at step {seq} without being "
                                f"read anywhere in this branch, so the agent changed "
                                f"content it never saw here. A read in an earlier session "
                                f"would not appear in this trace."
                            ),
                            key=f"blind:{path}:{seq:08d}",
                            branch=branch.name,
                            evidence=(uid,),
                            metrics={"path": path, "kind": "blind_write", "seq": seq},
                        )
                    )
                    # One report per path is enough; the pattern is the point.
                    break
                break

    return findings


def _short(path: str, keep: int = 2) -> str:
    parts = [p for p in path.split("/") if p]
    return "/".join(parts[-keep:]) if parts else path
