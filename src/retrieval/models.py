"""Shared data models for the retrieval layer.

These types are used by both spec.py and regex_fallback.py to avoid
circular imports. They do NOT depend on any other retrieval modules.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional


@dataclass(frozen=True)
class TargetWindow:
    """A time window for the observation target.

    Attributes:
        start: Start of the window (inclusive), station-local time.
        end: End of the window (inclusive), station-local time.
    """

    start: datetime
    end: datetime

    def __post_init__(self):
        if self.start > self.end:
            raise ValueError(
                f"TargetWindow start ({self.start}) must not be after end ({self.end})"
            )

    @property
    def is_single_day(self) -> bool:
        """True if this window spans a single calendar day."""
        return (
            self.start.date() == self.end.date()
            and (self.end - self.start) <= timedelta(hours=24)
        )

    @property
    def is_monthly(self) -> bool:
        """True if this window spans roughly a month."""
        delta = self.end - self.start
        return timedelta(days=27) <= delta <= timedelta(days=32)


@dataclass
class CrossValidationResult:
    """Result of cross-validating extracted spec fields against question_data.

    Attributes:
        date_agreement: Whether extracted window agrees with end_date_iso.
        date_agreement_detail: Explanation of the date agreement check.
        unit_consistency: Whether extracted unit matches what the title implies.
        unit_consistency_detail: Explanation of the unit consistency check.
        measurement_consistency: Whether extracted measurement matches title keywords.
        measurement_consistency_detail: Explanation of measurement consistency.
        station_city_awareness: Whether station city differs from title city.
        station_city_awareness_detail: Recorded note about station/city mismatch.
        all_checks_pass: True if no check gates to unclear.
    """

    date_agreement: bool = True
    date_agreement_detail: str = ""
    unit_consistency: bool = True
    unit_consistency_detail: str = ""
    measurement_consistency: bool = True
    measurement_consistency_detail: str = ""
    station_city_awareness: bool = True
    station_city_awareness_detail: str = ""
    all_checks_pass: bool = True


class ExtractionMethod(str, Enum):
    """How the RetrievalSpec fields were extracted."""
    LLM = "llm"
    REGEX = "regex"
    MANUAL = "manual"


# ── Known enumerations ────────────────────────────────────────────────

SOURCE_TYPES = frozenset({"wunderground_station", "noaa_monthly"})

MEASUREMENT_TYPES = frozenset({
    "temperature", "precipitation", "wind_speed", "wind_gust",
    "humidity", "visibility", "pressure", "snow",
    "uv_index", "cloud_cover", "dew_point",
})

AGGREGATION_TYPES = frozenset({"min", "max", "sum", "point"})

VALID_UNITS = frozenset({"C", "F", "in", "mm"})
