"""Data models for normalization output."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class QualityFlag(str, Enum):
    """Soft flags — reduce confidence but don't gate."""
    PARTIAL_DATA = "partial_data"
    OBSERVATION_GAP = "observation_gap"
    UNIT_PROVENANCE_CONFLICT = "unit_provenance_conflict"
    NEAR_DAY_BOUNDARY = "near_day_boundary"


class AnomalyFlag(str, Enum):
    """Hard flags — gate to unclear."""
    VALUE_OUT_OF_PHYSICAL_RANGE = "value_out_of_physical_range"
    SENSOR_ERROR_SUSPECTED = "sensor_error_suspected"


@dataclass(frozen=True)
class NormalizedObservation:
    """Normalized, verified observation ready for reconciliation.

    Attributes:
        value: The normalized, converted, rounded value in the market's expected unit.
        unit: The unit this value is in — guaranteed to match RetrievalSpec.unit.
        precision: The precision this value is rounded to.
        observation_count: Number of in-window observations that contributed.
        expected_count: Expected number of observations (~24 for single day).
        completeness: Ratio of actual to expected observations (0.0 – 1.0).
        quality_flags: Soft flags that reduce confidence.
        anomaly_flags: Hard flags that gate to unclear.
        raw_value: The original value before normalization (for trace).
        raw_unit: The original unit before conversion (for trace).
    """

    value: float
    unit: str
    precision: int
    observation_count: int
    expected_count: int
    completeness: float
    quality_flags: tuple[QualityFlag, ...] = ()
    anomaly_flags: tuple[AnomalyFlag, ...] = ()
    raw_value: float = 0.0
    raw_unit: str = ""

    @property
    def is_clean(self) -> bool:
        """True if no quality or anomaly flags are set."""
        return len(self.quality_flags) == 0 and len(self.anomaly_flags) == 0

    @property
    def has_hard_anomaly(self) -> bool:
        """True if there are hard anomaly flags (gates to unclear)."""
        return len(self.anomaly_flags) > 0
