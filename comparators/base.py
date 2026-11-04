from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class CompareResult:
    """Outcome of one comparator. Score is 0..1. Evidence feeds the mismatch explanation."""
    score: float
    method: str
    missing: list[str] = field(default_factory=list)   # in uploaded, absent in production
    extra: list[str] = field(default_factory=list)     # in production, absent in uploaded
    evidence: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.score = max(0.0, min(1.0, float(self.score)))


@dataclass
class CompareContext:
    attribute: str
    abs_tol: float = 0.01
    rel_tol: float = 0.005
    item_threshold: float = 0.8
