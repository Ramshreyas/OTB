"""Reconciliation stage — applies three gates and returns a verdict.

Entry point: reconcile(normalized, batch, market_case, spec) → ReconciliationVerdict
"""

from __future__ import annotations

import logging

from src.normalization.models import NormalizedObservation
from src.reconciliation.finality import check_finality
from src.reconciliation.quality_gate import check_quality
from src.reconciliation.rule_parser import parse_rules
from src.reconciliation.comparator import compare
from src.reconciliation.models import ReconciliationVerdict
from src.retrieval.dispatch import RawObservationBatch
from src.retrieval.spec import RetrievalSpec
from src.validation.models import MarketCase

logger = logging.getLogger(__name__)


def reconcile(
    normalized: NormalizedObservation,
    batch: RawObservationBatch,
    market_case: MarketCase,
    spec: RetrievalSpec,
) -> ReconciliationVerdict:
    """Reconcile normalized observations against market rules.

    Three sequential gates:
    1. Finality — is the next-day datapoint published?
    2. Quality — are observations trustworthy?
    3. Comparison — does value meet threshold?

    Any gate can short-circuit to p4 or unclear.

    Args:
        normalized: NormalizedObservation from Stage 4.
        batch: RawObservationBatch (for finality status).
        market_case: Original MarketCase (for title parsing).
        spec: RetrievalSpec (for station/measurement context).

    Returns:
        ReconciliationVerdict with the final verdict.
    """
    # ── Gate 1: Finality ──
    finality_verdict = check_finality(batch)
    if finality_verdict is not None:
        logger.info("Reconciliation blocked at finality gate: %s", finality_verdict.verdict)
        return finality_verdict

    # ── Gate 2: Quality ──
    quality_result = check_quality(normalized)
    if quality_result is not None and quality_result.verdict == "unclear":
        logger.info("Reconciliation blocked at quality gate: unclear")
        return quality_result
    penalties = quality_result.confidence_penalties if quality_result else ()

    # ── Gate 3: Parse rules & compare ──
    title = market_case.question_data.title
    parsed = parse_rules(title)

    if not parsed["parsed"]:
        return ReconciliationVerdict(
            verdict="unclear",
            gate="comparison",
            reasoning=f"Could not parse market rules from title: '{title}'",
        )

    operator = str(parsed["operator"])
    threshold = parsed["threshold"]
    value = normalized.value

    try:
        result = compare(value, operator, threshold)
    except ValueError as e:
        return ReconciliationVerdict(
            verdict="unclear",
            gate="comparison",
            reasoning=f"Comparison error: {e}",
        )

    # ── Build reasoning ──
    thr_str = (
        f"{threshold[0]}-{threshold[1]}"
        if isinstance(threshold, tuple)
        else str(threshold)
    )
    reasoning = (
        f"Station {spec.station_code} recorded a {spec.aggregation} of "
        f"{value}{normalized.unit} on {spec.target_window.start.date()}. "
        f"Market asked: {spec.measurement} {operator} {thr_str}{normalized.unit}? "
        f"{value} {'meets' if result else 'does not meet'} threshold → "
        f"{'Yes' if result else 'No'}."
    )

    verdict = "Yes" if result else "No"

    return ReconciliationVerdict(
        verdict=verdict,
        gate="comparison",
        operator=operator,
        threshold=threshold,
        comparison_result=result,
        confidence_penalties=penalties,
        reasoning=reasoning,
        normalized_value=value,
    )
