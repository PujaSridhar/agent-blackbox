"""The adapter boundary.

An adapter is the only part of this tool that knows a vendor's log format.
Everything else consumes :class:`~blackbox.events.Event`. Vendor trace formats
are mostly undocumented and they drift; isolating that churn here is what
keeps a format change from being a rewrite.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from blackbox.events import Event


@dataclass
class Trace:
    """A parsed agent run."""

    events: list[Event]
    source: str
    adapter: str
    meta: dict[str, Any] = field(default_factory=dict)
    skipped_lines: int = 0


@runtime_checkable
class Adapter(Protocol):
    name: str

    def sniff(self, path: str) -> bool:
        """Return True when this adapter recognises the file."""
        ...

    def parse(self, path: str) -> Trace:
        """Parse the file into canonical events."""
        ...
