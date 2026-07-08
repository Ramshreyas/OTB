"""Precision rounding per market spec."""


def round_to_precision(value: float, precision: int) -> float:
    """Round a value to the specified decimal precision.

    Args:
        value: The value to round.
        precision: Number of decimal places (1 = whole, 2 = hundredths).

    Returns:
        Rounded value.
    """
    if precision <= 0:
        return float(round(value))

    return round(value, precision)
