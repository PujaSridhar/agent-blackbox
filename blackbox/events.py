"""Canonical event model.

Every adapter turns its native trace format into a list of :class:`Event`.
Nothing downstream -- graph, cost, detectors, reports -- knows which agent
framework produced the trace. That boundary is the whole point of this module:
when a vendor changes their log format, only an adapter changes.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

# --- event kinds ----------------------------------------------------------

USER_MESSAGE = "user_message"
ASSISTANT_MESSAGE = "assistant_message"
TOOL_CALL = "tool_call"
TOOL_RESULT = "tool_result"
META = "meta"

KINDS = (USER_MESSAGE, ASSISTANT_MESSAGE, TOOL_CALL, TOOL_RESULT, META)

MAIN_BRANCH = "main"


def parse_ts(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp, tolerating a trailing ``Z``."""
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@dataclass(frozen=True)
class Usage:
    """Token usage for one model call, with the cache tiers kept apart.

    The cache split matters: a 5-minute cache write and a 1-hour cache write
    are billed at different multiples of the input rate, and a cache *read* is
    an order of magnitude cheaper than either. Collapsing these into one
    "input tokens" number is how an agent's real cost gets hidden.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_5m_tokens: int = 0
    cache_write_1h_tokens: int = 0
    thinking_tokens: int = 0

    @property
    def cache_write_tokens(self) -> int:
        return self.cache_write_5m_tokens + self.cache_write_1h_tokens

    @property
    def billed_input_tokens(self) -> int:
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens

    @property
    def total_tokens(self) -> int:
        return self.billed_input_tokens + self.output_tokens

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_5m_tokens=self.cache_write_5m_tokens + other.cache_write_5m_tokens,
            cache_write_1h_tokens=self.cache_write_1h_tokens + other.cache_write_1h_tokens,
            thinking_tokens=self.thinking_tokens + other.thinking_tokens,
        )

    def is_empty(self) -> bool:
        return self.total_tokens == 0


ZERO_USAGE = Usage()


@dataclass(frozen=True)
class Event:
    """One thing that happened in an agent run.

    ``seq`` is the authoritative order (source line order). Timestamps are
    advisory: they can be absent, and in long-lived sessions they can jump.
    Every downstream pass orders by ``seq`` so results never depend on clocks.
    """

    uid: str
    seq: int
    kind: str
    parent_uid: str | None = None
    ts: datetime | None = None
    is_sidechain: bool = False
    branch: str = MAIN_BRANCH

    model: str | None = None
    usage: Usage = ZERO_USAGE

    tool_name: str | None = None
    tool_use_id: str | None = None
    tool_input: dict[str, Any] = field(default_factory=dict)
    tool_ok: bool | None = None
    tool_output: str = ""

    text: str = ""
    flags: tuple[str, ...] = ()
    raw_type: str = ""
    source_line: int = 0

    # -- derived helpers ---------------------------------------------------

    @property
    def is_tool(self) -> bool:
        return self.kind in (TOOL_CALL, TOOL_RESULT)

    def output_digest(self) -> str:
        """Stable digest of a tool result's payload, for contradiction checks."""
        return hashlib.sha256(self.tool_output.encode("utf-8", "replace")).hexdigest()[:16]

    def input_digest(self) -> str:
        """Stable digest of a tool call's *meaningful* input.

        Keys that describe rather than determine the call are dropped, so that
        two invocations which do the same work collapse to the same digest even
        when their prose differs.
        """
        ignored = {"description", "action_summary", "label", "reason", "explanation"}
        payload = {k: v for k, v in sorted(self.tool_input.items()) if k not in ignored}
        blob = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=True)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]

    def to_row(self) -> dict[str, Any]:
        """Flat dict for the DuckDB event table."""
        d = asdict(self)
        d.pop("usage")
        d["ts"] = self.ts.isoformat() if self.ts else None
        d["tool_input"] = json.dumps(self.tool_input, sort_keys=True, default=str)
        d["flags"] = ",".join(self.flags)
        # Stored in full on purpose: the contradiction detector hashes tool
        # output, so a truncated copy would make a replay disagree with the
        # original pass. Truncation belongs in the renderers, not the store.
        d["tool_output"] = self.tool_output
        d["text"] = self.text
        for name in (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_5m_tokens",
            "cache_write_1h_tokens",
            "thinking_tokens",
        ):
            d[name] = getattr(self.usage, name)
        return d
