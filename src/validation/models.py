"""Immutable data models for market cases.

These dataclasses represent a validated market case. They are frozen after
construction to ensure pipeline stages cannot accidentally mutate input data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass(frozen=True)
class Outcomes:
    """The four outcome labels for a market.

    Attributes:
        p1: Label for outcome p1 (typically "No").
        p2: Label for outcome p2 (typically "Yes").
        p3: Label for outcome p3 (typically "50/50 outcome").
        p4: Label for outcome p4 (typically "Too Early").
    """

    p1: str
    p2: str
    p3: str
    p4: str


@dataclass(frozen=True)
class QuestionData:
    """Structured question metadata extracted from the market case.

    Attributes:
        title: The market question title.
        end_date_iso: ISO 8601 date string for the target resolution date.
        outcomes: The four outcome labels.
        question_id: Unique OTB question identifier (optional).
        market_id: Polymarket market identifier (optional).
        market_slug: URL-friendly market slug (optional).
        gamma_slug: Gamma market slug (optional).
        proposal_time: When the proposal was created (optional).
        processed_time: When the proposal was processed by OTB (optional).
        resolution_conditions: Text describing resolution conditions (optional).
        proposed_outcome: The outcome proposed by the proposer (optional).
    """

    title: str
    end_date_iso: str
    outcomes: Outcomes
    question_id: Optional[str] = None
    market_id: Optional[str] = None
    market_slug: Optional[str] = None
    gamma_slug: Optional[str] = None
    proposal_time: Optional[str] = None
    processed_time: Optional[str] = None
    resolution_conditions: Optional[str] = None
    proposed_outcome: Optional[str] = None


@dataclass(frozen=True)
class MarketCase:
    """An immutable, validated market case.

    Once constructed, this object cannot be modified. Pipeline stages build
    parallel resolution data structures rather than mutating the case.

    Attributes:
        case_id: Unique identifier for this market case.
        polymarket_url: URL to the Polymarket event page.
        proposal_tx_hash: Transaction hash of the OTB proposal.
        question_data: Structured question metadata.
        ancillary_data: Raw ancillary data string containing station URL,
            unit, precision, finality rules, and bulletin board info.
        fixture_path: Optional path to a pre-captured fixture file.
    """

    case_id: str
    polymarket_url: str
    proposal_tx_hash: str
    question_data: QuestionData
    ancillary_data: str
    fixture_path: Optional[str] = None

    def __post_init__(self):
        """Validate field-level invariants after construction."""
        if not self.case_id.strip():
            raise ValueError("case_id must be a non-empty string")
        if not self.polymarket_url.strip():
            raise ValueError("polymarket_url must be a non-empty string")
        if not self.proposal_tx_hash.strip():
            raise ValueError("proposal_tx_hash must be a non-empty string")
        if not self.ancillary_data.strip():
            raise ValueError("ancillary_data must be a non-empty string")


@dataclass(frozen=True)
class MarketManifest:
    """The top-level container for a loaded and validated markets.json manifest.

    Attributes:
        schema_version: Version of the input manifest schema.
        markets: List of validated MarketCase objects, in input order.
        generated_at: ISO 8601 timestamp when the manifest was generated (optional).
        description: Human-readable description of the manifest (optional).
        market_selection: Metadata about market selection (optional).
    """

    schema_version: str
    markets: tuple[MarketCase, ...] = field(default_factory=tuple)
    generated_at: Optional[str] = None
    description: Optional[str] = None
    market_selection: Optional[dict] = None
