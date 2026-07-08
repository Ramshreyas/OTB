"""NOAA Monthly Summary Retrieval — For precipitation and monthly aggregate markets.

Uses NOAA/NWS public API to fetch monthly climate summaries. This is a
simpler path than Wunderground — no Playwright fallback is implemented
because NOAA data is available via their public API.

Key behaviors:
- Fetches monthly climate data from NOAA's API
- Handles source lag (NOAA monthly summaries lag 3-5 days after month-end)
- Returns RawObservationBatch with normalized observations
- No Playwright fallback — failure → unclear
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone as dt_timezone
from typing import Any, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .dispatch import (
    SourceTraceEntry,
    RawObservationBatch,
    ExtractedValue,
    FinalityResult,
)

logger = logging.getLogger(__name__)


class NOAAError(Exception):
    """Raised when NOAA data retrieval fails."""

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _build_noaa_url(station_url: str, year: int, month: int) -> str:
    """Build the NOAA API URL for monthly climate data.

    For weather.gov/NOAA stations, the typical endpoint is:
    https://www.ncei.noaa.gov/access/services/data/v1

    Args:
        station_url: Base URL from ancillary_data.
        year: Target year.
        month: Target month (1-12).

    Returns:
        Constructed API URL.
    """
    # If the URL is a specific endpoint, use it directly
    if "ncei.noaa.gov" in station_url or "api.weather.gov" in station_url:
        return station_url

    # Default: try to construct NCEI CDO endpoint
    return station_url


def _has_source_lag(month: int, year: int, lag_days: int = 5) -> bool:
    """Check if we're within the NOAA source lag window.

    NOAA monthly summaries typically lag 3-5 days after month-end.

    Args:
        month: Target month (1-12).
        year: Target year.
        lag_days: Expected lag in days.

    Returns:
        True if today's date is still within the expected lag window.
    """
    today = datetime.utcnow().date()
    # First day of next month
    if month == 12:
        next_month_start = datetime(year + 1, 1, 1).date()
    else:
        next_month_start = datetime(year, month + 1, 1).date()

    expected_available = next_month_start
    from datetime import timedelta
    expected_available += timedelta(days=lag_days)

    return today < expected_available


def fetch_noaa_monthly(
    station_url: str,
    target_window_start: datetime,
    target_window_end: datetime,
    measurement: str,
    aggregation: str,
    unit: str,
    guardrails: list[str],
    finality_after: datetime,
) -> RawObservationBatch:
    """Fetch NOAA monthly climate summary data.

    Args:
        station_url: NOAA/NWS station URL from ancillary_data.
        target_window_start: Window start.
        target_window_end: Window end.
        measurement: Measurement type.
        aggregation: Aggregation type.
        unit: Expected unit.
        guardrails: Station-specific guardrails.
        finality_after: Finality threshold datetime.

    Returns:
        RawObservationBatch with monthly summary observations.

    Raises:
        NOAAError: If retrieval fails.
    """
    trace_entries: list[SourceTraceEntry] = []
    start_time = time.time()

    year = target_window_start.year
    month = target_window_start.month

    # Check source lag
    lag = _has_source_lag(month, year, lag_days=5)
    if lag:
        logger.info(
            "NOAA source lag: month %d/%d may not have final data yet "
            "(within expected 5-day lag window)", month, year
        )

    api_url = _build_noaa_url(station_url, year, month)
    logger.info("Fetching NOAA monthly data: %s %d-%02d", station_url, year, month)

    session = requests.Session()
    retry_strategy = Retry(
        total=3,
        backoff_factor=5.0,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    response_data: dict[str, Any] = {}
    http_status: Optional[int] = None
    error: Optional[str] = None

    try:
        response = session.get(api_url, timeout=30)
        http_status = response.status_code
        elapsed_ms = (time.time() - start_time) * 1000

        if response.status_code == 200:
            try:
                response_data = response.json()
            except json.JSONDecodeError:
                # NOAA might return CSV or other format
                response_data = {"raw": response.text}
                logger.info("NOAA response is not JSON; stored as raw text")
        else:
            error = f"HTTP {response.status_code}: {response.text[:500]}"

    except requests.RequestException as e:
        elapsed_ms = (time.time() - start_time) * 1000
        error = f"Request failed: {e}"

    trace_entries.append(SourceTraceEntry(
        url=api_url,
        http_status=http_status,
        response_size_bytes=len(str(response_data)),
        latency_ms=elapsed_ms,
        path="noaa",
        retry_count=0,
        guardrail_flags=["source_lag"] if lag else [],
        error=error,
        timestamp=datetime.utcnow().isoformat(),
    ))

    if error:
        raise NOAAError(
            f"NOAA retrieval failed: {error}",
            status_code=http_status,
        )

    # Build observations list from response data
    # NOAA response format varies; we store it as a single "observation" with the
    # monthly summary data
    observations: list[dict[str, Any]] = [{
        "monthly_summary": response_data,
        "valid_time_gmt": int(target_window_start.replace(
            tzinfo=dt_timezone.utc).timestamp()),
        "precip_total": response_data.get("precip_total") or response_data.get("precipitation"),
    }]

    # Extract value
    raw_value = response_data.get("precip_total") or response_data.get("precipitation")
    try:
        value = float(raw_value) if raw_value is not None else None
    except (ValueError, TypeError):
        value = None

    extracted_value = ExtractedValue(
        value=value,
        unit=unit,
        field="precip_total",
        aggregation=aggregation,
    )

    # Finality: if within lag window, finality is not_yet
    finality_status = "not_yet" if lag else "confirmed"
    finality = FinalityResult(
        status=finality_status,
        first_next_day_ts=None,
    )

    logger.info(
        "NOAA monthly data retrieved: value=%s, finality=%s, lag=%s",
        value, finality_status, lag,
    )

    return RawObservationBatch(
        observations=tuple(observations),
        extracted_value=extracted_value,
        finality=finality,
        source_trace=tuple(trace_entries),
    )
