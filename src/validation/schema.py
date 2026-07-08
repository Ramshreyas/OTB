"""JSON Schema validation for markets.json input manifest.

Validates every market case object against the schema defined in
data/schema/market_input.schema.json. Fails fast — if any case is
malformed, the entire manifest is rejected with a clear error message.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema


# Cached schema instance — loaded once on first use
_SCHEMA_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "schema" / "market_input.schema.json"
_schema: dict[str, Any] | None = None


def _load_schema() -> dict[str, Any]:
    """Load (and cache) the JSON Schema from disk."""
    global _schema
    if _schema is None:
        if not _SCHEMA_PATH.exists():
            raise FileNotFoundError(
                f"Schema file not found at {_SCHEMA_PATH}. "
                "Ensure data/schema/market_input.schema.json exists."
            )
        with open(_SCHEMA_PATH, encoding="utf-8") as f:
            _schema = json.load(f)
    return _schema


class SchemaValidationError(Exception):
    """Raised when the markets.json manifest fails schema validation.

    Attributes:
        message: Human-readable error description.
        validation_errors: List of individual jsonschema validation error messages.
    """

    def __init__(self, message: str, validation_errors: list[str] | None = None):
        super().__init__(message)
        self.message = message
        self.validation_errors = validation_errors or []

    def __str__(self) -> str:
        if self.validation_errors:
            details = "\n".join(f"  - {e}" for e in self.validation_errors)
            return f"{self.message}\n{details}"
        return self.message


def validate_manifest(data: dict[str, Any]) -> None:
    """Validate a parsed markets.json manifest against the input schema.

    Args:
        data: The parsed JSON object (the entire markets.json content).

    Raises:
        SchemaValidationError: If any schema violation is found. All violations
            are collected and reported together (fail-fast on the whole manifest,
            but comprehensive on reporting).
    """
    schema = _load_schema()

    validator = jsonschema.Draft202012Validator(schema)
    errors = list(validator.iter_errors(data))

    if errors:
        error_messages = []
        for error in errors:
            path = " → ".join(str(p) for p in error.absolute_path) or "(root)"
            error_messages.append(f"{path}: {error.message}")

        raise SchemaValidationError(
            f"Schema validation failed with {len(errors)} error(s):",
            validation_errors=error_messages,
        )


def validate_case_ids_are_unique(data: dict[str, Any]) -> None:
    """Check that all case_id values in the manifest are unique.

    This is a semantic check that JSON Schema cannot easily express,
    so it runs as a separate validation step after schema validation.

    Args:
        data: The parsed JSON object containing the markets array.

    Raises:
        SchemaValidationError: If duplicate case_ids are found.
    """
    markets: list[dict[str, Any]] = data.get("markets", [])
    seen: set[str] = set()
    duplicates: list[str] = []

    for i, market in enumerate(markets):
        case_id = market.get("case_id", "")
        if case_id in seen:
            duplicates.append(f"  - '{case_id}' at index {i}")
        else:
            seen.add(case_id)

    if duplicates:
        raise SchemaValidationError(
            "Duplicate case_id(s) found in markets array. case_id must be unique.",
            validation_errors=duplicates,
        )


def validate_end_date_parseable(data: dict[str, Any]) -> None:
    """Check that every end_date_iso is a parseable ISO 8601 date string.

    This validates that the date can actually be parsed (not just that it's
    a non-empty string, which the JSON Schema checks).

    Args:
        data: The parsed JSON object containing the markets array.

    Raises:
        SchemaValidationError: If any end_date_iso cannot be parsed.
    """
    from datetime import datetime

    markets: list[dict[str, Any]] = data.get("markets", [])
    failures: list[str] = []

    for market in markets:
        case_id = market.get("case_id", "(unknown)")
        qd = market.get("question_data", {})
        end_date = qd.get("end_date_iso", "")

        if not end_date:
            failures.append(f"  - {case_id}: end_date_iso is empty or missing")
            continue

        # Try multiple ISO 8601 formats
        for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                datetime.strptime(end_date.replace("Z", "").split("T")[0], "%Y-%m-%d")
                break
            except ValueError:
                continue
        else:
            failures.append(
                f"  - {case_id}: end_date_iso='{end_date}' is not a parseable ISO 8601 date"
            )

    if failures:
        raise SchemaValidationError(
            "end_date_iso values must be parseable ISO 8601 date strings.",
            validation_errors=failures,
        )


def validate_ancillary_has_station_url(data: dict[str, Any]) -> None:
    """Check that ancillary_data contains a recognizable Wunderground or NOAA URL.

    The ancillary_data must contain a URL pointing to the resolution source.
    This is a semantic check beyond basic schema validation.

    Args:
        data: The parsed JSON object containing the markets array.

    Raises:
        SchemaValidationError: If any ancillary_data string lacks a station URL.
    """
    markets: list[dict[str, Any]] = data.get("markets", [])
    failures: list[str] = []

    for market in markets:
        case_id = market.get("case_id", "(unknown)")
        ancillary = market.get("ancillary_data", "")

        if not isinstance(ancillary, str) or not ancillary.strip():
            failures.append(f"  - {case_id}: ancillary_data is empty")
            continue

        # Check for a Wunderground or NOAA URL pattern
        has_wunderground = "wunderground.com/history/daily/" in ancillary
        has_noaa = "noaa.gov" in ancillary or "weather.gov" in ancillary

        if not (has_wunderground or has_noaa):
            failures.append(
                f"  - {case_id}: ancillary_data does not contain a recognized "
                "station URL (expected wunderground.com/history/daily/... or noaa.gov/...)"
            )

    if failures:
        raise SchemaValidationError(
            "ancillary_data must contain a resolvable station URL.",
            validation_errors=failures,
        )
