"""Station Guardrails — Operational hints for the retrieval layer.

After extraction, the RetrievalSpec is augmented with station-specific notes
drawn from a curated registry of known behaviors. The retrieval layer reads
these to adjust its strategy.

Guardrail types:
- unit-label verification: verify response unit label, not request param
- partial-data windows: flag incomplete data near day boundaries
- source lag tolerance: extend effective finality date for slow sources
- expected response shape: check row counts for data completeness
- retry guidance: backoff strategy for known-flaky stations
"""

from __future__ import annotations

from src.retrieval.station_registry import lookup_by_icao_code, STATION_REGISTRY


# ── Default guardrails (applied to all stations) ─────────────────────

_DEFAULT_GUARDRAILS = [
    "Daily high/low observations are distinct from intraday point readings — "
    "verify that the retrieved value is the daily summary, not a single hourly reading.",
    "Data can change before finality; after first next-day datapoint, revisions "
    "are ignored per market rules.",
    "Wunderground UI units are per-session toggles — the scraper must explicitly "
    "request the correct unit or verify the response unit label.",
    "Expected response shape: daily observations table should have 24+ rows "
    "for a complete day. Fewer rows = partial data; flag in quality check.",
]


def get_guardrails(station_code: str) -> list[str]:
    """Get station-specific guardrails for a given ICAO code.

    Returns the station's known quirks plus default guardrails that
    apply to all stations.

    Args:
        station_code: ICAO airport code (e.g., "RJTT").

    Returns:
        List of guardrail strings. Never empty — always includes defaults.
    """
    guardrails = list(_DEFAULT_GUARDRAILS)

    info = lookup_by_icao_code(station_code)
    if info and info.known_quirks:
        guardrails.extend(info.known_quirks)

    return guardrails


def get_guardrails_for_all_stations() -> dict[str, list[str]]:
    """Get guardrails for all registered stations.

    Returns:
        Dict mapping ICAO code → list of guardrail strings.
    """
    result: dict[str, list[str]] = {}
    for code in STATION_REGISTRY:
        result[code] = get_guardrails(code)
    return result
