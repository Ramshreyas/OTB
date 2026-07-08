"""Wunderground API Retrieval — Primary retrieval path for Wunderground station data.

Uses the Wunderground historical observations internal API endpoint to fetch
hourly weather data for a given station and date range. This is the primary
path; if it fails, the pipeline falls back to Playwright headless browser.

Key behaviors:
- Applies retry guardrails from the RetrievalSpec
- Validates response shape (observation count matches expected for the window)
- Verifies actual response unit against requested unit
- Filters observations to station-local timezone boundaries
- Builds a finality check fetch for the next day
- Emits complete source traces
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone as dt_timezone, timedelta
from typing import Any, Optional
from urllib.parse import urlencode

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .dispatch import (
    SourceTraceEntry,
    RawObservationBatch,
    ExtractedValue,
    FinalityResult,
    FIELD_MAPPING_TABLE,
)

logger = logging.getLogger(__name__)


# ── Constants ─────────────────────────────────────────────────────────

# Wunderground internal API endpoint for historical observations
_WU_OBSERVATIONS_URL = (
    "https://api.weather.com/v1/location/{icao}:{cc}/observations/historical.json"
)

# Expected API key (passed as 'apiKey' query param)
# The API key can be read from environment, but the endpoint is public-ish

# Unit mapping: C → m (metric), F → e (imperial)
_UNIT_PARAM_MAP = {"C": "m", "F": "e"}

# Observation response field names per measurement type
_OBS_FIELD_NAMES: dict[str, str] = {
    "temp": "temp",
    "wspd": "wspd",
    "gust": "gust",
    "precip_total": "precip_total",
    "rh": "rh",
    "vis": "vis",
    "pressure": "pressure",
    "dewPt": "dewPt",
}


class WundergroundAPIError(Exception):
    """Raised when the Wunderground API call fails or returns unusable data."""

    def __init__(self, message: str, status_code: Optional[int] = None, url: str = ""):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.url = url


def _build_retry_session(
    max_retries: int = 3,
    backoff_factor: float = 5.0,
    status_forcelist: tuple[int, ...] = (429, 500, 502, 503, 504),
) -> requests.Session:
    """Build a requests Session with configured retry behavior."""
    retry_strategy = Retry(
        total=max_retries,
        backoff_factor=backoff_factor,
        status_forcelist=list(status_forcelist),
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def _parse_retry_from_guardrails(guardrails: list[str]) -> tuple[int, float]:
    """Extract retry count and backoff from guardrail strings.

    Default: 3 retries, 5s backoff.
    """
    max_retries = 3
    backoff = 5.0

    for g in guardrails:
        if "up to" in g.lower() and "retry" in g.lower():
            import re
            count_match = re.search(r"up to (\d+)", g)
            if count_match:
                max_retries = int(count_match.group(1))
            backoff_match = re.search(r"(\d+)s?\s*backoff", g)
            if backoff_match:
                backoff = float(backoff_match.group(1))

    return max_retries, backoff


def _downgrade_retry_settings(guardrails: list[str]) -> tuple[int, float]:
    """Return conservative retry settings from guardrails."""
    return _parse_retry_from_guardrails(guardrails)


def _build_api_url(
    icao_code: str,
    country_code: str,
    start_date_str: str,
    end_date_str: str,
    unit_param: str,
    api_key: str = "",
) -> str:
    """Build the Wunderground observations API URL.

    Args:
        icao_code: ICAO airport code (e.g., "RJTT").
        country_code: ISO country code, lowercase (e.g., "jp").
        start_date_str: YYYYMMDD format.
        end_date_str: YYYYMMDD format.
        unit_param: 'm' for metric/Celsius or 'e' for imperial/Fahrenheit.
        api_key: API key (if empty, uses e1f10a1e78da46f5b10a1e78da96f525).

    Returns:
        Full API URL string.
    """
    if not api_key:
        api_key = "e1f10a1e78da46f5b10a1e78da96f525"  # Public-ish key

    base = f"https://api.weather.com/v1/location/{icao_code}:{country_code}/observations/historical.json"
    params = {
        "apiKey": api_key,
        "units": unit_param,
        "startDate": start_date_str,
        "endDate": end_date_str,
    }
    return f"{base}?{urlencode(params)}"


def _extract_country_code_from_url(station_url: str) -> str:
    """Extract the two-letter country code from a Wunderground URL.

    e.g., .../history/daily/jp/tokyo/RJTT → 'jp'
    """
    import re
    match = re.search(r"/history/daily/([a-z]{2})/", station_url)
    if match:
        return match.group(1)
    return "xx"


def _extract_country_code_from_icao(icao: str) -> str:
    """Infer country code from ICAO prefix.

    RJ** → jp, RK** → kr, K*** → us, NZ** → nz
    """
    icao = icao.upper()
    # Two-letter prefixes for Asia-Pacific
    mapping = {
        "RJ": "jp",
        "RK": "kr",
        "NZ": "nz",
    }
    prefix2 = icao[:2]
    if prefix2 in mapping:
        return mapping[prefix2]
    # Single-letter K prefix = contiguous United States (K***)
    if icao.startswith("K"):
        return "us"
    return "xx"


def _format_date_for_api(dt: datetime) -> str:
    """Format a datetime as YYYYMMDD for the Wunderground API."""
    return dt.strftime("%Y%m%d")


def _verify_response_unit(
    response_data: dict[str, Any],
    requested_unit: str,
) -> tuple[bool, str]:
    """Verify the actual unit in the API response against what was requested.

    Some stations (e.g., RJTT) return °C regardless of the ?units= param.

    Args:
        response_data: Parsed API response JSON.
        requested_unit: The unit param passed to the API ('m' or 'e').

    Returns:
        (match, actual_unit) — match is True if response unit matches expectation.
    """
    actual_unit = "unknown"

    if not response_data:
        return False, actual_unit

    # Check metadata for unit info
    metadata = response_data.get("metadata", {})
    if not metadata:
        # Try root-level
        metadata = response_data

    units_field = metadata.get("units", {})
    if isinstance(units_field, dict):
        temp_unit = units_field.get("temperature", "")
        if temp_unit == "C":
            actual_unit = "C"
        elif temp_unit == "F":
            actual_unit = "F"

    if actual_unit == "unknown":
        # Heuristic: check first observation's temp value range
        observations = response_data.get("observations", [])
        if observations:
            first_temp = observations[0].get("temp")
            if isinstance(first_temp, (int, float)):
                if first_temp > 50:
                    actual_unit = "F"
                else:
                    actual_unit = "C"

    expected_unit = "C" if requested_unit == "m" else "F"
    match = actual_unit == expected_unit

    if not match:
        logger.warning(
            "Unit mismatch: requested %s (%s), response appears to be %s",
            requested_unit, expected_unit, actual_unit,
        )

    return match, actual_unit


def _filter_observations_by_timezone(
    observations: list[dict[str, Any]],
    target_window_start: datetime,
    target_window_end: datetime,
    station_timezone_str: str,
) -> list[dict[str, Any]]:
    """Filter observations to those within the station-local target window.

    Converts observation UTC timestamps to station-local time and keeps
    only those that fall within the target window.

    Args:
        observations: List of raw observation dicts, each with valid_time_gmt (epoch).
        target_window_start: Window start in station-local time.
        target_window_end: Window end in station-local time.
        station_timezone_str: IANA timezone string.

    Returns:
        Filtered list of observations within the target window.
    """
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(station_timezone_str)
    except Exception:
        logger.warning("Could not load timezone %s, using UTC filter.", station_timezone_str)
        # Fall back to UTC-based filtering — make target window UTC-aware
        tw_start = target_window_start
        tw_end = target_window_end
        if tw_start.tzinfo is None:
            tw_start = tw_start.replace(tzinfo=dt_timezone.utc)
        if tw_end.tzinfo is None:
            tw_end = tw_end.replace(tzinfo=dt_timezone.utc)
        filtered = []
        for obs in observations:
            ts_epoch = obs.get("valid_time_gmt")
            if ts_epoch is None:
                continue
            obs_time = datetime.fromtimestamp(ts_epoch, tz=dt_timezone.utc)
            if tw_start <= obs_time <= tw_end:
                filtered.append(obs)
        return filtered

    filtered: list[dict[str, Any]] = []
    # Make target window timezone-aware for comparison
    tw_start = target_window_start
    tw_end = target_window_end
    if tw_start.tzinfo is None:
        tw_start = tw_start.replace(tzinfo=tz)
    if tw_end.tzinfo is None:
        tw_end = tw_end.replace(tzinfo=tz)

    for obs in observations:
        ts_epoch = obs.get("valid_time_gmt")
        if ts_epoch is None:
            continue
        obs_time_utc = datetime.fromtimestamp(ts_epoch, tz=dt_timezone.utc)
        obs_time_local = obs_time_utc.astimezone(tz)

        if tw_start <= obs_time_local <= tw_end:
            filtered.append(obs)

    return filtered


def _count_observations_in_window(
    observations: list[dict[str, Any]],
    target_window_start: datetime,
    target_window_end: datetime,
) -> int:
    """Count how many observations fall within the window (UTC-based)."""
    # Make target window tz-aware (UTC) for comparison with UTC observations
    tw_start = target_window_start
    tw_end = target_window_end
    if tw_start.tzinfo is None:
        tw_start = tw_start.replace(tzinfo=dt_timezone.utc)
    if tw_end.tzinfo is None:
        tw_end = tw_end.replace(tzinfo=dt_timezone.utc)

    count = 0
    for obs in observations:
        ts_epoch = obs.get("valid_time_gmt")
        if ts_epoch is None:
            continue
        obs_time = datetime.fromtimestamp(ts_epoch, tz=dt_timezone.utc)
        if tw_start <= obs_time <= tw_end:
            count += 1
    return count


def _apply_aggregation(
    field_name: str,
    aggregation: str,
    observations: list[dict[str, Any]],
) -> Optional[float]:
    """Apply an aggregation function to the target field across observations.

    Args:
        field_name: The API field to extract from each observation.
        aggregation: One of 'min', 'max', 'sum', 'point'.
        observations: List of observation dicts.

    Returns:
        Aggregated value, or None if no valid observations.
    """
    values: list[float] = []
    for obs in observations:
        val = obs.get(field_name)
        if val is None:
            continue
        try:
            values.append(float(val))
        except (ValueError, TypeError):
            continue

    if not values:
        return None

    if aggregation == "min":
        return min(values)
    elif aggregation == "max":
        return max(values)
    elif aggregation == "sum":
        return sum(values)
    elif aggregation == "point":
        # Point: return the first value (or specify exact timestamp match)
        return values[0]

    return None


def fetch_wunderground_observations(
    station_url: str,
    station_code: str,
    target_window_start: datetime,
    target_window_end: datetime,
    timezone_str: str,
    measurement: str,
    aggregation: str,
    unit: str,
    guardrails: list[str],
    finality_after: datetime,
    api_key: str = "",
) -> RawObservationBatch:
    """Fetch weather observations from Wunderground API.

    This is the primary retrieval path for wunderground_station source type.

    Args:
        station_url: The full Wunderground station URL.
        station_code: ICAO station code.
        target_window_start: Window start (station-local unaware or UTC).
        target_window_end: Window end.
        timezone_str: IANA timezone string.
        measurement: Measurement type (temperature, precipitation, etc.).
        aggregation: Aggregation type (min, max, sum, point).
        unit: Expected unit (C, F).
        guardrails: Station-specific guardrails.
        finality_after: Datetime after which finality can be confirmed.
        api_key: Optional Wunderground API key.

    Returns:
        RawObservationBatch with observations, extracted value, finality, and trace.

    Raises:
        WundergroundAPIError: If the API call fails after all retries.
    """
    # Prepare trace entries
    trace_entries: list[SourceTraceEntry] = []

    # Extract country code
    country_code = _extract_country_code_from_url(station_url)
    if country_code == "xx":
        country_code = _extract_country_code_from_icao(station_code)

    # Unit param mapping
    unit_param = _UNIT_PARAM_MAP.get(unit, "m")

    # Build date strings
    start_date_str = _format_date_for_api(target_window_start)
    end_date_str = _format_date_for_api(target_window_end)

    # Parse retry settings from guardrails
    max_retries, backoff_factor = _downgrade_retry_settings(guardrails)

    # Build the API URL
    api_url = _build_api_url(
        station_code, country_code,
        start_date_str, end_date_str,
        unit_param, api_key,
    )

    logger.info(
        "Fetching Wunderground observations: %s %s-%s (unit=%s)",
        station_code, start_date_str, end_date_str, unit_param,
    )

    # Execute fetch with retry
    session = _build_retry_session(
        max_retries=max_retries,
        backoff_factor=backoff_factor,
    )

    start_time = time.time()
    response_data: dict[str, Any] = {}
    http_status: Optional[int] = None
    response_size: int = 0
    retry_count: int = 0
    error: Optional[str] = None

    try:
        response = session.get(api_url, timeout=30)
        http_status = response.status_code
        elapsed_ms = (time.time() - start_time) * 1000
        response_size = len(response.content)

        if response.status_code == 200:
            try:
                response_data = response.json()
            except json.JSONDecodeError as e:
                error = f"Response was not valid JSON: {e}"
                logger.error("API responded with non-JSON body: %s", response.text[:200])
        else:
            error = f"HTTP {response.status_code}: {response.text[:500]}"
            logger.warning("API returned non-200 status: %d", response.status_code)

    except requests.RequestException as e:
        elapsed_ms = (time.time() - start_time) * 1000
        error = f"Request failed: {e}"
        logger.error("Wunderground API request failed: %s", e)

    # Record primary fetch trace
    trace_entries.append(SourceTraceEntry(
        url=api_url,
        http_status=http_status,
        response_size_bytes=response_size,
        latency_ms=elapsed_ms,
        path="api",
        retry_count=retry_count,
        guardrail_flags=[],
        error=error,
        timestamp=datetime.utcnow().isoformat(),
    ))

    # If primary fetch failed, raise to trigger fallback
    if error or http_status != 200:
        raise WundergroundAPIError(
            f"Wunderground API fetch failed for {station_code}: {error or 'Unknown error'}",
            status_code=http_status,
            url=api_url,
        )

    # ── Step 7: Validate response shape ──
    observations_raw = response_data.get("observations")
    if not isinstance(observations_raw, list):
        raise WundergroundAPIError(
            f"Unexpected response shape: 'observations' field missing or not a list "
            f"(got {type(observations_raw).__name__})",
            url=api_url,
        )

    in_window_count = _count_observations_in_window(
        observations_raw, target_window_start, target_window_end
    )

    expected_min = 1  # At least one observation per day
    partial_data_flags: list[str] = []
    if in_window_count < expected_min:
        partial_data_flags.append(
            f"Only {in_window_count} observations in window (expected >= {expected_min})"
        )

    # Check guardrails for partial-data windows
    for g in guardrails:
        if "intraday" in g.lower() or "partial" in g.lower() or "06:00" in g:
            partial_data_flags.append(f"Guardrail triggered: {g}")

    # ── Step 8: Verify unit label ──
    unit_match, actual_unit = _verify_response_unit(response_data, unit_param)
    if not unit_match:
        partial_data_flags.append(
            f"Unit mismatch: requested {unit_param} (expected {unit}), "
            f"but response unit appears to be {actual_unit}"
        )

    # ── Step 9: Filter by timezone ──
    filtered_observations = _filter_observations_by_timezone(
        observations_raw,
        target_window_start,
        target_window_end,
        timezone_str,
    )

    logger.info(
        "Observations: %d raw, %d in window (UTC), %d after timezone filter (%s)",
        len(observations_raw), in_window_count, len(filtered_observations), timezone_str,
    )

    # ── Step 10-11: Look up field mapping and aggregate ──
    field_mapping = FIELD_MAPPING_TABLE.get((measurement, aggregation))
    if field_mapping is None:
        raise WundergroundAPIError(
            f"No field mapping for measurement='{measurement}', aggregation='{aggregation}'",
            url=api_url,
        )

    api_field = field_mapping.api_field
    agg_fn_name = field_mapping.aggregation_fn

    extracted_val = _apply_aggregation(api_field, aggregation, filtered_observations)

    extracted_value = ExtractedValue(
        value=extracted_val,
        unit=actual_unit if not unit_match else unit,
        field=api_field,
        aggregation=aggregation,
    )

    # ── Step 12: Finality fetch ──
    finality_result = _check_finality(
        station_code, country_code, unit_param,
        finality_after, api_key, guardrails,
    )
    trace_entries.extend(finality_result.get("traces", []))

    # ── Step 13-14: Apply remaining guardrails ──
    guardrail_flags = list(partial_data_flags)
    for g in guardrails:
        if "RJTT returns" in g:
            guardrail_flags.append(f"guardrail: {g}")

    # ── Step 15: Emit RawObservationBatch ──
    batch = RawObservationBatch(
        observations=tuple(filtered_observations),
        extracted_value=extracted_value,
        finality=finality_result["result"],
        source_trace=tuple(trace_entries),
    )

    logger.info(
        "Retrieved %d observations for %s: extracted_value=%s, finality=%s",
        len(filtered_observations), station_code,
        extracted_val, finality_result["result"].status,
    )

    return batch


def _check_finality(
    station_code: str,
    country_code: str,
    unit_param: str,
    finality_after: datetime,
    api_key: str = "",
    guardrails: list[str] | None = None,
) -> dict[str, Any]:
    """Check if next-day data exists (finality gate).

    Constructs a fetch for date+1 and checks if any observations exist
    with a timestamp >= finality_after.

    Returns:
        Dict with 'result' (FinalityResult) and 'traces' (list of SourceTraceEntry).
    """
    guardrails = guardrails or []
    max_retries, backoff_factor = _downgrade_retry_settings(guardrails)
    traces: list[SourceTraceEntry] = []

    next_day = finality_after
    start_date_str = _format_date_for_api(next_day)
    end_date_str = start_date_str  # Single day

    api_url = _build_api_url(
        station_code, country_code,
        start_date_str, end_date_str,
        unit_param, api_key,
    )

    logger.info("Checking finality: %s %s", station_code, start_date_str)

    session = _build_retry_session(max_retries=max_retries, backoff_factor=backoff_factor)
    start_time = time.time()

    try:
        response = session.get(api_url, timeout=30)
        elapsed_ms = (time.time() - start_time) * 1000

        traces.append(SourceTraceEntry(
            url=api_url,
            http_status=response.status_code,
            response_size_bytes=len(response.content),
            latency_ms=elapsed_ms,
            path="api",
            retry_count=0,
            guardrail_flags=["finality_check"],
            error=None if response.status_code == 200 else f"HTTP {response.status_code}",
            timestamp=datetime.utcnow().isoformat(),
        ))

        if response.status_code != 200:
            return {
                "result": FinalityResult(status="unknown", first_next_day_ts=None),
                "traces": traces,
            }

        data = response.json()
        observations = data.get("observations", [])

        # Check if any observation timestamp >= finality_after
        first_next_day_ts = None
        # Make finality_after UTC-aware if needed for comparison
        fa = finality_after
        if fa.tzinfo is None:
            fa = fa.replace(tzinfo=dt_timezone.utc)
        for obs in observations:
            ts = obs.get("valid_time_gmt")
            if ts is not None:
                obs_time = datetime.fromtimestamp(ts, tz=dt_timezone.utc)
                if obs_time >= fa:
                    first_next_day_ts = obs_time.isoformat()
                    break

        if first_next_day_ts:
            return {
                "result": FinalityResult(status="confirmed", first_next_day_ts=first_next_day_ts),
                "traces": traces,
            }
        else:
            return {
                "result": FinalityResult(status="not_yet", first_next_day_ts=None),
                "traces": traces,
            }

    except requests.RequestException as e:
        elapsed_ms = (time.time() - start_time) * 1000
        traces.append(SourceTraceEntry(
            url=api_url,
            http_status=None,
            response_size_bytes=0,
            latency_ms=elapsed_ms,
            path="api",
            retry_count=0,
            guardrail_flags=["finality_check"],
            error=f"Request failed: {e}",
            timestamp=datetime.utcnow().isoformat(),
        ))
        return {
            "result": FinalityResult(status="unknown", first_next_day_ts=None),
            "traces": traces,
        }
