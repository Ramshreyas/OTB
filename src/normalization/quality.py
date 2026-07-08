"""Quality checks — completeness, gaps, flag escalation."""

from __future__ import annotations

import logging
from typing import Any

from src.normalization.models import QualityFlag
from src.retrieval.dispatch import RawObservationBatch

logger = logging.getLogger(__name__)


def assess_quality(
    batch: RawObservationBatch,
    expected_count: int,
) -> tuple[QualityFlag, ...]:
    """Assess observation quality and return soft flags.

    Args:
        batch: The raw observation batch from retrieval.
        expected_count: Expected number of observations for a full window.

    Returns:
        Tuple of quality flags (empty if clean).
    """
    flags: list[QualityFlag] = []

    obs_count = len(batch.observations)

    # ── Completeness check ──
    if obs_count < expected_count * 0.75:
        flags.append(QualityFlag.PARTIAL_DATA)
        logger.info("Partial data: %d/%d observations", obs_count, expected_count)

    # ── Gap detection ──
    if obs_count >= 2:
        if _has_temporal_gaps(batch.observations):
            flags.append(QualityFlag.OBSERVATION_GAP)
            logger.info("Temporal gaps detected in observations")

    # ── Escalate guardrail flags from source trace ──
    for entry in batch.source_trace:
        for gf in entry.guardrail_flags:
            if "unit" in gf.lower() and "mismatch" in gf.lower():
                if QualityFlag.UNIT_PROVENANCE_CONFLICT not in flags:
                    flags.append(QualityFlag.UNIT_PROVENANCE_CONFLICT)
            if "near_day_boundary" in gf.lower():
                if QualityFlag.NEAR_DAY_BOUNDARY not in flags:
                    flags.append(QualityFlag.NEAR_DAY_BOUNDARY)

    return tuple(flags)


def _has_temporal_gaps(observations: tuple[dict[str, Any], ...]) -> bool:
    """Check for gaps > 2 hours between consecutive observations."""
    from datetime import datetime

    # Normalize timestamps to epoch seconds, handling both int and datetime values
    timestamps: list[float] = []
    for obs in observations:
        ts = obs.get("valid_time_gmt")
        if ts is None:
            continue
        if isinstance(ts, datetime):
            timestamps.append(ts.timestamp())
        else:
            timestamps.append(float(ts))

    timestamps.sort()
    if len(timestamps) < 2:
        return False

    for i in range(len(timestamps) - 1):
        gap_s = timestamps[i + 1] - timestamps[i]
        if gap_s > 7200:  # 2 hours
            return True
    return False
