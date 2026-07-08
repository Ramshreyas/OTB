"""Data models for reconciliation output."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ReconciliationVerdict:
    """The result of reconciling normalized observations against market rules.

    Attributes:
        verdict: "Yes", "No", "p3_tie", "p4_too_early", or "unclear".
        gate: Which gate produced the verdict ("finality", "quality", "comparison").
        operator: Parsed operator: "exact", "gte", "lte", "range".
        threshold: Parsed threshold value(s) — single float or [low, high].
        comparison_result: True/False/None (None if gated before comparison).
        confidence_penalties: List of (flag_name, penalty_amount) tuples.
        reasoning: Concise explanation string.
        normalized_value: The value that was compared.
    """

    verdict: str                                          # "Yes" | "No" | "p3_tie" | "p4_too_early" | "unclear"
    gate: str                                             # "finality" | "quality" | "comparison"
    operator: str = ""                                    # "exact" | "gte" | "lte" | "range"
    threshold: float | tuple[float, float] | None = None  # Single or range
    comparison_result: Optional[bool] = None
    confidence_penalties: tuple[tuple[str, float], ...] = ()
    reasoning: str = ""
    normalized_value: float = 0.0

    def __post_init__(self):
        # Empty string is allowed as a transitional state (e.g., quality gate
        # returns penalties but leaves the final verdict for the comparison gate).
        valid_verdicts = {"", "Yes", "No", "p3_tie", "p4_too_early", "unclear"}
        if self.verdict not in valid_verdicts:
            raise ValueError(f"verdict must be one of {valid_verdicts}")
