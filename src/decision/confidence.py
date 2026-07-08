"""Confidence calculation — objective, auditable factors, no LLM."""

from __future__ import annotations

from src.normalization.models import NormalizedObservation, QualityFlag


def compute_confidence(
    normalized: NormalizedObservation,
    source_path: str,  # "api", "playwright", "replay"
    confidence_penalties: tuple[tuple[str, float], ...] = (),
) -> float:
    """Compute confidence from objective factors.

    Args:
        normalized: Normalized observation (for completeness, quality_flags).
        source_path: How data was retrieved ("api", "playwright", "replay").
        confidence_penalties: Penalties from reconciliation's quality gate.

    Returns:
        Confidence score 0.0 – 1.0 (clamped to [0.50, 1.0] floor).
    """
    # Base confidence by retrieval path
    base_map = {
        "api": 0.95,
        "playwright": 0.85,
        "replay": 0.90,
    }
    confidence = base_map.get(source_path, 0.85)

    # Apply penalties from reconciliation
    for _flag_name, penalty in confidence_penalties:
        confidence -= penalty

    # Additional penalties from normalization quality flags
    for flag in normalized.quality_flags:
        confidence -= 0.08

    # Completeness penalty
    if normalized.completeness < 0.75:
        confidence -= 0.10

    # Floor at 0.50
    return max(0.50, min(1.0, confidence))
