"""Shared test fixtures for the validation test suite."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest


# ── Paths ──────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def project_root() -> Path:
    """Absolute path to the project root directory."""
    return Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def markets_json_path(project_root: Path) -> Path:
    """Path to the real markets.json manifest."""
    return project_root / "data" / "markets.json"


@pytest.fixture(scope="session")
def schema_path(project_root: Path) -> Path:
    """Path to the JSON Schema file."""
    return project_root / "data" / "schema" / "market_input.schema.json"


# ── Helper: build a minimal valid manifest ────────────────────────────

def make_valid_manifest(
    markets: list[dict[str, Any]] | None = None,
    schema_version: str = "otb-weather-case-v1",
) -> dict[str, Any]:
    """Build a minimal valid markets.json manifest dict.

    Args:
        markets: List of market case dicts. If None, a single valid default is used.
        schema_version: Schema version string.

    Returns:
        A complete manifest dict that passes schema validation.
    """
    if markets is None:
        markets = [_make_minimal_valid_case()]

    return {
        "schema_version": schema_version,
        "generated_at": "2026-06-01T00:00:00Z",
        "description": "Test manifest",
        "markets": markets,
    }


def _make_minimal_valid_case(
    case_id: str = "test_case_01",
    ancillary_data: str | None = None,
) -> dict[str, Any]:
    """Build a single minimal valid market case dict."""
    if ancillary_data is None:
        ancillary_data = (
            "q: title: Will the lowest temperature in Tokyo be 20°C on June 1?, "
            "description: This market will resolve using Wunderground. "
            "Resolution source: https://www.wunderground.com/history/daily/jp/tokyo/RJTT. "
            "This market can not resolve until the first data point for the following date "
            "has been published. Measures temperatures to whole degrees Celsius. "
            "res_data: p1: 0, p2: 1, p3: 0.5. "
        )

    return {
        "case_id": case_id,
        "polymarket_url": f"https://polymarket.com/event/{case_id}",
        "proposal_tx_hash": "0x" + "a" * 64,
        "question_data": {
            "question_id": "0x" + "b" * 64,
            "market_id": "1234567",
            "market_slug": f"{case_id}-slug",
            "gamma_slug": f"{case_id}-gamma",
            "title": "Will the lowest temperature in Tokyo be 20°C on June 1?",
            "proposal_time": "2026-06-01T15:06:18Z",
            "processed_time": "2026-06-01T15:07:44Z",
            "end_date_iso": "2026-06-01T00:00:00Z",
            "resolution_conditions": "p1: No. p2: Yes. p3: 50/50 outcome. p4: Too Early.",
            "proposed_outcome": "p2",
            "outcomes": {
                "p1": "No",
                "p2": "Yes",
                "p3": "50/50 outcome",
                "p4": "Too Early",
            },
        },
        "ancillary_data": ancillary_data,
    }


def make_case_with_overrides(
    case_id: str = "test_case_01",
    **overrides: Any,
) -> dict[str, Any]:
    """Build a minimal valid case with specific field overrides.

    Args:
        case_id: The case_id to use.
        **overrides: Top-level keys to override in the case dict.
            Use dotted keys for nested overrides, e.g.,
            ``question_data__title="new title"``.

    Returns:
        A market case dict with overrides applied.
    """
    case = _make_minimal_valid_case(case_id=case_id)

    nested_overrides: dict[str, dict[str, Any]] = {}
    for key, value in overrides.items():
        if "__" in key:
            parent, child = key.split("__", 1)
            if parent not in nested_overrides:
                nested_overrides[parent] = {}
            nested_overrides[parent][child] = value
        else:
            case[key] = value

    for parent, child_updates in nested_overrides.items():
        if parent in case and isinstance(case[parent], dict):
            case[parent].update(child_updates)

    return case


# ── Reusable fixtures ──────────────────────────────────────────────────

@pytest.fixture
def valid_manifest() -> dict[str, Any]:
    """A minimal valid manifest with one market case."""
    return make_valid_manifest()


@pytest.fixture
def valid_manifest_five_cases() -> dict[str, Any]:
    """A valid manifest with five unique market cases."""
    return make_valid_manifest(
        markets=[
            _make_minimal_valid_case(case_id=f"case_{i:02d}")
            for i in range(1, 6)
        ]
    )


@pytest.fixture
def valid_case_dict() -> dict[str, Any]:
    """A single minimal valid market case dict."""
    return _make_minimal_valid_case()


@pytest.fixture
def tokyo_low_case_dict() -> dict[str, Any]:
    """A case dict matching the Tokyo low temperature market."""
    return {
        "case_id": "tokyo_low_2026_06_01_20c",
        "polymarket_url": "https://polymarket.com/event/lowest-temperature-in-tokyo-on-june-1-2026",
        "proposal_tx_hash": "0x59460aab91d57412308e194816f24939726baa831990fa225d64ef1e23689b18",
        "question_data": {
            "question_id": "0xcb8828e277c11f54e623dd910c867f37d52bffbd3fe4a8910303ad53d22c0c77",
            "market_id": "2391835",
            "market_slug": "lowest-temperature-in-tokyo-on-june-1-2026-20c",
            "gamma_slug": "lowest-temperature-in-tokyo-on-june-1-2026",
            "title": "Will the lowest temperature in Tokyo be 20°C on June 1?",
            "proposal_time": "2026-06-01T15:06:18Z",
            "processed_time": "2026-06-01T15:07:44Z",
            "end_date_iso": "2026-06-01T00:00:00Z",
            "resolution_conditions": "p1: No. p2: Yes. p3: 50/50 outcome. p4: Too Early.",
            "proposed_outcome": "p2",
            "outcomes": {
                "p1": "No",
                "p2": "Yes",
                "p3": "50/50 outcome",
                "p4": "Too Early",
            },
        },
        "ancillary_data": (
            "q: title: Will the lowest temperature in Tokyo be 20°C on June 1?, "
            "description: This market will resolve to the temperature range that contains "
            "the lowest temperature recorded at the Tokyo Haneda Airport Station in degrees "
            "Celsius on 1 Jun '26. Resolution source: "
            "https://www.wunderground.com/history/daily/jp/tokyo/RJTT. "
            "This market can not resolve until the first data point for the following date "
            "has been published. Measures temperatures to whole degrees Celsius. "
            "res_data: p1: 0, p2: 1, p3: 0.5."
        ),
    }
