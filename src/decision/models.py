"""Data models for decision output."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class LLMReview:
    """Result of the LLM reviewer safety check.

    Attributes:
        invoked: Whether the reviewer was invoked.
        model: Which model was used.
        agreed: Whether the reviewer agreed with the deterministic result.
        reasoning: Reviewer's explanation.
    """

    invoked: bool = False
    model: str = ""
    agreed: bool = True
    reasoning: str = ""


@dataclass(frozen=True)
class Resolution:
    """Final resolution for a market case.

    Attributes:
        recommendation: p1, p2, p3, p4, or "unclear".
        confidence: 0.0 to 1.0, computed from objective factors.
        path: How the resolution was reached
            ("deterministic", "deterministic+llm_reviewed", "llm_escalated_to_unclear").
        llm_review: LLM review details (null if not invoked).
        reasoning: Concise explanation carried forward from reconciliation.
        review_reason: Why unclear or escalated (if applicable), else None.
    """

    recommendation: str  # "p1" | "p2" | "p3" | "p4" | "unclear"
    confidence: float
    path: str = "deterministic"
    llm_review: Optional[LLMReview] = None
    reasoning: str = ""
    review_reason: Optional[str] = None

    def __post_init__(self):
        valid = {"p1", "p2", "p3", "p4", "unclear"}
        if self.recommendation not in valid:
            raise ValueError(f"recommendation must be one of {valid}")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence must be 0.0-1.0, got {self.confidence}")
