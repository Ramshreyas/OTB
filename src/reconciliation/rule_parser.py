"""Gate 3a: Parse market rules from title text — regex-based, no LLM."""

from __future__ import annotations

import re
from typing import Optional


# Known patterns for Polymarket weather markets
_PATTERNS = [
    # "Will the lowest temperature in Tokyo be 20°C on June 1?"
    # operator: exact
    (r"be\s+(\d+(?:\.\d+)?)\s*°?\s*[CF]\s+on\b", "exact", 1, None),
    # "Will the highest temperature in Tokyo be 29°C or higher on June 1?"
    (r"be\s+(\d+(?:\.\d+)?)\s*°?\s*[CF]\s+or\s+higher", "gte", 1, None),
    # "Will the highest temperature in Tokyo be 29°C or above on June 1?"
    (r"be\s+(\d+(?:\.\d+)?)\s*°?\s*[CF]\s+or\s+above", "gte", 1, None),
    # "Will the highest temperature in Busan be 22°C or below on June 1?"
    (r"be\s+(\d+(?:\.\d+)?)\s*°?\s*[CF]\s+or\s+below", "lte", 1, None),
    # "Will the highest temperature in Denver be between 68-69°F on May 31?"
    (r"between\s+(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*°?\s*[CF]", "range", 1, 2),
]


def parse_rules(title: str) -> dict[str, object]:
    """Parse operator and threshold from a market question title.

    Args:
        title: The question title (e.g., "Will the lowest temperature in Tokyo be 20°C on June 1?").

    Returns:
        Dict with keys: operator (str), threshold (float or [float, float]),
        parsed (bool). If parsing fails, parsed=False.
    """
    title_clean = title.strip()

    for pattern, operator, *group_nums in _PATTERNS:
        match = re.search(pattern, title_clean, re.IGNORECASE)
        if match:
            if operator == "range" and len(group_nums) >= 2:
                low = float(match.group(group_nums[0]))
                high = float(match.group(group_nums[1]))
                return {"operator": operator, "threshold": (low, high), "parsed": True}
            else:
                val = float(match.group(group_nums[0]))
                return {"operator": operator, "threshold": val, "parsed": True}

    return {"operator": "", "threshold": None, "parsed": False}
