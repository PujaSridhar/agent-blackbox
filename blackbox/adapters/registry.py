"""Adapter selection."""

from __future__ import annotations

from blackbox.adapters import claude_code, codex
from blackbox.adapters.base import Trace

ADAPTERS = (claude_code, codex)


class NoAdapterError(RuntimeError):
    pass


def load(path: str, adapter: str | None = None) -> Trace:
    """Parse ``path`` with the named adapter, or the first one that recognises it."""
    if adapter:
        for mod in ADAPTERS:
            if mod.NAME == adapter:
                return mod.parse(path)
        names = ", ".join(m.NAME for m in ADAPTERS)
        raise NoAdapterError(f"unknown adapter {adapter!r}; available: {names}")

    for mod in ADAPTERS:
        if mod.sniff(path):
            return mod.parse(path)
    raise NoAdapterError(
        f"no adapter recognised {path!r}. Supported: "
        + ", ".join(m.NAME for m in ADAPTERS)
    )
