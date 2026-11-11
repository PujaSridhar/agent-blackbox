"""Finding model and detector configuration.

Every detector is a pure function of the graph. No model calls, no network, no
clock reads -- which is what makes the whole report replayable (see
``blackbox.replay``). A detector that needed an LLM to decide whether
something was a retry loop would make this tool unverifiable, and an
unverifiable analysis of an unverifiable agent is worth nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

HIGH = "high"
MEDIUM = "medium"
LOW = "low"

_SEVERITY_RANK = {HIGH: 0, MEDIUM: 1, LOW: 2}


@dataclass(frozen=True)
class Finding:
    """One thing worth a human's attention, with the evidence to check it."""

    detector: str
    severity: str
    title: str
    detail: str
    key: str
    branch: str = "main"
    count: int = 1
    evidence: tuple[str, ...] = ()
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def rank(self) -> int:
        return _SEVERITY_RANK.get(self.severity, 9)

    def sort_key(self) -> tuple:
        """Total order over findings, independent of discovery order."""
        return (self.rank, self.detector, self.branch, self.key)

    def to_dict(self) -> dict[str, Any]:
        return {
            "detector": self.detector,
            "severity": self.severity,
            "title": self.title,
            "detail": self.detail,
            "key": self.key,
            "branch": self.branch,
            "count": self.count,
            "evidence": list(self.evidence),
            "metrics": {k: self.metrics[k] for k in sorted(self.metrics)},
        }


@dataclass(frozen=True)
class Config:
    """Detector thresholds. Everything tunable lives here, nothing is hidden."""

    stall_seconds: float = 60.0
    idle_seconds: float = 900.0
    retry_min_repeats: int = 3
    retry_window: int = 12
    thrash_min_edits: int = 3
    error_cluster_min: int = 2

    def to_dict(self) -> dict[str, Any]:
        return {
            "stall_seconds": self.stall_seconds,
            "idle_seconds": self.idle_seconds,
            "retry_min_repeats": self.retry_min_repeats,
            "retry_window": self.retry_window,
            "thrash_min_edits": self.thrash_min_edits,
            "error_cluster_min": self.error_cluster_min,
        }
