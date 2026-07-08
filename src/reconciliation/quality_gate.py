"""Gate 2: Quality & anomaly checks."""

from __future__ import annotations

from src.normalization.models import NormalizedObservation, QualityFlag, AnomalyFlag
from src.reconciliation.models import ReconciliationVerdict


# Penalty per soft quality flag
_PENALTY_PER_FLAG = 0.08
_COMPLETENESS_PENALTY = 0.10  # if completeness < 0.75


def check_quality(normalized: NormalizedObservation) -> ReconciliationVerdict | None:
    """Check quality and anomaly flags. Returns a verdict if it must gate.

    Soft flags → accumulate confidence penalties (returned in verdict).
    Hard flags → gate to unclear.
    Completeness < 0.5 → gate to unclear.

    Args:
        normalized: NormalizedObservation from Stage 4.

    Returns:
        ReconciliationVerdict if hard gate fails,
        or a passing verdict with confidence_penalties populated.
        None if there's nothing to report (no flags at all).
    """
    # ── Hard gate: anomaly flags ──
    if normalized.has_hard_anomaly:
        return ReconciliationVerdict(
            verdict="unclear",
            gate="quality",
            reasoning=f"Hard anomaly detected: {[f.value for f in normalized.anomaly_flags]}",
        )

    # ── Hard gate: critically low completeness ──
    if normalized.completeness < 0.10 and normalized.observation_count < 3:
        return ReconciliationVerdict(
            verdict="unclear",
            gate="quality",
            reasoning=f"Observation completeness critically low ({normalized.completeness:.2f}); "
                      f"only {normalized.observation_count}/{normalized.expected_count} observations.",
        )

    # ── Soft flags: accumulate penalties ──
    penalties: list[tuple[str, float]] = []

    for flag in normalized.quality_flags:
        penalties.append((flag.value, _PENALTY_PER_FLAG))

    if normalized.completeness < 0.75:
        penalties.append(("low_completeness", _COMPLETENESS_PENALTY))

    if not penalties:
        return None  # Clean — no penalties

    return ReconciliationVerdict(
        verdict="",  # not set yet; will be filled by comparison
        gate="quality",
        confidence_penalties=tuple(penalties),
    )
