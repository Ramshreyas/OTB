"""Station Registry — Maps ICAO codes and URLs to station metadata.

The registry is the authoritative source for station timezones, full URLs,
and known quirks. It is consulted after extraction (whether LLM or regex)
to enrich the RetrievalSpec and attach guardrails.

Key principles:
- Timezone is always from station metadata, never from city name.
- City name ≠ station location — "Denver" resolves to KBKF in Aurora.
- The registry is curated, not inferred. Unknown stations are flagged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class StationInfo:
    """Metadata for a known weather station.

    Attributes:
        icao_code: ICAO airport code (e.g., "RJTT").
        name: Human-readable station name.
        city: City the station is near (for display, NOT for resolution logic).
        country: Country code (ISO 3166-1 alpha-2, lowercase).
        region: Region/subdivision path used in Wunderground URLs.
        timezone: IANA timezone name (e.g., "Asia/Tokyo").
        url: Full Wunderground station history URL.
        city_note: If the city in the market title differs from the station
            city, this note explains the discrepancy for operator visibility.
            None if they match.
        known_quirks: List of known behavioral quirks for this station.
    """

    icao_code: str
    name: str
    city: str
    country: str
    region: str
    timezone: str
    url: str
    city_note: Optional[str] = None
    known_quirks: list[str] = field(default_factory=list)


# ── Registry ──────────────────────────────────────────────────────────
# Map of ICAO code → StationInfo

_RAW_REGISTRY: dict[str, dict[str, object]] = {
    "RJTT": {
        "name": "Tokyo Haneda Airport",
        "city": "Tokyo",
        "country": "jp",
        "region": "tokyo",
        "timezone": "Asia/Tokyo",
        "known_quirks": [
            "RJTT returns °C regardless of ?units= query param — verify response unit label, not request param",
        ],
    },
    "RKSI": {
        "name": "Incheon International Airport",
        "city": "Incheon",
        "country": "kr",
        "region": "incheon",
        "timezone": "Asia/Seoul",
        "city_note": "Market title says 'Seoul' but resolution source is Incheon Intl (RKSI), not a Seoul city station.",
        "known_quirks": [],
    },
    "RKPK": {
        "name": "Gimhae International Airport",
        "city": "Busan",
        "country": "kr",
        "region": "busan",
        "timezone": "Asia/Seoul",
        "known_quirks": [],
    },
    "KBKF": {
        "name": "Buckley Space Force Base",
        "city": "Aurora",
        "country": "us",
        "region": "co/aurora",
        "timezone": "America/Denver",
        "city_note": "Market title says 'Denver' but resolution source is Buckley SFB (KBKF) in Aurora, CO.",
        "known_quirks": [
            "KBKF may serve intraday values before 06:00 local — if retrieval is near day boundary, flag incomplete",
        ],
    },
    "NZWN": {
        "name": "Wellington International Airport",
        "city": "Wellington",
        "country": "nz",
        "region": "wellington",
        "timezone": "Pacific/Auckland",
        "known_quirks": [],
    },
    "KSEA": {
        "name": "Seattle-Tacoma International Airport",
        "city": "Seattle",
        "country": "us",
        "region": "wa/seattle",
        "timezone": "America/Los_Angeles",
        "known_quirks": [],
    },
}

# Build the official registry
STATION_REGISTRY: dict[str, StationInfo] = {}

for _code, _data in _RAW_REGISTRY.items():
    _url = f"https://www.wunderground.com/history/daily/{_data['country']}/{_data['region']}/{_code}"
    STATION_REGISTRY[_code] = StationInfo(
        icao_code=_code,
        name=str(_data["name"]),
        city=str(_data["city"]),
        country=str(_data["country"]),
        region=str(_data["region"]),
        timezone=str(_data["timezone"]),
        url=_url,
        city_note=str(_data.get("city_note", "")) or None,
        known_quirks=[str(q) for q in (_data.get("known_quirks", []) or [])],
    )

# Reverse index: URL → ICAO code
_URL_TO_ICAO: dict[str, str] = {
    info.url: code for code, info in STATION_REGISTRY.items()
}
# Also index by partial match (country/region/code suffix)
for code, info in STATION_REGISTRY.items():
    path_suffix = f"/{info.country}/{info.region}/{code}"
    _URL_TO_ICAO[path_suffix] = code


# ── Public API ────────────────────────────────────────────────────────

def lookup_by_icao_code(icao_code: str) -> Optional[StationInfo]:
    """Look up a station by its ICAO code.

    Args:
        icao_code: ICAO airport code, case-insensitive.

    Returns:
        StationInfo if found, None otherwise.
    """
    return STATION_REGISTRY.get(icao_code.upper())


def lookup_by_url(url: str) -> Optional[StationInfo]:
    """Look up a station by its Wunderground URL.

    Args:
        url: Full or partial Wunderground station URL.

    Returns:
        StationInfo if found, None otherwise.
    """
    # Exact match
    if url in _URL_TO_ICAO:
        return STATION_REGISTRY[_URL_TO_ICAO[url]]

    # Partial match — check if URL contains any known path suffix
    for suffix, code in _URL_TO_ICAO.items():
        if suffix in url and len(suffix) > 5:  # Avoid matching just "/us" etc.
            return STATION_REGISTRY[code]

    # Try to extract ICAO code from URL path
    parts = url.rstrip("/").split("/")
    if parts:
        candidate = parts[-1].upper()
        if candidate in STATION_REGISTRY:
            return STATION_REGISTRY[candidate]

    return None


def get_station_info(
    station_code: str = "",
    url: str = "",
) -> Optional[StationInfo]:
    """Get station info by trying ICAO code first, then URL.

    Args:
        station_code: ICAO code (e.g., "RJTT").
        url: Station URL.

    Returns:
        StationInfo if found, None otherwise.
    """
    if station_code:
        info = lookup_by_icao_code(station_code)
        if info:
            return info

    if url:
        info = lookup_by_url(url)
        if info:
            return info

    return None


def list_all_stations() -> list[StationInfo]:
    """Return all registered station info entries, sorted by ICAO code."""
    return sorted(STATION_REGISTRY.values(), key=lambda s: s.icao_code)
