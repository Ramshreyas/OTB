"""Fixture Replay — Loads pre-captured weather data for deterministic retrieval.

When the pipeline runs in replay mode, this module loads fixture JSON files
from data/fixtures/ instead of making live network calls. The rest of the
pipeline (normalization, reconciliation, decision) runs identically — it
does not know the data source.

Fixture format matches RawObservationBatch output, so replay produces the
same interface as live retrieval.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .dispatch import RawObservationBatch, ExtractedValue, FinalityResult, SourceTraceEntry

logger = logging.getLogger(__name__)


class ReplayError(Exception):
    """Raised when a fixture cannot be loaded or is malformed."""

    def __init__(self, message: str, cause: Exception | None = None):
        super().__init__(message)
        self.message = message
        self.cause = cause


def load_fixture(fixture_path: str | Path) -> dict[str, Any]:
    """Load a raw fixture JSON file from disk.

    Args:
        fixture_path: Path to the fixture file.

    Returns:
        Parsed fixture dict.

    Raises:
        ReplayError: If the file does not exist or is not valid JSON.
    """
    fixture_path = Path(fixture_path)

    if not fixture_path.exists():
        raise ReplayError(f"Fixture file not found: {fixture_path}")

    try:
        with open(fixture_path, encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise ReplayError(
            f"Fixture file is not valid JSON: {fixture_path}",
            cause=e,
        ) from e

    if not isinstance(data, dict):
        raise ReplayError(
            f"Fixture must be a JSON object, got {type(data).__name__}: {fixture_path}"
        )

    logger.info("Loaded fixture from %s", fixture_path)
    return data


def resolve_fixture_path(
    case_id: str,
    fixtures_dir: str | Path,
    fixture_path_override: Optional[str] = None,
) -> Path:
    """Resolve the fixture path for a given case.

    Priority:
    1. Explicit fixture_path_override (from market case or CLI)
    2. {fixtures_dir}/{case_id}.json

    Args:
        case_id: Market case identifier.
        fixtures_dir: Base directory for fixture files.
        fixture_path_override: Optional explicit path override.

    Returns:
        Resolved Path to the fixture file.

    Raises:
        ReplayError: If the fixture cannot be found.
    """
    fixtures_dir = Path(fixtures_dir)

    if fixture_path_override:
        path = Path(fixture_path_override)
        if not path.is_absolute():
            path = fixtures_dir / path
        if path.exists():
            return path
        raise ReplayError(
            f"Fixture path override does not exist: {path}"
        )

    # Default: {fixtures_dir}/{case_id}.json
    path = fixtures_dir / f"{case_id}.json"
    if path.exists():
        return path

    raise ReplayError(
        f"No fixture found for case '{case_id}' in {fixtures_dir}. "
        f"Expected: {path}"
    )


def replay_observation_batch(fixture_data: dict[str, Any]) -> RawObservationBatch:
    """Build a RawObservationBatch from fixture data.

    The fixture format matches the RawObservationBatch output structure.
    This function deserializes it into the immutable model objects.

    Args:
        fixture_data: Parsed fixture JSON dict.

    Returns:
        A RawObservationBatch with the fixture data loaded as model objects.

    Raises:
        ReplayError: If the fixture data is structurally invalid.
    """
    try:
        return _deserialize_batch(fixture_data)
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        raise ReplayError(
            f"Fixture data is malformed or missing required fields: {e}",
            cause=e,
        ) from e


def _deserialize_batch(data: dict[str, Any]) -> RawObservationBatch:
    """Deserialize a fixture dict into a RawObservationBatch."""
    # Observations
    raw_obs = data.get("observations", [])
    observations: list[dict[str, Any]] = []
    for obs in raw_obs:
        if isinstance(obs, dict):
            # Parse timestamps
            obs_parsed = dict(obs)
            for ts_field in ("valid_time_gmt", "obs_time_local"):
                if ts_field in obs_parsed and isinstance(obs_parsed[ts_field], (int, float)):
                    obs_parsed[ts_field] = datetime.utcfromtimestamp(obs_parsed[ts_field])
            observations.append(obs_parsed)

    # Extracted value
    ev_data = data.get("extracted_value", {})
    extracted_value = ExtractedValue(
        value=ev_data.get("value"),
        unit=ev_data.get("unit", ""),
        field=ev_data.get("field", ""),
        aggregation=ev_data.get("aggregation", ""),
    )

    # Finality
    fin_data = data.get("finality", {})
    finality = FinalityResult(
        status=fin_data.get("status", "unknown"),
        first_next_day_ts=fin_data.get("first_next_day_ts"),
    )

    # Source trace
    trace_entries: list[SourceTraceEntry] = []
    for entry in data.get("source_trace", []):
        trace_entries.append(SourceTraceEntry(
            url=entry.get("url", ""),
            http_status=entry.get("http_status"),
            response_size_bytes=entry.get("response_size_bytes"),
            latency_ms=entry.get("latency_ms"),
            path=entry.get("path", "replay"),
            retry_count=entry.get("retry_count", 0),
            guardrail_flags=entry.get("guardrail_flags", []),
            error=entry.get("error"),
            timestamp=entry.get("timestamp", datetime.utcnow().isoformat()),
        ))

    return RawObservationBatch(
        observations=tuple(observations),
        extracted_value=extracted_value,
        finality=finality,
        source_trace=tuple(trace_entries),
    )
