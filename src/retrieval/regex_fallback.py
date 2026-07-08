"""Regex Fallback — Deterministic extraction of RetrievalSpec fields.

When the LLM is unavailable or returns non-conforming output, this module
provides pre-compiled regex patterns to extract the required fields from
ancillary_data and question_data.title.

This is the safety net — it must handle all the fields that the LLM would
extract, using only pattern matching on the structured text patterns found
in UMA weather market ancillary_data.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from src.retrieval.models import TargetWindow

# ── Compiled regex patterns ───────────────────────────────────────────

# Wunderground station history URL pattern:
# https://www.wunderground.com/history/daily/{country}/{region}/{code}/...
_WUNDERGROUND_URL_RE = re.compile(
    r"https?://(?i:www\.)?(?i:wunderground)\.com/history/daily/"
    r"(?P<country>[a-z]{2})/(?P<region>[a-z][a-z/]*)/(?P<code>[A-Z0-9]{3,4})"
    r"(?:/date/\d{4}-\d{2}-\d{2})?",
)

# NOAA/weather.gov URL pattern
_NOAA_URL_RE = re.compile(
    r"https?://(?:www\.)?(?:weather\.gov|noaa\.gov)/\S+",
    re.IGNORECASE,
)

# Station ICAO code — standalone 3-4 uppercase letters
_ICAO_CODE_RE = re.compile(
    r"\b([A-Z]{4})\b"  # 4-letter ICAO codes (e.g., RJTT, RKSI, KBKF)
)

# Date patterns in ancillary_data
_DATE_DD_MMM_YY_RE = re.compile(
    r"(?P<day>\d{1,2})\s+(?P<month>Jan|Feb|Mar|Apr|May|Jun|"
    r"Jul|Aug|Sep|Oct|Nov|Dec)\s+'(?P<year>\d{2})",
    re.IGNORECASE,
)
_DATE_ISO_RE = re.compile(
    r"(?P<date>\d{4}-\d{2}-\d{2})"
)

# Temperature/unit patterns
_CELSIUS_PHRASE_RE = re.compile(
    r"(?:degrees?\s+)?Celsius|°\s*C\b",
    re.IGNORECASE,
)
_FAHRENHEIT_PHRASE_RE = re.compile(
    r"(?:degrees?\s+)?Fahrenheit|°\s*F\b",
    re.IGNORECASE,
)

# Precision patterns
_WHOLE_DEGREES_RE = re.compile(
    r"(?:measures\s+temperatures?\s+to\s+)?whole\s+degrees?\s+(?:Celsius|Fahrenheit)?",
    re.IGNORECASE,
)
_TWO_DECIMAL_RE = re.compile(
    r"(?:to\s+)?(\d)[- ]decimal\s+(?:places?|precision)",
    re.IGNORECASE,
)

# Finality rule pattern
_FINALITY_NEXT_DAY_RE = re.compile(
    r"(?:can\s+not|cannot)\s+resolve\s+until\s+the\s+first\s+data\s?point\s+"
    r"for\s+the\s+following\s+date\s+has\s+been\s+published",
    re.IGNORECASE,
)

# Measurement & aggregation keyword patterns
_AGGREGATION_LOWEST = re.compile(r"\blowest\b", re.IGNORECASE)
_AGGREGATION_HIGHEST = re.compile(r"\bhighest\b", re.IGNORECASE)
_AGGREGATION_MAXIMUM = re.compile(r"\bmaximum\b", re.IGNORECASE)
_AGGREGATION_MINIMUM = re.compile(r"\bminimum\b", re.IGNORECASE)

# Temperature measurement indicators
_TEMPERATURE_INDICATORS = re.compile(
    r"(?:temperature|°\s*[CF]|degrees?\s+(?:Celsius|Fahrenheit))",
    re.IGNORECASE,
)
_PRECIPITATION_INDICATORS = re.compile(
    r"\bprecipitation\b", re.IGNORECASE,
)

# Month name mapping
_MONTH_MAP: dict[str, int] = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


# ── Result dataclass ──────────────────────────────────────────────────

@dataclass
class RegexExtractionResult:
    """Container for regex-extracted fields.

    All fields are optional — the caller must validate completeness.
    Unparseable fields are left as None.

    Attributes:
        source_type: "wunderground_station" or "noaa_monthly".
        station_url: Full URL to the station page.
        station_code: ICAO or internal station identifier.
        target_window: Parsed target time window.
        measurement: The measurement type.
        aggregation: Aggregation type.
        unit: Unit string.
        precision: Decimal places.
        timezone: IANA timezone (may be empty — enriched by station registry).
        finality_after: Next-day datetime after window end.
    """

    source_type: Optional[str] = None
    station_url: Optional[str] = None
    station_code: Optional[str] = None
    target_window: Optional[TargetWindow] = None
    measurement: Optional[str] = None
    aggregation: Optional[str] = None
    unit: Optional[str] = None
    precision: int = 1
    timezone: str = "UTC"
    finality_after: Optional[datetime] = None


# ── Main extraction function ──────────────────────────────────────────

def extract_with_regex(
    ancillary_data: str,
    title: str,
    end_date_iso: str = "",
) -> RegexExtractionResult:
    """Extract all RetrievalSpec fields from ancillary_data using regex patterns.

    This is the deterministic fallback path. It uses pre-compiled regex
    patterns to match the structured patterns found in UMA weather
    market ancillary_data.

    Args:
        ancillary_data: Raw ancillary_data string from the market case.
        title: Market question title.
        end_date_iso: ISO 8601 end date from question_data.

    Returns:
        RegexExtractionResult with all extractable fields populated.
        Fields that cannot be parsed remain None or their default.
    """
    result = RegexExtractionResult()

    # 1. Source type
    result.source_type = _extract_source_type(ancillary_data)

    # 2. Station URL and code
    result.station_url = _extract_station_url(ancillary_data)
    result.station_code = _extract_station_code(ancillary_data)

    # 3. Target window (from ancillary_data date OR end_date_iso)
    result.target_window = _extract_target_window(ancillary_data, title, end_date_iso)

    # 4. Measurement type
    result.measurement = _extract_measurement(ancillary_data, title)

    # 5. Aggregation type
    result.aggregation = _extract_aggregation(ancillary_data, title)

    # 6. Unit
    result.unit = _extract_unit(ancillary_data)

    # 7. Precision
    result.precision = _extract_precision(ancillary_data)

    # 8. Finality after (next day after window start at 00:00)
    if result.target_window is not None:
        result.finality_after = result.target_window.start + timedelta(days=1)

    return result


# ── Individual extraction functions ───────────────────────────────────

def _extract_source_type(ancillary_data: str) -> str:
    """Determine source type from URL patterns in ancillary_data."""
    if _WUNDERGROUND_URL_RE.search(ancillary_data):
        return "wunderground_station"
    if _NOAA_URL_RE.search(ancillary_data):
        return "noaa_monthly"
    # Default: most weather markets use Wunderground
    return "wunderground_station"


def _extract_station_url(ancillary_data: str) -> Optional[str]:
    """Extract the Wunderground or NOAA station URL."""
    m = _WUNDERGROUND_URL_RE.search(ancillary_data)
    if m:
        return f"https://www.wunderground.com/history/daily/{m.group('country')}/{m.group('region')}/{m.group('code')}"

    m = _NOAA_URL_RE.search(ancillary_data)
    if m:
        return m.group(0)

    return None


def _extract_station_code(ancillary_data: str) -> Optional[str]:
    """Extract the ICAO station code from the URL or text."""
    # Try from Wunderground URL first
    m = _WUNDERGROUND_URL_RE.search(ancillary_data)
    if m:
        return m.group("code")

    # Try standalone 4-letter ICAO code
    # Look in the last part of any URL-like path
    url_match = re.search(r'/([A-Z]{4})(?:/|$|\b)', ancillary_data)
    if url_match:
        return url_match.group(1)

    return None


def _extract_target_window(
    ancillary_data: str,
    title: str,
    end_date_iso: str,
) -> Optional[TargetWindow]:
    """Extract the target observation window.

    Tries (in order):
    1. "on DD Mon 'YY" pattern in ancillary_data
    2. ISO date (YYYY-MM-DD) in ancillary_data
    3. end_date_iso from question_data
    4. Date in title
    """
    # Try "on DD Mon 'YY" format
    text_to_search = ancillary_data
    m = _DATE_DD_MMM_YY_RE.search(text_to_search)
    if m:
        day = int(m.group("day"))
        month = _MONTH_MAP.get(m.group("month").lower(), 1)
        year = 2000 + int(m.group("year"))
        date = datetime(year, month, day)
        return TargetWindow(
            start=date,
            end=date.replace(hour=23, minute=59, second=59),
        )

    # Try ISO date in ancillary_data
    m = _DATE_ISO_RE.search(ancillary_data)
    if m:
        date_str = m.group("date")
        try:
            date = datetime.strptime(date_str, "%Y-%m-%d")
            return TargetWindow(
                start=date,
                end=date.replace(hour=23, minute=59, second=59),
            )
        except ValueError:
            pass

    # Try end_date_iso
    if end_date_iso:
        try:
            date_str = end_date_iso.replace("Z", "").split("T")[0]
            date = datetime.strptime(date_str, "%Y-%m-%d")
            return TargetWindow(
                start=date,
                end=date.replace(hour=23, minute=59, second=59),
            )
        except ValueError:
            pass

    # Try from title — "on June 1", "on May 31"
    full_month_pattern = r"(?:on\s+)?(?P<month>January|February|March|April|May|June|July|August|September|October|November|December)\s+(?P<day>\d{1,2})"
    m = re.search(full_month_pattern, title, re.IGNORECASE)
    if m:
        month_name = m.group("month").lower()
        month = _MONTH_MAP.get(month_name[:3], 1)
        day = int(m.group("day"))
        # Year: use end_date_iso year if available, else current
        try:
            year = datetime.strptime(end_date_iso[:4], "%Y").year if end_date_iso else 2026
        except (ValueError, IndexError):
            year = 2026
        date = datetime(year, month, day)
        return TargetWindow(
            start=date,
            end=date.replace(hour=23, minute=59, second=59),
        )

    # Try short month pattern in title: "Jun 1"
    short_month_pattern = rf"(?:on\s+)?(?P<day>\d{{1,2}})\s+(?P<month>{'|'.join(_MONTH_MAP.keys())})"
    m = re.search(short_month_pattern, title, re.IGNORECASE)
    if m:
        month = _MONTH_MAP.get(m.group("month").lower(), 1)
        day = int(m.group("day"))
        try:
            year = datetime.strptime(end_date_iso[:4], "%Y").year if end_date_iso else 2026
        except (ValueError, IndexError):
            year = 2026
        date = datetime(year, month, day)
        return TargetWindow(
            start=date,
            end=date.replace(hour=23, minute=59, second=59),
        )

    return None


def _extract_measurement(ancillary_data: str, title: str) -> str:
    """Extract the measurement type."""
    combined = f"{ancillary_data} {title}"

    if _TEMPERATURE_INDICATORS.search(combined):
        return "temperature"
    if _PRECIPITATION_INDICATORS.search(combined):
        return "precipitation"

    # Default to temperature — most weather markets are temperature
    return "temperature"


def _extract_aggregation(ancillary_data: str, title: str) -> str:
    """Extract the aggregation type (min, max, sum, point)."""
    combined = f"{ancillary_data} {title}"

    if _AGGREGATION_LOWEST.search(combined) or _AGGREGATION_MINIMUM.search(combined):
        return "min"
    if _AGGREGATION_HIGHEST.search(combined) or _AGGREGATION_MAXIMUM.search(combined):
        return "max"

    # Precip → sum
    if _PRECIPITATION_INDICATORS.search(combined):
        return "sum"

    # Default: max (most common for daily high markets)
    return "max"


def _extract_unit(ancillary_data: str) -> str:
    """Extract the primary unit from ancillary_data.

    Priority: the measurement precision phrase specifies the unit
    (e.g., "measures temperatures to whole degrees Celsius").
    Falls back to general Fahrenheit/Celsius mentions.
    """
    # Check the precision phrase first — this is authoritative
    # "whole degrees Celsius" or "whole degrees Fahrenheit"
    whole_match = re.search(
        r"whole\s+degrees?\s+(Celsius|Fahrenheit)",
        ancillary_data,
        re.IGNORECASE,
    )
    if whole_match:
        unit_name = whole_match.group(1).lower()
        if unit_name == "fahrenheit":
            return "F"
        if unit_name == "celsius":
            return "C"

    # Fall back to general mentions
    if _FAHRENHEIT_PHRASE_RE.search(ancillary_data):
        return "F"
    if _CELSIUS_PHRASE_RE.search(ancillary_data):
        return "C"

    # Check for "inches" (precipitation)
    if re.search(r"\binches?\b", ancillary_data, re.IGNORECASE):
        return "in"

    return "C"  # Default to Celsius


def _extract_precision(ancillary_data: str) -> int:
    """Extract the decimal precision from ancillary_data."""
    # "whole degrees" → 1 (whole number = 0 decimal places, precision=1 for rounding)
    if _WHOLE_DEGREES_RE.search(ancillary_data):
        return 1

    # "N decimal places"
    m = _TWO_DECIMAL_RE.search(ancillary_data)
    if m:
        return int(m.group(1))

    # Default: 1 for whole degrees
    return 1
