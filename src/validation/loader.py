"""Market manifest loader.

Loads markets.json from disk, validates against the input schema,
and constructs immutable MarketCase objects.

This is the entry point for the Input & Validation pipeline stage.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .models import MarketCase, MarketManifest, Outcomes, QuestionData
from .schema import (
    SchemaValidationError,
    validate_ancillary_has_station_url,
    validate_case_ids_are_unique,
    validate_end_date_parseable,
    validate_manifest,
)

logger = logging.getLogger(__name__)


class LoadError(Exception):
    """Raised when the markets.json file cannot be loaded or validated.

    Attributes:
        message: Human-readable error description.
        cause: The underlying exception, if any.
    """

    def __init__(self, message: str, cause: Exception | None = None):
        super().__init__(message)
        self.message = message
        self.cause = cause

    def __str__(self) -> str:
        if self.cause:
            return f"{self.message}\nCaused by: {self.cause}"
        return self.message


def load_markets(input_path: str | Path) -> MarketManifest:
    """Load and validate a markets.json manifest.

    This performs the full Input & Validation stage:
    1. Reads and parses the JSON file
    2. Validates against the JSON Schema
    3. Runs semantic validation (unique case_ids, parseable dates, station URLs)
    4. Constructs immutable MarketCase objects

    Args:
        input_path: Path to the markets.json manifest file.

    Returns:
        A MarketManifest containing all validated MarketCase objects.

    Raises:
        LoadError: If the file cannot be read, parsed, or fails any validation.
            The error message will include details on all violations found.
    """
    input_path = Path(input_path)

    # 1. Read and parse JSON
    data = _read_json(input_path)

    # 2. Schema validation (fail-fast)
    try:
        validate_manifest(data)
    except SchemaValidationError as e:
        raise LoadError(
            f"Schema validation failed for '{input_path}'.",
            cause=e,
        ) from e

    # 3. Semantic validation
    try:
        validate_case_ids_are_unique(data)
    except SchemaValidationError as e:
        raise LoadError(
            f"Duplicate case_id validation failed for '{input_path}'.",
            cause=e,
        ) from e

    try:
        validate_end_date_parseable(data)
    except SchemaValidationError as e:
        raise LoadError(
            f"Date validation failed for '{input_path}'.",
            cause=e,
        ) from e

    try:
        validate_ancillary_has_station_url(data)
    except SchemaValidationError as e:
        raise LoadError(
            f"Station URL validation failed for '{input_path}'.",
            cause=e,
        ) from e

    # 4. Build immutable model objects
    markets = _build_market_cases(data["markets"])

    manifest = MarketManifest(
        schema_version=data["schema_version"],
        markets=tuple(markets),
        generated_at=data.get("generated_at"),
        description=data.get("description"),
        market_selection=data.get("market_selection"),
    )

    logger.info(
        "Loaded %d market case(s) from '%s' (schema_version=%s).",
        len(manifest.markets),
        input_path,
        manifest.schema_version,
    )

    return manifest


def _read_json(path: Path) -> dict[str, Any]:
    """Read and parse a JSON file.

    Args:
        path: Path to the JSON file.

    Returns:
        Parsed JSON as a dict.

    Raises:
        LoadError: If the file doesn't exist, isn't valid JSON, or isn't a dict.
    """
    if not path.exists():
        raise LoadError(f"Input file not found: '{path}'.")

    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise LoadError(
            f"Failed to parse JSON from '{path}'.",
            cause=e,
        ) from e

    if not isinstance(data, dict):
        raise LoadError(
            f"Expected a JSON object at top level in '{path}', "
            f"got {type(data).__name__}."
        )

    return data


def _build_market_cases(raw_markets: list[dict[str, Any]]) -> list[MarketCase]:
    """Build a list of immutable MarketCase objects from raw market dicts.

    Args:
        raw_markets: List of raw market dicts from the parsed JSON.

    Returns:
        List of validated MarketCase instances.
    """
    cases: list[MarketCase] = []

    for raw in raw_markets:
        qd = raw.get("question_data", {})
        outcomes_raw = qd.get("outcomes", {})

        question_data = QuestionData(
            title=qd.get("title", ""),
            end_date_iso=qd.get("end_date_iso", ""),
            outcomes=Outcomes(
                p1=outcomes_raw.get("p1", ""),
                p2=outcomes_raw.get("p2", ""),
                p3=outcomes_raw.get("p3", ""),
                p4=outcomes_raw.get("p4", ""),
            ),
            question_id=qd.get("question_id"),
            market_id=qd.get("market_id"),
            market_slug=qd.get("market_slug"),
            gamma_slug=qd.get("gamma_slug"),
            proposal_time=qd.get("proposal_time"),
            processed_time=qd.get("processed_time"),
            resolution_conditions=qd.get("resolution_conditions"),
            proposed_outcome=qd.get("proposed_outcome"),
        )

        case = MarketCase(
            case_id=raw["case_id"],
            polymarket_url=raw["polymarket_url"],
            proposal_tx_hash=raw["proposal_tx_hash"],
            question_data=question_data,
            ancillary_data=raw["ancillary_data"],
            fixture_path=raw.get("fixture_path"),
        )

        cases.append(case)

    return cases
