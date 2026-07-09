"""OTB API → MarketCase transformer.

Converts OTBMarketItem objects (from the Oracle API) into the MarketCase
format that the existing resolution pipeline expects. This is the bridge
between live OTB mode and the existing 6-stage resolution pipeline.

The transformer:
1. Generates deterministic case_ids from market/event slugs
2. Reconstructs polymarket_url
3. Parses resolution_conditions to extract outcome labels (p1/p2/p3/p4)
4. Derives end_date_iso from the API's end_date field
5. Preserves the raw API item for traceability
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime
from typing import Optional

from src.validation.models import MarketCase, Outcomes, QuestionData
from src.otb.api import OTBMarketItem

logger = logging.getLogger(__name__)


# ── Main transformation function ───────────────────────────────────────


def otb_item_to_market_case(item: OTBMarketItem) -> MarketCase:
    """Transform a single OTB API item into a validated MarketCase.

    Args:
        item: An OTBMarketItem from the Oracle API.

    Returns:
        A validated, immutable MarketCase ready for the pipeline.

    Raises:
        ValueError: If required fields are missing or unparseable.
    """
    case_id = _derive_case_id(item)
    polymarket_url = _build_polymarket_url(item)
    outcomes = _parse_outcomes(item.resolution_conditions, item.title)
    end_date_iso = _normalize_end_date(item.end_date)

    # Extract market_id from ancillary_text if available
    market_id = _extract_market_id(item.ancillary_text, item.question_id)

    question_data = QuestionData(
        title=item.title,
        end_date_iso=end_date_iso,
        outcomes=outcomes,
        question_id=item.question_id,
        market_id=market_id,
        market_slug=item.market_slug,
        gamma_slug=None,
        proposal_time=item.proposal_time,
        processed_time=None,
        resolution_conditions=item.resolution_conditions,
        proposed_outcome=_derive_proposed_outcome(item.proposed_price),
    )

    case = MarketCase(
        case_id=case_id,
        polymarket_url=polymarket_url,
        proposal_tx_hash=item.proposal_tx_hash or item.request_tx_hash,
        question_data=question_data,
        ancillary_data=item.ancillary_text,
        fixture_path=None,  # Live mode doesn't use fixtures by default
    )

    logger.info(
        "[%s] Transformed OTB item: status=%s end_date=%s title=%s",
        case_id, item.status, end_date_iso, item.title[:60],
    )

    return case


def otb_items_to_market_cases(
    items: tuple[OTBMarketItem, ...],
    *,
    status_filter: Optional[str] = None,
) -> tuple[MarketCase, ...]:
    """Transform a collection of OTB API items into MarketCase objects.

    Items that fail transformation are logged and skipped (the pipeline
    can't run on invalid inputs anyway).

    Args:
        items: Tuple of OTBMarketItem objects.
        status_filter: If set, only include items with this status.

    Returns:
        Tuple of MarketCase objects, in the same order as input.
    """
    cases: list[MarketCase] = []
    skipped = 0

    for item in items:
        if status_filter and item.status != status_filter:
            skipped += 1
            continue

        try:
            case = otb_item_to_market_case(item)
            cases.append(case)
        except Exception as e:
            logger.warning(
                "Skipping OTB item %s: transformation failed: %s",
                item.question_id[:16], e,
            )
            skipped += 1

    if skipped:
        logger.info(
            "Transformed %d/%d OTB items to MarketCases (%d skipped).",
            len(cases), len(items), skipped,
        )

    return tuple(cases)


# ── Internal helpers ───────────────────────────────────────────────────


def _derive_case_id(item: OTBMarketItem) -> str:
    """Derive a deterministic, human-readable case_id from the OTB item.

    Uses market_slug + a short hash prefix of question_id to guarantee
    uniqueness across markets with similar slugs (e.g., Denver temp ranges).

    Format: {slug[:50]}_{question_id[:8]}

    Args:
        item: OTBMarketItem.

    Returns:
        A unique case_id string.
    """
    slug = item.market_slug or item.event_slug or "unknown"
    # Truncate slug to keep case_ids reasonable
    slug_clean = re.sub(r"[^a-zA-Z0-9_-]", "_", slug)[:50]
    qid_prefix = item.question_id[:8] if item.question_id else "noqid"
    return f"{slug_clean}_{qid_prefix}"


def _build_polymarket_url(item: OTBMarketItem) -> str:
    """Reconstruct the Polymarket event URL.

    Uses event_slug to build: https://polymarket.com/event/{event_slug}

    Args:
        item: OTBMarketItem.

    Returns:
        Polymarket URL string.
    """
    if item.event_slug:
        return f"https://polymarket.com/event/{item.event_slug}"
    # Fallback: use market_slug
    if item.market_slug:
        return f"https://polymarket.com/event/{item.market_slug}"
    return ""


def _parse_outcomes(resolution_conditions: str, title: str) -> Outcomes:
    """Parse p1/p2/p3/p4 outcome labels from resolution_conditions.

    The pattern is typically:
        "p1: 0, p2: 1, p3: 0.5. Where p1 corresponds to No, p2 to Yes,
         p3 to unknown. This request MUST only resolve to p1 or p2."

    We extract the labels ("No", "Yes", "unknown") and set defaults for p4.

    Args:
        resolution_conditions: Raw resolution conditions text.
        title: Market title (fallback for deriving labels).

    Returns:
        Outcomes with extracted labels.
    """
    # Try to extract labels: "p1 corresponds to X, p2 to Y, p3 to Z"
    p1_label = _extract_label(resolution_conditions, "p1", "No")
    p2_label = _extract_label(resolution_conditions, "p2", "Yes")
    p3_label = _extract_label(resolution_conditions, "p3", "50/50 outcome")
    p4_label = "Too Early"  # p4 is almost always "Too Early" in weather markets

    return Outcomes(
        p1=p1_label,
        p2=p2_label,
        p3=p3_label,
        p4=p4_label,
    )


def _extract_label(text: str, outcome: str, default: str) -> str:
    """Extract the human-readable label for an outcome.

    Args:
        text: Resolution conditions text.
        outcome: Outcome key (p1, p2, p3).
        default: Default label if not found.

    Returns:
        Extracted label or default.
    """
    # Patterns: "p1 corresponds to No", "p2 to Yes", or "p1: No"
    patterns = [
        rf"{outcome}\s+corresponds?\s+to\s+([^,.]+)",
        rf"{outcome}\s+to\s+([^,.]+)",
        rf"{outcome}:\s*([^,.]+)",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            label = m.group(1).strip().rstrip(".")
            if label:
                return label
    return default


def _normalize_end_date(end_date: str) -> str:
    """Normalize the end_date field to a standard ISO format.

    The API may return various formats. We ensure it's ISO 8601.

    Args:
        end_date: Raw end_date from API.

    Returns:
        Normalized ISO 8601 string (YYYY-MM-DDTHH:MM:SSZ) or empty string.
    """
    if not end_date:
        return ""

    # Try parsing common formats
    formats = [
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(end_date.replace("Z", ""), fmt)
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            continue

    logger.warning("Could not parse end_date '%s', using as-is.", end_date)
    return end_date


def _extract_market_id(ancillary_text: str, question_id: str) -> str:
    """Extract the Polymarket market_id from ancillary_text.

    The ancillary_text typically contains a pattern like 'market_id: 2824904'.

    Args:
        ancillary_text: Full ancillary data string.
        question_id: Question ID as fallback.

    Returns:
        Market ID string, or empty string if not found.
    """
    m = re.search(r'market_id:\s*(\d+)', ancillary_text)
    if m:
        return m.group(1)
    return ""


def _derive_proposed_outcome(proposed_price: str) -> Optional[str]:
    """Derive the proposed outcome (p1/p2/p3/p4) from the on-chain price.

    Args:
        proposed_price: Raw proposed_price string from API (e.g., "0", "1", "0.5").

    Returns:
        Outcome label or None.
    """
    try:
        price = float(proposed_price)
    except (ValueError, TypeError):
        return None

    if price == 0:
        return "p1"
    elif price == 1:
        return "p2"
    elif price == 0.5:
        return "p3"
    return None
