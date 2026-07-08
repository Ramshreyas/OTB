"""Unit conversion — pure math, no LLM."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Conversion constants
_CONVERSIONS: dict[tuple[str, str], callable] = {}


def _register(from_unit: str, to_unit: str, fn):
    _CONVERSIONS[(from_unit, to_unit)] = fn


_register("F", "C", lambda v: (v - 32) * 5 / 9)
_register("C", "F", lambda v: v * 9 / 5 + 32)
_register("in", "mm", lambda v: v * 25.4)
_register("mm", "in", lambda v: v / 25.4)
_register("mph", "kph", lambda v: v * 1.60934)
_register("kph", "mph", lambda v: v / 1.60934)


def convert(value: float, from_unit: str, to_unit: str) -> float:
    """Convert a value between units.

    Args:
        value: The numeric value to convert.
        from_unit: Source unit (C, F, in, mm, mph, kph).
        to_unit: Target unit.

    Returns:
        Converted value.

    Raises:
        ValueError: If the conversion is not supported.
    """
    if from_unit == to_unit:
        return value

    fn = _CONVERSIONS.get((from_unit, to_unit))
    if fn is None:
        raise ValueError(
            f"No conversion from '{from_unit}' to '{to_unit}'."
        )

    result = fn(value)
    logger.debug("convert: %.4f %s → %.4f %s", value, from_unit, result, to_unit)
    return result
