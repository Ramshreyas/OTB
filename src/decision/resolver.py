"""Decision stage — maps reconciliation verdict to final recommendation.

Entry point: resolve(verdict, batch, normalized, market_case, spec) → Resolution
"""

from __future__ import annotations

import logging

from src.decision.confidence import compute_confidence
from src.decision.reviewer import review as llm_review
from src.decision.models import Resolution, LLMReview
from src.reconciliation.models import ReconciliationVerdict
from src.normalization.models import NormalizedObservation
from src.retrieval.dispatch import RawObservationBatch
from src.retrieval.spec import RetrievalSpec
from src.validation.models import MarketCase

logger = logging.getLogger(__name__)


def resolve(
    verdict: ReconciliationVerdict,
    batch: RawObservationBatch,
    normalized: NormalizedObservation,
    market_case: MarketCase,
    spec: RetrievalSpec,
) -> Resolution:
    """Produce the final resolution from a reconciliation verdict.

    1. Map verdict → recommendation (deterministic)
    2. Compute confidence from objective factors
    3. Conditionally invoke LLM reviewer (confidence < 0.85)
    4. Apply reviewer escalation (can only go to unclear)

    Args:
        verdict: ReconciliationVerdict from Stage 5.
        batch: RawObservationBatch (for source path info).
        normalized: NormalizedObservation (for quality, completeness).
        market_case: Original MarketCase (for title, case_id).
        spec: RetrievalSpec (for station, measurement context).

    Returns:
        Resolution with recommendation, confidence, path, and reasoning.
    """
    # ── Step 1: Deterministic mapping ──
    _VERDICT_MAP = {
        "Yes": "p2",
        "No": "p1",
        "p3_tie": "p3",
        "p4_too_early": "p4",
        "unclear": "unclear",
    }
    recommendation = _VERDICT_MAP.get(verdict.verdict, "unclear")

    # ── Step 2: Confidence ──
    source_path = _extract_source_path(batch)
    confidence = compute_confidence(
        normalized=normalized,
        source_path=source_path,
        confidence_penalties=verdict.confidence_penalties,
    )

    # For unclear/p4 verdicts, confidence is 1.0 (we're certain it's unclear/too-early)
    if recommendation in ("unclear", "p4"):
        return Resolution(
            recommendation=recommendation,
            confidence=1.0,
            path="deterministic",
            reasoning=verdict.reasoning,
            review_reason=verdict.reasoning if recommendation == "unclear" else None,
        )

    # ── Step 3: LLM reviewer (conditional) ──
    llm_result: LLMReview | None = None
    path = "deterministic"

    if confidence < 0.85:
        logger.info("Confidence %.2f < 0.85 — invoking LLM reviewer.", confidence)
        llm_result = llm_review(
            recommendation=recommendation,
            confidence=confidence,
            reasoning=verdict.reasoning,
            title=market_case.question_data.title,
            station_code=spec.station_code,
            station_url=spec.station_url,
            measurement=spec.measurement,
            aggregation=spec.aggregation,
            window=f"{spec.target_window.start.date()} to {spec.target_window.end.date()}",
            value=normalized.value,
            unit=normalized.unit,
            precision=normalized.precision,
            quality_flags=[f.value for f in normalized.quality_flags],
            completeness=normalized.completeness,
        )
        path = "deterministic+llm_reviewed"

        if not llm_result.agreed:
            logger.warning(
                "LLM reviewer disagreed with recommendation %s — escalating to unclear.",
                recommendation,
            )
            return Resolution(
                recommendation="unclear",
                confidence=min(confidence, 0.70),
                path="llm_escalated_to_unclear",
                llm_review=llm_result,
                reasoning=verdict.reasoning,
                review_reason=(
                    f"LLM reviewer ({llm_result.model}) disagreed: {llm_result.reasoning}"
                ),
            )

        # For low-confidence cases (0.50-0.70), cap confidence even if reviewer agrees
        if confidence < 0.70:
            confidence = 0.70

    return Resolution(
        recommendation=recommendation,
        confidence=confidence,
        path=path,
        llm_review=llm_result,
        reasoning=verdict.reasoning,
    )


def _extract_source_path(batch: RawObservationBatch) -> str:
    """Determine the retrieval path from source trace entries."""
    for entry in batch.source_trace:
        if entry.error is None:
            return entry.path
    return "api"  # fallback
