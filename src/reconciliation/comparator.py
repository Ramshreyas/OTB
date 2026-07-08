"""Gate 3b: Math comparison of normalized value against threshold."""

from __future__ import annotations


def compare(value: float, operator: str, threshold: float | tuple[float, float]) -> bool:
    """Compare a normalized value against a threshold using the given operator.

    Args:
        value: The normalized observation value.
        operator: "exact", "gte", "lte", or "range".
        threshold: Single float for exact/gte/lte, or (low, high) tuple for range.

    Returns:
        True if the comparison is satisfied (→ Yes), False otherwise (→ No).

    Raises:
        ValueError: If the operator is unknown.
    """
    if operator == "exact":
        return value == threshold

    if operator == "gte":
        return value >= float(threshold)  # type: ignore[arg-type]

    if operator == "lte":
        return value <= float(threshold)  # type: ignore[arg-type]

    if operator == "range":
        low, high = threshold  # type: ignore[misc]
        return float(low) <= value <= float(high)  # type: ignore[arg-type]

    raise ValueError(f"Unknown operator: {operator}")
