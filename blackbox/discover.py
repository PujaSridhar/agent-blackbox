"""Finding trace files, so nobody has to know a UUID.

Agent transcripts live at ``~/.claude/projects/<encoded-cwd>/<session-id>.jsonl``.
Nobody remembers the session id of the run they just finished, and requiring it
is the difference between a tool people try once and a tool people use.

The directory name is the working directory with every non-alphanumeric
character replaced by a hyphen. That rule is derived, not assumed: it was
checked against every project directory on the machine this was written on by
comparing each directory name to the ``cwd`` recorded inside its transcripts.
"""

from __future__ import annotations

import glob
import os
import re
from dataclasses import dataclass

DEFAULT_ROOT = os.path.expanduser("~/.claude/projects")

_NON_ALNUM = re.compile(r"[^A-Za-z0-9]")


def encode_cwd(path: str) -> str:
    """The project directory name a working directory maps to."""
    return _NON_ALNUM.sub("-", os.path.abspath(path))


@dataclass
class Session:
    path: str
    project: str
    modified: float
    size: int

    @property
    def session_id(self) -> str:
        return os.path.splitext(os.path.basename(self.path))[0]


def _session(path: str) -> Session:
    stat = os.stat(path)
    return Session(
        path=path,
        project=os.path.basename(os.path.dirname(path)),
        modified=stat.st_mtime,
        size=stat.st_size,
    )


def traces_in(path: str) -> list[str]:
    """Every trace under ``path``, which may be a file, a directory or a glob."""
    if os.path.isfile(path):
        return [path]
    if os.path.isdir(path):
        return sorted(glob.glob(os.path.join(path, "**", "*.jsonl"), recursive=True))
    return sorted(glob.glob(path, recursive=True))


def candidate_dirs(cwd: str | None = None, root: str | None = None) -> list[str]:
    """Every project directory this working directory could be recorded under.

    A path and its symlink-resolved form encode differently -- on macOS
    ``/tmp/x`` resolves to ``/private/tmp/x`` -- and the agent recorded
    whichever one it saw. Checking both is the difference between finding a
    session and reporting that none exists.
    """
    root = root or DEFAULT_ROOT
    base = cwd or os.getcwd()
    names = []
    for variant in (os.path.abspath(base), os.path.realpath(base)):
        name = encode_cwd(variant)
        if name not in names:
            names.append(name)
    return [os.path.join(root, name) for name in names]


def sessions_for_cwd(cwd: str | None = None, root: str | None = None) -> list[Session]:
    """Sessions recorded for one working directory, newest first."""
    root = root or DEFAULT_ROOT
    found: dict[str, Session] = {}
    for directory in candidate_dirs(cwd, root):
        if not os.path.isdir(directory):
            continue
        for path in glob.glob(os.path.join(directory, "*.jsonl")):
            found.setdefault(os.path.realpath(path), _session(path))
    return sorted(found.values(), key=lambda s: s.modified, reverse=True)


def latest_for_cwd(cwd: str | None = None, root: str | None = None) -> Session | None:
    """The most recently written session for this working directory."""
    found = sessions_for_cwd(cwd, root)
    return found[0] if found else None


def recent_sessions(root: str | None = None, limit: int = 20) -> list[Session]:
    """The most recently written sessions across every project."""
    root = root or DEFAULT_ROOT
    found = [_session(p) for p in glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True)]
    found.sort(key=lambda s: s.modified, reverse=True)
    return found[:limit]


def decode_project(name: str) -> str:
    """A readable label for an encoded project directory name.

    The encoding is lossy -- a hyphen could have been a slash, a space or a
    dot -- so this is presentation only and never used to resolve a path.
    """
    return name.replace("-", "/").lstrip("/") or name
