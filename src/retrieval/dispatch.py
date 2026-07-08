"""Retrieval Dispatch — Stage 3 orchestrator and data models.

Routes retrieval based on source_type and mode (replay vs live), manages
the fallback tree, and emits RawObservationBatch results. This is the
entry point for Stage 3 of the resolution pipeline.

Design:
- Replay mode: loads fixtures from data/fixtures/
- Live mode: dispatches to source-specific retrieval paths
  - wunderground_station: API → Playwright fallback → unclear
  - noaa_monthly: NOAA API → unclear (no Playwright path)

Field mapping table: measurement + aggregation → API field name + aggregation function
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from src.retrieval.spec import RetrievalSpec

logger = logging.getLogger(__name__)


# ── Output data models ─────────────────────────────────────────────────


@dataclass(frozen=True)
class ExtractedValue:
    """The value extracted after applying aggregation to observations.

    Attributes:
        value: The aggregated numeric value, or None if not computable.
        unit: Unit of the value (C, F, in, mm, etc.).
        field: The API field name the value was extracted from.
        aggregation: The aggregation function used (min, max, sum, point).
    """

    value: Optional[float]
    unit: str
    field: str
    aggregation: str


@dataclass(frozen=True)
class FinalityResult:
    """Result of the finality gate check.

    Attributes:
        status: 'confirmed' if next-day data exists, 'not_yet' if not,
            'unknown' if the check could not be performed.
        first_next_day_ts: ISO 8601 timestamp of the first next-day
            observation, if confirmed.
    """

    status: str  # "confirmed", "not_yet", "unknown"
    first_next_day_ts: Optional[str] = None

    def __post_init__(self):
        valid_statuses = {"confirmed", "not_yet", "unknown"}
        if self.status not in valid_statuses:
            raise ValueError(
                f"Finality status must be one of {valid_statuses}, "
                f"got '{self.status}'"
            )


@dataclass(frozen=True)
class SourceTraceEntry:
    """A single entry in the source trace for a retrieval attempt.

    Multiple entries may exist if fallbacks were attempted (e.g., API failed,
    then Playwright succeeded).

    Attributes:
        url: The URL that was queried.
        http_status: HTTP status code, or None if not an HTTP request.
        response_size_bytes: Size of the response in bytes.
        latency_ms: Round-trip latency in milliseconds.
        path: Retrieval path identifier ('api', 'playwright', 'replay', 'noaa').
        retry_count: Number of retries before success/failure.
        guardrail_flags: List of guardrail flags triggered.
        error: Error message if this attempt failed, None if successful.
        timestamp: ISO 8601 timestamp of the retrieval.
    """

    url: str
    http_status: Optional[int]
    response_size_bytes: int
    latency_ms: float
    path: str
    retry_count: int = 0
    guardrail_flags: list[str] = field(default_factory=list)
    error: Optional[str] = None
    timestamp: str = ""


@dataclass(frozen=True)
class RawObservationBatch:
    """The output of the retrieval stage — raw weather observations and metadata.

    This is an immutable container passed to Stage 4 (Normalization).
    Normalization does not know or care whether the data came from API,
    Playwright, replay, or NOAA.

    Attributes:
        observations: Tuple of raw observation dicts within the target window.
        extracted_value: The value after applying aggregation.
        finality: Finality gate check result.
        source_trace: Ordered list of retrieval attempts (API → Playwright → …).
    """

    observations: tuple[dict[str, Any], ...]
    extracted_value: ExtractedValue
    finality: FinalityResult
    source_trace: tuple[SourceTraceEntry, ...]


# ── Field mapping table ────────────────────────────────────────────────

@dataclass(frozen=True)
class FieldMapping:
    """Mapping from (measurement, aggregation) to API field name and aggregation function.

    Attributes:
        api_field: The field name in the Wunderground/NOAA observation dict.
        aggregation_fn: The name of the aggregation function ('min', 'max', 'sum', 'point').
    """

    api_field: str
    aggregation_fn: str


# Every known (measurement, aggregation) pair is mapped to the exact API field.
# Unknown pairs that reach retrieval (gated in stage 2) → unclear.
FIELD_MAPPING_TABLE: dict[tuple[str, str], FieldMapping] = {
    ("temperature", "min"): FieldMapping(api_field="temp", aggregation_fn="min"),
    ("temperature", "max"): FieldMapping(api_field="temp", aggregation_fn="max"),
    ("temperature", "point"): FieldMapping(api_field="temp", aggregation_fn="point"),
    ("wind_speed", "max"): FieldMapping(api_field="wspd", aggregation_fn="max"),
    ("wind_speed", "min"): FieldMapping(api_field="wspd", aggregation_fn="min"),
    ("wind_gust", "max"): FieldMapping(api_field="gust", aggregation_fn="max"),
    ("precipitation", "sum"): FieldMapping(api_field="precip_total", aggregation_fn="sum"),
    ("humidity", "min"): FieldMapping(api_field="rh", aggregation_fn="min"),
    ("humidity", "max"): FieldMapping(api_field="rh", aggregation_fn="max"),
    ("visibility", "min"): FieldMapping(api_field="vis", aggregation_fn="min"),
    ("snow", "sum"): FieldMapping(api_field="snow_hrly", aggregation_fn="sum"),
    ("pressure", "point"): FieldMapping(api_field="pressure", aggregation_fn="point"),
    ("pressure", "min"): FieldMapping(api_field="pressure", aggregation_fn="min"),
    ("pressure", "max"): FieldMapping(api_field="pressure", aggregation_fn="max"),
    ("dew_point", "min"): FieldMapping(api_field="dewPt", aggregation_fn="min"),
    ("dew_point", "max"): FieldMapping(api_field="dewPt", aggregation_fn="max"),
    ("cloud_cover", "point"): FieldMapping(api_field="clds", aggregation_fn="point"),
    ("uv_index", "max"): FieldMapping(api_field="uv_index", aggregation_fn="max"),
}


def get_field_mapping(measurement: str, aggregation: str) -> Optional[FieldMapping]:
    """Look up the API field mapping for a measurement + aggregation pair.

    Args:
        measurement: Measurement type from RetrievalSpec.
        aggregation: Aggregation type from RetrievalSpec.

    Returns:
        FieldMapping if found, None if the pair is unknown.
    """
    return FIELD_MAPPING_TABLE.get((measurement, aggregation))


# ── Exceptions ─────────────────────────────────────────────────────────


class RetrievalError(Exception):
    """Raised when retrieval fails for a market case after all fallbacks are exhausted.

    Attributes:
        case_id: The case that failed.
        reason: Brief reason string.
        detail: Human-readable explanation.
        source_trace: Any trace entries recorded before failure.
    """

    def __init__(
        self,
        case_id: str,
        reason: str,
        detail: str,
        source_trace: tuple[SourceTraceEntry, ...] = (),
    ):
        super().__init__(f"[{case_id}] Retrieval failed — {reason}: {detail}")
        self.case_id = case_id
        self.reason = reason
        self.detail = detail
        self.source_trace = source_trace


# ── Main dispatch function ─────────────────────────────────────────────


def retrieve_observations(
    spec: RetrievalSpec,
    *,
    mode: str = "live",
    fixtures_dir: str = "data/fixtures",
    fixture_path_override: Optional[str] = None,
    api_key: str = "",
) -> RawObservationBatch:
    """Retrieve weather observations based on the RetrievalSpec.

    This is the entry point for Stage 3 of the pipeline. It:
    1. Checks if replay mode → loads fixture
    2. Dispatches by source_type to the correct retrieval path
    3. wunderground_station: API → Playwright fallback → unclear
    4. noaa_monthly: NOAA API → unclear (no Playwright path)
    5. Records full source trace for every attempt

    Args:
        spec: The RetrievalSpec from Stage 2.
        mode: 'replay' or 'live'.
        fixtures_dir: Base directory for fixture files (replay mode only).
        fixture_path_override: Explicit fixture path override.
        api_key: Optional Wunderground API key.

    Returns:
        RawObservationBatch with observations, extracted value, finality, and trace.

    Raises:
        RetrievalError: If all retrieval paths are exhausted.
    """
    # ── Replay mode ──
    if mode == "replay":
        return _retrieve_replay(spec, fixtures_dir, fixture_path_override)

    # ── Live mode: dispatch by source_type ──
    batch: RawObservationBatch
    if spec.source_type == "wunderground_station":
        batch = _retrieve_wunderground(spec, api_key)
    elif spec.source_type == "noaa_monthly":
        batch = _retrieve_noaa(spec)
    else:
        raise RetrievalError(
            case_id="(unknown)",
            reason="unknown_source_type",
            detail=f"Unrecognized source type '{spec.source_type}'; "
                   f"expected one of: wunderground_station, noaa_monthly",
        )

    # ── Automatically save fixture for future replay ──
    _save_fixture(batch, spec, Path(fixtures_dir))

    return batch


# ── Internal dispatch helpers ──────────────────────────────────────────


def _retrieve_replay(
    spec: RetrievalSpec,
    fixtures_dir: str,
    fixture_path_override: Optional[str],
) -> RawObservationBatch:
    """Load observations from a fixture file."""
    from .replay import (
        resolve_fixture_path,
        load_fixture,
        replay_observation_batch,
        ReplayError,
    )

    # Determine case_id from the spec's cross-validation context
    # We don't store case_id in RetrievalSpec, so we derive it from station_code + window
    case_id = f"{spec.station_code}_{spec.target_window.start.strftime('%Y%m%d')}_{spec.measurement}_{spec.aggregation}"

    try:
        fixture_path = resolve_fixture_path(
            # Try to use the spec details to find the fixture
            # Use a fallback approach: look for station_code in filename
            case_id=case_id,
            fixtures_dir=fixtures_dir,
            fixture_path_override=fixture_path_override,
        )
        fixture_data = load_fixture(fixture_path)
        result = replay_observation_batch(fixture_data)
        logger.info("Replay: loaded %d observations from %s",
                     len(result.observations), fixture_path)
        return result
    except ReplayError as e:
        raise RetrievalError(
            case_id=case_id,
            reason="replay_fixture_missing_or_malformed",
            detail=str(e),
        ) from e


def _retrieve_wunderground(spec: RetrievalSpec, api_key: str) -> RawObservationBatch:
    """Retrieve from Wunderground: API first, then Playwright fallback."""
    from .wunderground_api import (
        fetch_wunderground_observations,
        WundergroundAPIError,
    )
    from .wunderground_playwright import (
        fetch_wunderground_playwright,
        PlaywrightError,
    )

    # Extract parameters from spec
    station_url = spec.station_url
    station_code = spec.station_code
    target_window_start = spec.target_window.start
    target_window_end = spec.target_window.end
    timezone_str = spec.timezone
    measurement = spec.measurement
    aggregation = spec.aggregation
    unit = spec.unit
    guardrails = spec.guardrails
    finality_after = spec.finality_after

    case_id = f"{station_code}_{target_window_start.strftime('%Y%m%d')}"

    # ── Primary path: API ──
    try:
        batch = fetch_wunderground_observations(
            station_url=station_url,
            station_code=station_code,
            target_window_start=target_window_start,
            target_window_end=target_window_end,
            timezone_str=timezone_str,
            measurement=measurement,
            aggregation=aggregation,
            unit=unit,
            guardrails=guardrails,
            finality_after=finality_after,
            api_key=api_key,
        )
        return batch
    except WundergroundAPIError as e:
        logger.warning("Wunderground API failed for %s: %s. Trying Playwright fallback.",
                       station_code, e.message)

    # ── Fallback path: Playwright ──
    try:
        batch = fetch_wunderground_playwright(
            station_url=station_url,
            station_code=station_code,
            target_window_start=target_window_start,
            target_window_end=target_window_end,
            timezone_str=timezone_str,
            measurement=measurement,
            aggregation=aggregation,
            unit=unit,
            guardrails=guardrails,
        )
        logger.info("Playwright fallback succeeded for %s.", station_code)
        return batch
    except PlaywrightError as e:
        logger.error("Playwright fallback also failed for %s: %s", station_code, e.message)

    # ── Exhausted ──
    raise RetrievalError(
        case_id=case_id,
        reason="retrieval_exhausted",
        detail=(
            f"All retrieval paths exhausted for {station_code}: "
            f"Wunderground API and Playwright fallback both failed."
        ),
    )


def _retrieve_noaa(spec: RetrievalSpec) -> RawObservationBatch:
    """Retrieve from NOAA — single path, no Playwright fallback."""
    from .noaa import fetch_noaa_monthly, NOAAError

    case_id = f"{spec.station_code}_{spec.target_window.start.strftime('%Y%m')}"

    try:
        batch = fetch_noaa_monthly(
            station_url=spec.station_url,
            target_window_start=spec.target_window.start,
            target_window_end=spec.target_window.end,
            measurement=spec.measurement,
            aggregation=spec.aggregation,
            unit=spec.unit,
            guardrails=spec.guardrails,
            finality_after=spec.finality_after,
        )
        return batch
    except NOAAError as e:
        raise RetrievalError(
            case_id=case_id,
            reason="retrieval_exhausted",
            detail=f"NOAA retrieval failed: {e.message}. No fallback path exists.",
        ) from e


# ── Fixture persistence ─────────────────────────────────────────────────

def _save_fixture(batch: RawObservationBatch, spec: RetrievalSpec, fixtures_dir: Path) -> None:
    """Save a retrieved batch as a fixture file for future replay.

    Live mode always records its snapshots so that replay runs can
    reproduce the same decisions deterministically.

    The fixture is saved as {fixtures_dir}/{station_code}_{YYYYMMDD}_{measurement}_{aggregation}.json

    Args:
        batch: The retrieved observation batch.
        spec: The RetrievalSpec used for retrieval.
        fixtures_dir: Directory to save fixtures into.
    """
    fixtures_dir.mkdir(parents=True, exist_ok=True)

    # Build a consistent fixture name from spec fields
    window_str = spec.target_window.start.strftime("%Y%m%d")
    fixture_name = (
        f"{spec.station_code}_{window_str}_"
        f"{spec.measurement}_{spec.aggregation}.json"
    )
    fixture_path = fixtures_dir / fixture_name

    # Serialize RawObservationBatch to JSON
    fixture_data = _batch_to_dict(batch)

    with open(fixture_path, "w", encoding="utf-8") as f:
        json.dump(fixture_data, f, indent=2, default=str)

    logger.info("Saved fixture to %s (%d observations)",
                fixture_path, len(batch.observations))


def _batch_to_dict(batch: RawObservationBatch) -> dict[str, Any]:
    """Serialize a RawObservationBatch to a JSON-serializable dict."""
    # Serialize observations — epoch timestamps stay as ints
    obs_list: list[dict[str, Any]] = []
    for obs in batch.observations:
        obs_copy = dict(obs)
        # Convert any datetime values to isoformat strings
        for key, val in obs_copy.items():
            if isinstance(val, datetime):
                obs_copy[key] = val.isoformat()
        obs_list.append(obs_copy)

    # Serialize extracted value
    ev = batch.extracted_value
    ev_dict: dict[str, Any] = {
        "value": ev.value,
        "unit": ev.unit,
        "field": ev.field,
        "aggregation": ev.aggregation,
    }

    # Serialize finality
    fin = batch.finality
    fin_dict: dict[str, Any] = {
        "status": fin.status,
        "first_next_day_ts": fin.first_next_day_ts,
    }

    # Serialize source trace
    trace_list: list[dict[str, Any]] = []
    for entry in batch.source_trace:
        trace_list.append({
            "url": entry.url,
            "http_status": entry.http_status,
            "response_size_bytes": entry.response_size_bytes,
            "latency_ms": entry.latency_ms,
            "path": entry.path,
            "retry_count": entry.retry_count,
            "guardrail_flags": entry.guardrail_flags,
            "error": entry.error,
            "timestamp": entry.timestamp,
        })

    return {
        "observations": obs_list,
        "extracted_value": ev_dict,
        "finality": fin_dict,
        "source_trace": trace_list,
    }
