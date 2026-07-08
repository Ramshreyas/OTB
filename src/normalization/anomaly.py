"""Anomaly detection — physical-limit threshold checks."""

from __future__ import annotations

import logging
from typing import Optional

from src.normalization.models import AnomalyFlag

logger = logging.getLogger(__name__)

# Physical limits per measurement type
_PHYSICAL_LIMITS: dict[str, tuple[float, float]] = {
    "temperature": (-80.0, 60.0),     # °C: Vostok (−89.2) to Death Valley (56.7)
    "wind_speed": (0.0, 400.0),       # kph: Barrow Island cyclone (408)
    "wind_gust": (0.0, 500.0),        # kph: slightly higher than sustained
    "precipitation": (0.0, 3000.0),   # mm/month: Cherrapunji extreme
    "humidity": (0.0, 100.0),         # %: physical definition
    "pressure": (850.0, 1090.0),      # hPa: Typhoon Tip (870) to Siberia (1083.8)
    "visibility": (0.0, 100.0),       # km
    "snow": (0.0, 5000.0),            # mm water equivalent
    "uv_index": (0.0, 20.0),          # UV index scale
    "cloud_cover": (0.0, 100.0),      # %
    "dew_point": (-80.0, 40.0),       # °C
}


def check_anomalies(
    value: Optional[float],
    measurement: str,
    unit: str,
) -> tuple[AnomalyFlag, ...]:
    """Check if a value falls outside known physical limits.

    Args:
        value: The value to check (may be None for missing data).
        measurement: Measurement type (temperature, wind_gust, etc.).
        unit: Unit of the value (for context, used to convert to C if needed).

    Returns:
        Tuple of anomaly flags (empty if clean).
    """
    if value is None:
        return (AnomalyFlag.SENSOR_ERROR_SUSPECTED,)

    limits = _PHYSICAL_LIMITS.get(measurement)
    if limits is None:
        logger.debug("No physical limits for measurement '%s'", measurement)
        return ()

    # Convert to Celsius for temperature checks (physical limits are in Celsius)
    check_value = value
    if measurement == "temperature" and unit == "F":
        check_value = (value - 32) * 5 / 9
    elif measurement == "precipitation" and unit == "mm":
        # Limits are in mm, but value in inches — convert
        check_value = value * 25.4

    low, high = limits
    if check_value < low or check_value > high:
        logger.warning(
            "Anomaly: %s = %.2f %s (%.2f in base units) outside [%.1f, %.1f]",
            measurement, value, unit, check_value, low, high,
        )
        return (AnomalyFlag.VALUE_OUT_OF_PHYSICAL_RANGE,)

    return ()
