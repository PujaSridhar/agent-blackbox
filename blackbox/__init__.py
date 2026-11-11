"""Structured analytics for a single agent run, from the trace file it already writes.

The command line is the usual entry point, but everything it does is available
here, because the interesting questions are the ones nobody anticipated:

    >>> from blackbox import analyze
    >>> run = analyze("session.jsonl")
    >>> run.cost.total_usd
    319.28
    >>> run.wasted()["share_of_run"]
    0.078
    >>> [f.title for f in run.findings if f.severity == "high"]

Nothing in this path makes a model call or opens a socket.

Note that ``blackbox.replay`` is this function, not the module it lives in.
Internal code imports it as ``from blackbox.replay import replay`` so the two
never get confused -- a mistake that broke ``--verify`` for anyone installing
the package rather than running it from a checkout.
"""

from __future__ import annotations

from blackbox.adapters.base import Adapter, Trace
from blackbox.adapters.registry import ADAPTERS, NoAdapterError
from blackbox.analysis import Analysis, analyze, analyze_events
from blackbox.cost import CostReport, ModelCost
from blackbox.detectors.base import Config, Finding
from blackbox.events import Event, Usage
from blackbox.graph import Branch, Graph
from blackbox.pricing import PRICING_AS_OF, Price
from blackbox.replay import replay  # noqa: F401  (shadows the module of the same name)

__version__ = "0.2.0"

__all__ = [
    "__version__",
    # analysis
    "analyze",
    "analyze_events",
    "Analysis",
    "replay",
    # configuration and results
    "Config",
    "Finding",
    "CostReport",
    "ModelCost",
    # the canonical model
    "Event",
    "Usage",
    "Graph",
    "Branch",
    # adapters
    "Adapter",
    "Trace",
    "ADAPTERS",
    "NoAdapterError",
    # pricing
    "Price",
    "PRICING_AS_OF",
]
