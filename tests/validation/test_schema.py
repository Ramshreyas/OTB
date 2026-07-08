"""Tests for JSON Schema validation of markets.json.

Covers:
- Valid manifests pass
- Missing required fields (case_id, polymarket_url, ancillary_data, etc.)
- Missing outcomes (p1, p2, p3, p4)
- Empty ancillary_data
- Invalid URL format
- Empty markets array
"""

from __future__ import annotations

import pytest

from src.validation.schema import (
    SchemaValidationError,
    validate_ancillary_has_station_url,
    validate_case_ids_are_unique,
    validate_end_date_parseable,
    validate_manifest,
)
from tests.conftest import make_case_with_overrides, make_valid_manifest


# ═══════════════════════════════════════════════════════════════════════
# Schema validation — valid manifests
# ═══════════════════════════════════════════════════════════════════════


class TestValidManifests:
    """Valid manifests should pass validation without error."""

    def test_single_valid_case_passes(self, valid_manifest):
        """A minimal valid manifest with one case should validate."""
        validate_manifest(valid_manifest)  # Should not raise

    def test_five_valid_cases_pass(self, valid_manifest_five_cases):
        """A manifest with five valid cases should validate."""
        validate_manifest(valid_manifest_five_cases)  # Should not raise

    def test_manifest_with_optional_fields_passes(self):
        """Optional top-level fields (generated_at, description) are accepted."""
        manifest = make_valid_manifest()
        manifest["generated_at"] = "2026-07-08T12:00:00Z"
        manifest["description"] = "A test manifest"
        manifest["market_selection"] = {"project": "test"}
        validate_manifest(manifest)  # Should not raise


# ═══════════════════════════════════════════════════════════════════════
# Schema validation — missing required top-level fields
# ═══════════════════════════════════════════════════════════════════════


class TestMissingRequiredFields:
    """Missing required fields at the market case level should be caught."""

    def test_missing_case_id_fails(self):
        """case_id is required — missing it should raise."""
        case = make_case_with_overrides()
        del case["case_id"]
        manifest = make_valid_manifest(markets=[case])

        with pytest.raises(SchemaValidationError) as exc_info:
            validate_manifest(manifest)
        assert "case_id" in str(exc_info.value)

    def test_missing_polymarket_url_fails(self):
        """polymarket_url is required — missing it should raise."""
        case = make_case_with_overrides()
        del case["polymarket_url"]
        manifest = make_valid_manifest(markets=[case])

        with pytest.raises(SchemaValidationError) as exc_info:
            validate_manifest(manifest)
        assert "polymarket_url" in str(exc_info.value)

    def test_missing_proposal_tx_hash_fails(self):
        """proposal_tx_hash is required — missing it should raise."""
        case = make_case_with_overrides()
        del case["proposal_tx_hash"]
        manifest = make_valid_manifest(markets=[case])

        with pytest.raises(SchemaValidationError) as exc_info:
            validate_manifest(manifest)
        assert "proposal_tx_hash" in str(exc_info.value)

    def test_missing_question_data_fails(self):
        """question_data is required — missing it should raise."""
        case = make_case_with_overrides()
        del case["question_data"]
        manifest = make_valid_manifest(markets=[case])

        with pytest.raises(SchemaValidationError) as exc_info:
            validate_manifest(manifest)
        assert "question_data" in str(exc_info.value)

    def test_missing_ancillary_data_fails(self):
        """ancillary_data is required — missing it should raise."""
        case = make_case_with_overrides()
        del case["ancillary_data"]
        manifest = make_valid_manifest(markets=[case])

        with pytest.raises(SchemaValidationError) as exc_info:
            validate_manifest(manifest)
        assert "ancillary_data" in str(exc_info.value)


# ═══════════════════════════════════════════════════════════════════════
# Schema validation — missing question_data sub-fields
# ═══════════════════════════════════════════════════════════════════════


class TestMissingQuestionDataFields:
    """Required sub-fields inside question_data should be validated."""

    def test_missing_title_fails(self):
        """question_data.title is required."""
        case = make_case_with_overrides(question_data__title="")
        manifest = make_valid_manifest(markets=[case])

        with pytest.raises(SchemaValidationError) as exc_info:
            validate_manifest(manifest)
        assert "title" in str(exc_info.value)

    def test_missing_end_date_iso_fails(self):
        """question_data.end_date_iso is required."""
        case = make_case_with_overrides(question_data__end_date_iso="")
        manifest = make_valid_manifest(markets=[case])

        with pytest.raises(SchemaValidationError) as exc_info:
            validate_manifest(manifest)
        assert "end_date_iso" in str(exc_info.value)


# ═══════════════════════════════════════════════════════════════════════
# Schema validation — outcomes
# ═══════════════════════════════════════════════════════════════════════


class TestOutcomesValidation:
    """Outcomes must contain all four (p1, p2, p3, p4) as non-empty strings."""

    def test_missing_p1_fails(self):
        """Missing p1 in outcomes should fail."""
        case = make_case_with_overrides()
        del case["question_data"]["outcomes"]["p1"]
        manifest = make_valid_manifest(markets=[case])

        with pytest.raises(SchemaValidationError) as exc_info:
            validate_manifest(manifest)
        assert "p1" in str(exc_info.value)

    def test_missing_p2_fails(self):
        """Missing p2 in outcomes should fail."""
        case = make_case_with_overrides()
        del case["question_data"]["outcomes"]["p2"]
        manifest = make_valid_manifest(markets=[case])

        with pytest.raises(SchemaValidationError) as exc_info:
            validate_manifest(manifest)
        assert "p2" in str(exc_info.value)

    def test_missing_p3_fails(self):
        """Missing p3 in outcomes should fail."""
        case = make_case_with_overrides()
        del case["question_data"]["outcomes"]["p3"]
        manifest = make_valid_manifest(markets=[case])

        with pytest.raises(SchemaValidationError) as exc_info:
            validate_manifest(manifest)
        assert "p3" in str(exc_info.value)

    def test_missing_p4_fails(self):
        """Missing p4 in outcomes should fail."""
        case = make_case_with_overrides()
        del case["question_data"]["outcomes"]["p4"]
        manifest = make_valid_manifest(markets=[case])

        with pytest.raises(SchemaValidationError) as exc_info:
            validate_manifest(manifest)
        assert "p4" in str(exc_info.value)

    def test_empty_p1_string_fails(self):
        """p1 as an empty string should fail (minLength: 1)."""
        case = make_case_with_overrides(question_data__outcomes={
            "p1": "", "p2": "Yes", "p3": "50/50", "p4": "Too Early"
        })
        manifest = make_valid_manifest(markets=[case])

        with pytest.raises(SchemaValidationError) as exc_info:
            validate_manifest(manifest)
        assert "p1" in str(exc_info.value)

    def test_extra_keys_in_outcomes_are_rejected(self):
        """outcomes has additionalProperties: false — extra keys should fail."""
        case = make_case_with_overrides(question_data__outcomes={
            "p1": "No", "p2": "Yes", "p3": "50/50", "p4": "Too Early", "p5": "Extra"
        })
        manifest = make_valid_manifest(markets=[case])

        with pytest.raises(SchemaValidationError):
            validate_manifest(manifest)


# ═══════════════════════════════════════════════════════════════════════
# Schema validation — ancillary_data
# ═══════════════════════════════════════════════════════════════════════


class TestAncillaryDataValidation:
    """ancillary_data must be a non-empty string."""

    def test_empty_ancillary_data_fails(self):
        """Empty ancillary_data string should fail."""
        case = make_case_with_overrides(ancillary_data="")
        manifest = make_valid_manifest(markets=[case])

        with pytest.raises(SchemaValidationError) as exc_info:
            validate_manifest(manifest)
        assert "ancillary_data" in str(exc_info.value)

    def test_whitespace_only_ancillary_data_fails(self):
        """Whitespace-only ancillary_data should fail the station URL semantic check."""
        case = make_case_with_overrides(ancillary_data="   ")
        manifest = make_valid_manifest(markets=[case])

        with pytest.raises(SchemaValidationError) as exc_info:
            validate_ancillary_has_station_url(manifest)
        assert "station" in str(exc_info.value).lower()

    def test_null_ancillary_data_fails(self):
        """null ancillary_data should fail type validation."""
        case = make_case_with_overrides(ancillary_data=None)
        manifest = make_valid_manifest(markets=[case])

        with pytest.raises(SchemaValidationError) as exc_info:
            validate_manifest(manifest)
        assert "ancillary_data" in str(exc_info.value)


# ═══════════════════════════════════════════════════════════════════════
# Schema validation — empty markets array
# ═══════════════════════════════════════════════════════════════════════


class TestEmptyMarkets:
    """The markets array must contain at least one item."""

    def test_empty_markets_array_fails(self):
        """An empty markets array should fail (minItems: 1)."""
        manifest = make_valid_manifest(markets=[])

        with pytest.raises(SchemaValidationError) as exc_info:
            validate_manifest(manifest)
        assert "markets" in str(exc_info.value) or "minItems" in str(exc_info.value)


# ═══════════════════════════════════════════════════════════════════════
# Schema validation — unique case_ids
# ═══════════════════════════════════════════════════════════════════════


class TestUniqueCaseIds:
    """case_id must be unique across all markets in the manifest."""

    def test_duplicate_case_ids_fails(self):
        """Two markets with the same case_id should fail."""
        manifest = make_valid_manifest(
            markets=[
                make_case_with_overrides(case_id="duplicate_id"),
                make_case_with_overrides(case_id="duplicate_id"),
            ]
        )

        with pytest.raises(SchemaValidationError) as exc_info:
            validate_case_ids_are_unique(manifest)
        assert "duplicate_id" in str(exc_info.value)

    def test_all_unique_case_ids_pass(self, valid_manifest_five_cases):
        """Five markets with unique case_ids should pass."""
        validate_case_ids_are_unique(valid_manifest_five_cases)  # Should not raise

    def test_single_case_passes(self, valid_manifest):
        """A single case trivially has unique case_id."""
        validate_case_ids_are_unique(valid_manifest)  # Should not raise


# ═══════════════════════════════════════════════════════════════════════
# Semantic validation — parseable dates
# ═══════════════════════════════════════════════════════════════════════


class TestParseableDates:
    """end_date_iso must be parseable as an ISO 8601 date."""

    @pytest.mark.parametrize("date_str", [
        "2026-06-01T00:00:00Z",
        "2026-06-01T00:00:00",
        "2026-06-01",
        "2026-12-31",
        "2026-01-01",
    ])
    def test_parseable_dates_pass(self, date_str):
        """Various valid ISO 8601 date formats should pass."""
        case = make_case_with_overrides(
            case_id="date_test",
            question_data__end_date_iso=date_str,
        )
        manifest = make_valid_manifest(markets=[case])
        validate_end_date_parseable(manifest)  # Should not raise

    @pytest.mark.parametrize("date_str", [
        "not-a-date",
        "June 1, 2026",
        "2026-13-01",   # month 13
        "2026-06-32",   # day 32
        "",
    ])
    def test_unparseable_dates_fail(self, date_str):
        """Invalid date strings should fail."""
        case = make_case_with_overrides(
            case_id="bad_date",
            question_data__end_date_iso=date_str,
        )
        manifest = make_valid_manifest(markets=[case])

        with pytest.raises(SchemaValidationError) as exc_info:
            validate_end_date_parseable(manifest)
        assert "end_date_iso" in str(exc_info.value)


# ═══════════════════════════════════════════════════════════════════════
# Semantic validation — station URL in ancillary_data
# ═══════════════════════════════════════════════════════════════════════


class TestStationUrlValidation:
    """ancillary_data must contain a resolvable Wunderground or NOAA URL."""

    def test_wunderground_url_passes(self):
        """A Wunderground station history URL should pass."""
        manifest = make_valid_manifest()
        validate_ancillary_has_station_url(manifest)  # Should not raise

    def test_noaa_url_passes(self):
        """A NOAA/weather.gov URL should pass."""
        case = make_case_with_overrides(
            ancillary_data="Source: https://www.weather.gov/foo/bar precipitation data."
        )
        manifest = make_valid_manifest(markets=[case])
        validate_ancillary_has_station_url(manifest)  # Should not raise

    def test_missing_station_url_fails(self):
        """Ancillary data without any URL should fail."""
        case = make_case_with_overrides(
            ancillary_data="This market resolves based on temperature at the station."
        )
        manifest = make_valid_manifest(markets=[case])

        with pytest.raises(SchemaValidationError) as exc_info:
            validate_ancillary_has_station_url(manifest)
        assert "station" in str(exc_info.value).lower()


# ═══════════════════════════════════════════════════════════════════════
# Schema — error aggregation
# ═══════════════════════════════════════════════════════════════════════


class TestErrorAggregation:
    """When multiple validation errors exist, they should all be reported."""

    def test_multiple_errors_reported(self):
        """A case with multiple missing required fields reports all errors."""
        case = {
            "case_id": "bad_case",
            # Missing: polymarket_url, proposal_tx_hash, question_data, ancillary_data
        }
        manifest = make_valid_manifest(markets=[case])

        with pytest.raises(SchemaValidationError) as exc_info:
            validate_manifest(manifest)

        assert len(exc_info.value.validation_errors) >= 4

    def test_error_includes_path_context(self):
        """Each error message should include the path to the offending field."""
        case = make_case_with_overrides()
        del case["question_data"]["outcomes"]["p1"]
        manifest = make_valid_manifest(markets=[case])

        with pytest.raises(SchemaValidationError) as exc_info:
            validate_manifest(manifest)

        # Error path should mention outcomes, p1, or the index
        error_text = str(exc_info.value)
        assert "p1" in error_text
