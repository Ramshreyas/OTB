"""Normalization stage — transforms RawObservationBatch into NormalizedObservation.

Entry point: normalize(batch, spec) → NormalizedObservation
"""

from __future__ import annotations

import logging

from src.normalization.convert import convert
from src.normalization.round import round_to_precision
from src.normalization.verify import verify_window
from src.normalization.quality import assess_quality
from src.normalization.anomaly import check_anomalies
from src.normalization.models import NormalizedObservation, QualityFlag, AnomalyFlag
from src.retrieval.dispatch import RawObservationBatch
from src.retrieval.spec import RetrievalSpec

logger = logging.getLogger(__name__)


class NormalizationError(Exception):
    """Raised when normalization encounters a hard failure (gates to unclear)."""

    def __init__(self, reason: str, detail: str):
        super().__init__(f"Normalization failed — {reason}: {detail}")
        self.reason = reason
        self.detail = detail


def normalize(
    batch: RawObservationBatch,
    spec: RetrievalSpec,
) -> NormalizedObservation:
    """Normalize raw observations into a form ready for reconciliation.

    Steps:
    1. Verify all observations are within the target window
    2. Convert units if needed
    3. Round to spec precision
    4. Assess quality (completeness, gaps, guardrail escalation)
    5. Check for physical anomalies

    Args:
        batch: RawObservationBatch from Stage 3 (retrieval).
        spec: RetrievalSpec from Stage 2.

    Returns:
        NormalizedObservation ready for reconciliation.

    Raises:
        NormalizationError: On hard failures (no observations, unconvertible units,
            anomalous data).
    """
    raw_value = batch.extracted_value.value
    raw_unit = batch.extracted_value.unit
    target_unit = spec.unit
    precision = spec.precision
    measurement = spec.measurement

    # ── Hard gate: at least one observation? ──
    obs_count = len(batch.observations)
    if obs_count == 0 or raw_value is None:
        raise NormalizationError(
            "no_observations",
            "No observations in target window; cannot normalize.",
        )

    # ── Step 1: Window-boundary verification ──
    in_window = verify_window(
        batch.observations,
        spec.target_window.start,
        spec.target_window.end,
        spec.timezone,
    )
    if not in_window:
        logger.warning("Some observations fall outside target window — flagged.")

    # ── Step 2: Unit conversion ──
    if raw_unit != target_unit:
        try:
            value = convert(raw_value, raw_unit, target_unit)
        except ValueError as e:
            raise NormalizationError(
                "unit_conversion",
                f"Cannot convert {raw_unit} → {target_unit}: {e}",
            ) from e
    else:
        value = raw_value

    # ── Step 3: Precision rounding ──
    value = round_to_precision(value, precision)

    # ── Step 4: Quality assessment ──
    expected_count = 24 if spec.target_window.is_single_day else 720
    quality_flags = assess_quality(batch, expected_count)

    # ── Step 5: Anomaly detection ──
    anomaly_flags = check_anomalies(value, measurement, target_unit)

    # ── Hard gate: physical anomaly? ──
    if len(anomaly_flags) > 0:
        raise NormalizationError(
            "anomalous_data",
            f"Value {value}{target_unit} outside physical limits for {measurement}. "
            f"Flags: {[f.value for f in anomaly_flags]}",
        )

    completeness = obs_count / expected_count if expected_count > 0 else 1.0

    normalized = NormalizedObservation(
        value=value,
        unit=target_unit,
        precision=precision,
        observation_count=obs_count,
        expected_count=expected_count,
        completeness=min(completeness, 1.0),
        quality_flags=quality_flags,
        anomaly_flags=(),
        raw_value=raw_value,
        raw_unit=raw_unit,
    )

    logger.info(
        "Normalized: %.1f %s (raw: %.1f %s, %d/%d obs, completeness=%.2f, "
        "quality=%s)",
        value, target_unit, raw_value, raw_unit,
        obs_count, expected_count, completeness,
        [f.value for f in quality_flags] if quality_flags else "clean",
    )

    return normalized
