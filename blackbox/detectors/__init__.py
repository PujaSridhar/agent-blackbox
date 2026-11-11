"""Detector registry.

Order here is irrelevant to the output: findings are sorted by severity and
then by a stable key, so adding a detector cannot reshuffle existing results.
"""

from __future__ import annotations

from blackbox.detectors import (
    contradictions,
    error_clusters,
    retry_loops,
    stalls,
    thrash,
)
from blackbox.detectors.base import Config, Finding
from blackbox.graph import Graph

DETECTORS = (retry_loops, stalls, error_clusters, contradictions, thrash)

__all__ = ["Config", "Finding", "DETECTORS", "run_all"]


def run_all(graph: Graph, config: Config | None = None) -> list[Finding]:
    """Run every detector and return findings in a stable, severity-first order."""
    config = config or Config()
    findings: list[Finding] = []
    for module in DETECTORS:
        findings.extend(module.run(graph, config))
    findings.sort(key=Finding.sort_key)
    return findings
